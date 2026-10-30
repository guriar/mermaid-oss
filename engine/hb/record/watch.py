"""hb.run.watch - one plain line per boundary, for the chat that launched a run.

`hb run watch <run id> [--after N] [--max-min M]` blocks until the journal holds a
notable event past cursor N (a phase, a seat that ended, a leg, a fix round, a question,
a control verb, a standing run's next step, a failed venue, a pre-existing red, a round of
gates with each gate's seconds, the suite's scope or crash), prints
those events as short lines and the new cursor, then exits. The chat launches it again with
the new cursor. It ends early when the run reached a final phase, or a standing phase past
the cursor, and at `--max-min` with nothing new so a watcher never hangs on a dead run.
"""
import time

from hb.rig.common import out
from hb.record.journal import Journal
from hb.record.timeline import gate_seconds

from hb.record.journal import FINAL, STANDING
POLL_S = 5


def _line(e):
    k = e.get("kind")
    ts = (e.get("ts") or "")[11:16]
    if k == "phase":
        ph = e.get("phase")
        extra = {x: e.get(x) for x in ("leg", "findings", "fix", "stop", "works", "broken", "unmeasured", "red",
                                       "legs", "built", "why", "pr", "captures") if e.get(x) not in (None, "", [], {})}
        return f"{ts} phase {ph}" + (" " + ", ".join(f"{a} {b}" for a, b in extra.items()) if extra else "")
    if k == "finish":
        facts = {x: e.get(x) for x in ("record", "run", "how", "merge", "commit") if e.get(x)}
        why = "; ".join(e.get("why") or [])
        return f"{ts} finish {e.get('state')}" + (" " + ", ".join(f"{a} {b}" for a, b in facts.items()) if facts else "") + (f": {why}" if why else "")
    if k == "seat":
        st = e.get("state")
        if st == "started":
            return None
        cost = ((e.get("usage") or {}).get("cost_usd"))
        bits = [f"{ts} seat {e.get('seat')} {e.get('attempt')} {st}", f"{e.get('minutes')} min"]
        if cost is not None:
            bits.append(f"${cost:.2f}")
        if e.get("problems"):
            bits.append("; ".join(str(p)[:80] for p in e["problems"][:2]))
        return " · ".join(bits)
    if k == "build-leg":
        return f"{ts} leg {e.get('leg')} attempt {e.get('attempt')} {e.get('result')} tip {(e.get('tip') or '')[:9]}"
    if k == "fix-round":
        return f"{ts} fix round {e.get('stage')} {e.get('result')}"
    if k == "question":
        return f"{ts} QUESTION from {e.get('seat')}: {e.get('text')}"
    if k == "control":
        return f"{ts} control {e.get('verb')}" + (f" reached {e.get('reached')}" if e.get("reached") else "") + \
               (f": {e.get('text')}" if e.get("text") else "")
    if k == "next":
        refused = f" ({e.get('refused')})" if e.get("refused") else ""
        return f"{ts} next {e.get('verb') or 'refused'}" + (f": {e.get('text')}" if e.get("text") else "") + refused
    if k == "next-done":
        stops = f", stops {'; '.join(e.get('stops'))}" if e.get("stops") else ""
        return f"{ts} next {e.get('verb')} done: {e.get('result')}" + stops
    if k == "venue" and e.get("state") == "failed":
        return f"{ts} venue failed ({e.get('stage') or 'drive'}): {str(e.get('why'))[:160]}"
    if k == "preexisting-red":
        return f"{ts} pre-existing red on {e.get('gate')}: {', '.join((e.get('red_ids') or [])[:4])}"
    if k == "op" and str(e.get("key") or "").startswith("gates-") and e.get("state") == "complete":
        rows = [r for r in e.get("result") or [] if isinstance(r, dict)]
        took = gate_seconds(rows, e.get("ts"))
        bits = [f"{r.get('name')}" + (f" {took[i]:.1f}s" if took[i] is not None else "")
                + ("" if r.get("true_exit") == 0 else " red" if r.get("true_exit") is not None else " measured nothing")
                for i, r in enumerate(rows)]
        return f"{ts} {str(e['key']).split('@')[0]}: " + (", ".join(bits) or "no gate ran")
    if k == "suite-scope":
        return f"{ts} round {e.get('round')} runs the {e.get('scope')} backend tests: {e.get('why')}"
    if k == "verdict" and e.get("outcome") != "WORKS":
        who = e.get("gate") or e.get("subject")
        return (f"{ts} {who}: {e.get('outcome')} ({e.get('cause_class')}" + (f", {e['signature']}" if e.get("signature") else "")
                + (", rerun" if e.get("rerun") else "") + ")")
    if k == "gate-recovery":
        return f"{ts} {e.get('gate')}: {e.get('signature')} is the environment's; once more after {e.get('condition')}"
    if k == "respawn":
        return f"{ts} respawn {e.get('seat')} after {e.get('after')}"
    if k == "battery-run":
        score = f"pass^k {e.get('pass_k')} (k {e.get('k')}, {e.get('cases')} cases, bar {e.get('bar')})" if e.get("pass_k") is not None else str(e.get("why") or e.get("exit"))
        return f"{ts} battery {e.get('item')} {e.get('set')}: {e.get('state')} {score}"
    if k == "tester-find":
        return f"{ts} tester find {e.get('item')}: {len(e.get('found') or [])} proposed golden case(s)"
    if k == "success-check":
        return f"{ts} success check due {e.get('due') or 'no date'}: {e.get('number')} read in {e.get('read')}, target {e.get('target')}"
    if k == "wait":
        return f"{ts} waiting {e.get('minutes')} min: {e.get('reason')} {e.get('reset_hint') or ''}"
    return None


def cmd_watch(a):
    j = Journal(a.ref)
    after = int(a.after or 0)
    t0 = time.time()
    while True:
        evs = j.events()
        new = evs[after:]
        lines = [l for l in (_line(e) for e in new) if l]
        at, last_phase = next(((n, e.get("phase")) for n, e in reversed(list(enumerate(evs))) if e.get("kind") == "phase"),
                              (0, None))
        # a run stands between steps: its standing phase ends a watch once, past the cursor
        final = last_phase if (last_phase in FINAL or last_phase == STANDING) and (last_phase != STANDING or at >= after) else None
        if lines or final or (time.time() - t0) > a.max_min * 60:
            for l in lines:
                print(l)
            out({"cursor": len(evs), "run_id": a.ref, "final": final, "lines": len(lines),
                 "phase": j.fact("phase")}, 0)
        time.sleep(POLL_S)
