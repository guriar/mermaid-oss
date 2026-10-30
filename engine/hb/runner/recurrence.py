"""hb.run.recurrence - one journal line per check, fix and rerun, and the cap per problem (Q8, A18).

Every red the loop measures (a check case, a battery set, a review finding, a gate) and
every rerun of one is a `red-line` event: its id, whether it was red, the line it failed on
when it printed one, the round, and the tip of the fix it was measured after (None before any
fix). The id is the behaviour, line or no line: a red still red after two fix rounds ends by
name, once, as an `ended-by-name` event, ships red and leaves every fix list. A red that went
green, or a new id, gets another round.
"""

FIXES_BEFORE_THE_END = 2


def note(j, case, line, round, fixed_by_tip, branch=None, red=True):
    """The one line a measurement of `case` leaves in the journal."""
    return j.append("red-line", branch=branch, case=case, red=bool(red), line=line, round=round, fixed_by=fixed_by_tip)


def _lines(j, case, branch):
    """One line per round: a round re-entered after a kill measures again and the last reading stands."""
    by_round = {}
    for e in j.events():
        if e.get("kind") == "red-line" and e.get("case") == case and e.get("branch") in (None, branch):
            by_round[e.get("round")] = e
    return list(by_round.values())


def ended_ids(j, branch=None):
    return {e.get("case") for e in j.events() if e.get("kind") == "ended-by-name" and e.get("branch") in (None, branch)}


def end_if_stuck(j, case, branch=None):
    """True when `case` has ended by name: its last measurements were red, first before a fix
    and then after each of two fixes. Journaled the first time."""
    if case in ended_ids(j, branch):
        return True
    tail = _lines(j, case, branch)[-(FIXES_BEFORE_THE_END + 1):]
    if len(tail) <= FIXES_BEFORE_THE_END or not all(e.get("red") for e in tail):
        return False
    if not all(e.get("fixed_by") for e in tail[1:]):
        return False
    j.append("ended-by-name", branch=branch, case=case, line=tail[-1].get("line"), rounds=[e.get("round") for e in tail],
             why="still red after two fixes")
    return True


def report_lines(j):
    """The PR's record of the reds whose loop ended by name, each with the line it kept."""
    ends = {e.get("case"): e for e in j.events() if e.get("kind") == "ended-by-name"}
    if not ends:
        return []
    return [("Reds that ended by name (still red after two fixes, shipped red)",
             [f"{cid}: {str(e.get('line') or '')[:240]}" for cid, e in ends.items()])]
