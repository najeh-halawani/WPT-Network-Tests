"""Colored, per-test progress output.

Every test produces two lines, so a student can watch the run and read off
what each test did without opening a JSON file:

    ▶ testing  /fetch/api/basic/request-head.any.html
      ✔ network emits  2 request(s)  HEAD :8000/fetch/api/resources/top.txt ...

Colors go to a terminal only.  Piped output, NO_COLOR, or --no-color give the
same text without escape codes, so logs written to a file stay readable.
"""
from __future__ import annotations

import os
import sys
import threading

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

    def header(self, msg: str) -> None:
        self._emit("", self.paint(msg, "bold", "cyan"))

    # -- one test -------------------------------------------------------------
    def test_result(self, idx: int, total: int, result, detail: int = 3) -> None:
        """The two-line record of one test: what ran, then what it emitted."""
        if self.quiet and not result.emitted:
            return
        counter = self.paint(f"[{idx:>{len(str(total))}}/{total}]", "grey")
        head = (f"{counter} {self.paint('▶ testing', 'bold', 'blue')}  "
                f"{result.url}")
        if result.error:
            body = (f"    {self.paint('✖ error', 'red', 'bold')}  "
                    f"{self.paint(result.error[:160], 'red')}")
        elif result.emitted:
            shown = "  ".join(result.logged[:detail])
            more = len(result.logged) - detail
            tail = self.paint(f"  (+{more} more)", "grey") if more > 0 else ""
            body = (f"    {self.paint('✔ network emits', 'green', 'bold')}  "
                    f"{self.paint(f'{result.n_logged} request(s)', 'green')}  "
                    f"{self.paint(shown, 'dim')}{tail}")
        elif result.cdp_only:
            body = (f"    {self.paint('⚠ attempted, never reached server', 'yellow', 'bold')}  "
                    f"{self.paint(f'{result.n_cdp} browser request(s)', 'yellow')}")
        else:
            body = f"    {self.paint('· no network', 'grey')}"
        flags = []
        if not result.completed and result.type == "testharness":
            flags.append("harness did not report")
        if flags:
            body += self.paint("  [" + ", ".join(flags) + "]", "magenta")
        self._emit(head, body)

    def summary(self, s: dict) -> None:
        n, e = s["n_tests"], s["n_emitted"]
        pct = 100.0 * e / max(1, n)
        self._emit(
            "",
            self.paint("━" * 64, "grey"),
            f"{self.paint('tests run', 'bold')}            {n}",
            f"{self.paint('network emits', 'bold', 'green')}        "
            + self.paint(f"{e}  ({pct:.1f}%)", "green", "bold"),
            f"{self.paint('no network', 'bold')}           {n - e - s['n_cdp_only']}",
            f"{self.paint('attempted, not served', 'bold', 'yellow')} "
            + self.paint(str(s["n_cdp_only"]), "yellow"),
            f"{self.paint('errors', 'bold', 'red')}               "
            + self.paint(str(s["n_errors"]), "red" if s["n_errors"] else "grey"),
            f"{self.paint('unattributed lines', 'bold')}   {s['orphan_requests']}"
            + self.paint("  (no Referer; never charged to a test)", "grey"),
            self.paint("━" * 64, "grey"),
        )
