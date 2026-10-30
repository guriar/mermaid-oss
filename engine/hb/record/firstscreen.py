"""hb.run.firstscreen - what the owner reads first, as data.

One reading of a finished run in the order the owner ruled: the verdict, what this is,
what needs a ruling, what was decided without asking, what does not work (broken apart
from could-not-be-tested), how to try it, and what it cost. The PR body and the run page
both render this and nothing else above the fold, so the two never disagree.
"""
import re

from hb.contract import policy
from hb.record.journal import MERGEABLE, RUN_COMPLETE, STOPPED
from hb.runner.stops import parked_id

READY, KNOWN_REDS, NOT_READY = "Ready", "Known reds", "Not ready"


def first_sentence(text, limit=220):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    s = m.group(1) if m and len(m.group(1)) >= 20 else t
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def _then(item_id, items):
    it = next((x for x in items or [] if x.get("item") == item_id), None)
    return first_sentence((it or {}).get("then") or item_id, 110)


def verdict(receipts, items, goals=(), review=None, gates=(), preexisting=(), stops=()):
    """Ready when every item works and nothing waits on the owner. Not ready when a goal
    row is red, the review says the goal was missed, or a stop is open. Anything else red is
    Known reds: mergeable, each red named."""
    from hb.gates import gating
    by_item = {r.get("item"): r for r in receipts or []}
    goal_red = [g["item"] for g in goals or [] if g.get("drivable") is not False
                and by_item.get(g["item"], {}).get("state") != "WORKS"]
    missed = bool(review) and not ((review.get("goal_verdict") or {}).get("met", True))
    if stops or goal_red or missed:
        why = ("a stop is waiting on you" if stops else "the review says the goal was missed" if missed
               else f"goal red: {', '.join(goal_red)}")
        return {"word": NOT_READY, "why": why}
    red = policy.blocking_red(receipts, items)
    unmeasured_gates = gating.unmeasured_gate_names(gates)
    if red or unmeasured_gates or gating.gates_red(gates, preexisting):
        n = len(red)
        return {"word": KNOWN_REDS, "why": f"{n} item{'s' if n != 1 else ''} red" if n else "a check measured nothing"}
    return {"word": READY, "why": ""}


def score(receipts, items, review=None, gates=(), preexisting=(), stops=(), record="published", changed=True, goals=()):
    """Merge readiness as five facts from receipts, never a seat's estimate (ruling 18): the goal met
    with nothing P1 open, every gate green at the tip, every measured row green, nothing waiting on
    the owner, the record published. {n, facts: [{fact, ok, receipt}]}; the loop fixes only a false fact."""
    from hb.gates import gating
    receipts, items, gates, stops = list(receipts or []), list(items or []), list(gates or []), list(stops or [])
    preexisting, goals = list(preexisting or []), list(goals or [])
    gv = (review or {}).get("goal_verdict") if review else None
    open_p1 = [f.get("id") for f in (review or {}).get("findings") or []
               if f.get("status") != "fixed" and str(f.get("severity") or "P1").upper() == "P1"]
    riding = [f.get("id") for f in (review or {}).get("findings") or []
              if f.get("status") != "fixed" and str(f.get("severity") or "P1").upper() != "P1"]
    by_item = {r.get("item"): r for r in receipts or []}
    goal_red = [g["item"] for g in goals or [] if g.get("drivable") is not False
                and by_item.get(g["item"], {}).get("state") != "WORKS"]
    missed = bool(review) and not ((gv or {}).get("met", True))
    facts = []
    goal_ok = not missed and not open_p1 and not goal_red
    facts.append({"fact": "goal met, nothing P1 open", "ok": goal_ok,
                  "receipt": ("the review says the goal was missed" if missed else
                              f"P1 open: {', '.join(map(str, open_p1))}" if open_p1 else
                              f"goal red: {', '.join(goal_red)}" if goal_red else
                              "the review says the goal is met" if review else "no review read the change")
                  + (f"; riding for the owner: {', '.join(map(str, riding))}" if riding else "")})
    red_gates = gating.gates_red(gates, preexisting)
    unmeasured = gating.unmeasured_gate_names(gates)
    facts.append({"fact": "every gate green at the tip", "ok": not red_gates and not unmeasured,
                  "receipt": (f"red: {', '.join(g.get('name', '?') for g in red_gates)}" if red_gates else
                              f"measured nothing: {', '.join(unmeasured)}" if unmeasured else
                              f"{len(gates or [])} gates green")})
    red = policy.blocking_red(receipts, items)
    facts.append({"fact": "every measured row green", "ok": not red,
                  "receipt": f"{len(red)} item{'s' if len(red) != 1 else ''} red" if red else
                  f"{sum(1 for r in receipts or [] if r.get('state') == 'WORKS')} of {len(receipts or [])} rows WORKS"})
    facts.append({"fact": "nothing waiting on the owner", "ok": not stops,
                  "receipt": f"{len(stops)} stop{'s' if len(stops) != 1 else ''} open" if stops else "no stop open"})
    facts.append({"fact": "the record published", "ok": record == "published" and changed,
                  "receipt": "record not published" if record != "published" else
                  "nothing to merge: the branch carries no change" if not changed else "published"})
    return {"n": sum(1 for f in facts if f["ok"]), "of": len(facts), "facts": facts}


