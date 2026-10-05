"""Check individual tests, one at a time, and show the access-log evidence.

This is the command to reach for when a row in the census looks surprising.
It runs the test SERIALLY, so every line wptserve logged during the run
belongs to it -- including the no-referer lines a parallel census cannot
attribute -- and prints each line under the label that decided it:

    own      counted: the test asked for this                   (green)
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
    emitted = 0
    with WptServe(cfg.wpt, log_path):
        console.info(f"wpt serve --verbose up  (access log: {log_path})")
        router = accesslog.LogRouter(log_path)
        router.start()
        try:
            with Browser(cfg.chrome, slot=0) as b:
                for i, t in enumerate(tests, 1):
                    router.orphans()                 # discard pre-test lines
                    key = router.open_test(t.url)
                    try:
                        visit = b.visit(t.full_url, cfg.timeout, cfg.settle)
                    except Exception as exc:
                        visit = Visit(error=f"{type(exc).__name__}: {exc}")
                    logged = router.take(key)
                    other = router.orphans(flush_pending=True)
                    result = classify(t, visit, logged)
                    emitted += result.emitted
                    console.test_result(i, len(tests), result, detail=0)
                    _evidence(console, t, logged, other, show_noise)
        finally:
            router.stop()
    console.info(f"{emitted}/{len(tests)} emitted network requests")
    return 0


def _evidence(c: Console, t: manifest.Test, logged: list, other: list,
              show_noise: bool) -> None:
    own_paths = accesslog.self_paths(t.url)
    for r in accesslog.dedup(logged):
        noise = accesslog.is_noise(r["path"], own_paths)
        if noise and not show_noise:
            continue
        tag, style = ("noise", "grey") if noise else ("own  ", "green")
        status = r["status"] if r["status"] is not None else "---"
        c.line(f"        {tag}  {status}  {r['method']:<7} "
                        f":{r['port']}{r['path']}", style)
    for r in accesslog.dedup(other):
        if accesslog.is_noise(r["path"], own_paths) and not show_noise:
            continue
        status = r["status"] if r["status"] is not None else "---"
        ref = r["referer"] or "no referer"
        c.line(f"        other  {status}  {r['method']:<7} "
                        f":{r['port']}{r['path']}  ({ref})", "yellow")
