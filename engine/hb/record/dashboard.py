"""hb.run.dashboard - the home's view of the whole machine, beside the runs.

What is queued on GitHub, what the docket holds, which venues stand, and whether the
rig, the landing branch's gate and the deployed tier are healthy. GitHub and the venue
answer in seconds, so no request waits on them: each reader runs on its own timer in a
thread and a request gets the value held last, with its age. It writes nothing and decides
nothing.
"""
import functools
import json
import threading
import time
import urllib.request

from hb.rig.common import cfg, gh, state_dir
from hb.record import view
from hb.venue import adapter

_CACHE = {}
_BUSY = set()
_LOCK = threading.Lock()
# who hears that a block was read again: the watcher, which tells the open pages
LISTENERS = []
TIMER_S = 5


def _refresh(key, fn):
    try:
        value = fn()
    except Exception as ex:  # a reader that fails leaves a named gap, never a dead timer
        value = {"unread": f"{ex.__class__.__name__}: {ex}"}
    with _LOCK:
        _CACHE[key] = (time.time(), value)
        _BUSY.discard(key)
    for hear in list(LISTENERS):
        hear(key)


def _cached(key, ttl_s, fn):
    """The value held last, at once, with `age_s`; a stale or missing one is read again in
    a thread, one reader per key. Nothing held yet reads as `reading`."""
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        start = (not hit or now - hit[0] >= ttl_s) and key not in _BUSY
        if start:
            _BUSY.add(key)
    if start:
        threading.Thread(target=_refresh, args=(key, fn), daemon=True, name=f"read-{key}").start()
    if not hit:
        return {"reading": True, "age_s": None}
    return {**hit[1], "age_s": round(now - hit[0])}


def _label(key):
    """The issue label prefix the product names under queue.labels; no default."""
    prefix = ((cfg().get("queue") or {}).get("labels") or {}).get(key)
    if not prefix:
        raise KeyError(f"queue.labels.{key} is not in harness-config.json")
    return prefix


def _issues(label):
    wave_prefix = _label("wave")
    rc, o, _ = gh(["issue", "list", "--repo", cfg()["repo"], "--state", "open", "--label", label,
                   "--limit", "60", "--json", "number,title,labels"], timeout=45)
    if rc != 0:
        return None
    rows = []
    for it in json.loads(o or "[]"):
        names = [l.get("name") for l in it.get("labels") or []]
        wave = next((n[len(wave_prefix):] for n in names if n.startswith(wave_prefix)), None)
        rows.append({"number": it["number"], "title": it["title"], "wave": wave,
                     "anchor": it["title"].lower().startswith(("wave", "anchor")) or "anchor" in it["title"].lower()})
    return rows


def queue():
    """The slices GitHub holds for the doors, by lane: ready to launch, queued behind a gate,
    held by the owner. An anchor stands for its wave."""
    def read():
        lanes = {}
        unread = []
        autonomy_prefix = _label("autonomy")
        for lane in ("ready", "queued", "held"):
            rows = _issues(f"{autonomy_prefix}{lane}")
            if rows is None:
                unread.append(lane)
                rows = []
            lanes[lane] = rows
        return {"lanes": lanes, "unread": unread,
                "counts": {k: len(v) for k, v in lanes.items()}}
    return _cached("queue", 60, read)


def docket():
    """Open docket items by lane. Rig items are fixed in a session and never filed; the
    other lanes wait for the owner's walk."""
    def read():
        p = state_dir() / "docket.json"
        try:
            entries = json.loads(p.read_text(encoding="utf-8")).get("entries") or []
        except (OSError, ValueError):
            return {"open": {}, "total": 0, "unread": str(p)}
        counts = {}
        for e in entries:
            if e.get("status") == "open":
                counts[e.get("lane") or "unlaned"] = counts.get(e.get("lane") or "unlaned", 0) + 1
        order = ["rig", "hotfix", "slice", "sitting", "ruling"]
        lanes = sorted(counts, key=lambda k: (order.index(k) if k in order else 99, k))
        return {"open": {k: counts[k] for k in lanes}, "total": sum(counts.values())}
    return _cached("docket", 30, read)


