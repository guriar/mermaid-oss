"""hb.run.rehearsal - every state a contract names, walked before a tester sits.

A plain script, no seat and no model. It reports and never blocks: every row is driven
as before, whatever the rehearsal found. For each state the contract's rules hold in, the
product's catalog (`venue.states`, read through `adapter.states_catalog()`) says whether the
state exists and how it is proven. A state is built on the venue (`adapter.state("build", id,
name)`), its predicate asked apart from the build's exit code (`adapter.state("check", ...)`),
undone (`adapter.state("undo", ...)`), and built and asked again; a state is reachable only
when it is reached twice. A state whose entry has no `check` is graded by the build's exit
code, the only word left, and says so by name (`no-predicate`). A state the catalog marks
`external` is listed and not walked. Without the `states` capability nothing is walked and the
journal says `not offered: states`. Journaled `rehearsal`; a walked state whose predicate or
exit code failed (`unreachable`, `not-resettable`) is named in the record.
"""
from hb.venue import adapter
from hb.record import reducer
from hb.venue import standin
from hb.gates import verdict
from hb.contract.rules import EVERY_STATE

# a walked state whose build did not reach it
UNREACHED = ("unreachable", "not-resettable")
COVERED = ("reachable", "external")


def named_states(contract):
    """The state ids the contract's items hold in, in first-named order; `all` names none."""
    out = []
    for it in contract.get("items") or []:
        for s in it.get("states") or []:
            if s != EVERY_STATE and s not in out:
                out.append(s)
    return out


def _catalog():
    """{state id: entry} from the product's catalog."""
    doc = adapter.states_catalog()
    return {s["id"]: s for s in (doc.get("states") if isinstance(doc, dict) else None) or []
            if isinstance(s, dict) and s.get("id")}


def _do(verb, sid, name):
    """One state verb on the venue, recorded as the walk's step."""
    ok, tail = adapter.state(verb, sid, name)
    return {"cmd": f"{verb} {sid}", "ok": ok, "tail": None if ok else tail}


def _row(sid, verdict_, why, walked=None):
    return {"state": sid, "verdict": verdict_, "why": why, "doors": walked or []}


def _holds(entry, sid, name, built):
    """(holds, why, by_exit): the predicate's answer, or the build's exit code when the
    catalog gives the state no `check`."""
    if not entry.get("check"):
        if not built["ok"]:
            return False, f"by exit code, no predicate: `{built['cmd']}` failed: {built['tail']}", True
        return True, "by exit code, no predicate: the build ran and no `check` asserts the state", True
    asked = _do("check", sid, name)
    return asked["ok"], (None if asked["ok"] else asked["tail"]), False


def walk_state(sid, states, name):
    entry = states.get(sid)
    if entry is None:
        return _row(sid, "no-door", "not in the venue states catalog")
    if entry.get("external") is True:
        return _row(sid, "external", entry.get("says") or "human-only")
    built = _do("build", sid, name)
    holds, why, by_exit = _holds(entry, sid, name, built)
    walked = [built]
    if not holds:
        return _row(sid, "unreachable", why or "the check did not hold after the build", walked)
    walked.append(_do("undo", sid, name))
    again = _do("build", sid, name)
    walked.append(again)
    holds, why2, _ = _holds(entry, sid, name, again)
    if not holds:
        return _row(sid, "not-resettable", f"reached once, not after the way back: {why2}", walked)
    return _row(sid, "no-predicate" if by_exit else "reachable", why, walked)


def coverage(contract, rows):
    """Every case whose state the rehearsal did not prove reachable or list as external."""
    by = {r["state"]: r for r in rows}
    out = []
    for it in contract.get("items") or []:
        for s in it.get("states") or []:
            r = by.get(s)
            if s == EVERY_STATE or (r and r["verdict"] in COVERED):
                continue
            for case in it.get("cases") or [{"id": it["item"]}]:
                out.append({"case": case.get("id"), "rule": it["item"], "state": s,
                            "verdict": r["verdict"] if r else "not-rehearsed", "why": (r or {}).get("why")})
    return out


