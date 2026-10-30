"""hb.run.discovery - one discovery seat per slice, on the builder's venue, after the build (Q2).

The seat is the opener with her hands full on the real model: it tries to break each model
item's act and writes what it broke as finds, never a verdict on the item. Its finds go two
ways (Q4): to the builder's next round as DISCOVERY_FINDS, and into the product's golden file
as proposed cases under their set, apart from the frozen ones, for the reviewer to accept.
"""
from hb.contract import battery
from hb.runner import seating
from hb.venue import standin

def _facts(contract, items, golden, venue):
    return {"Goal": contract["goal"],
            "Model items to break": items,
            "Golden cases already held, never a find": golden,
            "The venue": standin.venue_facts(venue)}


def run_after_build(j, c, contract, wt, base_sha, slug, venue, seat_kw):
    """The finds of one discovery seat over the slice's model items, each tagged with its item's
    case set, after the proposed golden cases are written. A seat that fails finds nothing and
    says why in the journal; no model item means no seat."""
    items = [it for it in contract.get("items") or [] if battery.is_model(it)]
    if not items:
        j.append("discovery-finds", n=0, slice=slug, why="no model items")
        return []
    held = {it["item"]: (None, battery.golden_says(wt, c, battery.case_set(it))) for it in items}
    golden = battery.golden_rows(items, held)
    res = seating.sit(j, "discovery", _facts(contract, items, golden, venue), wt, base_sha,
                      contract["issue"], **seat_kw)
    if res.get("result") != "ok":
        j.append("discovery-finds", n=0, slice=slug, why=f"discovery seat: {res.get('result')}")
        return []
    sets = {it["item"]: battery.case_set(it) for it in items}
    finds = [dict(f, set=sets[f["item"]] or None) for f in res["report"].get("finds") or [] if f.get("item") in sets]
    written, why = battery.propose_golden(finds, wt, c)
    j.append("discovery-finds", n=len(finds), slice=slug, proposed=written, why=why)
    return finds


def finds_as_fix_facts(finds):
    """The builder's fix-round facts for the finds: one line each, keyed DISCOVERY_FINDS. Each
    find on a model item is already a proposed golden case; the builder commits it with its fix."""
    if not finds:
        return {}
    return {"DISCOVERY_FINDS": [f"{f['item']}: \"{f['said']}\": {f['wrong']}"
                                + (f" (capture {f['capture']})" if f.get("capture") else "") for f in finds]}