def audit_lines(audit, seat_min=None):
    """The honesty score (Q9): every case the blind audit and the builder's checks disagreed on,
    the cases the audit drove and the checked cases it never reached, read off the journal's
    audit-disagreements event. No audit reads unmeasured, never zero."""
    lines = ["## Audit disagreements", ""]
    if not audit:
        return lines + ["- no audit: unmeasured", ""]
    disagreed = audit.get("disagreed") or {}
    for kind, label in (("false_greens", "False greens (check WORKS, audit BROKEN)"),
                        ("false_reds", "False reds (check BROKEN, audit WORKS)")):
        rows = disagreed.get(kind) or []
        lines.append(f"- {label}: {len(rows)}")
        lines += [f"  - {r.get('case')} ({r.get('rule')}): the check said `{r.get('check')}`; "
                  f"the audit saw: {first_sentence(r.get('audit'), 200)}" for r in rows]
    excluded = audit.get("excluded")
    lines.append(f"- Cases the audit drove: {audit.get('cases')}")
    lines.append("- Checked cases the audit did not reach (UNMEASURED): "
                 + (", ".join(excluded) if excluded else "none" if excluded is not None else "unmeasured"))
    if seat_min is not None:
        lines.append(f"- Seat hours, every role: {seat_min / 60:.1f} h")
    return lines + [""]


ID_RE = re.compile(r"\b([A-Z]{1,4}-\d+|[A-Z]{1,4}-R\d+|dk-\d+)\b")


def owed_questions(questions, answers):
    """(owed, answered): a question whose item or ruling ids an answer also names is
    answered by it; the owner is asked only what no answer reached."""
    owed, answered = [], []
    answer_ids = [set(ID_RE.findall(a.get("text") or "")) for a in answers or []]
    for q in questions or []:
        ids = set(ID_RE.findall(q.get("text") or ""))
        if ids and any(ids & aids for aids in answer_ids):
            answered.append(q)
        else:
            owed.append(q)
    return owed, answered


def yours_to_rule(stops, parked=()):
    """What only the owner can answer, one line each: every open stop and every slice a wave
    parked."""
    from hb.runner import stops as stop_ids
    lines = [x[2:] if x.startswith("- ") else x for x in stop_ids.open_lines(stops)]
    for p in parked or []:
        sid = parked_id(p.get("issue"))
        lines.append(f"[{sid}] Slice #{p.get('issue')} is not in this PR: {first_sentence(p.get('why'), 160)} "
                     f"`answer: {sid}: <ruling>` resumes it.")
    return lines


def decided_for_you(note, answers):
    """The questions a seat kept going on and decided itself, which no answer of the owner's
    has reached yet, each with the call it made: the owner's to overrule by an answer."""
    questions = [na for na in (note or {}).get("needs_authority") or [] if na.get("kind") == "question"]
    owed, _answered = owed_questions(questions, answers)
    return [first_sentence(q.get("text"), 260) + (f" Went with: {q.get('recommendation') or q.get('recommended')}"
                                                  if q.get("recommendation") or q.get("recommended") else "") for q in owed]


def build(contract, receipts, note, review, gates, preexisting=(), answers=(), allowance=None, goals=(),
          parked=(), stops=(), wall_min=None, headline="", size=None, audit=None, seat_min=None):
    """The first screen as one dict. Every list holds plain sentences ready to print."""
    note = note or {}
    receipts = receipts or []
    items = list(contract.get("items") or []) + list(goals or [])
    works = sum(1 for r in receipts if r.get("state") == "WORKS")
    v = verdict(receipts, contract.get("items"), goals, review, gates, preexisting, stops)
    ruling = yours_to_rule(stops, parked)
    decided = decided_for_you(note, answers)
    broken = [f"{_then(r.get('item'), items)}: {first_sentence(r.get('seen') or r.get('observed'), 160)}"
              for r in receipts if r.get("state") in ("BROKEN", "DIVERGED")]
    untested = [f"{_then(r.get('item'), items)}: {first_sentence(r.get('seen') or r.get('observed'), 160)}"
                for r in receipts if r.get("state") == "UNMEASURED"]
    untested += [f"{p.get('gate')}: also red without this change" for p in preexisting or []]
    tries = []
    for r in receipts:
        it = next((x for x in items if x.get("item") == r.get("item")), None)
        if r.get("state") == "WORKS" and it and it.get("proof") != "test" and (it.get("kind") or "") not in ("measure", "api", "goal"):
            when = re.sub(r"\s+", " ", it.get("when") or "").strip().rstrip(",")
            if when:
                tries.append(when[0].upper() + when[1:])
        if len(tries) == 3:
            break
    sc = score(receipts, items, review, gates, preexisting, stops, "published", True, goals)
    return {"verdict": v["word"], "why": v["why"], "headline": headline, "score": sc,
            "summary": f"{works} of {len(receipts)} items work" if receipts else "Nothing was measured on a stand-in",
            "for_user": [str(x) for x in note.get("for_user") or []] or ([headline] if headline else []),
            "underneath": [str(x) for x in note.get("underneath") or []]
                          or [f"{f.get('path')}: {f.get('owns')}" for f in (note.get("files") or [])[:5]],
            "studio": contract.get("studio"),
            "ruling": ruling, "size": list((size or {}).get("reasons") or []), "decided": decided, "broken": broken, "untested": untested, "try": tries,
            "audit": audit, "seat_min": seat_min,
            "cost": {"agent_min": (allowance or {}).get("minutes"), "list_usd": (allowance or {}).get("usd"),
                     "wall_min": wall_min, "answers": len(answers or []),
                     "by_role": (allowance or {}).get("by_role") or {},
                     "peak_context_tokens": (allowance or {}).get("peak_context_tokens")}}


