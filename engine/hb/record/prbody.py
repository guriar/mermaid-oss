"""hb.run.prbody - the PR body the owner reads.

The headline, the three finish states, the first screen (hb.run.firstscreen) with its row
lists folded to counts, and a synopsis that links the record (hb.run.record) by commit sha.
Everything an agent or a retro needs, every item, gate, cause, decision and test, is in the
record, so this body is bounded by construction and never trimmed.
"""
import json
from hb.record.journal import MERGEABLE
import re
from pathlib import Path

from hb.contract import battery
from hb.record import firstscreen
from hb.record import success
from hb.venue import adapter
from hb.runner import usage




def _answers_lines(answers):
    """The owner's answers from the control file: which seat read each first, or that
    it arrived after every seat and was not used."""
    if not answers:
        return []
    lines = ["### Answers from the owner", ""]
    for a in answers:
        where = f"reached the {a['reached']} seat" if a.get("reached") else "arrived after every seat; not used in this run"
        lines.append(f"- {a['text']} ({where})")
    return lines + [""]


ENGINE_COMMITTERS = ("hb", "hb seat")


def session_commits(wt, base_sha):
    """Commits on the branch's own line that neither a seat nor the runner made: the session's
    hand work, the number Q9 wants at zero. None when the branch cannot be read."""
    if not wt or not base_sha:
        return None
    from hb.rig.gitops import git
    rc, o, _ = git(wt, "log", "--first-parent", "--no-merges", "--format=%cn", f"{base_sha}..HEAD")
    if rc != 0:
        return None
    return sum(1 for name in o.splitlines() if name.strip() and name.strip() not in ENGINE_COMMITTERS)


def journal_allowance(j, wt=None):
    """What the run cost and who touched it, from the journal (Q9): seat minutes by role, the
    helpers' minutes, the skills each role invoked, the fullest context any one seat ran with,
    the audit's disagreements with the checks, the owner's touches, battery spend, wall time."""
    evs = j.events()
    s = usage.spent(evs)
    audit = next((e for e in reversed(evs) if e.get("kind") == "audit-disagreements"), None)
    spends = [e for e in evs if e.get("kind") == "battery-spend"]
    opened = sum(1 for e in evs if e.get("kind") == "phase" and e.get("phase") == "opened")
    human = {"answers": sum(1 for e in evs if e.get("kind") == "control" and e.get("verb") == "answer"),
             "resumes": max(0, opened - 1),
             "session_commits": session_commits(wt or j.fact("worktree"), j.fact("base_sha"))}
    return {"minutes": round(s["minutes"]), "usd": round(s["usd"]), "wall_min": wall_minutes(evs), "by_role": s["by_role"],
            "peak_context_tokens": s["peak_context_tokens"], "helper_min": s["helpers"]["minutes"], "skills": s["skills"],
            "audit": audit,
            "battery_usd": round(sum(float(e.get("usd") or 0) for e in spends), 2) if spends else None,
            "human": human}


def numbers_lines(al):
    """The run in numbers, the order Q9 ruled: seat hours by role, helper hours, skills, peak
    context, false greens, human touches, battery spend, wall clock. A number nobody measured
    reads unmeasured, never zero."""
    al = al or {}
    by_role = al.get("by_role") or {}
    roles = list(usage.SEAT_ROLES) + sorted(r for r in by_role if r not in usage.SEAT_ROLES)
    hours = lambda m: f"{(m or 0) / 60:.1f} h"
    skills = al.get("skills") or {}
    audit, human = al.get("audit"), al.get("human") or {}
    commits = human.get("session_commits")
    rows = ["Seat hours: " + ", ".join(f"{r} {hours(by_role.get(r))}" for r in roles),
            f"Helper hours: {hours(al.get('helper_min'))}",
            "Skills invoked: " + ("; ".join(f"{r}: {', '.join(skills[r])}" for r in roles if skills.get(r))
                                  or "none recorded"),
            "Peak context: " + (f"{al['peak_context_tokens'] // 1000}k tokens" if al.get("peak_context_tokens") else "unmeasured"),
            (f"False greens: {audit.get('false_greens')} (check green, audit red); false reds {audit.get('false_reds')}; "
             f"{audit.get('cases')} cases compared" if audit else "False greens: unmeasured, no audit compared the checks"),
            f"Human: {human.get('answers', 0)} answers, {human.get('resumes', 0)} resumes, "
            + (f"{commits} session commits" if commits is not None else "session commits unmeasured"),
            "Battery spend: " + (f"${al['battery_usd']}" if al.get("battery_usd") is not None else "unmeasured"),
            "Wall clock: " + (hours(al["wall_min"]) if al.get("wall_min") is not None else "unmeasured")]
    if al.get("minutes") is not None:
        rows.append(f"{al['minutes']} minutes of agent work, about ${al.get('usd')} at list price, "
                    "nothing billed beyond the subscription")
    return ["## Time and cost", ""] + [f"- {x}" for x in rows] + [""]


