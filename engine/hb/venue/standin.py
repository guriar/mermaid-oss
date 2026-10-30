"""hb.venue.standin - the venue a run drives on, kept across rounds.

Up at a tip, down when the run is done, and the facts a tester needs to reach it. A venue is
reused only while it still serves the tip the run is about to measure; whether a change keeps
it, reloads it or rebuilds it is the product's table (`venue.reuse`), read through the adapter.
"""

import json
import re
import time
from pathlib import Path

from hb.venue import admission
from hb.venue import adapter
from hb.contract import policy
from hb.rig import gitops
from hb.runner import hbcall
from hb.rig.common import run


def venue_number(slug):
    """A venue is keyed by an integer issue number. A run from a
    sentence has none, so it takes a stable number from its slug, high enough
    never to collide with a real issue."""
    import zlib
    m = re.match(r"^#?(\d+)$", (slug or "").strip()) or re.match(r"^(?:issue|hotfix|slice)-(\d+)$", (slug or "").strip())
    if m:
        return int(m.group(1))
    return 900000 + zlib.crc32(slug.encode("utf-8")) % 90000


def venue_landed(p, tip=None):
    """A venue op landed only while its venue still stands AND serves `tip`: one that stood
    once and is down now, or serves an older commit (the wave's re-drive after a fix), is
    retried, not handed back."""
    if not (p or {}).get("venue"):
        return False
    rec = ((p or {}).get("record") or {}).get("record")
    try:
        record = json.loads(Path(rec).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if record.get("state") != "up":
        return False
    served = str(record.get("served_sha") or "")
    if tip and served and not (tip.startswith(served[:9]) or served.startswith(tip[:9])):
        return False
    return True


def venue_bytes(name):
    """Bytes the venue holds now, by the product's `venue.memory`; None when it offers none."""
    return adapter.memory_bytes(name)


# the admission grant of every venue this process stands, released when it takes the venue down
_VENUE_LEASES = {}


def venue_up(j, slug, wt, real_llm=False, key="venue-up", attempt=1, give_up=None):
    """Stand a venue once admission grants it. `give_up()` true while waiting ends the ask with
    `no_room` and no venue; the answer carries `admission_wait_s` either way."""
    import os
    number = str(venue_number(slug))
    stub = (os.environ.get("HB_VENUE_STUB") or "").strip() == "1"
    name = f"venue-{number}-{attempt}"
    # a venue lives outside this process, so its grant is measured by the product's memory command;
    # the drills' stub venue takes the same grant, with nothing to measure
    asked_at = time.time()
    release = admission.hold("venue", f"{j.run_id} {name}", note=j.append,
                             sample=(lambda: None) if stub else (lambda: venue_bytes(name)), give_up=give_up)
    waited = round(time.time() - asked_at, 2)
    if release is None:
        return {"venue": None, "no_room": True, "admission_wait_s": waited,
                "error": "admission found no room before the ask was given up"}

    def check():
        rc, js, _ = hbcall.hb(["venue", "list"], cwd=wt, timeout=120)
        for row in js.get("venues") or js.get("rows") or []:
            if (str(row.get("issue")).lstrip("#") == number and int(row.get("attempt") or 1) == attempt
                    and row.get("state") == "up"):
                return {"venue": row.get("id") or row.get("venue"), "record": row}
        return None

    def do():
        args = ["venue", "up", "--issue", number, "--owner", "runner", "--worktree", str(wt)]
        if real_llm:
            args.append("--real-llm")
        if attempt != 1:
            args += ["--attempt", str(attempt)]
        rc, js, text = hbcall.hb(args, cwd=wt, timeout=policy.VENUE_UP_MIN * 60)
        vid = js.get("venue") or js.get("id") or js.get("venue_id")
        if rc != 0 or not vid:
            return {"error": (js.get("error") or text[-400:]), "venue": None, "legs": js.get("legs")}
        return {"venue": vid, "record": js}
    tip = gitops.head_of(wt)
    try:
        res = j.op(j.fresh_key(key, lambda p: venue_landed(p, tip)), do, check=check)
    except BaseException:
        if release:
            release()
        raise
    if release:
        if (res or {}).get("venue"):
            _VENUE_LEASES[res["venue"]] = release
        else:
            release()
    return dict(res or {}, admission_wait_s=waited)


def _release_venue(venue):
    release = _VENUE_LEASES.pop(venue, None)
    if release:
        release()


def reload_venue(j, v):
    """Reload a standing venue in place through the product's `venue.reload`. True when it did."""
    import os
    name = str(((v or {}).get("record") or {}).get("name") or "")
    if not name:
        return False
    if (os.environ.get("HB_VENUE_STUB") or "").strip() == "1":
        j.append("venue", state="reloaded", venue=v["venue"], stub=True)
        return True
    if "reload" not in adapter.block():
        return False
    ok, tail = adapter.reload(name)
    j.append("venue", state="reloaded" if ok else "reload-failed", venue=v["venue"], why=None if ok else tail)
    return ok


def ensure(j, slug, wt, v, served_tip, real_llm=False, key="venue-up", attempt=1):
    """The venue at the tip: kept when the product's reuse table says the change needs nothing,
    reloaded in place when it says reload, else taken down (data kept) and stood again.
    Returns the venue record dict (`venue` None on failure)."""
    tip = gitops.head_of(wt)
    if v and v.get("venue") and served_tip:
        changed = gitops.changed_paths(wt, served_tip, tip) if served_tip != tip else []
        verdict = adapter.reuse(changed)
        if verdict == "keep":
            j.append("venue", state="kept", venue=v["venue"], since=served_tip[:9], tip=tip[:9],
                     why="no change" if not changed else "the product's reuse table keeps the venue for this change")
            return v
        if verdict == "reload" and reload_venue(j, v):
            return v
        venue_down_keep(j, v["venue"], wt, key + "-down")
    return venue_up(j, slug, wt, real_llm=real_llm, key=key, attempt=attempt)


def venue_down_keep(j, venue, wt, key):
    """Take the stand-in down but keep its database, so the drive after the build
    serves the tip on the same data the before captures saw."""
    if not venue:
        return None
    _release_venue(venue)
    return j.op(key, lambda: hbcall.hb(["venue", "down", "--venue", venue, "--keep-db"], cwd=wt, timeout=policy.VENUE_DOWN_MIN * 60)[1])


def model_on(j, v, wt, real):
    """Put a standing venue on the model this drive asked for before a seat sits, through the
    product's `venue.set_model`. A stub venue or a venue without the model capability asks nothing."""
    name = str(((v or {}).get("record") or {}).get("name") or "")
    if not real or not name or (v or {}).get("stub") or not adapter.offers("model"):
        return None
    ok, said = adapter.set_model(name, real)
    j.append("model", venue=name, state="real" if ok else "unknown", why=None if ok else str(said)[-200:])
    return said


def venue_down(j, venue, wt, key="venue-down"):
    if not venue:
        return None
    _release_venue(venue)
    return j.op(key, lambda: hbcall.hb(["venue", "down", "--venue", venue], cwd=wt, timeout=policy.VENUE_DOWN_MIN * 60)[1])


def venue_facts(venue_rec):
    """What a seat, the checks runner and the battery need to reach the venue: the record's own
    keys and the receipt's `facts`, flattened, nothing invented."""
    rec = venue_rec or {}
    keep = {}
    for k in ("venue", "origin", "served_sha", "tenant_id", "browser_profile", "storage_state", "name", "record", "model", "capabilities"):
        val = rec.get(k) if isinstance(rec, dict) else None
        if val:
            keep[k] = val
    inner = rec.get("record") if isinstance(rec, dict) else None
    if isinstance(inner, dict):
        for k, val in inner.items():
            if k not in keep and isinstance(val, (str, int, list)) and k not in ("legs",):
                keep[k] = val
        for k, val in (inner.get("facts") or {}).items():
            keep.setdefault(k, val)
    return keep
