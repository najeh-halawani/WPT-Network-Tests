"""Headless Chrome with STOCK flags, driven over CDP, one per worker."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import proc
from .cdp import CDP

# Injected into every document before any script runs.  It sets
# window.__netcensusDone when the test is finished, by the test's OWN signal:
#
#   testharness   add_completion_callback fires.  The hook is registered the
#                 moment testharness.js DEFINES that function (an accessor on
#                 the global), not by polling for it: a test that completes
#                 synchronously during parse -- a failing setup(), a trivial
#                 assertion -- is over before a 15 ms poll first runs, and
#                 add_completion_callback never replays a past completion.
#                 That miss turned every fast test into a full --timeout wait.
#                 Registration is deferred one microtask because testharness
#                 exposes the function before it builds the `tests` object it
#                 needs (the same trap dynamic-harness/lna_shim.js documents).
#   reftest /     no testharness: done at `load`, or -- WPT's convention for
#   crashtest     tests that keep working after load -- once `reftest-wait`
#                 is removed from the root element.
_DONE_HOOK = r"""
(() => {
  if (window.__netcensusInstalled) return;
  window.__netcensusInstalled = true;
  window.__netcensusDone = false;
  const G = window;
  let registered = false;
  const register = (fn) => {
    if (registered || typeof fn !== 'function') return;
    registered = true;
    try { fn(() => { G.__netcensusDone = true; }); } catch (e) { registered = false; }
  };
  if (typeof G.add_completion_callback === 'function') {
    register(G.add_completion_callback);
  } else {
    try {
      Object.defineProperty(G, 'add_completion_callback', {
        configurable: true, enumerable: true,
        get() { return undefined; },
        set(fn) {
          Object.defineProperty(G, 'add_completion_callback',
            {value: fn, writable: true, configurable: true, enumerable: true});
          Promise.resolve().then(() => register(fn));
        }
      });
    } catch (e) {}
  }
  // Fallback, should the accessor have been refused.
  const iv = setInterval(() => {
    if (registered) { clearInterval(iv); return; }
    if (typeof G.add_completion_callback === 'function') {
      clearInterval(iv); register(G.add_completion_callback);
    }
  }, 25);
  // No testharness at all: a reftest or crashtest.
  G.addEventListener('load', () => {
    setTimeout(() => {
      if (registered || typeof G.add_completion_callback === 'function') return;
      clearInterval(iv);
      const root = document.documentElement;
      const waiting = () => root && root.classList.contains('reftest-wait');
      if (!waiting()) { G.__netcensusDone = true; return; }
      new MutationObserver((_, obs) => {
        if (!waiting()) { obs.disconnect(); G.__netcensusDone = true; }
      }).observe(root, {attributes: true, attributeFilter: ['class']});
    }, 0);
  });
})();
"""

# WebRTC has no server to read a log from: peers connect to each other over
# real UDP (or TCP) sockets.  The evidence is ICE reaching `connected`, which
# requires a STUN connectivity check to have made a request/response round
# trip over the network stack.  This observer records that, per peer, with
# the selected candidate pair (protocol, candidate type, address, port).
#
# It is deliberately PASSIVE.  It does not replace the RTCPeerConnection
# constructor -- that breaks `instanceof`, subclassing and idlharness's
# identity checks, i.e. it would change what the test does.  It wraps
# setLocalDescription / setRemoteDescription (every connecting peer calls
# one), keeps their name and length, and only adds a state listener.
_RTC_HOOK = r"""
(() => {
  const P = window.RTCPeerConnection;
  if (window.__netcensusRtc || typeof P !== 'function') return;
  const log = window.__netcensusRtc = [];
  const seen = new WeakSet();
  const pairOf = (pc) => {
    try {
      const t = (pc.sctp && pc.sctp.transport)
        || pc.getSenders().map(s => s.transport).find(Boolean)
        || pc.getReceivers().map(r => r.transport).find(Boolean);
      const p = t && t.iceTransport && t.iceTransport.getSelectedCandidatePair();
      const c = (x) => x && {protocol: x.protocol, type: x.type,
                             address: x.address, port: x.port};
      return p ? {local: c(p.local), remote: c(p.remote)} : null;
    } catch (e) { return null; }
  };
  const watch = (pc) => {
    if (seen.has(pc)) return;
    seen.add(pc);
    let done = false;
    pc.addEventListener('iceconnectionstatechange', () => {
      const s = pc.iceConnectionState;
      if (!done && (s === 'connected' || s === 'completed')) {
        done = true;
        log.push({state: s, pair: pairOf(pc), t: Date.now()});
      }
    });
  };
  for (const name of ['setLocalDescription', 'setRemoteDescription']) {
    const d = Object.getOwnPropertyDescriptor(P.prototype, name);
    if (!d || typeof d.value !== 'function') continue;
    const orig = d.value;
    const wrapped = {[name](...a) { try { watch(this); } catch (e) {}
                                    return orig.apply(this, a); }}[name];
    Object.defineProperty(wrapped, 'length', {value: orig.length});
    Object.defineProperty(P.prototype, name, {...d, value: wrapped});
  }
})();
"""

CHROME_FLAGS = (
    "--headless=new", "--no-sandbox", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check",
    "--disable-search-engine-choice-screen",
    "--disable-backgrounding-occluded-windows",
    # wptserve's certificate is self-signed; wptrunner passes the same flag.
    "--ignore-certificate-errors",
    # Resolve the WPT host aliases inside the browser, so a run does not depend
    # on the hosts file of whoever started it.
    "--host-resolver-rules=MAP *.web-platform.test 127.0.0.1,"
    "MAP web-platform.test 127.0.0.1,"
    "MAP *.not-web-platform.test 127.0.0.1,MAP not-web-platform.test 127.0.0.1",
    # wptrunner enables these too.  Without them a class of tests bails at a
    # feature check and makes no request, which would read as "no network".
    "--enable-experimental-web-platform-features",
    "--enable-blink-test-features",
    # Cross-origin frames become their own targets, so auto-attach reports
    # their requests as well.
    "--site-per-process",
    # -- the rest mirror tools/wptrunner/wptrunner/browsers/chrome.py --------
    # Each one decides whether a family of tests gets far enough to touch the
    # network at all; without it the test bails and reads as "no network".
    "--webtransport-developer-mode",          # WebTransport to the local h3 server
    "--use-fake-device-for-media-stream",     # getUserMedia -> WebRTC tests
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-fedcm",                # FedCM fetches its IdP config
    "--short-reporting-delay",                # Reporting API sends within settle
)


def spki_flag(wpt: str) -> str | None:
    """`--ignore-certificate-errors-spki-list` for the WPT certificate.

    --ignore-certificate-errors does not cover QUIC, so without this every
    WebTransport session fails its TLS handshake and the test reads as "no
    network".  The fingerprints are read from the WPT checkout itself
    (generated there by `wpt regen-certs`), exactly the list wptrunner passes,
    so they always match the certificate this server actually presents.
    """
    path = os.path.join(wpt, "tools", "wptrunner", "wptrunner", "browsers",
                        "chrome_spki_certs.py")
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return None
    fps = re.findall(r"^\w+_FINGERPRINT\s*=\s*'([^']+)'", src, re.M)
    return "--ignore-certificate-errors-spki-list=" + ",".join(fps) if fps else None


@dataclass
class Visit:
    completed: bool = False
    targets: int = 0
    requests: list = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0       # navigate -> harness reported (excl. settle)
    rtc: list = field(default_factory=list)   # ICE-connected peers (_RTC_HOOK)


class Browser:
    """Each worker owns its profile and its DevTools port.  A shared port does
    not fail: the second browser silently attaches to the first, and every
    worker's tests run as background tabs in one browser."""

    def __init__(self, chrome: str, slot: int, wpt: str | None = None):
        self.slot = slot
        self.profile = proc.tmpdir(f"netcensus_profile_{slot}")
        shutil.rmtree(self.profile, ignore_errors=True)
        os.makedirs(self.profile, exist_ok=True)
        spki = spki_flag(wpt) if wpt else None
        args = [chrome, *CHROME_FLAGS, *([spki] if spki else []),
                # 0 = Chrome picks a free port and writes it to the profile.
                "--remote-debugging-port=0",
                f"--user-data-dir={self.profile}", "about:blank"]
        self.proc = subprocess.Popen(
            args, stdout=open(proc.tmpdir(f"netcensus_chrome_{slot}.log"), "w"),
            stderr=subprocess.STDOUT, **proc.child_kwargs())
        proc.bind_to_parent(self.proc)
        self.cdp = CDP(self._devtools_url())
        # SHARED WORKERS belong to the browser, not to the page that started
        # them, so the page-level auto-attach below never reaches them: their
        # fetches still show up in the access log (Referer = the worker
        # script, which OwnDocs maps back to the test), but a WebSocket opened
        # inside one has no other witness.  Measured on websockets/: the ws
        # server logged 869 handshakes and the census credited 731, with
        # every sharedworker variant at 0.  A browser-level auto-attach,
        # filtered to shared workers and paused on start like every other
        # target, closes that.  Each browser runs one test at a time, so a
        # shared worker that appears during a visit belongs to that test.
        self.cdp.try_send("Target.setAutoAttach",
                          {"autoAttach": True, "waitForDebuggerOnStart": True,
                           "flatten": True,
                           "filter": [{"type": "shared_worker"}]})

    def _devtools_url(self, wait: float = 30) -> str:
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                with open(os.path.join(self.profile, "DevToolsActivePort")) as fh:
                    port = int(fh.readline().strip())
            except (OSError, ValueError):
                time.sleep(0.25)
                continue
            # Chrome binds one loopback form and does not say which; on
            # Windows it is [::1].
            for host in ("127.0.0.1", "[::1]"):
                try:
                    v = json.load(urllib.request.urlopen(
                        f"http://{host}:{port}/json/version", timeout=2))
                    return v["webSocketDebuggerUrl"]
                except (urllib.error.URLError, OSError, KeyError, ValueError):
                    continue
            time.sleep(0.25)
        raise RuntimeError(f"chrome worker {self.slot}: DevTools never came up")

    def close(self) -> None:
        self.cdp.close()
        proc.kill_tree(self.proc)

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- one test -------------------------------------------------------------
    def _init_session(self, sess: str, kind: str = "page",
                      wait: bool = True) -> None:
        """Instrument one target: network census on, auto-attach cascaded.

        Each target type gets only the domains it HAS -- a worker has no Page
        domain, and a command it cannot answer is a timeout, not an error.
        Child targets are set up with post() (no waiting): they arrive paused,
        and a paused worker answers nothing until it is resumed.
        """
        send = self.cdp.try_send if wait else self.cdp.post
        is_doc = kind in ("page", "iframe")
        if is_doc:
            send("Page.enable", session=sess)
        send("Network.enable", session=sess)
        if is_doc:
            send("Runtime.enable", session=sess)
            send("Page.addScriptToEvaluateOnNewDocument",
                 {"source": _DONE_HOOK}, session=sess)
            send("Page.addScriptToEvaluateOnNewDocument",
                 {"source": _RTC_HOOK}, session=sess)
        # Cascade: every popup, OOPIF and worker the test creates is attached
        # PAUSED, instrumented, then resumed -- so none of its requests can be
        # sent before we are listening.
        send("Target.setAutoAttach",
             {"autoAttach": True, "waitForDebuggerOnStart": True,
              "flatten": True}, session=sess)

    def visit(self, url: str, timeout: float, settle: float) -> Visit:
        """Load one test in a fresh tab; return every request it attempted."""
        tid = self.cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        sess = self.cdp.send("Target.attachToTarget",
                             {"targetId": tid, "flatten": True})["sessionId"]
        sessions, reqs = {sess}, {}
        self._docs = {sess}           # document sessions (page + iframes)
        v = Visit()
        try:
            self._init_session(sess)
            self.cdp.drain()
            t0 = time.time()
            self.cdp.send("Page.navigate", {"url": url}, session=sess)
            deadline = t0 + timeout
            while time.time() < deadline:
                self._absorb(self.cdp.drain(), sessions, reqs)
                r = self.cdp.try_send(
                    "Runtime.evaluate",
                    {"expression": "window.__netcensusDone === true",
                     "returnByValue": True}, session=sess, timeout=2)
                if r and r.get("result", {}).get("value") is True:
                    v.completed = True
                    break
                time.sleep(0.25)
            v.seconds = round(time.time() - t0, 2)
            # Requests fired after the harness reports, or by a test that never
            # reports at all, still count.  Keep watching for `settle`.
            time.sleep(settle)
            self._absorb(self.cdp.drain(), sessions, reqs)
            # Collected before the tab closes: the log outlives pc.close(),
            # but not the document.
            for ds in self._docs:
                r = self.cdp.try_send(
                    "Runtime.evaluate",
                    {"expression": "JSON.stringify(window.__netcensusRtc || [])",
                     "returnByValue": True}, session=ds, timeout=2)
                try:
                    v.rtc += json.loads(r["result"]["value"]) if r else []
                except (KeyError, TypeError, ValueError):
                    pass
        finally:
            self.cdp.try_send("Target.closeTarget", {"targetId": tid})
        v.targets = len(sessions)
        v.requests = list(reqs.values())
        return v

    def _absorb(self, events: list, sessions: set, reqs: dict) -> None:
        for e in events:
            m = e.get("method", "")
            if m == "Target.attachedToTarget":
                ns = e["params"]["sessionId"]
                kind = e["params"].get("targetInfo", {}).get("type", "")
                sessions.add(ns)
                if kind == "iframe":
                    self._docs.add(ns)
                self._init_session(ns, kind, wait=False)
                self.cdp.post("Runtime.runIfWaitingForDebugger", session=ns)
                continue
            if e.get("sessionId") not in sessions:
                continue
            p = e.get("params", {})
            if m == "Network.requestWillBeSent":
                req = p.get("request", {})
                rid = p.get("requestId", "")
                # A redirect re-uses the requestId for the next hop.  Each hop
                # is its own request on the wire, so each keeps its own row,
                # and inherits how the FIRST hop was initiated.
                first = reqs.get(rid)
                key = rid if first is None else f"{rid}#{len(reqs)}"
                reqs[key] = {
                    "url": req.get("url", ""), "method": req.get("method", ""),
                    "type": p.get("type", ""),
                    "initiator": (first or {}).get("initiator")
                                 or p.get("initiator", {}).get("type", ""),
                    "redirect": first is not None}
            elif m == "Network.webSocketCreated":
                reqs["ws:" + p.get("requestId", "")] = {
                    "url": p.get("url", ""), "method": "GET",
                    "type": "WebSocket", "initiator": "script",
                    "redirect": False, "status": None}
            elif m == "Network.webSocketHandshakeResponseReceived":
                # The server's answer to the handshake.  101 can only come
                # from a server that accepted it, which makes it proof the
                # request reached the WebSocket server -- a server whose own
                # log names no path or referer and so cannot attribute it.
                ws = reqs.get("ws:" + p.get("requestId", ""))
                if ws is not None:
                    ws["status"] = p.get("response", {}).get("status")
            elif m == "Network.webSocketWillSendHandshakeRequest":
                # Fired when the handshake request is written on an
                # ESTABLISHED connection.  A test that closes the socket while
                # it is still connecting never gets a 101, yet its request did
                # reach the server -- measured: 17 such tests in websockets/,
                # each logged by the ws server and invisible to a 101-only rule.
                ws = reqs.get("ws:" + p.get("requestId", ""))
                if ws is not None:
                    ws["sent"] = True
            elif m == "Network.webTransportCreated":
                reqs["wt:" + p.get("transportId", "")] = {
                    "url": p.get("url", ""), "method": "CONNECT",
                    "type": "WebTransport", "initiator": "script",
                    "redirect": False, "established": False}
            elif m == "Network.webTransportConnectionEstablished":
                # QUIC handshake done AND the server accepted the HTTP/3
                # extended CONNECT.  The h3 server logs no sessions, so this
                # answer is the evidence it was reached.
                wt = reqs.get("wt:" + p.get("transportId", ""))
                if wt is not None:
                    wt["established"] = True