def wall_minutes(evs, now=None):
    """Minutes the run's passes were open: each pass from its `opened` to its last event, so
    the hours a stopped run waited for an answer are not counted as work. A running run's
    last pass is still open: given `now`, it counts to now, since a seat writes no line until it ends."""
    from datetime import datetime, timezone
    stamp = lambda e: datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00"))
    if now is not None and evs:
        evs = list(evs) + [{"ts": datetime.fromtimestamp(now, timezone.utc).isoformat()}]
    total, start, last = 0.0, None, None
    for e in evs:
        if not e.get("ts"):
            continue
        if e.get("kind") == "phase" and e.get("phase") == "opened":
            if start is not None:
                total += (stamp(last) - stamp(start)).total_seconds()
            start = e
        last = e
    if start is not None:
        total += (stamp(last) - stamp(start)).total_seconds()
    return round(total / 60)


def seen(receipt, limit=150):
    """What the owner reads beside an item: the first sentence of what the tester saw."""
    return firstscreen.first_sentence(receipt.get("seen") or receipt.get("observed"), limit)


def finding_line(f):
    """A review finding as one line: its id and severity, then the first sentence of what is wrong."""
    what = firstscreen.first_sentence(f.get("title") or f.get("summary") or f.get("rule"), 220)
    head = " ".join(x for x in (f.get("id"), f"({f.get('severity')})" if f.get("severity") else None) if x)
    return f"{head}: {what}" if head else what


def headline(contract, note):
    """The one line the PR title and its first line carry: the changelog line the builder
    wrote for the product's user, else the wave's own line, else the contract's goal."""
    return (str((note or {}).get("changelog") or "").strip() or str(contract.get("headline") or "").strip()
            or str(contract.get("goal") or "").strip())


def goals_met(goals, receipts):
    """(met, drivable, grey): a goal the anchor filed grey is not a miss; it is named."""
    by_item = {r.get("item"): r for r in receipts or []}
    grey = [g for g in goals or [] if g.get("drivable") is False]
    drivable = [g for g in goals or [] if g.get("drivable") is not False]
    met = sum(1 for g in drivable if by_item.get(g["item"], {}).get("state") == "WORKS")
    return met, len(drivable), len(grey)


