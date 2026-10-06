"""Platform differences, stated once: process trees, TCP ports, temp paths.

WHY THIS IS SHARED RATHER THAN INLINE
-------------------------------------
Both jobs have to be done, both are done more than once in this harness, and
both are POSIX-only as originally written:

    os.killpg(os.getpgid(pid), SIGKILL)   -> AttributeError on Windows
    fuser -k -n tcp <port> / lsof -ti     -> neither ships on Windows

Neither failure is quiet in a useful way. `wpt serve` spawns multiprocessing
workers; if the parent dies and they do not, they keep holding 8000/8001/8446,
the next run's server logs "Address already in use", the public origin never
binds, and EVERY navigation lands on the browser's network-error page. That
reads downstream as "no rewrites, no sink hit" for the entire sweep -- a clean
looking corpus of false NO-TRIGGERs. It has cost a full run before, which is why
the harness kills ports up front and verifies afterwards.

So: one module, two functions, and the platform difference stated once.

On Windows a process group is not the unit of cleanup; the job is done with
`taskkill /T /F`, which walks the child tree the way killpg walks the group.
Ports are read from `netstat -ano`, because Windows has no lsof.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

WINDOWS = os.name == "nt" or sys.platform.startswith("win")


def kill_tree(proc) -> None:
    """Kill a Popen and everything it spawned. Never raises.

    POSIX: the process GROUP, which is why the process is started with
    start_new_session=True -- killing the parent alone leaves wpt serve's
    multiprocessing workers holding the ports.
    Windows: taskkill /T walks the child tree instead; there is no group.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False, timeout=20)
        else:
            pgid = os.getpgid(proc.pid)
            if pgid == os.getpgrp():
                # Spawned without its own session: the group is OURS, and
                # killpg would take this process and its shell down too.
                proc.kill()
            else:
                os.killpg(pgid, signal.SIGKILL)
    except Exception:
        # Whatever went wrong, the caller still wants the process gone.
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=8)
    except Exception:
        pass


def spawn_kwargs() -> dict:
    """Popen kwargs that make kill_tree() able to do its job.

    POSIX wants its own session so there is a group to kill. Windows wants a new
    process group so Ctrl-C in this console does not also interrupt the child,
    and so taskkill /T has a clean tree to walk.
    """
    if WINDOWS:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


# ------------------------------------------------- die with the parent ------
# kill_tree() runs when the census exits normally.  It does not run when the
# census is killed hard (a closed terminal, `taskkill /F`, a stopped task), and
# then headless Chrome and `wpt serve` keep running -- holding the ports, and
# silently poisoning the next run.  So the OS is asked to do it instead:
#
#   Windows  every child joins a Job Object created with KILL_ON_JOB_CLOSE.
#            The job handle closes when this process dies, however it dies,
#            and Windows then terminates everything in the job.
#   Linux    the child asks for SIGKILL when its parent dies (PR_SET_PDEATHSIG).
#   macOS    has neither; kill_tree() at exit is the only guarantee there.
_JOB = None


def _windows_job():
    global _JOB
    if _JOB is not None:
        return _JOB
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        _JOB = False
        return _JOB
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9,              # ExtendedLimitInformation
                                ctypes.byref(info), ctypes.sizeof(info))
    _JOB = (k32, job)
    return _JOB


def bind_to_parent(popen) -> None:
    """Make `popen` (and everything it spawns) die with this process. Never
    raises: failing to bind costs only the guarantee, not the run."""
    if not WINDOWS:
        return                    # Linux: done at spawn via preexec_fn
    try:
        job = _windows_job()
        if not job:
            return
        import ctypes
        k32, handle = job
        k32.OpenProcess.restype = ctypes.c_void_p
        ph = k32.OpenProcess(0x0001 | 0x0100, False, popen.pid)  # TERMINATE|SET_QUOTA
        if ph:
            k32.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(ph))
            k32.CloseHandle(ctypes.c_void_p(ph))
    except Exception:
        pass


def _linux_pdeathsig() -> None:          # runs in the child, before exec
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)
    except Exception:
        pass


def child_kwargs(new_group: bool = False) -> dict:
    """Popen kwargs for a child that must not outlive this process."""
    kw = spawn_kwargs() if new_group else {}
    if sys.platform.startswith("linux"):
        kw["preexec_fn"] = _linux_pdeathsig
    return kw


