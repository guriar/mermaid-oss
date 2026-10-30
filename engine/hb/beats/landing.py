"""hb.run.landing - what a run leaves for the owner, the same for every beat.

`land` is the one lander a slice, a hotfix, a wave and a standing pass call: the beat's own
booking (ledger rows, the changelog), every instrument at the PR tip (gating.pr_tip), the
one record published (hb.run.record), the PR body in the owner's shape that links it, the
walk status (config `landing.status_context`) and the PR, opened once and refreshed after.
"""

import json
from hb.record.journal import RUN_COMPLETE

from hb.rig.common import branch_setting, gh, state_dir
from hb.gates.gates import size_hold
from hb.record import control
from hb.runner import stops as stop_ids
from hb.rig import gitops


def issues_of(contract):
    """The issues a PR names: the numbered issue it was cut from, or a wave's slices; a
    sentence or a folder names none. The closeout closes them after the merge."""
    per = contract.get("per_issue")
    if per:
        return [str(n) for n in per if str(n).isdigit()]
    n = str(contract.get("issue") or "").lstrip("#")
    return [n] if n.isdigit() else []


def post_walk(j, c, tip, finish, shadow):
    """The run's merge fact on the commit it drove, as the status the branch protection
    requires (its context is config `landing.status_context`): success only when the finish
    state is mergeable, failure naming why otherwise, and nothing at all for a commit nobody
    drove. A later commit carries no status until it is driven, so a fix can never ride an
    older walk. Without the key no status is posted and the journal says so."""
    if shadow or not tip:
        return {"skipped": "shadow" if shadow else "no tip"}
    context = (c.get("landing") or {}).get("status_context")
    if not context:
        j.append("walk-status", posted=False, why="not configured: landing.status_context")
        return {"skipped": "not configured: landing.status_context"}
    if finish.get("merge") == "eligible":
        state, description = "success", "mergeable: every row and gate green"
    else:
        state, description = "failure", "; ".join(finish.get("why") or ["not mergeable"])
    page = ((c.get("page") or {}).get("url") or "http://127.0.0.1:4950/").rstrip("/")

    via = (c.get("landing") or {}).get("status_via") or "status"

    def do():
        if via == "dispatch":
            # a required check pinned to the Actions app takes no status from a person's token; the
            # product's `walk` workflow posts this verdict as Actions, from the dispatched payload
            rc, o, e = gh(["api", f"repos/{c['repo']}/dispatches", "-f", "event_type=walk",
                           "-f", f"client_payload[sha]={tip}", "-f", f"client_payload[state]={state}",
                           "-f", f"client_payload[context]={context}", "-f", f"client_payload[description]={description[:140]}",
                           "-f", f"client_payload[target_url]={page}/"], timeout=60)
        else:
            rc, o, e = gh(["api", f"repos/{c['repo']}/statuses/{tip}", "-f", f"state={state}", "-f", f"context={context}",
                           "-f", f"description={description[:140]}", "-f", f"target_url={page}/"], timeout=60)
        return {"state": state, "description": description, "sha": tip, "rc": rc, "via": via,
                "error": None if rc == 0 else (e or o or "")[-200:]}
    # keyed by the tip and the fact: a tip walked red and then green posts both, never the cached first
    return j.op(f"walk-status@{tip[:9]}:{state}" + (":dispatch" if via == "dispatch" else ""), do)


def pr_body(contract, tip, run_id, gates, review, receipts, note, evidence, finish, record, retired=(),
            preexisting=None, answers=None, allowance=None, goals=None, parked=None, stops=None, size=None):
    """The PR body is for the owner (see prbody): the headline, the finish state, the first
    screen, and a bounded synopsis that links the record by commit."""
    from hb.record.prbody import render
    return render(contract, tip, run_id, gates, review, receipts, note, evidence, finish, record,
                  retired=retired, preexisting=preexisting, answers=answers, allowance=allowance, goals=goals,
                  parked=parked, stops=stops, issues=issues_of(contract), size=size)