def render(contract, tip, run_id, gates, review, receipts, note, evidence, finish, record, retired=(),
           preexisting=None, answers=None, allowance=None, goals=None, parked=None, stops=None, wall_min=None,
           issues=None, size=None):
    """The PR body: the headline, the three finish states, the first screen with its row
    lists folded to counts, and a synopsis that links the record by commit. Bounded by
    construction: no line here is written once per item, so no body is ever trimmed."""
    note = note or {}
    receipts = receipts or []
    counts = {s: sum(1 for r in receipts if r.get("state") == s) for s in ("WORKS", "BROKEN", "UNMEASURED", "DIVERGED")}
    # the cost lines are this module's (numbers_lines); the first screen carries none of its own
    fs = firstscreen.build(contract, receipts, note, review, gates, preexisting=preexisting, answers=answers,
                           goals=goals, parked=parked, stops=stops,
                           headline=headline(contract, note), size=size,
                           audit=(allowance or {}).get("audit"), seat_min=(allowance or {}).get("minutes"))
    try:
        (Path(evidence) / "first-screen.json").write_text(json.dumps(fs, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    link = record.get("url") or "not published: " + str(record.get("error") or "no repo")
    lines = [headline(contract, note), "", finish_line(finish, record), ""]
    lines += firstscreen.markdown(fs, record={"link": link})
    lines += numbers_lines(dict(allowance or {}, **({"wall_min": wall_min} if wall_min is not None else {})))
    missing = [t["path"] for t in retired or [] if not t.get("proven")]
    if goals:
        met, total, grey = goals_met(goals, receipts)
        lines += [f"GOALS MET {met}/{total}" + (f" ({grey} not testable here)" if grey else ""), ""]
    for title, rows in battery.report_lines(receipts):
        lines += [f"## {title}", ""] + [f"- {x}" for x in rows] + [""]
    if contract.get("success"):
        lines += ["## Success (read after the merge)", ""] + [f"- {x}" for x in success.lines(contract["success"])] + [""]
    # at most six lines, one per capability: bounded like the rest of the body
    skipped = adapter.not_offered_lines()
    if skipped:
        lines += ["## Not offered by the venue", ""] + [f"- {x}" for x in skipped] + [""]
    lines += ["## Record", "",
              f"- {counts['WORKS']} WORKS, {counts['BROKEN']} BROKEN, {counts['UNMEASURED']} UNMEASURED, {counts['DIVERGED']} DIVERGED",
              f"- retired tests: {len(retired or [])}" + (f", {len(missing)} without a replacement shown failing: " + ", ".join(f"`{m}`" for m in missing) if missing else ""),
              f"- gates: {sum(1 for g in gates or [] if g.get('true_exit') == 0)} of {len(gates or [])} green",
              f"- code tip {tip[:9]}, harness {finish.get('harness') or 'unknown'}, run {run_id}",
              f"- the record, at its exact revision: {link}", ""]
    # `Issue: #N`, never `Closes`: a trailer lets GitHub close the issue at merge and race the
    # review gate; the closeout closes it after the merge
    tail = [f"Issue: #{str(n).lstrip('#')}" for n in issues or []] + ([""] if issues else [])
    return "\n".join(lines + tail)


def finish_line(finish, record):
    """The first line after the headline: which of the three states the run is in, and why
    it is not further. A red record is never written in the words of a finished one."""
    rec = f"record published at {record['commit'][:9]}" if record.get("commit") else "record not published"
    merge = MERGEABLE if finish.get("merge") == "eligible" else "not mergeable: " + "; ".join(finish.get("why") or ["unknown"])
    return f"**{finish.get('state')}.** {rec}; run {finish.get('run')}; {merge}."


def title(beat, head, state, width=72):
    """The PR title as a pure function of the record: `<beat> [<finish state>]: <headline>`,
    cut at a clause under the width and never mid-word. A beat the headline already opens
    with is not repeated (an anchor titled "wave: x" once became "wave: wave: x").
    Recomputed on every refresh, so a repair that moved the state moves the word."""
    h = re.sub(r"\s+", " ", str(head or "")).strip()
    h = re.sub(rf"^{re.escape(beat)}\s*[:,]\s*", "", h, flags=re.I)
    h = re.sub(r"\s*\((?:anchor|substrate)\)\s*$", "", h, flags=re.I)
    prefix = f"{beat} [{state}]: "
    room = width - len(prefix)
    if len(h) > room:
        cut = h[:room]
        clause = max(cut.rfind(", "), cut.rfind("; "), cut.rfind(": "))
        h = cut[:clause] if clause >= room // 2 else (cut[:cut.rfind(" ")] if " " in cut else cut)
    return prefix + h.rstrip(" ,;:.")
