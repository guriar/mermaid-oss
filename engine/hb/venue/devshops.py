"""hb.devshops - the dev venues the harness made: who owns each, when it ends, and its purge.

    hb venues sweep --expired [--dry-run]

The registry is the harness's (`<state_dir>/venues.jsonl`). A run that made a venue stamps its
owner and expiry on that venue's line here, the closeout purges the venues its run owns, and the
sweep purges the ones nobody closed out. Every purge goes through the product's own door,
`venue.purge`; a product that gives none leaves the line in place, its row saying
`not offered: purge`.
"""
import datetime as dt
import json

from hb.rig.common import atomic_write, now_iso, out, state_dir
from hb.venue import adapter

DEFAULT_TTL_DAYS = 7
DEMO_TTL_DAYS = 1
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def registry_path(c=None):
    return state_dir(c) / "venues.jsonl"


def read(path):
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _write(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))


def _parse(stamp):
    try:
        return dt.datetime.strptime(str(stamp), STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def register(path, name, owner, tool, ttl_days=DEFAULT_TTL_DAYS, tenant_id=None, now=None):
    """The one harness writer of a dev venue's line: owner, tool, born and expiry, stamped onto
    the unowned line already written for that name, else appended. Returns the line."""
    entries = read(path)
    line = next((e for e in entries if e.get("name") == name and not e.get("owner")), None)
    if line is None:
        line = {"name": name}
        entries.append(line)
    born = line.get("created_at") or now or now_iso()
    born_at = _parse(born) or dt.datetime.now(dt.timezone.utc)
    line.update({"owner": owner, "tool": tool, "born": born,
                 "expires": (born_at + dt.timedelta(days=ttl_days)).strftime(STAMP_FORMAT)})
    if tenant_id and not line.get("tenant_id"):
        line["tenant_id"] = tenant_id
    _write(path, entries)
    return line


def _candidates(entries):
    """The only lines a purge may ever take: a venue the harness stamped with an owner and a tool."""
    return [e for e in entries if e.get("name") and e.get("owner") and e.get("tool") and e.get("status") != "purged"]


def owned_by(entries, owner):
    return [e for e in _candidates(entries) if e.get("owner") == owner]


def expired(entries, now=None):
    """Stamped lines past their expiry; a line with no owner, tool or expiry is never swept."""
    now_at = _parse(now or now_iso())
    return [e for e in _candidates(entries) if _parse(e.get("expires")) and _parse(e.get("expires")) <= now_at]


def venue_purge(name):
    """(ok, the product's words): one `venue.purge`, or not offered when the product gives none."""
    if "purge" not in adapter.block():
        return False, adapter.not_offered("purge")
    ok, tail = adapter.purge(name)
    lines = [l.strip() for l in (tail or "").splitlines() if l.strip()]
    return ok, (lines[-1] if lines else ("ok" if ok else "venue.purge failed"))[:200]


def _same_line(a, b):
    return all(a.get(k) == b.get(k) for k in ("name", "owner", "born"))


def _note_on_line(path, venue, words):
    entries = read(path)
    for e in entries:
        if _same_line(e, venue):
            e["purge"] = words
    _write(path, entries)


def purge_all(path, venues, purge=None):
    """Purge each venue through the product's door; a line leaves the registry only when the
    product answered ok, else the line records the answer. Returns one row per venue."""
    purge = purge or venue_purge
    rows = []
    for venue in venues:
        ok, words = purge(venue["name"])
        if ok:
            _write(path, [e for e in read(path) if not _same_line(e, venue)])
        elif words == adapter.not_offered("purge"):
            _note_on_line(path, venue, words)
        rows.append({"name": venue["name"], "ok": ok, "words": words})
    return rows


def sweep(path, dry_run=False, purge=None, now=None):
    """The venues nobody closed out: every registry venue past its expiry, purged, or only named on a dry run."""
    due = expired(read(path), now)
    if dry_run:
        return {"verdict": "dry-run", "would_purge": [{"name": e["name"], "owner": e.get("owner"), "expires": e.get("expires")}
                                                      for e in due]}
    rows = purge_all(path, due, purge=purge)
    left = [r for r in rows if not r["ok"]]
    return {"verdict": "left-behind" if left else ("purged" if rows else "nothing-expired"),
            "purged": [r["name"] for r in rows if r["ok"]], "left_behind": left}


def cmd_shops(a):
    if not a.expired:
        out({"verdict": "refused", "why": "say which venues: --expired"}, 2)
    rec = sweep(registry_path(), dry_run=a.dry_run)
    out(rec, 1 if rec["verdict"] == "left-behind" else 0)


def register_verb(sub):
    s = sub.add_parser("venues", help="the dev venues the harness made: sweep the expired ones through the product's purge")
    s.add_argument("action", choices=["sweep"])
    s.add_argument("--expired", action="store_true", help="every registry venue past its expiry")
    s.add_argument("--dry-run", dest="dry_run", action="store_true", help="name what would be purged and purge nothing")
