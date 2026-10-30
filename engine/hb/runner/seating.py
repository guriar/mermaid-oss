"""hb.run.seating - a seat with the runner's recovery around it.

`seat.run_seat` launches one process and judges its report. This module is what a door
calls: it reads the conductor's control file at the boundary, renders the brief, and
respawns once on an invalid report, a crash or a timeout, waits once on a rate limit, and
never counts a conductor's cut. It also holds the facts every brief opens with.

A seat that ended ok is a kept step. Its journal line carries a key (what it was asked) and
the tip it left; a later pass of the same run that asks the same thing while that tip is
still in the tree's history gets the report back and no process starts. A seat that only
reads the tree (a tester, a reviewer, the second reader) answers for the tip it read,
so it is kept only while the tree is at that very tip. That is the whole of what a resume
keeps: no door asks whether it is resuming.
"""

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from hb.venue import admission
from hb.rig.common import report_text
from hb.runner import adapters
from hb.record import control
from hb.rig import gitops
from hb.runner.briefs import render
from hb.runner.seat import run_seat


RESPAWN_LIMIT = 1

# one process is one pass over a run; a pass is handed only what an earlier pass recorded,
# each record once, so a loop that asks the same thing twice in one pass sits twice
_PASS = uuid.uuid4().hex[:8]
_HANDED = set()
_HANDED_LOCK = threading.Lock()
# where a seat ran, never what it was asked: a stand-in rebuilt at the same tip has new ports
# the venue a seat ran against and the picture rule that rides with it are where it worked,
# not what it was asked: a resume stands a new venue and must still keep the seat (Q11, A16)
_PLACE = ("The venue", "VENUE", "Before pictures")
# the roles that read the tree and leave it where it was: a report of theirs is true of one tip
READS_ONLY = ("verify", "discovery", "review", "codex")
# the seats kept only at the exact tip they left: the readers, and a fix round (the builder
# journaled as `fix`), whose landed work the reducer reconciles from the tip instead
EXACT = READS_ONLY + ("fix",)


def asked(facts):
    """The key of what a seat is asked: its facts without the place it runs in."""
    if isinstance(facts, dict):
        facts = {k: v for k, v in facts.items() if k not in _PLACE}
    text = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def kept(j, label, key, wt, artifact="report.json", exact=False):
    """The journal line of an earlier pass's seat that was asked `key` and whose tip is still
    in the tree's history, or None; `exact` keeps only a seat that left the tree at its tip now.
    Each line is handed out once per pass."""
    head = gitops.head_of(wt) if exact else None
    with _HANDED_LOCK:
        for ev in j.events():
            if not (ev.get("kind") == "seat" and ev.get("seat") == label and ev.get("state") == "ok"
                    and ev.get("key") == key and ev.get("pass") != _PASS):
                continue
            ident = (j.run_id, label, ev.get("attempt"))
            if ident in _HANDED or not gitops.in_history(wt, ev.get("tip_out")) or (exact and ev.get("tip_out") != head):
                continue
            if not (j.dir / "seats" / f"{label}-{ev.get('attempt')}" / artifact).exists():
                continue
            _HANDED.add(ident)
            return ev
    return None


def left_at(res, wt):
    """The tip a seat left the tree at: its record's when the seat was kept, the tree's now."""
    return res.get("tip_out") if res.get("kept") else gitops.head_of(wt)


