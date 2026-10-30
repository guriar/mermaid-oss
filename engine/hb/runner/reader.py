"""hb.run.reader - the second reader.

A reader from another model family on the same diff beside the reviewer, on the post-build
and composed reads. One read at a time on this host. When Codex cannot read, a fresh Claude
reader takes the same brief at once; the run never waits on a plan's reset (Q5).
"""

import json
import time
from pathlib import Path

from hb.venue import machine
from hb.rig.common import cfg
from hb.contract import policy
from hb.rig import gitops
from hb.runner import seating


INSTRUCTION = """You are a second reader on one diff, from another model family than the reviewer.
Read the diff with `git diff {base}..{tip}` (`git diff --stat {base}..{tip}` lists its files), and
open a file only to confirm a suspicion the diff raised. Do not run tests.
List a wrong behaviour, a broken invariant, a missing test, or a ruling the change contradicts.
One claim per line as `file:line: <claim>`. No praise, no style notes, no summary."""

NO_SECOND_READER = "none: no second reader on this run (a hotfix, or a repair of an earlier run)"
LATE_CHECK = ("this is not a new review. The second reader read the whole change beside the review; "
              "FINDINGS_IN is its list. Put each claim through the quote gate: one you can quote at this tip "
              "is a finding at the severity it earns, with an id starting SR-; refute the rest. Raise nothing "
              "of your own")


def wanted(a):
    """The second reader reads every slice, alone or inside a wave, and a wave's whole diff:
    a finding inside one slice is cheapest before the merge. A hotfix asks for none; its diff
    is too small to earn the minutes. A capped read costs about 60k tokens."""
    return getattr(a, "kind", "") in ("wave", "slice")


def _first_error(launch):
    """The first non-empty line the failed read printed to stderr, else its exit class."""
    try:
        text = Path(launch.get("stderr") or "").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return next((l.strip() for l in text.splitlines() if l.strip()), launch.get("exit_class") or "no output")[:200]


def _claims_of_claude(sd):
    """A Claude reader's list is the `result` of its stream; written out so the reviewer reads
    the claims and not the stream."""
    text = ""
    for line in (sd / "stdout.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") == "result" and isinstance(o.get("result"), str):
            text = o["result"]
    (sd / "claims.txt").write_text(text, encoding="utf-8")
    return sd / "claims.txt"


def _read_once(j, runtime, model, sd, wt, attempt, prompt, tip, key):
    """One launch of the reader on `runtime`, journaled as the codex seat. Returns the launch."""
    from hb.runner import adapters
    effort = adapters.seat_effort("codex" if runtime == "codex" else "review")
    j.append("seat", seat="codex", attempt=attempt, state="started", runtime=runtime, model=model,
             brief=str(sd / "prompt.md"), prompt_bytes=len(prompt.encode("utf-8")))
    t0 = time.time()
    launch = adapters.launch(runtime, "codex", sd / "prompt.md", wt, sd, model=model,
                             timeout_s=policy.CODEX_READ_MIN * 60, effort=effort)
    state = launch["exit_class"]
    usage = launch.get("usage") or {}
    j.append("seat", seat="codex", attempt=attempt, state=state, minutes=round((time.time() - t0) / 60, 1),
             runtime=runtime, model=model, effort=effort, usage=launch.get("usage"), tokens=usage.get("total_tokens"),
             **({"key": key, "pass": seating._PASS, "tip_in": tip, "tip_out": gitops.head_of(wt)}
                if state == "ok" else {}))
    return launch


def codex_read(j, wt, base_sha, contract, evidence, timeout_min):
    """A second reader from another model family on the same diff beside the reviewer. Its
    claims reach the findings only through the reviewer's quote gate (roles/reviewer.md).
    One read at a time on this host. When Codex fails, out of usage or any other exit, a fresh
    Claude reader takes the same brief at once on the review seat's model: never a wait (Q5).
    `seats.codex.runtime: none` switches the seat off (drills)."""
    from hb.runner import adapters
    attempt = j.next_attempt("codex")
    sd = j.seat_dir("codex", attempt)
    spec = (cfg().get("seats") or {}).get("codex") or {}
    first = spec.get("runtime") or "codex"
    if first == "none":
        j.append("seat", seat="codex", attempt=attempt, state="missing", why="disabled by config")
        return {"fact": "none: second reader stand-in (disabled by config)", "path": None}
    tip = gitops.head_of(wt)
    prompt = "\n\n".join([INSTRUCTION.format(base=base_sha[:12], tip=tip[:12]),
                           "## The slice issue, verbatim\n\n" + str(contract.get("body") or ""),
                           "## Rulings, verbatim\n\n" + str(contract.get("rulings") or ""),
                           "## Contract items\n\n```json\n" + json.dumps(contract.get("items") or [], indent=1) + "\n```"])
    key = seating.asked(prompt)
    ev = seating.kept(j, "codex", key, wt, artifact="stdout.txt", exact=True)
    if ev:
        read = j.dir / "seats" / f"codex-{ev.get('attempt')}" / "stdout.txt"
        j.append("seat-kept", seat="codex", of=read.parent.name, tip=str(ev.get("tip_out"))[:9])
        return {"fact": f"read this file raw: {read}", "path": str(read), "runtime": ev.get("runtime")}
    (sd / "prompt.md").write_text(prompt, encoding="utf-8")
    ok, why = adapters.probe(first)
    if not ok:
        j.append("seat", seat="codex", attempt=attempt, state="missing", runtime=first, why=why)
        return {"fact": f"none: no second reader this run ({first}: {why})", "path": None}
    model = spec.get("model") or adapters.DEFAULT_MODELS.get(first, "")
    with machine.slot("codex", f"{j.run_id} second reader", note=j.append):
        launch = _read_once(j, first, model, sd, wt, attempt, prompt, tip, key)
        if launch["exit_class"] == "ok":
            return {"fact": f"read this file raw: {sd / 'stdout.txt'}", "path": str(sd / "stdout.txt"), "runtime": first}
        why = _first_error(launch)
        j.append("second-read", state="fallback", runtime="claude", why=why,
                 words=f"second-read fallback: claude why: {why}")
        _, model = adapters.seat_runtime("review", override="claude")
        attempt = j.next_attempt("codex")
        sd2 = j.seat_dir("codex", attempt)
        (sd2 / "prompt.md").write_text(prompt, encoding="utf-8")
        again = _read_once(j, "claude", model, sd2, wt, attempt, prompt, tip, key)
    if again["exit_class"] == "ok":
        claims = _claims_of_claude(sd2)
        return {"fact": f"read this file raw: {claims}", "path": str(claims), "runtime": "claude"}
    return {"fact": f"none: no second reader this run ({first}: {launch['exit_class']}, "
                    f"claude: {again['exit_class']})", "path": None}


def gate(j, wt, base_sha, contract, codex_in, seat_kw):
    """A late read's claims through the reviewer's quote gate, as one closing check. Returns
    the findings it adopted, which the run's findings ledger now holds like any other."""
    facts = seating.facts_common(contract, {"Closing check": LATE_CHECK,
                                            "FINDINGS_IN": codex_in["fact"], "TOUCHED": gitops.changed_paths(wt, base_sha),
                                            "Diff": f"git diff {base_sha}..HEAD in your working directory"})
    res = seating.sit(j, "review", facts, wt, base_sha, contract["issue"], **seat_kw)
    found = [f for f in ((res.get("report") or {}).get("findings") or []) if f.get("status") != "fixed"] \
        if res["result"] == "ok" else []
    j.append("second-read", state="gated", result=res["result"], adopted=[f.get("id") for f in found])
    return found


