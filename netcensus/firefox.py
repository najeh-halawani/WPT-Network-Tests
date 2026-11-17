from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from itertools import product
from urllib.parse import urlsplit

import websocket

from . import proc
from .browser import _DONE_HOOK, _RTC_HOOK, Visit
from .extension_settings import ExtensionSettings, STRICT_TYPES


_SUBS = ("www", "www1", "www2")


def wpt_hosts() -> list[str]:
    subs = sorted({".".join(x) for n in (1, 2) for x in product(_SUBS, repeat=n)})
    return [
        h if not s else f"{s.encode('idna').decode('ascii')}.{h}"
        for h in ("web-platform.test", "not-web-platform.test")
        for s in ("", *subs)
    ]


PREFS = {
    "remote.active-protocols": 1,
    "network.dns.localDomains": ",".join(wpt_hosts()),
    "network.http.http3.enable": True,
    "network.http.http3.disable_when_third_party_roots_found": False,
    "browser.chrome.site_icons": False,
    "browser.chrome.favicons": False,
    "xpinstall.signature.required": False,
    # This will be important once i alter the extensions
    "extensions.experiments.enabled": True,
    # I need this for the setting above
    "extensions.installation_mode.updates_disabled": True,
    # This is to prevent updates
}
_EXT_SETTLE = 1.0


class _Wire:
    """Minimal BiDi client: commands block on their reply, events queue up."""

    def __init__(self, url: str):

        self.ws = websocket.create_connection(url, timeout=15, suppress_origin=True)
        self.ws.settimeout(None)
        self.closed = False
        self._n = 0
        self._cv = threading.Condition()
        self._results: dict = {}
        self._events: list = []
        threading.Thread(target=self._read, daemon=True).start()

    @property
    def alive(self) -> bool:
        return not self.closed

    def _read(self) -> None:
        try:
            while True:
                m = json.loads(self.ws.recv())
                with self._cv:
                    if "id" in m:
                        self._results[m["id"]] = m
                    elif m.get("type") == "event":
                        self._events.append(m)
                    self._cv.notify_all()
        except Exception:
            pass
        finally:
            with self._cv:
                self.closed = True
                self._cv.notify_all()

    def send(
        self, method: str, params: dict | None = None, timeout: float = 20
    ) -> dict:
        with self._cv:
            self._n += 1
            i = self._n
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        with self._cv:
            while i not in self._results:
                if self.closed:
                    raise RuntimeError(f"{method}: connection closed")
                if deadline - time.time() <= 0:
                    raise TimeoutError(f"{method}: no reply in {timeout:.0f}s")
                self._cv.wait(deadline - time.time())
            m = self._results.pop(i)
        if m.get("type") == "error":
            raise RuntimeError(f"{method}: {m.get('error')}: {m.get('message')}")
        return m.get("result", {})

    def drain(self) -> list:
        with self._cv:
            ev, self._events = self._events, []
        return ev

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


_DEST = {
    "document": "Document",
    "iframe": "Document",
    "frame": "Document",
    "script": "Script",
    "style": "Stylesheet",
    "image": "Image",
    "font": "Font",
    "audio": "Media",
    "video": "Media",
    "track": "Media",
    "manifest": "Manifest",
}
_BY_INITIATOR = {
    "fetch": "Fetch",
    "xmlhttprequest": "XHR",
    "beacon": "Ping",
    "ping": "Ping",
    "eventsource": "EventSource",
}


def _is_websocket(req: dict) -> bool:

    if urlsplit(req.get("url", "")).scheme in ("ws", "wss"):
        return True
    for h in req.get("headers", []):
        name = h.get("name", "").lower()
        val = str(h.get("value", {}).get("value", "")).lower()
        if (
            name in ("sec-websocket-key", "sec-websocket-version")
            or (name == "upgrade" and "websocket" in val)
            or (name == "sec-fetch-mode" and val == "websocket")
        ):
            return True
    return False


def _row(p: dict, first: dict | None) -> dict | None:

    req = p.get("request", {})
    url = req.get("url", "")
    if _is_websocket(req):
        u = urlsplit(url)
        if u.scheme in ("http", "https"):
            url = ("wss" if u.scheme == "https" else "ws") + url[len(u.scheme) :]
        return {
            "url": url,
            "method": req.get("method", "GET"),
            "type": "WebSocket",
            "initiator": "script",
            "redirect": False,
            "status": None,
            "sent": True,
        }
    if urlsplit(url).path == "/favicon.ico" and req.get("destination") == "image":
        return None
    rtype = _BY_INITIATOR.get((req.get("initiatorType") or "").lower()) or _DEST.get(
        (req.get("destination") or "").lower(), "Other"
    )
    initiator = (
        "script"
        if rtype in ("Fetch", "XHR", "Ping", "EventSource")
        else (p.get("initiator") or {}).get("type") or "other"
    )
    return {
        "url": url,
        "method": req.get("method", ""),
        "type": rtype,
        "initiator": (first or {}).get("initiator") or initiator,
        "redirect": first is not None,
    }


