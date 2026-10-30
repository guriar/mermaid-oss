"""hb.run.drive - one blind seat drives the stand-in (Q2, Q9).

The audit after the loop's last round, the hotfix's short drive and a standing pass's
re-drive all sit one seat on the audit page (`roles/audit.md`, the `verify` role), in its own
signed-in browser, over the items it is given. One seat, because the census of four shards
cost thirty seat hours and found less than one discovery seat (Q10).

Every model-held item is graded by hb.run.battery, never by the seat: the caller hands the
battery's scores in (the audit runs them at k=3, Q4), or they are run here at the config's k.
The battery's row stands for the item on every path, a failed or silent seat included.

The seat's browser is the venue's `ui.driver` (hb.venue.adapter.ui_driver), filled with the
venue's session and origin; a venue without `ui` sits no seat and every row reads not offered.
"""
import json

from hb.contract import battery
from hb.contract import policy
from hb.runner import seating
from hb.venue import adapter


def not_offered_rows(ids, capability):
    """UNMEASURED rows for a lane whose venue capability is absent: nothing ran, and the row says which."""
    why = adapter.not_offered(capability)
    return [{"item": i, "state": "UNMEASURED", "substate": "not-driven", "observed": why, "seen": why,
             "no_venue": why} for i in ids]


def _keyed(spec):
    """The seat takes tool specs by name; the venue's driver serves `ui`."""
    return {"ui": spec} if spec else None


def _failed_rows(ids, why):
    return [{"item": i, "state": "UNMEASURED", "substate": "verifier-failed", "observed": why, "seen": why}
            for i in ids]


def _merge_report(j, res, rows, items, scores):
    """The seat's report with the battery's rows standing for the model items, written back
    where the seat wrote it, so a repair entry reads the graded rows."""
    rep = dict(res.get("report") or {})
    rep["items"] = battery.fold(rows, items, scores, j)
    path = j.dir / "seats" / f"verify-{rep.get('attempt')}" / "report.json"
    if res.get("result") == "ok" and rep.get("attempt") and path.exists():
        path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return rep


def run(j, vfacts, v, wt, base_sha, issue, seat_kw, c, ids, scores=None):
    """Drive `ids` on venue `v` with one seat; returns a seat-shaped result {"result",
    "report"} whose report carries every row, the battery's for each model item."""
    items = [it for i in ids for it in vfacts.get("Contract items") or [] if it.get("item") == i]
    if scores is None and adapter.offers("model"):
        scores = battery.run_all(j, c, items, vfacts.get("The venue"), wt, seat_kw.get("evidence"),
                                 seat_kw.get("timeout_min") or policy.SEAT_FUSE_MIN, base_sha)
    elif scores is None:
        scores = {r["item"]: (r, {}) for r in not_offered_rows([it["item"] for it in items if battery.is_model(it)], "model")}
    if not adapter.offers("ui"):
        j.append("drive-seat", seated=False, why=adapter.not_offered("ui"))
        return {"result": "not-offered", "report": {"items": battery.fold(not_offered_rows(ids, "ui"), items, scores, j)}}
    facts = dict(vfacts, **{"Contract items": items})
    venue = dict(facts.get("The venue") or {})
    res = seating.sit(j, "verify", facts, wt, base_sha, issue,
                      extra_env={"HB_BROWSER_PROFILE": str(venue.get("browser_profile") or ""),
                                 "HB_STORAGE_STATE": str(venue.get("storage_state") or "")},
                      tools=_keyed(adapter.ui_driver(venue.get("storage_state"), venue.get("origin"))), **seat_kw)
    ok = res.get("result") == "ok"
    rows = (res.get("report") or {}).get("items") if ok else _failed_rows(ids, f"audit seat: {res.get('result')}")
    return dict(res, report=_merge_report(j, res, rows, items, scores))
