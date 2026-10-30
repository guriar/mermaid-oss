"""hb.run.liveness - whether a run's process is still there, asked of the process.

A run records its own pid and its process start time in `pid.json` beside its journal when
it opens; a detached launch records the child it started before that. The page asks the
operating system: a live pid with the same start time is running, however quiet; a gone
pid is finished. The start time guards against Windows handing the pid to a new process.
A run with no record predates this file and is read from its journal's clock instead.
"""
import json
import os
import time
from pathlib import Path

FILE = "pid.json"
# a recorded start and the process's own start agree to within clock rounding
START_SLACK_S = 2.0


def _probe_windows(pid):
    """(alive, started epoch or None) from the process handle."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        # access denied means the process is there and belongs to someone else
        return ctypes.get_last_error() == 5, None
    try:
        code = wintypes.DWORD()
        if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
            return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
            return True, None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return True, ticks / 1e7 - 11644473600
    finally:
        k.CloseHandle(h)


def probe(pid):
    """(alive, started epoch or None) for a pid."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, None
    if pid <= 0:
        return False, None
    if os.name == "nt":
        try:
            return _probe_windows(pid)
        except (OSError, AttributeError):
            return False, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError:
        return False, None
    return True, None


def record(run_dir, pid=None):
    """Write the run's pid and its process start time; the run's own process calls this
    when it opens, a launcher may call it first with the child's pid."""
    pid = int(pid or os.getpid())
    alive, started = probe(pid)
    rec = {"pid": pid, "started": started, "at": time.time()}
    p = Path(run_dir) / FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, p)
    return rec


def read(run_dir):
    try:
        rec = json.loads((Path(run_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("pid") else None


def alive(run_dir):
    """True while the run's process is there, False once it is gone, None when the run
    recorded no pid."""
    rec = read(run_dir)
    if not rec:
        return None
    up, started = probe(rec["pid"])
    if up and started is not None and rec.get("started") is not None:
        return abs(started - rec["started"]) <= START_SLACK_S
    return up
