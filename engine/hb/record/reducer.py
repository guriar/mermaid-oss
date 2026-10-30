"""hb.run.reducer - the loop's position, rebuilt from the journal alone.

`replay(events, branch)` folds the loop's events on one branch, from its last `loop-open`,
into a LoopState: the round, the phase inside it (measuring, fixing, pushing, ended), the tip
the round measures, each lane's result at that tip, the receipts and findings as they stand,
and what the rounds before it left. It is deterministic from the events: two replays of one
journal agree, and a resume takes its position from here and nowhere else.

An intent with no done is an interrupted effect, whatever its op (a fix round, a brief leg, a
door leg). `landed` reads the tree: a tip past the one the intent named means the effect
landed and is journaled done, never redone; a tip still at it means the seat is asked again,
and a seat that finished is kept by `seating`. A `harness-migrated` event owes every receipt
taken before it, as does an `invalidated` event for the rows it names.
"""

from hb.contract import policy
from hb.record.journal import MERGEABLE, RUN_COMPLETE


class LoopState:
    def __init__(self):
        self.round, self.phase, self.tip, self.tip_before = 0, "measuring", None, None
        self.lanes = {}
        self.receipts, self.findings, self.review, self.codex_in, self.asked = [], [], None, None, []
        self.preexisting = []
        self.fix_open = None
        self.ended, self.venue, self.prior = None, None, False

    def lane(self, name):
        """A lane's result for the current round at its tip, or None."""
        hit = self.lanes.get(name)
        return hit if hit and hit.get("tip") == self.tip else None


_FOLD = {}
# the events the loop writes with a branch; another branch's are another loop's
LOOP_KINDS = ("loop-open", "round-start", "lane-done", "invalidated", "intent", "done", "loop")


def on(kind):
    def wrap(fn):
        _FOLD[kind] = fn
        return fn
    return wrap


@on("loop-open")
def _open(st, ev):
    st.__init__()
    st.prior = bool(ev.get("prior"))
    st.receipts, st.findings = list(ev.get("receipts") or []), list(ev.get("findings") or [])
    st.review, st.asked, st.tip_before = ev.get("review"), list(ev.get("asked") or []), ev.get("tip_before")


@on("round-start")
def _round(st, ev):
    st.round, st.tip, st.phase, st.lanes = ev["round"], ev.get("tip"), "measuring", {}
    st.tip_before = ev.get("tip_before")


@on("lane-done")
def _lane(st, ev):
    if ev.get("round") != st.round:
        return
    st.lanes[ev["lane"]] = ev
    if ev["lane"] == "gates":
        st.preexisting = list((ev.get("got") or {}).get("preexisting") or st.preexisting)
    elif ev["lane"] == "review":
        st.review, st.findings = ev.get("review") or st.review, list(ev.get("findings") or [])
        st.codex_in = ev.get("codex_in") or st.codex_in
    elif ev["lane"] == "drive":
        st.receipts, st.venue = list(ev.get("receipts") or []), ev.get("venue") or st.venue


@on("invalidated")
def _invalidated(st, ev):
    st.receipts = policy.invalidate(st.receipts, ev.get("items") or [], ev.get("why") or "input changed")


@on("harness-migrated")
def _migrated(st, ev):
    st.receipts = policy.invalidate(st.receipts, [r.get("item") for r in st.receipts],
                                    f"harness moved from {ev.get('opened_on')} to {ev.get('now_on')}")


@on("intent")
def _intent(st, ev):
    if str(ev.get("op", "")).startswith("fix-r"):
        st.phase, st.fix_open = "fixing", ev


@on("done")
def _done(st, ev):
    if not str(ev.get("op", "")).startswith("fix-r"):
        return
    st.fix_open = None
    moved = ev.get("tip_out") and ev.get("tip_out") != st.tip
    if moved:
        st.phase, st.tip_before, st.tip = "pushing", st.tip, ev.get("tip_out")
    else:
        # the tip stands: the loop ends on this round
        st.phase = "measuring"


@on("loop")
def _loop(st, ev):
    st.ended, st.phase = ev.get("result"), "ended"


def replay(events, branch):
    """The LoopState of `branch` from its last loop-open, or None when the loop never opened."""
    st, opened = LoopState(), False
    for ev in events:
        kind = ev.get("kind")
        if kind in LOOP_KINDS and ev.get("branch") not in (None, branch):
            continue
        if kind == "loop-open":
            opened = True
        if not opened:
            continue
        fold = _FOLD.get(kind)
        if fold:
            fold(st, ev)
    return st if opened else None


def landed(j, op, head, leg_key=None, **facts):
    """The done of the last `op` intent, or one reconciled from the tree: a head past the tip
    the intent named means the effect landed, so its done is journaled now with `facts` and
    never redone. None when there is no intent, or the tree still stands at its tip.
    `leg_key` reads only the intent and done of that one leg (a brief's own hash), so a leg
    killed before its intent never takes an earlier leg's done as its own."""
    intent, done = None, None
    for ev in j.events():
        if ev.get("op") != op or (leg_key is not None and ev.get("key") != leg_key):
            continue
        if ev.get("kind") == "intent":
            intent, done = ev, None
        elif ev.get("kind") == "done" and intent is not None:
            done = ev
    if intent is None:
        return None
    if done is not None:
        return done
    if head == intent.get("tip"):
        return None
    return j.done(op, branch=intent.get("branch"), result="ok", tip_in=intent.get("tip"), tip_out=head, reconciled=True,
                  why="the effect landed on the branch before its completion was journaled",
                  **({"key": leg_key} if leg_key is not None else {}), **facts)


def reconcile(j, st, head):
    """An open fix intent against the tree: a tip past the intent's is the fix landed, journaled
    done with the tree's tip. Returns the state replayed after it."""
    if st.fix_open and head != st.tip:
        landed(j, st.fix_open["op"], head, round=st.round)
        return replay(j.events(), st.fix_open.get("branch"))
    return st


def finish_of(events):
    """The run's finish states as the journal holds them: the last `finish` event of each
    state (record-published, run-complete or run-stopped, mergeable or not-mergeable), and
    `state`, the furthest one reached."""
    out = {"record": None, "run": None, "merge": None, "state": None}
    for ev in events:
        if ev.get("kind") != "finish":
            continue
        st = ev.get("state")
        if st == "record-published":
            out["record"] = ev.get("record")
        elif st in (RUN_COMPLETE, "run-stopped"):
            out["run"] = ev.get("run")
        elif st in (MERGEABLE, "not-mergeable"):
            out["merge"], out["why"] = ev.get("merge"), ev.get("why")
        out["state"] = st
    return out
