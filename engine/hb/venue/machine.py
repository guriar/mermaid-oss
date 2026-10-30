"""hb.machine - the slot pools, and the process and heartbeat facts admission reads.

A pool holds a fixed number of slots for a resource that cannot be shared, the product's
database lease first among them (a lease taken through `adapter.database`). Room on the host is
not decided here: every heavy step is admitted by hb.admission, the one door.

A slot is a file `<state_dir>/machine/<pool>.<n>.json` naming its holder, pid, token and
heartbeat. Slots are claimed and freed under a short OS lock on `<pool>.lock`, which the
OS drops if its process dies. A slot whose pid is dead, or whose heartbeat is older than
`machine.stale_after_s`, is taken over, and the takeover is noted.

Config under `machine`: `<pool>_slots` (default 1), `stale_after_s` (default 900),
`poll_s` (default 5).
"""
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from hb.rig.common import atomic_write, cfg, now_iso, os_lock_acquire, os_lock_release, state_dir

DEFAULTS = {"stale_after_s": 900, "poll_s": 5,
            # ruling 13: a start settles after 30 s without growth or 3 min. force_after_s is a delay standing in
            # for a reading the host cannot give (memory a container VM holds is not reported as reclaimable), so
            # a heavy start on an empty host whose line is never met still starts; the grade line prices it
            "settle_s": 30, "settle_max_s": 180, "force_after_s": 90,
            # a light workload is a few seconds of our own python against the venue: it never tips a host
            # over, so it never waits on the line and never settles (166 checks once paid 90 s each)
            "light_kinds": ["checks"]}


def settings(c=None):
    c = c or cfg()
    # the drills set the waits through the environment, as they fix the host's reading
    # (HB_MEMORY_FREE_GB), so a drill measures the rule and never this host's clock; a
    # config's own machine block still wins over them
    from_env = {key: float(os.environ["HB_" + key.upper()]) for key in ("settle_s", "settle_max_s", "force_after_s")
                if os.environ.get("HB_" + key.upper())}
    return {**DEFAULTS, **from_env, **(c.get("machine") or {})}


def light_kinds(c=None):
    """The workload kinds admission admits at once: machine.light_kinds, the engine's own by default."""
    return frozenset(str(k) for k in settings(c).get("light_kinds") or ())


def capacity(pool, c=None):
    return max(1, int(settings(c).get(f"{pool}_slots") or 1))


def pid_alive(pid):
    """True while `pid` names a running process. Windows' os.kill ends the process it
    is asked about, so Windows asks the kernel for the exit code instead."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, owned by another user
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dir(c=None):
    d = state_dir(c) / "machine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stale_reason(rec, stale_after_s, now=None):
    """Why a slot record no longer holds its slot, or None while it does."""
    if not rec:
        return "unreadable"
    if not pid_alive(rec.get("pid")):
        return f"pid {rec.get('pid')} is not running"
    age = (now or time.time()) - float(rec.get("beat") or 0)
    if age > stale_after_s:
        return f"no heartbeat for {int(age)}s (pid {rec.get('pid')} may be reused)"
    return None


def _claim(pool, holder, token, cap, stale_after_s, c, note):
    """Under the pool's OS lock: free every stale slot, then take the first free one.
    Returns (slot path or None, the live holders seen)."""
    guard = os_lock_acquire(_dir(c) / f"{pool}.lock", timeout=120)
    try:
        live, free = [], []
        for n in range(cap):
            p = _dir(c) / f"{pool}.{n}.json"
            rec = _read(p) if p.exists() else None
            if p.exists():
                why = stale_reason(rec, stale_after_s)
                if why is None:
                    live.append(rec)
                    continue
                if note:
                    note("machine-takeover", pool=pool, slot=n, holder=holder,
                         stale_holder=(rec or {}).get("holder"), stale_pid=(rec or {}).get("pid"), why=why)
                p.unlink(missing_ok=True)
            free.append(p)
        # a slot numbered past a lowered capacity still holds its suite until it ends
        for p in _dir(c).glob(f"{pool}.*.json"):
            n = p.name.split(".")[1]
            if n.isdigit() and int(n) >= cap:
                rec = _read(p)
                if stale_reason(rec, stale_after_s) is None:
                    live.append(rec)
        if not free or len(live) >= cap:
            return None, live
        now = time.time()
        atomic_write(free[0], json.dumps({"pool": pool, "holder": holder, "pid": os.getpid(), "token": token,
                                          "since": now, "since_iso": now_iso(), "beat": now}))
        return free[0], live
    finally:
        os_lock_release(guard)


def _beat(path, pool, token, c, stop, every_s):
    """Refresh the heartbeat while the slot is still this holder's."""
    while not stop.wait(every_s):
        guard = os_lock_acquire(_dir(c) / f"{pool}.lock", timeout=120)
        try:
            rec = _read(path)
            if not rec or rec.get("token") != token:
                return
            rec["beat"] = time.time()
            atomic_write(path, json.dumps(rec))
        finally:
            os_lock_release(guard)


@contextmanager
def slot(pool, holder, c=None, note=None, timeout_s=None):
    """Hold one slot of `pool` for the body. A holder that waits notes once that it is
    waiting and for whom; `note(kind, **facts)` is the run journal's append.
    `timeout_s` None waits as long as it takes, since a stale holder is taken over."""
    s = settings(c)
    cap = capacity(pool, c)
    token = uuid.uuid4().hex
    t0 = time.time()
    waited_for = None
    while True:
        path, live = _claim(pool, holder, token, cap, float(s["stale_after_s"]), c, note)
        if path:
            break
        names = sorted(str(r.get("holder")) for r in live)
        if names != waited_for and note:
            note("machine-wait", pool=pool, holder=holder, capacity=cap, waiting_for=names)
        waited_for = names
        if timeout_s is not None and time.time() - t0 > timeout_s:
            raise TimeoutError(f"{pool} slot not free after {timeout_s}s; held by {names}")
        time.sleep(float(s["poll_s"]))
    waited = round(time.time() - t0, 2)
    if note:
        note("machine-slot", pool=pool, holder=holder, state="taken", slot=path.name, waited_s=waited)
    stop = threading.Event()
    beat_every = max(0.5, float(s["stale_after_s"]) / 10)
    beater = threading.Thread(target=_beat, args=(path, pool, token, c, stop, beat_every), daemon=True)
    beater.start()
    t1 = time.time()
    try:
        yield {"pool": pool, "slot": path.name, "waited_s": waited}
    finally:
        stop.set()
        beater.join(timeout=5)
        guard = os_lock_acquire(_dir(c) / f"{pool}.lock", timeout=120)
        try:
            rec = _read(path)
            if rec and rec.get("token") == token:
                path.unlink(missing_ok=True)
        finally:
            os_lock_release(guard)
        if note:
            note("machine-slot", pool=pool, holder=holder, state="released", slot=path.name,
                 held_s=round(time.time() - t1, 2))
