"""Colored, per-test progress output.

Every test produces two lines, so a student can watch the run and read off
what each test did without opening a JSON file:

    ▶ testing  /fetch/api/basic/request-head.any.html
      ✔ network emits  2 runtime request(s)  HEAD :8000/fetch/api/resources/top.txt ...

Colors go to a terminal only.  Piped output, NO_COLOR, or --no-color give the
same text without escape codes, so logs written to a file stay readable.
"""
from __future__ import annotations

import os
import sys
import threading
import time

_RESET = "\033[0m"
_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32",
          "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
          "grey": "90"}


def _enable_windows_vt() -> bool:
    """Windows 10+ consoles understand ANSI only once VT mode is switched on."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)                 # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return bool(os.environ.get("TERM") or os.environ.get("WT_SESSION"))
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


class Console:
    def __init__(self, color: bool | None = None, stream=None,
                 quiet: bool = False):
        self.stream = stream or sys.stdout
        if color is None:
            color = (self.stream.isatty() and "NO_COLOR" not in os.environ
                     and _enable_windows_vt())
        self.color = color
        self.quiet = quiet
        # Workers print concurrently; a test's two lines must stay together.
        self._lock = threading.Lock()

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "\033[" + ";".join(_CODES[s] for s in styles) + "m" + text + _RESET

    def _emit(self, *lines: str) -> None:
        with self._lock:
            for line in lines:
                self.stream.write(line + "\n")
            self.stream.flush()

    # -- generic --------------------------------------------------------------
    def line(self, text: str, *styles: str) -> None:
        self._emit(self.paint(text, *styles))

    def info(self, msg: str) -> None:
        self._emit(self.paint("• ", "blue") + msg)

    def warn(self, msg: str) -> None:
        self._emit(self.paint("! " + msg, "yellow"))

    def error(self, msg: str) -> None:
        self._emit(self.paint("✖ " + msg, "red", "bold"))

    def progress(self, done: int, total: int, elapsed: float, tally: dict) -> None:
        """Periodic run-level line, printed even with --quiet."""
        rate = done / max(elapsed, 1e-6) * 60
        left = (total - done) / max(rate / 60, 1e-6)
        eta = time.strftime("%H:%M", time.localtime(time.time() + left))
        line = (f"── progress {done}/{total} ({100.0 * done / max(1, total):.1f}%)"
                f"  {rate:.0f} tests/min  elapsed {elapsed / 3600:.1f}h"
                f"  eta {left / 3600:.1f}h (~{eta})"
                f"  runtime={tally.get('runtime', 0)} static={tally.get('static', 0)}"
                f" errors={tally.get('error', 0)} ──")
        self._emit(self.paint(line, "bold", "cyan"))

    def servers(self, up: dict) -> None:
        """One line: which protocol servers wpt serve actually started."""
        parts = [self.paint(f"✔ {k}", "green") if ok else
                 self.paint(f"✖ {k}", "red", "bold") for k, ok in up.items()]
        self._emit(self.paint("• ", "blue") + "servers  " + "  ".join(parts))

    def header(self, msg: str) -> None:
        self._emit("", self.paint(msg, "bold", "cyan"))

    # -- one test -------------------------------------------------------------
    def test_result(self, idx: int, total: int, result, detail: int = 3) -> None:
        """The two-line record of one test: what ran, then what it emitted."""
        if self.quiet and not result.runtime:
            return
        counter = self.paint(f"[{idx:>{len(str(total))}}/{total}]", "grey")
        head = (f"{counter} {self.paint('▶ testing', 'bold', 'blue')}  "
                f"{result.url}")
        tier = result.tier

        def listing(items):
            shown = "  ".join(items[:detail])
            more = len(items) - detail
            tail = self.paint(f"  (+{more} more)", "grey") if more > 0 and detail else ""
            return (self.paint(shown, "dim") if detail else "") + tail

        if tier == "error":
            body = (f"    {self.paint('✖ error', 'red', 'bold')}  "
                    f"{self.paint(result.error[:160], 'red')}")
        elif tier == "runtime":
            n_r, n_s = len(result.runtime_requests), len(result.static_requests)
            extra = self.paint(f" + {n_s} static", "cyan") if n_s else ""
            protos = " ".join(self.paint(f"[{p}]", *_PROTO_STYLE.get(p, ("bold",)))
                              for p in getattr(result, "protocols", []))
            body = (f"    {self.paint('✔ network emits', 'green', 'bold')} {protos}  "
                    + self.paint(f"{n_r} runtime request(s)", "green") + extra
                    + "  " + listing(result.runtime_requests))
        elif tier == "static":
            body = (f"    {self.paint('◦ static subresources only', 'cyan', 'bold')}  "
                    + self.paint(f"{len(result.static_requests)} request(s)", "cyan")
                    + "  " + listing(result.static_requests))
        elif tier == "cdp-only":
            body = (f"    {self.paint('⚠ attempted, never reached server', 'yellow', 'bold')}  "
                    f"{self.paint(f'{result.n_cdp} browser request(s)', 'yellow')}")
        else:
            body = f"    {self.paint('· no network', 'grey')}"
        if not result.completed and result.type == "testharness" and tier != "error":
            body += self.paint("  [harness did not report]", "magenta")
        self._emit(head, body)

    def summary(self, s: dict) -> None:
        n = s["n_tests"]
        pct = lambda k: f"{s[k]}  ({100.0 * s[k] / max(1, n):.1f}%)"
        row = lambda label, val, *st: (self.paint(f"{label:<28}", "bold", *st)
                                       + self.paint(val, *st))
        self._emit(
            "",
            self.paint("━" * 64, "grey"),
            row("tests run", str(n))
            + (self.paint(f"  ({s['rows_without_session']} from a killed session: "
                          "run-level counts below are a lower bound)", "magenta")
               if s.get("rows_without_session") else ""),
            row("✔ emit at runtime", pct("n_runtime"), "green"),
            row("◦ static subresources only", pct("n_static_only"), "cyan"),
            row("· no network", pct("n_none")),
            row("⚠ attempted, not served", str(s["n_cdp_only"]), "yellow"),
            row("✖ errors", str(s["n_errors"]), "red" if s["n_errors"] else "grey"),
            row("unattributed log lines", str(s["orphan_requests"]))
            + self.paint("  (never charged to a test)", "grey"),
            row("websocket handshakes", f"{s['ws_handshakes_attributed']} credited"
                f" / {s['ws_handshakes_server']} logged by the ws server",
                "green" if s["ws_handshakes_attributed"] == s["ws_handshakes_server"]
                else "yellow"),
            self.paint("── runtime emitters by protocol " + "─" * 32, "grey"),
            *[row(f"  [{p}]", str(c), *_PROTO_STYLE.get(p, ()))
              for p, c in s.get("n_by_protocol", {}).items()],
            self.paint("━" * 64, "grey"),
        )


_PROTO_STYLE = {"http": ("blue",), "websocket": ("magenta",),
                "webtransport": ("cyan",), "webrtc": ("yellow",)}