def sit(j, role, facts, wt, base_sha, issue, runtime, timeout_min, shadow, evidence,
          rate_limit_wait_min, extra_env=None, label=None, tools=None):
    """Run a seat with the runner's recovery: an invalid report is respawned once
    with the problems named; a rate limit waits for the reset once; a crash or a
    timeout is respawned once from the tree as it stands. Returns the seat result.
    `label` journals the seat under its own name (a drive shard, `verify.2`) while it
    runs the role's brief and schema. `tools` is the tool specs a driving seat starts
    (adapters.launch), named in its brief."""
    label = label or role
    key = asked(facts)
    ev = kept(j, label, key, wt, exact=role in EXACT or label.split(".")[0] in EXACT)
    if ev:
        name = f"{label}-{ev.get('attempt')}"
        try:
            report = json.loads(report_text(j.dir / "seats" / name / "report.json"))
        except (OSError, ValueError):
            report = None
        if report is not None:
            j.append("seat-kept", seat=label, of=name, tip=str(ev.get("tip_out"))[:9])
            return {"result": "ok", "report": report, "kept": True, "seat": name,
                    "tip_in": ev.get("tip_in"), "tip_out": ev.get("tip_out")}
    problems_note = None
    waited = False
    respawns = 0
    while True:
        # the conductor's control file is read here, at the boundary, and nowhere else
        if control.pause_requested(j.dir):
            j.append("control", verb="pause", before=role)
            raise control.Paused(j)
        attempt = j.next_attempt(label)
        sd = j.seat_dir(label, attempt)
        f = dict(facts)
        answers = control.note_answers(j, role)
        if answers:
            f["Answers from the owner, arrived during this run"] = answers
        if problems_note:
            f["Your previous report was refused"] = problems_note
        if label != role:
            # a merge seat wrote `seat: merge` off its folder name and was refused once
            f["Your report's `seat` field"] = f"`{role}` (this seat runs as {label})"
        prog = Path(evidence) / ("progress.md" if role == "build" else f"{label}-progress.md")
        brief = render(role, f, {"issue": issue, "run_id": j.run_id, "attempt": attempt,
                                 "base_sha": base_sha, "cwd": str(wt), "evidence_dir": str(evidence),
                                 "progress": str(prog) if role in ("build", "verify") else None,
                                 "output_path": str(sd / "report.json"), "tools": tools})
        (sd / "brief.md").write_text(brief, encoding="utf-8")
        env = {"HB_SEAT_ROLE": role, "HB_SEAT_LABEL": label, "HB_OUTPUT_PATH": str(sd / "report.json"), "HB_RUN_ID": j.run_id,
               "HB_ATTEMPT": str(attempt), "HB_BASE_SHA": base_sha, "HB_ISSUE": issue,
               "GIT_COMMITTER_NAME": gitops.SEAT_COMMITTER}
        env.update(extra_env or {})
        j.append("seat", seat=label, attempt=attempt, state="started", brief=str(sd / "brief.md"))
        t0 = time.time()
        tip_in = gitops.head_of(wt)
        # a seat starts when the host has room for its measured peak, across every run on the host;
        # its grant measures the whole tree, so a verb the seat types takes no grant of its own
        with admission.take(adapters.seat_kind(role), f"{j.run_id} {label}", note=j.append) as grant:
            res = run_seat(role, sd / "brief.md", sd / "report.json", wt, j.run_id, attempt,
                           runtime=runtime, timeout_s=timeout_min * 60, base_sha=base_sha,
                           env={**env, **(grant.get("env") or {})}, tools=tools)

        spent = (res.get("launch") or {}).get("usage")
        helped = helpers_of(res.get("launch"))
        if helped:
            # A17: a helper's tokens and minutes are the builder's, journaled under its seat
            spent = dict(spent or {}, helpers=helped)
            j.append("helpers", seat=label, attempt=attempt, **helped)
        j.append("seat", seat=label, attempt=attempt, state=res["result"], minutes=round((time.time() - t0) / 60, 1),
                 runtime=res.get("runtime"), model=res.get("model"), effort=res.get("effort"),
                 usage=spent, problems=res.get("problems"),
                 why=(res.get("launch") or {}).get("why"),
                 **({"key": key, "pass": _PASS, "tip_in": tip_in, "tip_out": gitops.head_of(wt)}
                    if res["result"] == "ok" else {}))
        res = dict(res, seat=f"{label}-{attempt}", tip_in=tip_in, tip_out=gitops.head_of(wt))
        # a commit someone else made while the seat sat is named, never absorbed as the seat's
        foreign = gitops.foreign_commits(wt, tip_in, res["tip_out"])
        if foreign:
            j.append("tip-moved", was=tip_in, now=res["tip_out"], commits=foreign, inside_seat=label,
                     touched=gitops.changed_paths(wt, tip_in, res["tip_out"]),
                     why="commits a seat did not make landed inside its window; the round's review reads them")
        if res["result"] == "ok":
            if role == "verify" and label == role:
                keep_drive_evidence(evidence, attempt)
            # a question the seat kept going on is surfaced now, not at the PR
            for na in (res.get("report") or {}).get("needs_authority") or []:
                if isinstance(na, dict) and na.get("kind") == "question":
                    j.append("question", seat=label, attempt=attempt, text=na.get("text"), evidence=na.get("evidence"))
            return res
        if res["result"] == "missing":
            return res
        limit = (res.get("launch") or {}).get("usage_limit")
        if res["result"] == "rate-limited" and limit:
            # no respawn and no wait: every seat after this one meets the same limit
            j.append("usage-limit", seat=label, attempt=attempt, text=limit.get("text"), resets=limit.get("resets"),
                     resets_at=limit.get("resets_at"))
            raise control.UsageLimited(j, label, limit)
        if res["result"] == "cut":
            # the conductor's cut is not the seat's failure; relaunch without counting it
            j.append("respawn", seat=label, after="cut", attempt=attempt)
            continue
        if res["result"] == "context" and _moved(res, prog, t0):
            # the runtime's window is a limit of the model, not a plan: the seat is relaunched on
            # its own progress file; one that moved nothing since it started is a counted respawn
            j.append("relaunched", seat=label, attempt=attempt, why="context",
                     tokens=(res.get("launch") or {}).get("context_tokens"))
            continue
        if res["result"] == "rate-limited" and not waited:
            waited = True
            hint = (res.get("launch") or {}).get("reset_hint")
            j.append("wait", seat=label, reason="rate-limited", reset_hint=hint, minutes=rate_limit_wait_min)
            if rate_limit_wait_min > 0:
                time.sleep(rate_limit_wait_min * 60)
            continue
        if respawns >= RESPAWN_LIMIT:
            return res
        respawns += 1
        if res["result"] == "invalid-report":
            problems_note = "\n".join(f"- {p}" for p in res["problems"])
        j.append("respawn", seat=label, after=res["result"], attempt=attempt)