def markdown(fs, record=None):
    """The first screen as the top of a PR body. With `record` ({link}) the broken and
    untested rows are counted and the rows live in the record, so the body's size does not
    grow with the item count."""
    def block(title, rows, empty="Nothing."):
        return [f"## {title}", ""] + ([f"- {x}" for x in rows] if rows else [empty]) + [""]
    lines = [f"**{fs['verdict']}.** {fs['summary']}." + (f" {fs['why'][0].upper() + fs['why'][1:]}." if fs.get("why") else ""), ""]
    sc = fs.get("score")
    if sc:
        lines += [f"Merge readiness {sc['n']}/{sc['of']}", ""]
        lines += [f"- {'yes' if f['ok'] else 'no'}: {f['fact']} ({f['receipt']})" for f in sc["facts"]] + [""]
    lines += ["## What this is", ""]
    lines += ["For the user:", ""] + [f"- {x}" for x in fs["for_user"]] + [""]
    if fs["underneath"]:
        lines += ["Underneath:", ""] + [f"- {x}" for x in fs["underneath"]] + [""]
    if fs.get("studio"):
        lines += [f"Starts from the frozen studio screens: `{fs['studio'].get('branch')}` at "
                  f"`{fs['studio'].get('commit')}`.", ""]
    lines += block("Needs your ruling", fs["ruling"])
    if fs.get("size"):
        lines += block("Grew past the size limit", fs["size"])
    lines += block("Decided for you", fs["decided"], "Nothing was decided without you.")
    lines += ["## What does not work", ""]
    if record is not None:
        lines += [f"{len(fs['broken'])} broken, {len(fs['untested'])} could not be tested; every row is in the record: {record['link']}", ""]
    else:
        lines += ["Broken:", ""] + ([f"- {x}" for x in fs["broken"]] or ["- Nothing."]) + [""]
        lines += ["Could not be tested:", ""] + ([f"- {x}" for x in fs["untested"]] or ["- Nothing."]) + [""]
    if fs["try"]:
        lines += block("Try it", fs["try"])
    lines += audit_lines(fs.get("audit"), fs.get("seat_min"))
    c = fs["cost"]
    cost = [f"{c['agent_min']} minutes of agent work, about ${c['list_usd']} at list price, nothing billed beyond the subscription"] \
        if c.get("agent_min") is not None else []
    if c.get("wall_min") is not None:
        cost.append(f"{c['wall_min']} minutes start to finish")
    roles = {"build": "building", "fix": "fixing", "review": "reviewing", "verify": "testing", "codex": "second reader",
             "merge": "merging"}
    by_role = {k: v for k, v in (c.get("by_role") or {}).items() if v}
    if by_role:
        cost.append("Seat hours: " + ", ".join(f"{roles.get(k, k)} {v / 60:.1f} h" for k, v in sorted(by_role.items(), key=lambda kv: -kv[1])))
    if c.get("peak_context_tokens"):
        cost.append(f"Fullest context any one seat ran with: {c['peak_context_tokens'] // 1000}k tokens")
    if cost:
        lines += block("Time and cost", cost)
    return lines


# ── the three finish states ──────────────────────────────────────────────


def finish_state(stops, receipts, items, record, gates=(), preexisting=(), review=None, goals=(), extra_why=(),
                 loop_end=None, harness=None, changed=True):
    """Finished is three states, never one: the record is published (truthful, may be red),
    the run is complete (how the loop ended), and the PR is mergeable: every blocking row
    green, the gates and CI parity green, nothing waiting on the owner. `state` is the
    furthest reached: stopped, run-complete or mergeable; a red record is never 'finished'."""
    sc = score(receipts, items, review, gates, preexisting, stops, record, changed, goals)  # the one decider of ready
    # a false fact's receipt is the why, in the words the record has always used
    why = [f["receipt"].split("; riding")[0] for f in sc["facts"] if not f["ok"]] + [w for w in extra_why or () if w]
    run = "stopped" if stops else "complete"
    merge = "eligible" if run == "complete" and not why else "not-eligible"
    state = STOPPED if stops else MERGEABLE if merge == "eligible" else RUN_COMPLETE
    return {"state": state, "record": record, "run": run, "how": loop_end, "merge": merge,
            "merge_state": MERGEABLE if merge == "eligible" else "not-mergeable", "why": why, "harness": harness,
            "score": sc}
