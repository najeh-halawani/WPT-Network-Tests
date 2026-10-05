"""Check individual tests, one at a time, and show the access-log evidence.

This is the command to reach for when a row in the census looks surprising.
It runs the test SERIALLY, so every line wptserve logged during the run
belongs to it -- including the no-referer lines a parallel census cannot
attribute -- and prints each line under the label that decided it:

    runtime  counted: issued by the test's code at run time      (green)
    static   counted: in the markup, or a declared META script   (cyan)
    noise    the test document or harness boilerplate           (grey)
    other    logged during the run with no attributable referer (yellow)
"""
from __future__ import annotations

from . import accesslog, manifest, proc
from .browser import Browser, Visit
from .classify import classify
from .config import RunConfig
from .console import Console
from .server import WptServe


def _resolve(man: dict, cfg: RunConfig, arg: str) -> list[manifest.Test]:
    """Accept a test URL, a source path, or a directory prefix."""
    arg = "/" + arg.replace("\\", "/").lstrip("/")
    every = manifest.tests(man, cfg.types)
    exact = [t for t in every if t.url == arg or t.path == arg]
    if exact:
        return exact
    # A source file (foo.any.js) expands to several URLs; a dir to many.
    stem = arg.rsplit(".", 1)[0] if arg.endswith(".js") else arg
    return [t for t in every if t.url.startswith(stem)]


def run(cfg: RunConfig, targets: list[str], console: Console,
        show_noise: bool = True) -> int:
    man = manifest.load(cfg.wpt)
    tests: list[manifest.Test] = []
    for a in targets:
        found = _resolve(man, cfg, a)
        if not found:
            console.error(f"no test in MANIFEST.json matches {a!r}")
            return 2
        tests += found
    console.header(f"verify: {len(tests)} test(s), serially")

    log_path = proc.tmpdir("netcensus_verify_access.log")
    emitted, ws_gap = 0, []
    with WptServe(cfg.wpt, log_path, cfg.require_all_servers) as serve:
        console.servers(serve.servers)
        console.info(f"wpt serve --verbose up  (access log: {log_path})")
        router = accesslog.LogRouter(log_path)
        router.start()
        try:
            with Browser(cfg.chrome, slot=0, wpt=cfg.wpt) as b:
                for i, t in enumerate(tests, 1):
                    router.orphans()                 # discard pre-test lines
                    ws_before = router.ws_handshakes
                    key = router.open_test(t.url)
                    try:
                        visit = b.visit(t.full_url, cfg.timeout, cfg.settle)
                    except Exception as exc:
                        visit = Visit(error=f"{type(exc).__name__}: {exc}")
                    logged = router.take(key)
                    other = router.orphans(flush_pending=True)
                    ws_server = router.ws_handshakes - ws_before
                    result = classify(t, visit, logged)
                    emitted += result.runtime
                    console.test_result(i, len(tests), result, detail=0)
                    _evidence(console, t, result, logged, other, show_noise)
                    # Serially, every handshake the ws server logged during
                    # this test is this test's.  A mismatch is a WebSocket the
                    # browser-side census did not see.
                    if ws_server != result.n_ws:
                        ws_gap.append((t.url, ws_server, result.n_ws))
                        console.line(
                            f"        ⚠ ws server logged {ws_server} handshake(s), "
                            f"{result.n_ws} credited", "yellow", "bold")
        finally:
            router.stop()
    console.info(f"{emitted}/{len(tests)} emit network requests at runtime")
    if ws_gap:
        console.warn(f"{len(ws_gap)} test(s) where the ws server saw handshakes "
                     f"the census did not credit:")
        for url, srv, got in ws_gap:
            console.line(f"    {url}   server={srv} credited={got}", "yellow")
    return 0


def _evidence(c: Console, t: manifest.Test, result, logged: list,
              other: list, show_noise: bool) -> None:
    own_paths = accesslog.self_paths(t.url)
    runtime = set(result.runtime_requests)
    for r in sorted(accesslog.dedup(logged),
                    key=lambda r: (accesslog.is_noise(r["path"], own_paths),
                                   r["path"])):
        noise = accesslog.is_noise(r["path"], own_paths)
        if noise and not show_noise:
            continue
        if noise:
            tag, style = "noise  ", "grey"
        elif f'{r["method"]} :{r["port"]}{r["path"]}' in runtime:
            tag, style = "runtime", "green"
        else:
            tag, style = "static ", "cyan"
        status = r["status"] if r["status"] is not None else "---"
        c.line(f"        {tag}  {status}  {r['method']:<7} "
               f":{r['port']}{r['path']}", style)
    # WebSocket / WebTransport / WebRTC evidence is not an access-log line.
    how = {"WS ": "handshake reached the ws server",
           "WT ": "session established with the h3 server",
           "RTC ": "ICE connected: STUN round trip on the wire"}
    for label in result.runtime_requests:
        for prefix, why in how.items():
            if label.startswith(prefix):
                kind, _, rest = label.partition(" ")
                c.line(f"        runtime  ok   {kind:<7} {rest}  ({why})", "green")
    for r in accesslog.dedup(other):
        if accesslog.is_noise(r["path"], own_paths) and not show_noise:
            continue
        status = r["status"] if r["status"] is not None else "---"
        ref = r["referer"] or "no referer"
        c.line(f"        other    {status}  {r['method']:<7} "
                        f":{r['port']}{r['path']}  ({ref})", "yellow")
