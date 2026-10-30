"""hb.run.standing - a slice or hotfix run that stays up after its PR.

Once the PR is opened or refreshed, the run stands: the venue down, no seat, no admission
grant, no browser. It polls control.txt every `standing.control_poll_s` (20) and the PR's
state every `standing.pr_poll_s` (60), and moves on a `next:` line the chat writes, never the
page. A step is the run's own order again: the builder relaunched with the owner's words
(an answer every later seat reads too), then the loop on the branch from where it stood, then
the same landing. `cut` ends the run at its verdict; a merged PR ends it at phase `merged`.
Each step is journaled `next` when taken and `next-done` when it ends; a resume re-queues one
taken and never ended.
"""
import json
import time
from pathlib import Path

from hb.rig.common import gh, out
from hb.rig.versions import hb_invocation
from hb.record import control
from hb.runner import fixing
from hb.rig import gitops
from hb.beats import loop

STATE = "standing.json"
KEEP = ("beat", "ref", "worktree", "branch", "base_sha", "slug", "note", "before", "receipts", "gates",
        "preexisting", "pr", "verdict", "walked")


def wanted(a, res):
    """A run stands when it opened or refreshed a PR of its own: never in a wave, a shadow or a stop."""
    return bool(res.get("pr")) and not getattr(a, "wave", None) and not a.shadow and not res.get("stops")


def context(beat, ref, wt, branch, base_sha, slug, note, before, st, res):
    return {"beat": beat, "ref": ref, "worktree": str(wt), "branch": branch, "base_sha": base_sha, "slug": slug,
            "note": note, "before": before or {}, "receipts": st["receipts"], "gates": st["gates"],
            "preexisting": st["preexisting"], "pr": res.get("pr"), "verdict": res.get("verdict"), "walked": res.get("tip")}


