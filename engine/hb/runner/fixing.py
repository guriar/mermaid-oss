"""hb.run.fixing - a fix round: the builder relaunched on what a round found.

There is no fixer role and no fix report. The builder is launched again with its own brief,
the contract whole, its progress file, and two facts: the failing check lines
(`FAILING_CHECKS`) and the reviewer's findings (`FINDINGS_FOR_ME`). It writes a build note
like any build seat (A16).
"""

from hb.runner import seating

FAILING_CHECKS = "FAILING_CHECKS"
FINDINGS_FOR_ME = "FINDINGS_FOR_ME"


def fix_pass(j, contract, note, wt, base_sha, seat_kw, findings=None, reds=None, gates=None, extra=None, label=None):
    """One builder seat on one round's reds, journaled as `fix` (or `label`). Returns the seat
    result, a build note in `report`."""
    facts = {}
    failing = list(gates or []) + list(reds or [])
    if failing:
        facts[FAILING_CHECKS] = failing
    if findings:
        facts[FINDINGS_FOR_ME] = findings
    decisions = (note or {}).get("decisions") or []
    if decisions:
        facts["Your decisions so far, from the build"] = decisions
    for k, v in (extra or {}).items():
        if v is not None:
            facts[k] = v
    return seating.sit(j, "build", seating.builder_facts(contract, facts), wt, base_sha,
                       contract["issue"], label=label or "fix", **seat_kw)


