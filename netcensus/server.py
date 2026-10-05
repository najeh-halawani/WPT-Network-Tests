"""`wpt serve --verbose`: started once, shared by every worker.

--verbose is the whole point.  It raises wptserve to DEBUG, the level its
per-request access lines are written at.  Without it the server behaves
identically and logs nothing this kit can read.

`wpt run` cannot be used for this: wptrunner pins the server's logger to INFO
(tools/wptrunner/wptrunner/environment.py, get_server_logger), so the access
lines are filtered out before they reach any log.
"""
from __future__ import annotations

import ast
import os
import re
import signal
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


# ---------------------------------------------------------------- preflight ---
# `wpt serve` starts one server per protocol, and a server that fails to start
# does NOT stop the others: measured, a missing `aioquic` meant no WebTransport
# server at all, no error anywhere a run would surface, and every WebTransport
# test scoring "no network".  So each protocol's server is checked before a
# single test runs.
_PORTS_RE = re.compile(r"Using ports: defaultdict\(<class 'list'>, (\{.*\})\)")
FIX_HINT = {
    "webtransport": "pip install -r requirements.txt   (needs aioquic==1.2.0)",
    "websocket": "check the ws/wss lines in the serve log",
    "h2": "check the h2 lines in the serve log (needs the `h2` package)",
}


def _tcp_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _udp_held(port: int) -> bool:
    """A UDP server cannot be connected to, but its port can be: if binding
    it fails, something -- our h3 server -- holds it."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        sk.close()


def server_ports(log_path: str) -> dict:
    """The ports wpt serve actually chose (`auto` ones included), from the
    `Using ports:` line it writes at startup."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _PORTS_RE.search(line)
                if m:
                    return ast.literal_eval(m.group(1))
    except (OSError, ValueError, SyntaxError):
        pass
    return {}


def preflight(log_path: str) -> dict[str, bool]:
    """protocol -> is its server up.  http/https by request, ws/wss/h2 by TCP
    connect, webtransport (UDP) by its port being held."""
    ports = server_ports(log_path)
    first = lambda k: next((p for p in ports.get(k, []) if isinstance(p, int)), None)
    up = {"http": is_serving()}
    for name, key in (("https", "https"), ("websocket", "ws"),
                      ("websocket-tls", "wss"), ("h2", "h2")):
        p = first(key)
        up[name] = bool(p) and _tcp_open(p)
    wt = first("webtransport-h3")
    up["webtransport"] = bool(wt) and _udp_held(wt)
    return up


class WptServe:
    def __init__(self, wpt: str, log_path: str, require_all: bool = True):
        self.wpt = wpt
        self.log_path = log_path
        self.require_all = require_all
        self.proc = None
        self.servers: dict[str, bool] = {}

    def _reap(self) -> None:
        """Free the ports before starting, without touching anyone else.

        `wpt serve` spawns workers that can outlive a killed parent and keep
        its ports.  A server started on top of them binds nothing, every
        navigation lands on the browser's error page, and every test reads as
        "no network" -- silently.  So the ports are checked, not hoped for.

        Only a leftover of THIS kit is killed: the server recorded in
        LEFTOVER_FILE by our previous run, or a process whose command line
        names our own serve config.  A port held by anything else (another
        harness, a dev server) is reported with its PID and the run refused --
        killing an unknown process to make room is not this tool's call.
        """
        _reap_previous_server()
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
        # --webtransport-h3: the HTTP/3 server is OPT-IN in `wpt serve`
        # (tools/serve/serve.py skips it otherwise, silently); wptrunner turns
        # it on for Chrome, and so does this.
        argv = [sys.executable, "./wpt", "serve", "--config", SERVE_CONFIG,
                "--verbose", "--webtransport-h3"]
        self.proc = subprocess.Popen(
            argv, cwd=self.wpt, stdout=open(self.log_path, "w"),
            stderr=subprocess.STDOUT, **proc.child_kwargs(new_group=True))
        proc.bind_to_parent(self.proc)
        _remember_server(self.proc.pid)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            if is_serving(2):
                self._check_servers()
                return
            time.sleep(1)
        self.stop()
        raise ServeError(f"wpt serve did not come up; see {self.log_path}")

    def _check_servers(self, wait: float = 15) -> None:
        """Every protocol server up, or refuse.  The non-http servers start
        a few seconds after http answers, so they are given `wait` to appear."""
        deadline = time.time() + wait
        while True:
            self.servers = preflight(self.log_path)
            down = [k for k, ok in self.servers.items() if not ok]
            if not down or time.time() > deadline:
                break
            time.sleep(1)
        if down and self.require_all:
            self.stop()
            hints = "\n".join(f"  {d:<14} {FIX_HINT.get(d.split('-')[0], 'see the serve log')}"
                              for d in down)
            raise ServeError(
                f"wpt serve is up, but these protocol servers are NOT:\n{hints}\n"
                f"Every test on those protocols would score \"no network\", so "
                f"the run is refused.  Fix them, or pass --allow-missing-servers "
                f"to run anyway (the gap is then recorded in the results).\n"
                f"serve log: {self.log_path}")

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
        _forget_server()


# ------------------------------------------- leftovers of OUR previous run ---
# When the census is killed before its cleanup runs (a dropped ssh session, a
# closed terminal, kill -9), wpt serve's workers survive, reparent to init and
# keep every port.  Windows prevents that with a job object; macOS has no
# equivalent.  Measured on the Mac mini: 13 orphaned workers holding all of
# 8000-9000, and the next run refused to start.  So the server is RECORDED:
#
#   POSIX    its process-group id.  It was started as a session leader, so
#            the group is its own, and orphaned workers KEEP that group id
#            after their parent dies -- the group names them exactly.
#   Windows  its pid; spawned workers carry `parent_pid=<pid>`.
LEFTOVER_FILE = proc.tmpdir("netcensus_wptserve.group")


def _remember_server(pid: int) -> None:
    try:
        with open(LEFTOVER_FILE, "w") as fh:
            fh.write(f"{pid}\n")
    except OSError:
        pass


def _forget_server() -> None:
    try:
        os.remove(LEFTOVER_FILE)
    except OSError:
        pass


def _group_members(pgid: int) -> list[tuple[int, str]]:
    """(pid, command) of every live process in a POSIX process group."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,command="],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    members = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pgid):
            members.append((int(parts[0]), parts[2]))
    return members


def _reap_previous_server() -> None:
    """Stop the server our own previous run left behind, and nothing else."""
    try:
        with open(LEFTOVER_FILE) as fh:
            old = int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return
    if proc.WINDOWS:
        proc.kill_matching(f"parent_pid={old}")
    else:
        members = _group_members(old)
        # A group id can be reused once its group is gone.  Kill only if every
        # member is still recognisably wpt serve (its python, or a
        # multiprocessing worker of it); otherwise leave it alone.
        ours = members and all(("wpt" in cmd or "multiprocessing" in cmd)
                               for _, cmd in members)
        if ours:
            try:
                os.killpg(old, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            time.sleep(1)
    _forget_server()