def pids_on_port(port: int) -> list[int]:
    """PIDs LISTENing on `port`. Empty when nothing is, or nothing can tell us."""
    if WINDOWS:
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True,
                                 check=False, timeout=25).stdout
        except Exception:
            return []
        pids = set()
        needle = ":%d" % port
        for line in out.splitlines():
            f = line.split()
            # proto  local  foreign  state  pid
            if len(f) >= 5 and f[3].upper() == "LISTENING" \
                    and f[1].endswith(needle):
                try:
                    pids.add(int(f[4]))
                except ValueError:
                    pass
        return sorted(pids)
    try:
        out = subprocess.run(["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
                             capture_output=True, text=True,
                             check=False, timeout=25).stdout
    except (FileNotFoundError, Exception):
        return []
    pids = []
    for x in out.split():
        try:
            pids.append(int(x.strip()))
        except ValueError:
            pass
    return pids


def free_port(port: int) -> None:
    """Kill whatever is LISTENing on `port`. Never raises.

    The processes being killed are orphaned `wpt serve` workers from a run that
    was interrupted rather than stopped; there is no live server at this point,
    so a hard kill is safe.
    """
    if WINDOWS:
        for pid in pids_on_port(port):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, check=False)
        return
    # fuser first (Linux psmisc). macOS ships BSD fuser, which takes FILES and
    # rejects -k/-n -- and under capture_output that failure is invisible, so
    # the harness printed "cleaning up", reaped nothing, and then refused to
    # start. Hence the explicit fallback rather than trusting the return code.
    try:
        r = subprocess.run(["fuser", "-k", "-n", "tcp", str(port)],
                           capture_output=True, check=False, timeout=25)
        if r.returncode == 0 and b"Unknown option" not in (r.stderr or b""):
            return
    except Exception:
        pass
    for pid in pids_on_port(port):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, ValueError, PermissionError):
                break
            time.sleep(0.3)


def _self_and_ancestors() -> set:
    """This process and everything that spawned it.

    kill_matching() must never kill its own caller, and "its own caller" is a
    CHAIN: a python process invoked from a shell whose command line contains the
    pattern is just as fatal as matching the python itself.
    """
    out = {os.getpid()}
    if not WINDOWS:
        try:
            out.add(os.getppid())
        except Exception:
            pass
        return out
    ps = ("$m = @{}; Get-CimInstance Win32_Process | "
          "ForEach-Object { $m[[int]$_.ProcessId] = [int]$_.ParentProcessId }; "
          "$p = %d; while ($p -and $m.ContainsKey($p)) "
          "{ Write-Output $p; $p = $m[$p] }" % os.getpid())
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, check=False, timeout=40)
        for tok in r.stdout.split():
            if tok.strip().isdigit():
                out.add(int(tok))
    except Exception:
        pass
    return out


def kill_matching(pattern: str) -> None:
    """Kill processes whose COMMAND LINE contains `pattern`. Never raises.

    The belt-and-suspenders pass after kill_tree: `wpt serve` workers can
    outlive the group/tree when the parent was killed uncleanly, and a stray one
    holding 8000 ruins the NEXT run rather than this one, which is what makes it
    worth a second sweep.

    POSIX has pkill. Windows does not -- and an unguarded pkill call is not a
    no-op there, it is a FileNotFoundError out of CreateProcess that took down
    the whole run from inside a cleanup path.

    NEVER KILLS ITS OWN CALLER OR ITS ANCESTORS. Matching is by command line, so
    the process that ASKS for the kill usually matches the pattern it passed --
    `python -c "prockill.kill_matching('lna_run_retargeted')"` has
    `lna_run_retargeted` in its own argv. Measured the hard way: two cleanup
    attempts killed the shell issuing them and returned exit 255 with no output,
    which looks like the tool crashing rather than the tool shooting the caller.
    pkill has the same hazard, so the exclusion applies on both platforms.
    """
    skip = _self_and_ancestors()
    if WINDOWS:
        # WMI is the only place the full command line lives.
        ps = ("Get-CimInstance Win32_Process | "
              "Where-Object { $_.CommandLine -like '*%s*' } | "
              "ForEach-Object { Write-Output $_.ProcessId }"
              % pattern.replace("'", "''"))
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", ps],
                               capture_output=True, text=True, check=False,
                               timeout=40)
        except Exception:
            return
        for tok in r.stdout.split():
            if not tok.strip().isdigit():
                continue
            pid = int(tok)
            if pid in skip:
                continue
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, check=False)
        return
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                             text=True, check=False, timeout=25).stdout
    except Exception:
        # No pgrep: fall back to pkill, which cannot exclude and so is a last
        # resort rather than the default.
        try:
            subprocess.run(["pkill", "-9", "-f", pattern],
                           capture_output=True, check=False, timeout=25)
        except Exception:
            pass
        return
    for tok in out.split():
        if not tok.strip().isdigit():
            continue
        pid = int(tok)
        if pid in skip:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            pass


def tmpdir(*parts) -> str:
    """A scratch path that exists on this platform. Never returns "/tmp/...".

    A literal "/tmp" is not merely unidiomatic on Windows, it BREAKS ARGUMENT
    PARSING: Firefox reads the leading slash as a switch prefix, so
    `--profile` with a "/tmp/..." value came back as
    `Error: argument --profile requires a path` and every test in the run scored
    UNMEASURED. Chrome was luckier only because a D:/tmp happened to exist.

    tempfile.gettempdir() honours TMPDIR on POSIX and TEMP/TMP on Windows, so
    both hosts get somewhere writable without either being special-cased.
    """
    import tempfile
    return os.path.join(tempfile.gettempdir(), *parts)


def utf8_stdio() -> None:
    """Make stdout/stderr able to carry the progress bar. Never raises.

    The Windows console defaults to cp1252, which cannot encode the block
    characters the progress bar draws, so the FIRST bar update died with
    UnicodeEncodeError and took the run with it -- from inside a progress
    display, which has no business being able to fail a sweep.

    errors="replace" rather than a plain UTF-8 switch: a console that still
    cannot render a glyph should print a substitute, not raise. On POSIX this is
    already the case and the call is a no-op.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