class Firefox:
    def __init__(
        self,
        binary,
        slot,
        wpt=None,
        extension_path=None,
        add_cust_rule=False,
        headless=False,
    ):

        if extension_path:
            self.extension_settings = ExtensionSettings(
                ext=extension_path.split("/")[-2],
                ext_dir=extension_path,
                resource_types=STRICT_TYPES,
            )
        else:
            self.extension_settings = None
        self.slot = slot
        self.profile = proc.tmpdir(f"netcensus_profile_firefox_{slot}")
        self.extension_ids: list = []
        self.wire = self.cdp = None
        self.add_cust_rule = add_cust_rule
        shutil.rmtree(self.profile, ignore_errors=True)
        os.makedirs(self.profile)
        with open(os.path.join(self.profile, "user.js"), "w", encoding="utf-8") as fh:
            for k, v in PREFS.items():
                fh.write(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n")
        args = [
            binary,
            "-no-remote",
            "-profile",
            self.profile,
            "--remote-debugging-port=0",
            "about:debugging",
        ]
        if headless:
            args.insert(1, "-headless")

        self.proc = subprocess.Popen(
            args,
            stdout=open(proc.tmpdir(f"netcensus_firefox_{slot}.log"), "w"),
            stderr=subprocess.STDOUT,
            **proc.child_kwargs(new_group=True),
        )
        proc.bind_to_parent(self.proc)
        try:
            self.wire = self.cdp = _Wire(self._bidi_url())
            self.wire.send(
                "session.new",
                {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": True}}},
            )
            self.wire.send(
                "session.subscribe",
                {
                    "events": [
                        "network.beforeRequestSent",
                        "network.responseStarted",
                        "browsingContext.contextCreated",
                    ]
                },
            )

            for hook in (_DONE_HOOK, _RTC_HOOK):
                self.wire.send(
                    "script.addPreloadScript",
                    {"functionDeclaration": "() => {" + hook + "}"},
                )
            if extension_path:
                self.extension_path = extension_path
                print("Loading Extension")
                self.extension_settings.update_manifest_v2(
                    add_cust_rule=self.add_cust_rule
                )
                if self.add_cust_rule:
                    self.extension_settings.add_test_rule()
                self._install(extension_path)
                print("Extension Sucessfully Loaded")

        except BaseException:
            self.close()
            raise

    def _bidi_url(self, wait: float = 30) -> str:
        path = os.path.join(self.profile, "WebDriverBiDiServer.json")
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"firefox worker {self.slot}: exited during startup")
            try:
                with open(path) as fh:
                    d = json.load(fh)
                return f"ws://{d['ws_host']}:{d['ws_port']}/session"
            except (OSError, ValueError, KeyError):
                time.sleep(0.25)
        raise RuntimeError(f"firefox worker {self.slot}: BiDi never came up")

    def _install(self, path: str) -> None:
        path = os.path.abspath(path)
        data = {"type": "path" if os.path.isdir(path) else "archivePath", "path": path}
        self.extension_ids.append(
            self.wire.send("webExtension.install", {"extensionData": data})["extension"]
        )

        time.sleep(_EXT_SETTLE)
        for c in self.wire.send("browsingContext.getTree").get("contexts", []):
            if c.get("url", "").startswith(("http://", "https://")):
                try:
                    self.wire.send("browsingContext.close", {"context": c["context"]})
                except RuntimeError:
                    pass

    def close(self) -> None:
        if self.wire is not None:
            self.wire.close()
        proc.kill_tree(self.proc)

    def __enter__(self) -> "Firefox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def visit(self, url: str, timeout: float, settle: float) -> Visit:
        """Load one test in a fresh tab; return every request it attempted."""
        w = self.wire
        w.drain()
        top = w.send("browsingContext.create", {"type": "tab"})["context"]
        ctxs, tops, reqs, last = {top}, {top}, {}, {}
  
        v = Visit()
        try:
            t0 = time.time()
            w.send(
                "browsingContext.navigate", {"context": top, "url": url, "wait": "none"}
            )
            while time.time() < t0 + timeout:
                self._absorb(w.drain(), ctxs, tops, reqs, last)
                try:
                    r = w.send(
                        "script.evaluate",
                        {
                            "expression": "window.__netcensusDone === true",
                            "target": {"context": top},
                            "awaitPromise": False,
                        },
                        timeout=2,
                    )
                    if r.get("result", {}).get("value") is True:
                        v.completed = True
                        break
                except (RuntimeError, TimeoutError):
                    pass
                time.sleep(0.25)
            v.seconds = round(time.time() - t0, 2)

            time.sleep(settle)
            self._absorb(w.drain(), ctxs, tops, reqs, last)
            for c in sorted(ctxs):
                try:
                    r = w.send(
                        "script.evaluate",
                        {
                            "expression": "JSON.stringify(window.__netcensusRtc || [])",
                            "target": {"context": c},
                            "awaitPromise": False,
                        },
                        timeout=2,
                    )
                    v.rtc += json.loads(r["result"]["value"])
                except (RuntimeError, TimeoutError, KeyError, TypeError, ValueError):
                    pass
        finally:
            for c in sorted(tops):
                try:
                    w.send("browsingContext.close", {"context": c}, timeout=5)
                except (RuntimeError, TimeoutError):
                    pass
        v.targets = len(ctxs)
        v.requests = list(reqs.values())
        return v

    @staticmethod
    def _absorb(events: list, ctxs: set, tops: set, reqs: dict, last: dict) -> None:
        for e in events:
            m, p = e.get("method", ""), e.get("params", {})
            if m == "browsingContext.contextCreated":
                ctxs.add(p["context"])
                if not p.get("parent"):
                    tops.add(p["context"])
                continue
            if p.get("context") not in ctxs:
                continue
            rid = p.get("request", {}).get("request", "")
            if m == "network.beforeRequestSent":
                n = p.get("redirectCount", 0)
                row = _row(p, reqs.get(rid) if n else None)
                if row is None:
                    continue
                key = rid if n == 0 else f"{rid}#{n}"
                reqs[key] = row
                last[rid] = key
            elif m == "network.responseStarted":
                row = reqs.get(last.get(rid, ""))
                if row is not None and row.get("type") == "WebSocket":
                    row["status"] = p.get("response", {}).get("status")