def saved(j):
    try:
        return json.loads((j.dir / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def after_ship(j, a, c, contract, ctx, land, res, code, seat_kw):
    """The door's last word: exit as before, or print the ship verdict and stand."""
    if not wanted(a, res):
        return out(res, code)
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return stand(j, a, c, contract, ctx, land, res, seat_kw)


def resume(j, a, c, contract, land, seat_kw):
    """`--resume` on a run that stood: back to standing, or the merged verdict it ended at."""
    ctx = saved(j)
    res = {"verdict": ctx["verdict"], "run_id": j.run_id, "branch": ctx["branch"], "pr": ctx["pr"], "tip": ctx["walked"]}
    if j.fact("phase") == "merged":
        return out(dict(res, phase="merged"), 0)
    return stand(j, a, c, contract, ctx, land, res, seat_kw)


def pr_state(url):
    rc, o, _ = gh(["pr", "view", str(url), "--json", "state"], timeout=60)
    try:
        return (json.loads(o or "{}") or {}).get("state") if rc == 0 else None
    except ValueError:
        return None


def _requeue(j):
    """A step taken and never ended (the process died inside it) is put back first in line."""
    evs = j.events()
    last = next((e for e in reversed(evs) if e.get("kind") == "next" and e.get("text")), None)
    if last and not any(e.get("kind") == "next-done" and e.get("n") == last.get("n") for e in evs):
        control.put_next(j.dir, last["text"], first=True)
        j.append("next-requeued", n=last.get("n"))


def stand(j, a, c, contract, ctx, land, res, seat_kw):
    conf = c.get("standing") or {}
    every, pr_every = float(conf.get("control_poll_s") or 20), float(conf.get("pr_poll_s") or 60)
    _requeue(j)
    n = sum(1 for e in j.events() if e.get("kind") == "next" and e.get("text"))
    while True:
        (j.dir / STATE).write_text(json.dumps({k: ctx.get(k) for k in KEEP}, indent=2, ensure_ascii=False), encoding="utf-8")
        j.phase("standing", pr=ctx["pr"], verdict=ctx["verdict"], tip=ctx["walked"])
        polled, lines = 0.0, []
        while not lines:
            if control.cut_requested(j.dir):
                j.append("control", verb="cut", during="standing")
                j.phase(ctx["verdict"], left="cut", pr=ctx["pr"], tip=ctx["walked"])
                return out(dict(res, left="cut"), 0)
            lines = control.take_all_next(j.dir)
            if not lines and time.time() - polled >= pr_every:
                polled = time.time()
                state = pr_state(ctx["pr"])
                if state == "MERGED":
                    j.phase("merged", pr=ctx["pr"], tip=ctx["walked"])
                    return out(dict(res, phase="merged",
                                    closeout=f"{hb_invocation()} closeout {j.run_id}"), 0)
                if state == "CLOSED":
                    j.phase(ctx["verdict"], left="closed", pr=ctx["pr"], tip=ctx["walked"])
                    return out(dict(res, left="closed"), 0)
            if not lines:
                time.sleep(every)
        n += 1
        text = "\n".join(lines)
        j.append("next", text=text, n=n)
        done = step(j, a, c, contract, ctx, land, seat_kw, n, text)
        if done.get("res"):
            res = done.pop("res")
            ctx.update(verdict=res.get("verdict"), walked=res.get("tip"), pr=res.get("pr") or ctx["pr"])
        j.append("next-done", n=n, tip=gitops.head_of(ctx["worktree"]), **done)


def step(j, a, c, contract, ctx, land, seat_kw, n, text):
    """The owner's words to the builder relaunched, then the loop on the branch from where
    the run stood, then the landing."""
    wt, branch, base_sha = Path(ctx["worktree"]), ctx["branch"], ctx["base_sha"]
    with open(control.path(j.dir), "a", encoding="utf-8") as f:
        f.write(f"answer: {text}\n")
    fix = fixing.fix_pass(j, contract, ctx["note"], wt, base_sha, seat_kw, extra={"Next step from the chat": text})
    j.append("fix-round", stage=f"next-{n}", result=fix["result"])
    if fix["result"] != "ok":
        return {"result": f"build seat: {fix['result']}"}
    gitops.push(j, wt, branch, a.shadow, f"push-next-{n}")
    findings, review = loop.open_findings(j)
    stops = []
    st = loop.run_loop(j, a, c, contract, wt, branch, base_sha, ctx["slug"], ctx["note"], j.dir / "evidence", stops,
                       seat_kw, before=ctx["before"], second_reader=False,
                       prior={"receipts": ctx["receipts"], "findings": findings, "review": review, "asked": [],
                              "tip": ctx["walked"]})
    res = land(dict(st, down_key=f"venue-down-next-{n}"), ctx["note"], stops)
    ctx.update(receipts=st["receipts"], gates=st["gates"], preexisting=st["preexisting"])
    return {"result": res.get("verdict"), "res": res, "pr": res.get("pr"), "stops": stops,
            "receipts": {s: sum(1 for r in st["receipts"] if r.get("state") == s) for s in ("WORKS", "BROKEN", "UNMEASURED")}}


def cmd_next(a):
    """`hb run next <run id> <words>`: the owner's next step for a standing run. A run whose
    process is gone reads no line, so it is refused with the line that re-enters it."""
    from hb.runner import liveness
    from hb.record.journal import Journal
    j = Journal(a.ref)
    if not j.path.exists():
        out({"verdict": "refused", "why": f"no run {a.ref}"}, 2)
    text = " ".join(getattr(a, "words", None) or []).strip()
    if not text:
        out({"verdict": "refused", "why": "no words: `hb run next <run id> <what the owner asks for>`"}, 2)
    ph = j.read_phase()
    ref = ph.get("ref") or (saved(j) or {}).get("ref") or "?"
    hb = f"{hb_invocation()} run"
    if ph.get("phase") == "merged" or not liveness.alive(j.dir):
        pid = (liveness.read(j.dir) or {}).get("pid")
        why = "its PR merged" if ph.get("phase") == "merged" else f"its process (pid {pid}) is not alive"
        cmds = {"resume": f"{hb} {ph.get('beat') or 'slice'} \"{ref}\" --resume {a.ref} --detach"}
        out({"verdict": "refused", "why": f"run {a.ref} takes no next step: {why}", **cmds}, 2)
    control.put_next(j.dir, text)
    out({"verdict": "queued", "run_id": a.ref, "line": f"next: {text}", "phase": ph.get("phase"),
         "watch": f"{hb} watch {a.ref} --after {len(j.events())}"}, 0)