def land(j, a, c, contract, wt, branch, base_sha, slug, evidence, st, beat, note, before, stops, book=None,
         goals=(), slices=None, parked=None, opens=True, head=None):
    """The one landing: `book()` (the beat's ledger rows and changelog, returning
    {"refused": [...], "extra_why": [...]}), the push, every instrument at the PR tip, the
    record published, the PR body and the PR; then the test database and the venue go and the
    final phase is journaled. `opens` is False for a run that lands no PR of its own (a slice
    inside a wave). Returns the verdict for the caller to print."""
    from hb.gates import gating
    from hb.contract import rehearsal
    from hb.runner import recurrence
    from hb.record import record
    from hb.venue import standin
    from hb.record.prbody import headline, journal_allowance
    gates, preexisting, review_report = st["gates"], st["preexisting"], st["review_report"]
    receipts, venue, codex_in = st["receipts"], st["venue"], st.get("codex_in") or {}
    booked = (book() if book else None) or {}
    refused = booked.get("refused") or []
    gitops.push(j, wt, branch, a.shadow, "push-final")
    if opens and not stops:
        gates = gating.pr_tip(j, wt, slug, base_sha, c, gates, stops)
    # a red CI script at the PR tip is a stop, and a stopped run opens no PR
    opens = opens and not stops
    # a refused ledger row is a rig defect for the docket; it rides the record only
    extra = [("Ledger rows refused (rig, for the docket)", [f"{x['item']}: {x['why'][:200]}" for x in refused])] if refused else []
    extra += rehearsal.report_lines(j, receipts, contract) + recurrence.report_lines(j)
    allowance = journal_allowance(j, wt=wt)
    answers = control.answer_status(j)
    fs, published, retired = record.land(j, wt, c, contract, evidence, base_sha, branch, receipts, gates,
                                                 preexisting, review_report, note, a.shadow or stops, stops, goals=goals,
                                                 extra_why=list(booked.get("extra_why") or [])
                                                 + ([f"{len(refused)} ledger rows refused"] if refused else []),
                                                 slices=slices, extra=extra or None, answers=answers,
                                                 codex_fact=codex_in.get("fact"), before=before or None, allowance=allowance)
    pr_text = pr_body(contract, gitops.head_of(wt), j.run_id, gates, review_report, receipts, note, evidence, fs, published,
                      retired=retired, preexisting=preexisting, answers=answers, allowance=allowance,
                      goals=goals or None, parked=parked, stops=stops, size=size_hold(base_sha, "HEAD", cwd=wt))
    (evidence / "pr-body.md").write_text(pr_text, encoding="utf-8")
    pr = None
    if opens:
        post_walk(j, c, gitops.head_of(wt), fs, a.shadow)
        pr = open_pr(j, wt, branch, dict(contract), c, a.shadow, pr_text, state=fs["state"], beat=beat,
                     head=head or headline(contract, note), stops=stops)
    j.append("test-db", released=gating.test_db(wt, "release", slug).get("dropped"))
    if venue and not a.keep_venue and (pr or a.shadow or not opens or beat == "wave"):
        standin.venue_down(j, venue, wt, key=st.get("down_key", "venue-down"))
    final = fs["state"]
    j.phase(final, tip=gitops.head_of(wt), pr=(pr or {}).get("url"), stops=stops, wave=getattr(a, "wave", None),
            finish=fs, record=published.get("commit"), ledger_refused=len(refused))
    # the run grades itself where it ends: minutes worked over minutes passed, and where it waited
    from hb.record import grade as _grade
    graded = _grade.read(j.path)
    grade_line = {k: graded.get(k) for k in ("wall_min", "model_min", "model_share", "wait_min", "top_waits")}
    (evidence / "grade.json").write_text(json.dumps(graded, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    j.append("grade", **grade_line)
    return {"verdict": final, "run_id": j.run_id, "branch": branch, "tip": gitops.head_of(wt),
            "wave": getattr(a, "wave", None), "pr": (pr or {}).get("url") if pr else None, "shadow": bool(a.shadow),
            "stops": stops,
            "receipts": {s_: sum(1 for r in receipts if r.get("state") == s_) for s_ in ("WORKS", "BROKEN", "DIVERGED", "UNMEASURED")},
            "ledger_refused": refused, "record": str(evidence / "record.md"), "journal": str(j.path), "grade": grade_line}

FOOTER = "\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"


def open_pr(j, wt, branch, contract, c, shadow, pr_text, state=RUN_COMPLETE, beat="hotfix", head=None,
            stops=None):
    """Open the PR, or refresh the one already open on the branch: the body AND the title,
    because both are functions of the record (a repair that moved a wave's finish state left
    the old one in a title set once at creation). The body is sent as rendered, never trimmed:
    it is bounded by construction and the record it links holds the rest. A create gh
    refuses is a stop in `stops`, named by the branch, and `--resume` tries the create again."""
    from hb.record.prbody import title as _title
    from hb.gates import gating
    tip = gitops.head_of(wt)
    if not gating.heavy_at_tip(j, tip):
        # every instrument CI will run is measured at the tip a PR carries; none was here
        why = f"no heavy gate was measured at the tip {str(tip)[:9]} the PR would carry"
        j.append("pr-refused", tip=tip, why=why)
        if stops is not None:
            stop_ids.raise_stop(j, stops, "pr", stop_ids.other_id(f"pr-tip {tip}"), why)
        return {"refused": why}
    if shadow:
        return {"skipped": "shadow"}
    default = branch_setting(c, "landing")
    title = _title(beat, head or (pr_text.splitlines() or [contract.get("goal") or ""])[0], state)
    # written as bytes: a text write on Windows turns each newline into CRLF, which GitHub counts
    body = state_dir() / "runs" / j.run_id / "pr-body.md"

    def check():
        rc, o, _ = gh(["pr", "list", "--head", branch, "--state", "open", "--json", "url"])
        try:
            rows = json.loads(o or "[]")
        except json.JSONDecodeError:
            rows = []
        return {"url": rows[0]["url"]} if rows else None

    def do():
        body.write_bytes((pr_text + FOOTER).encode("utf-8"))
        cmd = ["pr", "create", "--base", default, "--head", branch, "--title", title, "--body-file", str(body),
               "--label", beat]
        rc, o, e = gh(cmd, timeout=120)
        if rc != 0 and "label" in (e or "") and "not found" in (e or ""):
            # the beat's label is made once per repo, never typed by hand (run 20260922-010153)
            gh(["label", "create", beat, "--description", f"opened by the {beat} door", "--force"], timeout=60)
            rc, o, e = gh(cmd, timeout=120)
        return {"url": o.strip() if rc == 0 else None, "error": None if rc == 0 else e.strip()[:300]}
    existing = check()
    if existing and existing.get("url"):
        # a later pass (a re-drive on resume) refreshes the body the owner reads
        body.write_bytes((pr_text + FOOTER).encode("utf-8"))
        rc, o, e = gh(["pr", "edit", existing["url"], "--title", title, "--body-file", str(body)], timeout=120)
        j.append("pr-edit", url=existing["url"], title=title, rc=rc, error=None if rc == 0 else (e or "")[-200:])
        return existing
    res = j.op(j.fresh_key("pr", lambda p: bool((p or {}).get("url"))), do, check=check)
    if not (res or {}).get("url") and stops is not None:
        stop_ids.raise_stop(j, stops, "pr", stop_ids.other_id(f"pr {branch}"),
                            f"the PR did not open: {(res or {}).get('error')}; --resume tries the create again")
    return res
