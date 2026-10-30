"""hb.admission - the one door through which every heavy step on this host starts.

A workload (a seat, a venue, a gate, a test run) starts when the host's free memory is above
the line, one tenth of the machine (resource ruling 13), read live at that moment. Nothing is
predicted and nothing is remembered: no price per kind, no peaks file. Starts are one at a
time: the next claimant waits until the last start has settled, its tree not growing for
`settle_s` or `settle_max_s` passed, then reads the line again. Under the line nothing new
starts and what runs finishes. An empty host, nothing of ours running and still under the
line, waits `force_after_s`, then starts on a fresh reading and the journal says so; nothing
ever waits forever and nothing stops over memory alone. A reading the host refuses counts as
under the line. A fan-out suite (`units=(least, most)`) gets one worker per `WORKER_BYTES` of
free memory above the line, the one size this module assumes. A process a holder spawned
under its grant (`HB_ADMITTED`) takes no second grant: the parent's measures its tree. Every
holder names its run id first (`run_of`).

The record is `<state_dir>/machine/admission.json`: grants, waiters and the one start that is
settling, each with pid and heartbeat, read and written under one OS lock; a dead pid is
reclaimed. `HB_MEMORY_FREE_GB` and `HB_MEMORY_TOTAL_GB` fix the host readings, for the drills.
"""
import json
import os
import re
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from hb.venue import machine
from hb.rig.common import atomic_write, now_iso, os_lock_acquire, os_lock_release

GB = float(1 << 30)
_HERE = threading.local()
# set in the environment of every process a holder spawns under its grant
HELD_ENV = "HB_ADMITTED"
# ruling 13: the line is one tenth of the machine; a fan-out worker is sized at one gigabyte
LINE = 0.10
#: Kinds a grant may hold that are a stage for the work, never work: the venue a drive runs on.
#: The empty-host rule reads past them, or the drive that needs the venue could never force a start.
STAGE_KINDS = frozenset({"venue"})
WORKER_BYTES = 1 << 30


def _status():
    """Windows' memory status structure, filled, or None."""
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = Status()
    st.dwLength = ctypes.sizeof(Status)
    return st if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(st)) else None


def _meminfo():
    info = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.split()[0]) * 1024
    return info


def _fixed(var):
    v = os.environ.get(var)
    if not v:
        return False, None
    try:
        return True, int(float(v) * GB)
    except ValueError:
        return True, None


def read_free():
    """Bytes the host can still give: the lesser of free physical and commit headroom, or None."""
    fixed, value = _fixed("HB_MEMORY_FREE_GB")
    if fixed:
        return value
    try:
        if os.name == "nt":
            st = _status()
            return None if st is None else int(min(st.ullAvailPhys, st.ullAvailPageFile))
        info = _meminfo()
        return int(min(info["MemAvailable"], info["CommitLimit"] - info["Committed_AS"]))
    except Exception:
        return None


def read_total():
    """Bytes the machine has, or None."""
    fixed, value = _fixed("HB_MEMORY_TOTAL_GB")
    if fixed:
        return value
    try:
        if os.name == "nt":
            st = _status()
            return None if st is None else int(st.ullTotalPhys)
        return int(_meminfo()["MemTotal"])
    except Exception:
        return None


def tree_bytes(pid):
    """Bytes held by `pid` and every process under it, or None when the OS did not answer."""
    try:
        return _windows_tree_bytes(pid) if os.name == "nt" else _linux_tree_bytes(pid)
    except Exception:
        return None


def _descendants(root, children):
    seen, todo = set(), [int(root)]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(children.get(p, []))
    return seen


