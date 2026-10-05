"""Headless Chrome with STOCK flags, driven over CDP, one per worker."""
from __future__ import annotations

import json
import os
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
)


@dataclass
class Visit:
    completed: bool = False
    targets: int = 0
    requests: list = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0       # navigate -> harness reported (excl. settle)


class Browser:
    """Each worker owns its profile and its DevTools port.  A shared port does
    not fail: the second browser silently attaches to the first, and every
    worker's tests run as background tabs in one browser."""

    def __init__(self, chrome: str, slot: int):
        self.slot = slot
        self.profile = proc.tmpdir(f"netcensus_profile_{slot}")
        shutil.rmtree(self.profile, ignore_errors=True)
        os.makedirs(self.profile, exist_ok=True)
        args = [chrome, *CHROME_FLAGS,
                # 0 = Chrome picks a free port and writes it to the profile.
                "--remote-debugging-port=0",
                f"--user-data-dir={self.profile}", "about:blank"]
        self.proc = subprocess.Popen(
            args, stdout=open(proc.tmpdir(f"netcensus_chrome_{slot}.log"), "w"),
            stderr=subprocess.STDOUT, **proc.child_kwargs())
        proc.bind_to_parent(self.proc)
        self.cdp = CDP(self._devtools_url())

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
                self._init_session(ns, kind, wait=False)
                self.cdp.post("Runtime.runIfWaitingForDebugger", session=ns)
                continue
            if e.get("sessionId") not in sessions:
                continue
            p = e.get("params", {})
            if m == "Network.requestWillBeSent":
                req = p.get("request", {})
                reqs[p.get("requestId", "")] = {
                    "url": req.get("url", ""), "method": req.get("method", ""),
                    "type": p.get("type", "")}
            elif m == "Network.webSocketCreated":
                reqs["ws:" + p.get("requestId", "")] = {
                    "url": p.get("url", ""), "method": "WS", "type": "WebSocket"}