def _moved(res, prog, since):
    """Whether a seat left a commit or a progress line since it started."""
    try:
        wrote = prog.stat().st_mtime >= since
    except OSError:
        wrote = False
    return wrote or res.get("tip_out") != res.get("tip_in")


def helpers_of(launch):
    """The builder's helpers' spend from its stream, or None (hb.run.usage)."""
    from hb.runner.usage import helpers
    return helpers((launch or {}).get("stdout")) if launch else None


def keep_drive_evidence(evidence, attempt):
    """A re-drive rewrites video/ and after/; the films of every earlier drive are kept
    under drives/verify-<n>/ so the relay never loses one."""
    import shutil
    dst = Path(evidence) / "drives" / f"verify-{attempt}"
    for sub in ("video", "after"):
        src = Path(evidence) / sub
        if src.is_dir() and any(src.iterdir()):
            shutil.copytree(src, dst / sub, dirs_exist_ok=True)


def answered_findings(findings):
    """The findings a stop id answer routed to this fixer, each with the answer beside it."""
    rows = [{"id": f.get("id"), "stop": f.get("summary") or f.get("title"), "answer": f.get("answer")}
            for f in findings or [] if f.get("answered")]
    return rows or None


def builder_facts(contract, role_extra=None):
    """A build seat's facts: the common facts (the contract whole, every ruling), the slice
    issue verbatim, the studio line, and what the caller adds (the venue; on a fix round
    FAILING_CHECKS and FINDINGS_FOR_ME). Nothing is filtered or ordered for it; the builder
    plans its own work in its progress file."""
    f = facts_common(contract, role_extra)
    if contract.get("body"):
        f["The slice issue, verbatim"] = contract["body"]
    studio = contract.get("studio")
    if studio:
        f["Studio"] = (f"This branch starts from the frozen studio screens at {studio.get('commit')} "
                       f"({studio.get('branch')}): inherit them, wire data, never redraw")
    return f


def facts_common(contract, role_extra=None):
    f = {"Goal": contract["goal"],
         "Contract items": contract["items"]}
    if contract.get("rulings"):
        f["Rulings, verbatim"] = contract["rulings"]
    if contract.get("states"):
        f["Venue states the rules name, and how each is built"] = contract["states"]
    doors = {it["item"]: it["doors"] for it in contract.get("items") or [] if it.get("doors")}
    if doors:
        f["Venue states each item needs, worn before it is driven"] = doors
    if role_extra:
        f.update(role_extra)
    return f