def walk(j, wt, contract, venue, only=None, attempt=1):
    """Every named state (or `only`) walked on the venue; journaled `rehearsal`."""
    if not adapter.offers("states"):
        j.append("rehearsal", attempt=attempt, skipped=True, why=adapter.not_offered("states"))
        return {"states": [], "missing": [], "not_offered": "states"}
    name = str(standin.venue_facts(venue).get("name") or "")
    states = _catalog()
    ids = [s for s in named_states(contract) if not only or s in only]
    rows = [walk_state(s, states, name) for s in ids]
    missing = [r["state"] for r in rows if r["verdict"] in UNREACHED]
    j.append("rehearsal", attempt=attempt, venue=name, states=rows, uncovered=coverage(contract, rows), missing=missing)
    return {"states": rows, "missing": missing}


def folded(j):
    """{state: its newest row} over every rehearsal walk in the journal."""
    out = {}
    for ev in j.events():
        if ev.get("kind") == "rehearsal":
            for r in ev.get("states") or []:
                out[r["state"]] = r
    return out


def before_drive(j, a, c, contract, wt, branch, base_sha, slug, seat_kw, real):
    """The rehearsal before a run's first drive, once: the venue stood for it is handed to the
    loop (`venue`). A loop already open (a resume) rehearses nothing."""
    if a.no_venue or not named_states(contract):
        return {"venue": None}
    if not adapter.offers("states"):
        j.append("rehearsal", skipped=True, why=adapter.not_offered("states"))
        return {"venue": None}
    if reducer.replay(j.events(), branch) is not None:
        return {"venue": None}
    v = standin.venue_up(j, slug, wt, real_llm=real, key="venue-up-rehearsal")
    if not v.get("venue"):
        # the loop's own drive reports the lost venue; nothing is rehearsed on no venue
        j.append("rehearsal", skipped=True, why=f"the venue did not come up: {str(v.get('error'))[:200]}")
        return {"venue": None}
    j.op("rehearsal", lambda: walk(j, wt, contract, v))
    return {"venue": v}


def report_lines(j, receipts, contract):
    """The PR record's rehearsal sections: cases without executable coverage, the states named
    and not walked (human steps, no door, external), passes the tester then could not build on,
    doors first found by testers, and states the walk could not reach. Every row
    was driven whatever these say. Empty when nothing was rehearsed."""
    rows = folded(j)
    if not rows:
        return []
    states_of = {it["item"]: [s for s in it.get("states") or [] if s != EVERY_STATE] for it in contract.get("items") or []}
    tester = [r for r in receipts or [] if verdict.classify(r)["cause_class"] == "door"]
    passed = [r for r in tester if states_of.get(r.get("item"))
              and all(rows.get(s, {}).get("verdict") == "reachable" for s in states_of[r["item"]])]
    unnamed = [r for r in tester if not states_of.get(r.get("item"))]
    named = lambda verdicts: [f"{s}: {r['verdict']}, {r['why']}" for s, r in rows.items() if r["verdict"] in verdicts]  # noqa: E731
    sections = [
        ("Cases without executable coverage (rehearsal)",
         [f"{u['case']} ({u['rule']}, state {u['state']}): {u['verdict']}: {u['why']}" for u in coverage(contract, list(rows.values()))]),
        ("States the rehearsal named and did not walk (rows driven anyway)", named(("human-step", "no-door", "external"))),
        ("Rehearsal passed, then the tester could not build the state",
         [f"{r['item']}: {str(r.get('observed'))[:160]}" for r in passed]),
        ("Doors first found by testers (no state names the row)", [f"{r['item']}: {str(r.get('observed'))[:160]}" for r in unnamed]),
        ("States the walk could not reach (rows driven anyway)", named(UNREACHED)),
    ]
    return [(title, lines) for title, lines in sections if lines]
