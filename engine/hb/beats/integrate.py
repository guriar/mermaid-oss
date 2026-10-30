"""hb.run.integrate - the runner is the sole integrator of a run's branch.

Every tip the runner makes is in the journal: a seat's tip out, a compose, a round's fix, a
landing. Before a round starts the loop asks `expect`: the tree's head against
the last tip journaled. A head the journal never wrote is a commit someone else made on the
branch. It is neither refused nor absorbed: it is journaled `tip-moved` with its commits,
the receipts whose declared files it touched are invalidated (the reducer folds that), and
the loop re-enters at the moved tip. A session's fix reaches a run as a brief: an answer every
seat that starts after it reads.
"""

from hb.rig import gitops
from hb.contract import policy

# where an event records a tip the runner made
_TIP_FIELD = {"tip-moved": "now", "done": "tip_out", "round-start": "tip", "phase": "tip", "build": "tip",
              "next-done": "tip", "compose-merge": "commit", "seat": "tip_out"}


def expected(j, branch, events=None):
    """The last tip the runner journaled for `branch` (in `events`, else the whole journal), or
    None when it journaled none."""
    for ev in reversed(j.events() if events is None else events):
        field = _TIP_FIELD.get(ev.get("kind"))
        if not field or ev.get("branch") not in (None, branch):
            continue
        if ev.get("kind") == "seat" and ev.get("state") != "ok":
            continue
        tip = ev.get(field)
        if tip:
            return str(tip)
    return None


def expect(j, wt, branch, items):
    """True when the tree's head is not the tip the runner last journaled: the move is
    journaled with its commits and the rows it touched are invalidated by name."""
    head = gitops.head_of(wt)
    want = expected(j, branch)
    if not want or head == want:
        return False
    _, log, _ = gitops.git(wt, "log", "--oneline", f"{want}..{head}")
    touched = gitops.changed_paths(wt, want, head)
    moved = policy.rows_declaring(items, touched)
    j.append("tip-moved", branch=branch, was=want, now=head, commits=[l for l in (log or "").splitlines() if l.strip()],
             touched=touched, items=moved)
    if moved:
        j.append("invalidated", branch=branch, items=moved,
                 why=f"the branch tip moved outside the runner, {want[:9]} to {head[:9]}, on files these rows declare")
    return True


