"""`wpt serve --verbose`: started once, shared by every worker.

--verbose is the whole point.  It raises wptserve to DEBUG, the level its
per-request access lines are written at.  Without it the server behaves
identically and logs nothing this kit can read.

`wpt run` cannot be used for this: wptrunner pins the server's logger to INFO
(tools/wptrunner/wptrunner/environment.py, get_server_logger), so the access
lines are filtered out before they reach any log.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request

from . import proc
from .config import HTTP_ORIGIN, REQUIRED_PORTS, SERVE_CONFIG


class ServeError(RuntimeError):
    pass


def held_ports() -> list[int]:
    held = []
    for port in REQUIRED_PORTS:
        sk = socket.socket()
        try:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sk.bind(("127.0.0.1", port))
        except OSError:
            held.append(port)
        finally:
            sk.close()
    return held


def is_serving(timeout: float = 3.0) -> bool:
    """Something answering HTTP, not merely holding the port.  A half-dead
    server accepts TCP and then serves nothing."""
    try:
        with urllib.request.urlopen(HTTP_ORIGIN + "/", timeout=timeout) as r:
            r.read(64)
        return True
    except Exception:
        return False


class WptServe:
    def __init__(self, wpt: str, log_path: str):
        self.wpt = wpt
        self.log_path = log_path
        self.proc = None

    def _reap(self) -> None:
        """Free the ports before starting, without touching anyone else.

        `wpt serve` spawns workers that can outlive a killed parent and keep
        its ports.  A server started on top of them binds nothing, every
        navigation lands on the browser's error page, and every test reads as
        "no network" -- silently.  So the ports are checked, not hoped for.

        Only a leftover of THIS kit is killed: a process whose command line
        names our own serve config.  A port held by anything else (another
        harness, a dev server) is reported with its PID and the run refused --
        killing an unknown process to make room is not this tool's call.
        """
        if not held_ports():
            return
        proc.kill_matching(f"serve --config {SERVE_CONFIG}")
        for _ in range(20):
            if not held_ports():
                return
            time.sleep(0.5)
        held = held_ports()
        owners = {p: proc.pids_on_port(p) for p in held}
        raise ServeError(
            f"ports held by another process: {owners}.  A census started now "
            f"would score every test a false negative, so it is refused.  "
            f"Stop that process (or run `wpt serve` elsewhere) and retry.")

    def __enter__(self) -> "WptServe":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self, wait: float = 90) -> None:
        self._reap()
        argv = [sys.executable, "./wpt", "serve", "--config", SERVE_CONFIG,
                "--verbose"]
        self.proc = subprocess.Popen(
            argv, cwd=self.wpt, stdout=open(self.log_path, "w"),
            stderr=subprocess.STDOUT, **proc.child_kwargs(new_group=True))
        proc.bind_to_parent(self.proc)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            if is_serving(2):
                return
            time.sleep(1)
        self.stop()
        raise ServeError(f"wpt serve did not come up; see {self.log_path}")

    def stop(self) -> None:
        if not self.proc:
            return
        pid = self.proc.pid
        proc.kill_tree(self.proc)
        self.proc = None
        # On Windows the workers are spawned, not forked, and are not always
        # inside the tree taskkill walks.  Their command line carries
        # `parent_pid=<our server>`, which names them and nothing else.
        proc.kill_matching(f"parent_pid={pid}")
