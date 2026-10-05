"""Command line: python -m netcensus <command> ..."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, manifest, proc
from .config import DATA_DIR, KIT, RunConfig
from .console import Console


def _run_args(p: argparse.ArgumentParser) -> None:
    d = RunConfig()
    p.add_argument("--wpt", default=d.wpt, help=f"WPT checkout (default {d.wpt})")
    p.add_argument("--chrome", default=d.chrome, help="Chrome/Chromium binary")
    p.add_argument("--timeout", type=float, default=d.timeout,
                   help="seconds to wait for testharness to report")
    p.add_argument("--settle", type=float, default=d.settle,
                   help="seconds to keep watching after it does")
    p.add_argument("--types", default=",".join(d.types),
                   help="manifest test types to include")
    p.add_argument("--allow-missing-servers", action="store_true",
                   help="run even if a protocol server (ws, h2, webtransport) "
                        "did not start; the gap is recorded in the results")


def _cfg(a) -> RunConfig:
    return RunConfig(wpt=os.path.abspath(a.wpt), chrome=a.chrome,
                     jobs=getattr(a, "jobs", 1), timeout=a.timeout,
                     settle=a.settle, types=tuple(a.types.split(",")),
                     filter=getattr(a, "filter", ""),
                     limit=getattr(a, "limit", 0),
                     require_all_servers=not getattr(a, "allow_missing_servers", False),
                     serial_recheck=getattr(a, "serial_recheck", RunConfig().serial_recheck))


def _check_chrome(cfg: RunConfig, c: Console) -> bool:
    if os.path.exists(cfg.chrome):
        return True
    c.error(f"no Chrome at {cfg.chrome}  (pass --chrome or set WPT_CHROME)")
    return False


def cmd_list(a, c: Console) -> int:
    cfg = _cfg(a)
    tests = manifest.tests(manifest.load(cfg.wpt), cfg.types, a.filter)
    for t in tests[:a.limit or None]:
        print(f"{t.type}\t{t.url}")
    c.info(f"{len(tests)} test(s)")
    return 0


def cmd_census(a, c: Console) -> int:
    from .runner import Census
    cfg = _cfg(a)
    if not _check_chrome(cfg, c):
        return 2
    s = Census(cfg, os.path.abspath(a.out), c, resume=a.resume).run()
    return 0 if s["n_errors"] < s["n_tests"] else 1


def cmd_verify(a, c: Console) -> int:
    from . import verify
    cfg = _cfg(a)
    if not _check_chrome(cfg, c):
        return 2
    return verify.run(cfg, a.tests, c, show_noise=not a.hide_noise)


def cmd_subtree(a, c: Console) -> int:
    from . import subtree
    stats = subtree.build(os.path.abspath(a.wpt), a.census, os.path.abspath(a.out),
                          c, force=a.force, manifest=not a.no_manifest,
                          include_static=a.include_static)
    # a tree without a MANIFEST.json cannot be run: that is a failure
    return 0 if stats.get("manifest", True) else 1


def cmd_tree(a, c: Console) -> int:
    from . import tree
    text = tree.render(a.census, a.manifest, depth=2,
                       wpt=None if a.no_overview else os.path.abspath(a.wpt))
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        c.info(f"wrote {a.out}")
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m netcensus",
        description="Find the WPT tests that actually emit network requests, "
                    "proven by wptserve's access log.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--no-color", action="store_true", help="plain output")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="census: print only tests that emit at runtime")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="enumerate tests from MANIFEST.json (no browser)")
    _run_args(p)
    p.add_argument("--filter", default="", help="path prefix, e.g. fetch/api")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("census", help="run tests, decide which emit (parallel)")
    _run_args(p)
    p.add_argument("--filter", default="", help="path prefix, e.g. fetch/api")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("-j", "--jobs", type=int, default=RunConfig().jobs)
    p.add_argument("--out", default=os.path.join(DATA_DIR, "census.json"))
    p.add_argument("--resume", action="store_true",
                   help="skip tests already in the .jsonl checkpoint")
    p.add_argument("--serial-recheck", default=RunConfig().serial_recheck,
                   metavar="PREFIXES",
                   help="after the parallel pass, re-run serially the tests under "
                        "these comma-separated prefixes that showed no runtime "
                        "evidence (default: webrtc; \"\" disables)")
    p.set_defaults(fn=cmd_census)

    p = sub.add_parser("verify", help="run test(s) serially, show the access-log evidence")
    _run_args(p)
    p.add_argument("tests", nargs="+", help="test URL, source file or directory")
    p.add_argument("--hide-noise", action="store_true",
                   help="do not print harness boilerplate lines")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("subtree", help="build a pruned runnable WPT tree")
    p.add_argument("census", help="census JSON")
    p.add_argument("--wpt", default=RunConfig().wpt)
    p.add_argument("--out", default=os.path.join(KIT, "wpt-network"))
    p.add_argument("--force", action="store_true", help="replace --out")
    p.add_argument("--no-manifest", action="store_true",
                   help="skip `wpt manifest` in the new tree")
    p.add_argument("--include-static", action="store_true",
                   help="also keep tests whose only traffic is static "
                        "(markup subresources, META dependencies)")
    p.set_defaults(fn=cmd_subtree)

    p = sub.add_parser("tree", help="render TREE.md from a census")
    p.add_argument("census", nargs="+",
                   help="census JSON(s); several are merged, last row per URL wins")
    p.add_argument("--wpt", default=RunConfig().wpt,
                   help="WPT checkout for the full-tree manifest overview")
    p.add_argument("--no-overview", action="store_true",
                   help="omit the full-tree manifest overview")
    p.add_argument("--manifest", default=os.path.join(KIT, "wpt-network",
                                                      "MANIFEST.json"))
    p.add_argument("--out", help="write here instead of stdout")
    p.set_defaults(fn=cmd_tree)
    return ap


def _interrupt_on(*signames: str) -> None:
    """Treat these signals like Ctrl-C, so the normal cleanup runs: wpt serve
    and every Chrome are stopped, finished rows are kept, --resume continues.
    SIGHUP is what a dropped ssh session sends; without this the process dies
    on the spot and its servers are orphaned holding the ports."""
    import signal

    def handler(signum, frame):
        raise KeyboardInterrupt

    for name in signames:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        # An inherited "ignore" is a decision made by whoever started us:
        # `nohup` and `screen`-like launchers ignore SIGHUP precisely so the
        # run SURVIVES a dropped session.  Replacing it with a handler would
        # turn their "keep running" into "stop" -- measured: a nohup'd run on
        # the Mac mini stopped at 97/115 when the ssh session closed.
        if signal.getsignal(sig) == signal.SIG_IGN:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    proc.utf8_stdio()
    _interrupt_on("SIGHUP", "SIGTERM")
    a = build_parser().parse_args(argv)
    c = Console(color=False if a.no_color else None, quiet=a.quiet)
    from .server import ServeError
    try:
        return a.fn(a, c)
    except ServeError as exc:
        c.error(str(exc))
        return 2
    except KeyboardInterrupt:
        c.warn("interrupted")
        return 130