def _windows_tree_bytes(pid):
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    class Counters(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                           "PrivateUsage")]
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32First.argtypes = k.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), w.DWORD]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    children, e = {}, Entry()
    e.dwSize = ctypes.sizeof(Entry)
    ok = k.Process32First(snap, ctypes.byref(e))
    while ok:
        children.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
        ok = k.Process32Next(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    total = 0
    for p in _descendants(pid, children):
        h = k.OpenProcess(0x1000, False, p)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        cnt = Counters()
        cnt.cb = ctypes.sizeof(Counters)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(cnt), cnt.cb):
            total += cnt.PrivateUsage  # the commit charge is what the host runs out of
        k.CloseHandle(h)
    return total


def _linux_tree_bytes(pid):
    children, rss = {}, {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            fields = (d / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()
            children.setdefault(int(fields[1]), []).append(int(d.name))
            for line in (d / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    rss[int(d.name)] = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            continue
    return sum(rss.get(p, 0) for p in _descendants(pid, children))


def _paths(c):
    d = machine._dir(c)
    return d / "admission.lock", d / "admission.json"


def _load(path):
    rec = machine._read(path) if path.exists() else None
    if not isinstance(rec, dict):
        rec = {}
    return {"grants": rec.get("grants") or {}, "waiting": rec.get("waiting") or {}, "starting": rec.get("starting"),
            "empty_since": rec.get("empty_since")}


def run_of(holder):
    """The run a holder belongs to: every holder names its run id first."""
    return str(holder or "").split(" ", 1)[0]


def _sweep(doc, stale_after_s, note):
    """Drop every grant, waiter and settling start whose process is dead or whose heartbeat stopped."""
    for table in ("grants", "waiting"):
        for token, rec in list(doc[table].items()):
            why = machine.stale_reason(rec, stale_after_s)
            if why is None:
                continue
            del doc[table][token]
            if note:
                note("admission", state="reclaimed", was="grant" if table == "grants" else "waiter",
                     holder=rec.get("holder"), workload=rec.get("kind"), why=why)
    s = doc.get("starting")
    if s and (machine.stale_reason(s, stale_after_s) is not None or s["token"] not in doc["grants"]):
        doc["starting"] = None
    _mark_empty(doc)


def _working(doc):
    """The grants that are work: every grant but a stage the work runs on (`STAGE_KINDS`)."""
    return {token: g for token, g in doc["grants"].items() if g.get("kind") not in STAGE_KINDS}


def _mark_empty(doc):
    """Note when the host last became empty of our working grants; the forced wait counts from there."""
    if _working(doc):
        doc["empty_since"] = None
    elif doc.get("empty_since") is None:
        doc["empty_since"] = time.time()


def _decide(doc, token, free, line, now, force_after_s):
    """(admitted, forced, why) for the waiter `token`; `why` is the journaled reason either way."""
    me = doc["waiting"][token]
    settling = doc.get("starting")
    if settling and settling["token"] != token:
        return False, False, f"behind {settling['holder']}, which is still settling"
    if free is not None and free > line:
        return True, False, f"free {free} above the line {line}"
    # the forced wait counts from the later of the claimant's arrival and the host emptying, so a
    # start right after a grant ends still gives the host its minutes to hand memory back
    waited = now - max(float(me["since"]), float(doc.get("empty_since") or 0))
    if not _working(doc) and waited >= force_after_s:
        return True, True, f"empty host: nothing of ours runs and the line held {int(waited)} s; starting anyway"
    if free is None:
        return False, False, "the host did not answer how much memory is free; held as under the line"
    return False, False, f"free {free} under the line {line}; {len(doc['grants'])} running"


@contextmanager
def take(kind, holder, c=None, note=None, sample=None, held=False, units=None, give_up=None):
    """Hold a place for one `kind` through the body. Waits until `_decide` admits it, or until
    `give_up()` answers true between two readings, when the body gets None and nothing was
    taken; `note(event, **facts)` is the run journal's append. `sample()` returns the bytes the workload holds now;
    without it the tree of the pid the body `watch`es is read. `held` marks a grant that outlives
    its caller's scope (`hold`): it is never this thread's to `watch`, and may end on any thread.
    Under a parent's grant (`HELD_ENV`) nothing is taken: the parent measures this tree.
    `units=(least, most)` sizes a fan-out to the free memory above the line at one worker per
    `WORKER_BYTES`, never under `least` nor over `most`; the grant says how many (`grant["units"]`)."""
    parent = os.environ.get(HELD_ENV)
    if parent:
        if note:
            note("admission", state="under-parent", workload=kind, holder=holder, parent=parent)
        yield {"kind": kind, "holder": holder, "waited_s": 0, "pid": None, "peak": None, "under": parent,
               "units": units[1] if units else None}
        return
    s = machine.settings(c)
    light = kind in machine.light_kinds(c)
    stale_after_s, poll_s = float(s["stale_after_s"]), float(s["poll_s"])
    force_after_s = float(s["force_after_s"])
    lock, path = _paths(c)
    token = uuid.uuid4().hex
    run = run_of(holder)
    total = read_total()
    line = int(LINE * total) if total else 0
    t0 = time.time()
    noted_why = None
    sized = None
    gave_up = False
    try:
        while True:
            free = read_free()
            guard = os_lock_acquire(lock, timeout=120)
            try:
                doc = _load(path)
                _sweep(doc, stale_after_s, note)
                now = time.time()
                doc["waiting"][token] = {"kind": kind, "holder": holder, "run": run, "pid": os.getpid(),
                                         "since": doc["waiting"].get(token, {}).get("since", now), "beat": now}
                if light:
                    admitted, forced, why = True, False, f"light workload {kind}: admitted at once, never waits on the line"
                else:
                    admitted, forced, why = _decide(doc, token, free, line, now, force_after_s)
                if admitted:
                    if forced:
                        # ruling 13: a forced start reads the host once more and says what it saw
                        free = read_free()
                        why += f" on a fresh reading of {free}"
                    if units:
                        sized = max(units[0], min(units[1], int(max(0, (free or 0) - line) // WORKER_BYTES)))
                    del doc["waiting"][token]
                    doc["grants"][token] = {"kind": kind, "holder": holder, "run": run, "held": held, "pid": os.getpid(),
                                            "used": 0, "since": now, "since_iso": now_iso(), "beat": now}
                    # a settle of zero spaces nothing: the drills that measure side-by-side seats
                    if float(s["settle_s"]) > 0 and not light:
                        doc["starting"] = {"token": token, "holder": holder, "pid": os.getpid(), "since": now, "beat": now}
                    atomic_write(path, json.dumps(doc))
                    break
                running = sorted(g["holder"] for g in doc["grants"].values())
                atomic_write(path, json.dumps(doc))
            finally:
                os_lock_release(guard)
            # journaled again only when the reason changes, never when the reading moves a byte
            if _shape(why) != noted_why and note:
                noted_why = _shape(why)
                note("admission", state="waiting", workload=kind, holder=holder, free=free, line=line,
                     held_by=running, waited_s=round(time.time() - t0, 2), why=why)
            if give_up is not None and give_up():
                gave_up = True
                break
            time.sleep(poll_s)
    except BaseException:
        _drop(c, token)
        raise
    if gave_up:
        _drop(c, token)
        if note:
            note("admission", state="given-up", workload=kind, holder=holder, free=free, line=line,
                 waited_s=round(time.time() - t0, 2), why=why)
        yield None
        return
    waited = round(time.time() - t0, 2)
    grant = {"kind": kind, "holder": holder, "token": token, "waited_s": waited, "pid": None, "peak": None,
             "units": sized if units else None, "env": {HELD_ENV: holder}}
    if note:
        note("admission", state="granted", workload=kind, holder=holder, free=free, line=line, waited_s=waited,
             forced=forced, units=grant["units"], why=why)
    stop = threading.Event()
    beater = threading.Thread(target=_beat, args=(grant, token, sample, c, stop, s, note), daemon=True)
    if not held:
        _HERE.stack = getattr(_HERE, "stack", []) + [grant]
    beater.start()
    t1 = time.time()
    try:
        yield grant
    finally:
        # by identity: a held grant may end on another thread than the one that took it
        _HERE.stack = [g for g in getattr(_HERE, "stack", []) if g is not grant]
        stop.set()
        beater.join(timeout=5)
        _measure(grant, sample)
        guard = os_lock_acquire(lock, timeout=120)
        try:
            doc = _load(path)
            doc["grants"].pop(token, None)
            if (doc.get("starting") or {}).get("token") == token:
                doc["starting"] = None
            _mark_empty(doc)
            atomic_write(path, json.dumps(doc))
        finally:
            os_lock_release(guard)
        if note:
            note("admission", state="released", workload=kind, holder=holder, peak=grant["peak"],
                 held_s=round(time.time() - t1, 2))


def _shape(why):
    """A reason with its numbers taken out."""
    return re.sub(r"\d+", "#", why)


def _drop(c, token):
    """Take a waiter that will never be admitted out of the queue, so nobody waits behind it."""
    lock, path = _paths(c)
    guard = os_lock_acquire(lock, timeout=120)
    try:
        doc = _load(path)
        if doc["waiting"].pop(token, None) is not None:
            atomic_write(path, json.dumps(doc))
    finally:
        os_lock_release(guard)


def _measure(grant, sample):
    """One sample of what the workload holds now, folded into its peak."""
    got = sample() if sample else (tree_bytes(grant["pid"]) if grant["pid"] else None)
    if got is not None:
        grant["peak"] = max(grant["peak"] or 0, int(got))
    return got


def _settle_done(now, born, last_growth, settle_s, settle_max_s):
    """A start has settled when its tree has not grown for `settle_s`, or `settle_max_s` has
    passed since it was born, whichever is first; `now` is the caller's clock."""
    return now - last_growth >= settle_s or now - born >= settle_max_s


def _beat(grant, token, sample, c, stop, s, note):
    """Refresh the grant's heartbeat and what it uses now, until released; and end the start's
    settling once its tree has not grown for `settle_s`, or `settle_max_s` has passed."""
    lock, path = _paths(c)
    every_s, settle_s, settle_max_s = float(s["poll_s"]), float(s["settle_s"]), float(s["settle_max_s"])
    born = last_growth = time.time()
    high = 0
    settled = False
    while not stop.wait(every_s):
        used = _measure(grant, sample)
        now = time.time()
        if used is not None and used > high * 1.02:
            high, last_growth = used, now
        done = not settled and _settle_done(now, born, last_growth, settle_s, settle_max_s)
        guard = os_lock_acquire(lock, timeout=120)
        try:
            doc = _load(path)
            rec = doc["grants"].get(token)
            if not rec:
                return
            rec["beat"] = now
            if used is not None:
                rec["used"] = used
            starting = doc.get("starting") or {}
            if starting.get("token") == token:
                if done:
                    doc["starting"] = None
                else:
                    starting["beat"] = now
            atomic_write(path, json.dumps(doc))
        finally:
            os_lock_release(guard)
        if done:
            settled = True
            if note:
                note("admission", state="settled", workload=grant["kind"], holder=grant["holder"],
                     high=high, after_s=round(now - born, 2))


def watch(pid):
    """Tell the grant this thread holds which child to measure."""
    stack = getattr(_HERE, "stack", None)
    if stack:
        stack[-1]["pid"] = int(pid)


def hold(kind, holder, c=None, note=None, sample=None, give_up=None):
    """A grant that outlives a `with` block: returns the function that releases it, or None
    when `give_up` ended the wait first."""
    stack = ExitStack()
    grant = stack.enter_context(take(kind, holder, c, note, sample, held=True, give_up=give_up))
    if grant is None:
        stack.close()
        return None
    return stack.close