def venues():
    """Every venue the product lists as standing, with its venue record's issue and state where
    one names it, and the memory it holds; memory is not given when the product has no memory
    command."""
    def read():
        if not adapter.declared():
            return {"venues": [], "mem_mib": None, "undeclared": True}
        records = {}
        for p in sorted((state_dir() / "venues").glob("v-*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if rec.get("name"):
                records[rec["name"]] = rec
        rows = []
        for listed in adapter.listed():
            name = listed.get("name")
            rec = records.get(name) or {}
            held = adapter.memory_bytes(name)
            rows.append({"venue": name, "issue": rec.get("issue"), "state": rec.get("state") or "up",
                         "origin": listed.get("origin") or rec.get("origin"), "model": rec.get("model"),
                         "created": listed.get("started") or rec.get("created"), "owner": rec.get("owner"),
                         "mem_mib": round(held / 2 ** 20) if held is not None else None})
        given = [r["mem_mib"] for r in rows if r["mem_mib"] is not None]
        return {"venues": rows, "mem_mib": sum(given) if given else None}
    return _cached("venues", 20, read)


def _landing_gate():
    c = cfg()
    branch = (c.get("branches") or {}).get("landing")
    gate_check = (c.get("ci") or {}).get("gate_check")
    if not branch or not gate_check:
        return {"unread": "branches.landing or ci.gate_check is not in harness-config.json"}
    rc, o, _ = gh(["api", f"repos/{c['repo']}/commits/{branch}/status"], timeout=30)
    if rc != 0:
        return {"unread": "gh did not answer", "branch": branch}
    try:
        d = json.loads(o)
    except ValueError:
        return {"unread": "status was not JSON", "branch": branch}
    gate = next((s for s in d.get("statuses") or [] if s.get("context") == gate_check), None)
    return {"branch": branch, "sha": (d.get("sha") or "")[:9], "state": d.get("state"),
            "gate": (gate or {}).get("state"), "at": (gate or {}).get("updated_at")}


def _deployed():
    """The deployed tier's sha, probed only when the venue offers `deploy`."""
    try:
        offered = adapter.offers("deploy")
    except SystemExit:
        return {"unread": "the venue's capabilities did not answer"}
    if not offered:
        return {"unread": adapter.not_offered("deploy")}
    url = (cfg().get("deploy") or {}).get("version_url")
    if not url:
        return {"unread": "no deploy.version_url"}
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return {"sha": ((json.loads(r.read().decode("utf-8") or "{}") or {}).get("sha") or "")[:9], "url": url}
    except Exception as ex:
        return {"unread": f"{url}: {ex.__class__.__name__}", "url": url}


def _rig():
    """The engine this page runs: a frozen version's hash, or `working` from a checkout."""
    from hb.rig import versions
    return {"commit": versions.harness_version(), "line": f"mermaid@{versions.harness_version()}"}


def health():
    def read():
        return {"rig": _rig(), "landing": _landing_gate(), "deployed": _deployed()}
    return _cached("health", 120, read)


@functools.lru_cache(maxsize=1)
def _repo():
    """Asked once: finding the config asks git."""
    return cfg()["repo"]


def blocks():
    """The machine's four blocks as last read; never waits on a reader."""
    return {"repo": _repo(), "queue": queue(), "docket": docket(), "venues": venues(), "health": health()}


def keep_fresh():
    """Each reader on its own period (GitHub 60 s, venues 20 s, docket 30 s, health 120 s),
    read in its own thread, for as long as the server runs."""
    def loop():
        while True:
            blocks()
            time.sleep(TIMER_S)
    threading.Thread(target=loop, daemon=True, name="dashboard-timers").start()


def home():
    """The whole home read now, for a server with no watcher: the runs, then the machine."""
    return {**view.home(), **blocks()}
