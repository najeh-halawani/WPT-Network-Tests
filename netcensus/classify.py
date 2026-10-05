"""The one place the "does this test emit network requests?" decision is made.

EVIDENCE, PER PROTOCOL
A request only counts when something on the far side of the network answered
it or logged it.  What that "something" is depends on the protocol, because
WPT runs a different server for each:

  protocol   server (wpt serve)            evidence the request reached it
  ---------  ----------------------------  ---------------------------------------
  http(s)    wptserve, :8000/:8443/...     a line in wptserve's ACCESS LOG,
             (+ h2 on :9000)               attributed to the test by its Referer
  ws / wss   pywebsocket, :8888/:8889      the handshake request was written on
                                           an established connection, or the
                                           server answered it (101; 200 over h2).
                                           pywebsocket's own log names no path,
                                           so it is COUNTED per run as a
                                           cross-check (runner: ws_handshakes_*)
  webtrans.  aioquic h3 server (UDP)       the session was ESTABLISHED: QUIC
                                           handshake done and the server accepted
                                           the HTTP/3 CONNECT.  That server logs
                                           no sessions at all.
  webrtc     none: peer to peer            ICE reached `connected`, i.e. a STUN
                                           connectivity check made a request/
                                           response round trip over a real
                                           socket, with the selected candidate
                                           pair recorded

TWO TIERS (http only)
Not every http request a test causes is the test *doing* something.  A
<script src> in the markup, or a `// META: script=helper.js` dependency, is part
of loading the test, like testharness.js:

    static    parser- or preload-initiated, or a declared META dependency
    runtime   anything else the server saw: script, CORS preflight, the
              browser on the page's behalf, or unseen by the browser census
              (a browser-process fetch -- nothing shows it came from markup)

WebSocket, WebTransport and WebRTC are always runtime: only script opens them.

`emitted`   = any evidence of either tier, any protocol.
`runtime`   = any RUNTIME evidence.  The headline list.
`protocols` = which protocols carried the runtime evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from . import accesslog
from .browser import Visit
from .manifest import Test

_NETWORK_SCHEMES = ("http://", "https://", "ws://", "wss://")
_DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}
STATIC_INITIATORS = frozenset({"parser", "preload"})
PROTOCOLS = ("http", "websocket", "webtransport", "webrtc")


@dataclass
class TestResult:
    url: str
    type: str
    # -- THE ANSWERS -----------------------------------------------------------
    emitted: bool = False       # any evidence, any tier
    runtime: bool = False       # any RUNTIME evidence
    protocols: list = field(default_factory=list)   # subset of PROTOCOLS
    n_logged: int = 0
    logged: list = field(default_factory=list)      # every own item, labeled
    runtime_requests: list = field(default_factory=list)
    static_requests: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    n_ws: int = 0               # WebSocket handshakes that reached the server
    n_wt: int = 0               # WebTransport sessions established
    n_rtc: int = 0              # WebRTC peers whose ICE connected
    # -- context for reading the answers, not part of them ---------------------
    n_cdp: int = 0              # requests the browser attempted (non-noise)
    cdp_only: bool = False      # attempted, nothing reached a server
    completed: bool = False     # testharness reported
    targets: int = 0            # page + frames + workers attached
    seconds: float = 0.0        # time until the harness reported
    error: str | None = None

    @property
    def tier(self) -> str:
        if self.error:
            return "error"
        if self.runtime:
            return "runtime"
        if self.emitted:
            return "static"
        return "cdp-only" if self.cdp_only else "none"

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["error"] is None:
            del d["error"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TestResult":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def _cdp_key(url: str) -> tuple | None:
    """(port, path?query) of a browser-side URL, the shape the log uses."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in _DEFAULT_PORT:
        return None
    return (port or _DEFAULT_PORT[u.scheme],
            (u.path or "/") + (f"?{u.query}" if u.query else ""))


def _label(r: dict) -> str:
    return f'{r["method"]} :{r["port"]}{r["path"]}'


def _rtc_label(entry: dict) -> str:
    p = entry.get("pair") or {}
    lo, re_ = p.get("local") or {}, p.get("remote") or {}
    if not lo:
        return f"RTC ice-{entry.get('state', 'connected')}"
    return (f"RTC {lo.get('protocol', '?')} {lo.get('type', '?')} "
            f"{lo.get('address')}:{lo.get('port')} -> "
            f"{re_.get('address')}:{re_.get('port')}")


def classify(test: Test, visit: Visit, logged: list) -> TestResult:
    """Combine one test's observations into its row."""
    own_docs = accesslog.self_paths(test.url)
    deps = frozenset(test.deps)
    own = [r for r in accesslog.dedup(logged)
           if not accesslog.is_noise(r["path"], own_docs)]

    # -- http: access log, tiered by how the browser initiated it -------------
    initiated: dict = {}
    for c in visit.requests:
        if c.get("type") in ("WebSocket", "WebTransport"):
            continue
        k = _cdp_key(c["url"])
        if k is not None:
            initiated.setdefault(k, set()).add(c.get("initiator", ""))
    runtime, static = [], []
    for r in own:
        how = initiated.get((r["port"], r["path"]), set())
        declared = accesslog.strip_query(r["path"]) in deps
        # Static only if the markup made it and NOTHING else did: the same URL
        # also requested by script means the test used it at run time.
        if declared or (how and how <= STATIC_INITIATORS):
            static.append(_label(r))
        else:
            runtime.append(_label(r))
    protocols = {"http"} if runtime else set()

    # -- websocket ------------------------------------------------------------
    ws = []
    for c in visit.requests:
        if c.get("type") == "WebSocket" and (
                c.get("sent") or c.get("status") in (101, 200)):
            k = _cdp_key(c["url"])
            if k is not None:
                ws.append(f"WS :{k[0]}{k[1]}")
    # -- webtransport ---------------------------------------------------------
    wt = []
    for c in visit.requests:
        if c.get("type") == "WebTransport" and c.get("established"):
            try:
                u = urlsplit(c["url"])
                wt.append(f"WT :{u.port}{u.path}"
                          + (f"?{u.query}" if u.query else ""))
            except ValueError:
                continue
    # -- webrtc ---------------------------------------------------------------
    rtc = [_rtc_label(e) for e in visit.rtc]

    for name, items in (("websocket", ws), ("webtransport", wt), ("webrtc", rtc)):
        if items:
            protocols.add(name)
            runtime += items

    attempted = [c for c in visit.requests
                 if c["url"].startswith(_NETWORK_SCHEMES)
                 and not accesslog.is_noise(accesslog.url_path(c["url"]),
                                            own_docs)]
    attempted += [c for c in visit.requests if c.get("type") == "WebTransport"]
    evidence = bool(own) or bool(ws) or bool(wt) or bool(rtc)
    return TestResult(
        url=test.url,
        type=test.type,
        emitted=evidence,
        runtime=bool(runtime),
        protocols=[p for p in PROTOCOLS if p in protocols],
        n_logged=len(own) + len(ws) + len(wt) + len(rtc),
        logged=sorted({_label(r) for r in own} | set(ws) | set(wt) | set(rtc)),
        runtime_requests=sorted(set(runtime)),
        static_requests=sorted(set(static)),
        statuses=sorted({r["status"] for r in own if r["status"]}),
        n_ws=len(ws),
        n_wt=len(wt),
        n_rtc=len(rtc),
        n_cdp=len(attempted),
        cdp_only=bool(attempted) and not evidence,
        completed=visit.completed,
        targets=visit.targets,
        seconds=visit.seconds,
        error=visit.error,
    )
