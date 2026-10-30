"""hb.run.loop - the one loop a change goes through before it reaches dev.

A round, on the tip, in four lanes at once: the gates; the review (the whole diff on the
first round, with the second reader beside it; after that, the diff since the reviewer's last
read and the record of findings, its scope its own call, Q5); the builder's checks, run by the
runner case by case on the venue (Q1, A16); and the battery at the config's k under the run's
dollar ceiling (Q4). Then every red (failing check lines, battery sets under their bar, open
findings, red gates) goes to the builder relaunched warm (hb.run.fixing), with no
classification step, and the next round measures every lane again at the new tip. A red still
red after two fixes ends by name and ships red (Q8, hb.run.recurrence). The loop ends when no
red is open or every open red has ended by name, or when a fix moves nothing.

Then the audit (Q2, Q9): one blind seat drives every item once on the venue (hb.run.drive), the
battery at k=3 beside it. Its disagreements with the checks are counted and journaled, its reds
go to the builder for one more round, the checks rerun on their cases and the audit re-drives
only its own reds.

The wave door runs it on the composed branch (`after_compose` cases run there), the slice door
on a standalone slice's branch, and both re-enter it on `--repair`. A slice inside a wave runs
only `review_loop`. Nothing here stops a run for a red; the stops are a seat's report naming
one of the owner's stop kinds (hb.runner.schemas) and a review seat that never answered.

A lane whose venue capability is absent runs nothing and writes `not offered: <capability>`
as the row's observed, UNMEASURED: the drive and the pictures without `ui`, the battery
without `model` (docs/config.md, Not offered).
"""

import json
import threading
from pathlib import Path

from hb.rig.common import report_text
from hb.contract import battery
from hb.contract import checks
from hb.beats import discovery
from hb.beats import drive as drive_mod
from hb.runner import fixing
from hb.gates import gating
from hb.rig import gitops
from hb.beats import integrate
from hb.contract import policy
from hb.runner import reader
from hb.runner import recurrence
from hb.record import pictures
from hb.record import reducer
from hb.runner import schemas
from hb.runner import seating
from hb.venue import adapter
from hb.venue import standin
from hb.runner import stops as stop_ids


# One film is made after the merge, on the deployed tier. A drive leaves stills: the pair
# for a look that changed, and what the audit saw on a row that is not WORKS.
EVIDENCE_RULE = ("write plans under the evidence directory; record no video. For a screen whose look changed, the "
                 "after capture of the same screen at the same viewport as its before capture, as "
                 "after/<ITEM>-desktop.png and after/<ITEM>-phone.png with the desktop path in `capture`. For a row "
                 "you grade BROKEN or UNMEASURED, one capture of what you saw, as red/<ITEM>.png, with the path in `capture`")
LOOP_K = 1   # the battery's tries per case inside the loop, unless the config names `battery.k` (Q4)
AUDIT_K = 3  # and on the audit (Q4)
CHECKS_MIN = 30


def _counts(receipts):
    return {s: sum(1 for r in receipts if r.get("state") == s) for s in ("WORKS", "BROKEN", "DIVERGED", "UNMEASURED")}


def review_once(j, contract, note, wt, base_sha, seat_kw, extra, stops, scope=None):
    """One review seat; the findings it leaves open for the builder, its stop findings raised, and
    the ones that ride the record journaled by id (ruling 18). `scope` is the set of paths the
    round touched, for a read after the tree moved."""
    facts = seating.facts_common(contract, {"The slice issue, verbatim": contract.get("body"),
                                            "The builder's note": note, **extra})
    review = seating.sit(j, "review", facts, wt, base_sha, contract["issue"], **seat_kw)
    findings = (review.get("report") or {}).get("findings") or [] if review["result"] == "ok" else []
    if review["result"] == "ok":
        j.append("read", tip=gitops.head_of(wt), seat=review.get("seat"))
    report = review.get("report") or {}
    fix, stop, ride = policy.route_findings(findings, stop_ids.answers(j), goal_met=policy.goal_met(report), scope=scope)
    if ride:
        j.append("findings-ride", seat=review.get("seat"), ids=[f.get("id") for f in ride],
                 why={f.get("id"): f.get("rides") for f in ride})
    for f in stop:
        stop_ids.raise_stop(j, stops, "finding", f.get("id"), f"nonwaivable {f.get('id')}: {f.get('summary')}")
    if review["result"] != "ok":
        text = f"review seat: {review['result']}; the diff is unreviewed"
        stop_ids.raise_stop(j, stops, "other", stop_ids.other_id(text), text)
    return review, fix


def open_findings(j, until=None):
    """The run's findings ledger: every review's findings overlaid by id in seat order, so a
    later review that marks one fixed closes it and one it never mentions stays open.
    Returns (open fix-class findings, the last review report). `until` names a review seat:
    the ledger stops there, so a later pass walking an early round sees what that round saw."""
    found, last = _overlaid_findings(j, until)
    found = [f for f in found if str(f.get("id")) not in riding(j)]
    return policy.route_findings(found, stop_ids.answers(j), goal_met=policy.goal_met(last))[0], last


def riding(j):
    """The finding ids a review left riding the record (ruling 18), never handed to a builder."""
    return {str(i) for e in j.events() if e.get("kind") == "findings-ride" for i in e.get("ids") or []}


def _overlaid_findings(j, until=None):
    """Every review's findings overlaid by id in seat order, and the last review report."""
    seats = j.dir / "seats"
    by_id, last = {}, None
    stop = seats / str(until) / "report.json" if until else None
    reviews = [d for d in seats.iterdir() if d.name.startswith("review-") and (d / "report.json").exists()] \
        if seats.exists() else []
    if stop is not None and stop.exists():
        reviews = [d for d in reviews if (d / "report.json").stat().st_mtime <= stop.stat().st_mtime]
    # in the order the reports were written: a review seeded by hand carries any number
    for d in sorted(reviews, key=lambda x: (x / "report.json").stat().st_mtime):
        try:
            rep = json.loads(report_text(d / "report.json"))
        except ValueError:
            continue
        for f in rep.get("findings") or []:
            if f.get("id"):
                by_id[f["id"]] = f
        last = rep
    return list(by_id.values()), last


def _review_stood(j, findings, stops):
    """A review lane that stood from an earlier pass, read again with the answers given since:
    an unanswered nonwaivable finding stops the run again, an answered one joins the fix list."""
    found, _last = _overlaid_findings(j)
    found = [f for f in found if str(f.get("id")) not in riding(j)]
    fix, stop, _ride = policy.route_findings(found, stop_ids.answers(j), goal_met=policy.goal_met(_last))
    for f in stop:
        stop_ids.raise_stop(j, stops, "finding", f.get("id"), f"nonwaivable {f.get('id')}: {f.get('summary')}")
    by_id = {f.get("id"): f for f in findings}
    for f in fix:
        by_id[f.get("id")] = f
    return list(by_id.values())


def _checks_to_approve(c, wt, base_sha):
    """The check files the change touched that are not pinned at their hash."""
    where = checks.folder(c, wt).resolve()
    pinned = checks.approvals(where)
    out = []
    for rel in gitops.changed_paths(wt, base_sha):
        f = Path(wt) / rel
        if f.is_file() and where in f.resolve().parents and f.name != checks.APPROVED and pinned.get(rel) != checks.digest(f):
            out.append(rel)
    return out


def approve_checks(j, c, wt, branch, report):
    """The checks the reviewer approved, pinned at their hash and committed by the runner, so
    their rows grade from now on (Q1). Returns the paths pinned."""
    wanted = [p for p in (report or {}).get("approved_checks") or [] if isinstance(p, str)]
    if not wanted:
        return []
    try:
        done = checks.approve(c, wt, wanted)
    except ValueError as ex:
        j.append("checks-approved", branch=branch, paths=[], why=str(ex))
        return []
    tip_in = gitops.head_of(wt)
    gitops.git(wt, "add", "--", str(checks.folder(c, wt) / checks.APPROVED))
    gitops.git(wt, "-c", "user.name=hb", "-c", "user.email=hb@local", "commit", "-q", "-m",
               f"checks: {len(done)} approved by the review at {tip_in[:9]}")
    # journaled as a done with its tip, so the integrator reads the commit as the runner's
    j.done("approve-checks", branch=branch, tip_in=tip_in, tip_out=gitops.head_of(wt), paths=sorted(done))
    return sorted(done)


def _full_read(contract, c, wt, base_sha, tip, composed, finds):
    """The facts of the whole-diff read: the post-build one, or the composed one (Q5)."""
    return {"Read": "the composed branch, every slice's diff together" if composed else "the whole change, after the build",
            "Diff": f"git diff {base_sha}..{tip} in your working directory",
            "Checks to approve": _checks_to_approve(c, wt, base_sha),
            "Proposed golden cases": [f"{f.get('item')}: {f.get('said')}" for f in finds or []]}


def landing_files(c):
    """The files the runner's own landing commits (ledger rows, the changelog): a tree that moved
    by these alone moved by nobody's change."""
    paths = (c or {}).get("paths") or {}
    return {p for p in (paths.get("ledger_file"), paths.get("changelog_file")) if p}


def capture_before(j, a, items, wt, slug, base_sha, evidence, seat_kw, issue):
    """The pictures at the base commit (hb.record.pictures), asked of the venue's `ui` first:
    without it no capture seat sits and the journal names why."""
    if not adapter.offers("ui"):
        j.append("sized", before=False, why=adapter.not_offered("ui"))
        return {}
    return pictures.capture_before(j, a, items, wt, slug, base_sha, evidence, seat_kw, issue)


def _moved_by_landing_alone(c, wt, since):
    """True when every file that moved since `since` is one the runner's own landing writes."""
    moved = gitops.changed_paths(wt, since)
    return bool(moved) and set(moved) <= landing_files(c)


def last_read_tip(j, default):
    """The tip the reviewer last read (its newest `read`), else `default`: the base on a first
    pass, the tip a finished run left on a repair or a standing step."""
    tip = None
    for ev in j.events():
        if ev.get("kind") == "read":
            tip = ev.get("tip") or tip
    return tip or default


def _next_read(j, c, contract, note, wt, base_sha, seat_kw, tip, stops, since):
    """A read after the tree moved: the diff since the reviewer's last read and the record of
    findings; what it reads past that is its own call (Q5). Any check the move edited is
    unapproved until a review reads it. Returns (review seat result, the findings open after it)."""
    found, _last = _overlaid_findings(j)
    facts = {"Diff since your last read": f"git diff {since}..{tip} in your working directory",
             "Findings so far": [f for f in found if f.get("status") != "fixed"]}
    edited = [p for p in _checks_to_approve(c, wt, base_sha) if p in gitops.changed_paths(wt, since, tip)]
    if edited:
        facts["Checks to approve"] = edited
    review, _ = review_once(j, contract, note, wt, base_sha, seat_kw, facts, stops,
                            scope=set(p.replace("\\", "/") for p in gitops.changed_paths(wt, since, tip)))
    findings, _last = open_findings(j, until=review.get("seat"))
    j.phase("review", read="since-last", open=len(findings))
    return review, findings


def _facts_for_drive(contract, c, note, v, vextra, before):
    f = seating.facts_common(contract, {
        "The venue": standin.venue_facts(v),
        "Mock scenarios you may select by name": c.get("mock_scenarios") or "none configured",
        "Before captures": before or "none: no screen was captured at the base commit",
        "Where each item lives, the builder's pointer, never a claim about what works":
            {it.get("item"): it.get("see") for it in (note or {}).get("items") or [] if it.get("see")} or None,
        "Evidence": EVIDENCE_RULE})
    for k, val in (vextra or {}).items():
        if val is not None:
            f[k] = val
    return f


# what the reviewer reads in place of the second reader's list, which is read beside it
READ_BESIDE = ("none yet: the second reader reads the same diff beside you; its claims pass the quote gate in a "
               "closing check after you")


def _at_once(*lanes):
    """Run each lane in its own thread; the first error any lane raised is raised once all end."""
    errors = []

    def one(lane):
        try:
            lane()
        except BaseException as ex:  # a pause, a usage limit or a rig fault, raised after the others end
            errors.append(ex)

    threads = [threading.Thread(target=one, args=(lane,), daemon=True) for lane in lanes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


def _first_review(j, a, contract, note, wt, base_sha, seat_kw, evidence, review_facts, second_reader, codex_in, stops):
    """The whole-diff review with the second reader beside it, then the reader's claims through
    the quote gate. Returns (review report, fix-class findings, codex_in)."""
    read, out = {}, {}
    wanted = second_reader and reader.wanted(a)

    def codex():
        read["in"] = reader.codex_read(j, wt, base_sha, contract, evidence, a.seat_timeout_min)

    def review():
        out["review"], out["buckets"] = review_once(j, contract, note, wt, base_sha, seat_kw,
                                                {"FINDINGS_IN": READ_BESIDE if wanted else codex_in["fact"],
                                                 **review_facts}, stops)

    _at_once(review, *([codex] if wanted else []))
    codex_in = read.get("in") or codex_in
    rv, fix = out["review"], list(out["buckets"])
    if codex_in.get("path") and not stops:
        late_fix, late_stop, _late_ride = policy.route_findings(reader.gate(j, wt, base_sha, contract, codex_in, seat_kw),
                                                                stop_ids.answers(j), goal_met=policy.goal_met(rv.get("report")))
        for f in late_stop:
            stop_ids.raise_stop(j, stops, "finding", f.get("id"), f"nonwaivable {f.get('id')}: {f.get('summary')}")
        fix += late_fix
    j.phase("review", round=1, findings=len((rv.get("report") or {}).get("findings") or []), fix=len(fix))
    return rv, fix, codex_in


def _venue_for_fix(j, slug, wt, v, served_tip, real, skip_drive, key):
    """The venue at the tip for the builder's fix round, so it reproduces each red before it
    touches code (Q3, A16); (venue record or None, the tip it serves). None when the change
    has nothing to see, or the venue would not stand: the round goes on and says so."""
    if skip_drive:
        return v, served_tip
    v = standin.ensure(j, slug, wt, v, served_tip, real_llm=real, key=key)
    if not (v or {}).get("venue"):
        j.append("venue", state="failed", stage="fix", why=(v or {}).get("error"))
        return None, None
    return v, gitops.head_of(wt)


def _venue_fact(v):
    """The builder's VENUE fact, or nothing when no venue stands."""
    return {"VENUE": standin.venue_facts(v)} if (v or {}).get("venue") else {}


def _enter(j, c, st, wt, branch, prior, proven, repair=False):
    """Where this pass enters the loop on `branch`: the reducer's state, with an interrupted fix
    reconciled against the tree, or a fresh loop-open when none is open, one ended, or the
    tree no longer holds its tip. Returns (state, resuming)."""
    head = gitops.head_of(wt)
    if st and not st.ended and st.tip and not gitops.in_history(wt, st.tip):
        j.append("loop-stale", branch=branch, tip=st.tip, why="the tree no longer holds the tip the loop was at")
        st = None
    if st and not st.ended:
        st = reducer.reconcile(j, st, head)
        j.append("loop-resume", branch=branch, round=st.round, at=st.phase, tip=str(st.tip)[:9])
        return st, True
    last = None
    if st and st.ended:
        # the loop's own last tip: a later pass journals the compose tip before it reaches here
        evs = j.events()
        end = max(i for i, e in enumerate(evs) if e.get("kind") == "loop" and e.get("branch") == branch)
        last = integrate.expected(j, branch, evs[:end + 1]) or st.tip
    # a repair asks for work on the ended loop by name; only a plain resume stands on it
    if last and not repair and gitops.in_history(wt, last):
        # the landing's own commits (ledger rows, the changelog) move the tip after the loop
        # ended; a resume past them re-enters the ended loop, never a new one on the whole diff.
        # The loop's last tip is the one the runner journaled last (the audit's fix moves it past
        # the last round-start).
        moved = gitops.changed_paths(wt, last)
        if set(moved) <= landing_files(c):
            j.append("loop-stands", branch=branch, round=st.round, tip=str(last)[:9], head=str(head)[:9],
                     why="the loop ended at this tip" if not moved else "the loop ended; only its own landing moved the tree")
            return st, True
    receipts = list(prior.get("receipts") or []) if prior else list(proven)
    j.append("loop-open", branch=branch, prior=bool(prior), receipts=receipts, findings=list(prior.get("findings") or []),
             review=prior.get("review"), asked=list(prior.get("asked") or []), tip_before=prior.get("tip"))
    return reducer.replay(j.events(), branch), False


def _last_lane(j, branch, lane):
    """The newest lane-done of `lane` on `branch` since the loop last opened, or {}."""
    found = {}
    for ev in j.events():
        if ev.get("kind") == "loop-open" and ev.get("branch") == branch:
            found = {}
        elif ev.get("kind") == "lane-done" and ev.get("lane") == lane and ev.get("branch") == branch:
            found = ev
    return found


# ── the battery under the run's dollar ceiling (Q4) ──────────────────────────


def _ceiling(c):
    try:
        return float(((c or {}).get("battery") or {}).get("ceiling_usd"))
    except (TypeError, ValueError):
        return None


def _spent(j):
    return round(sum(float(e.get("usd") or 0) for e in j.events() if e.get("kind") == "battery-spend"), 4)


def _journal_spend(j, item, row):
    """The spend one run of a set reported on its summary line (`usd`), or `unmeasured`."""
    usd = None
    for path in (row or {}).get("evidence") or []:
        try:
            _cases, summary = battery.parse_output(Path(path).read_text(encoding="utf-8", errors="replace"))
            usd = summary.get("usd")
        except (OSError, ValueError):
            continue
    if isinstance(usd, (int, float)):
        j.append("battery-spend", item=item["item"], set=battery.case_set(item), usd=float(usd))
    else:
        j.append("battery-spend", item=item["item"], set=battery.case_set(item), usd=None, state="unmeasured",
                 why="the runner printed no `usd` on its summary line; the ceiling never stops on it")


def run_battery(j, c, items, venue_facts, wt, evidence, base_sha, k, timeout_min):
    """{item: (row, says)} for each model item of `items` at k tries, until the spend the
    runner reported passes the config's `battery.ceiling_usd`; the rest read "not run: ceiling"
    and are never green. Without the venue's `model` capability nothing runs and every item
    reads not offered."""
    ceiling, out = _ceiling(c), {}
    if items and not adapter.offers("model"):
        j.append("battery-run", items=[it["item"] for it in items], state="not-run", why=adapter.not_offered("model"))
        return {r["item"]: (dict(r, paths_tried=["battery"], evidence=[]), {})
                for r in drive_mod.not_offered_rows([it["item"] for it in items], "model")}
    for it in items:
        spent = _spent(j)
        if ceiling is not None and spent > ceiling:
            why = f"not run: ceiling: the run's battery spend ${spent:.2f} is past its ceiling ${ceiling:.2f}"
            j.append("battery-run", item=it["item"], set=battery.case_set(it) or None, state="not-run", why="ceiling")
            out[it["item"]] = ({"item": it["item"], "state": "UNMEASURED", "substate": "no-battery", "observed": why,
                                "seen": why, "paths_tried": ["battery"], "evidence": [],
                                "no_venue": "the battery did not run"}, {})
            continue
        ids = battery.new_case_ids(wt, c, base_sha, battery.case_set(it)) if battery.new_only(c) else None
        row, says = battery.run_one(j, c, dict(it, k=k, ids=ids), venue_facts, wt, evidence, timeout_min, base_sha)
        if battery.case_set(it) and battery.command(c):
            _journal_spend(j, it, row)
        out[it["item"]] = (row, says)
    return out


# ── what the rows say, per item ──────────────────────────────────────────────


def _rows_file(evidence, rows):
    """Every case's newest row in one file the receipts cite."""
    path = Path(evidence) / "checks" / checks.RESULTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows.values()), encoding="utf-8")
    return str(path)


def fold_receipts(j, contract, kinds, rows, scores, audit, proven, finds, evidence, venue_facts, carried=()):
    """One receipt per item: the test's for an item proven by a test, the checks' fold for a
    rule with cases, the battery's for a model item, the audit's for an item with no cases (a
    repair's earlier row, carried, when the audit wrote none). A rule the checks call WORKS
    and the audit saw BROKEN reads BROKEN with the audit's words: an honest red beats a check
    that passed."""
    before = {r.get("item"): dict(r, carried=True) for r in carried or []}
    by_audit = {r.get("item"): r for r in audit or []}
    cited = _rows_file(evidence, rows) if rows else None
    tenant = (venue_facts or {}).get("tenant_id")
    out = list(proven)
    for r in policy.receipts_from_checks(list(rows.values()), kinds["checked"]):
        r = dict(r, served_sha=(r.get("identity") or {}).get("tip"), evidence=[cited] if cited else [],
                 **({"tenant_id": tenant} if tenant else {"no_venue": "graded by the product's checks runner on the stand-in"}))
        seen = by_audit.get(r["item"])
        if r["state"] == "WORKS" and (seen or {}).get("state") == "BROKEN":
            r.update(state="BROKEN", substate=seen.get("substate") or "failed",
                     observed=f"the blind audit saw: {seen.get('observed')}", seen=seen.get("seen") or seen.get("observed"))
        elif (r["state"] == "UNMEASURED" and (seen or {}).get("state") in ("WORKS", "BROKEN")
              and str(r.get("observed") or "").split(":", 1)[0] != "after-compose"):
            # the code could not grade it (no check, no runner, unapproved) and the blind audit drove it:
            # the drive is the measurement and the gap stays named; an after-compose case waits (Q1, Q9, A18)
            r = dict(seen, checks_gap=r.get("observed"), cases=r.get("cases"))
        out.append(r)
    have = {r["item"] for r in out}
    for it in kinds["checked"]:
        if it["item"] not in have:
            why = "no check row: the checks have not run on this venue"
            out.append({"item": it["item"], "state": "UNMEASURED", "substate": "not-driven", "observed": why, "seen": why})
    out += battery.fold([], kinds["model"], scores, j, finds=finds)
    for it in kinds["audited"]:
        r = by_audit.get(it["item"]) or before.get(it["item"])
        if r is None:
            why = "no-check: the item carries no cases, and the audit has not driven it yet"
            r = {"item": it["item"], "state": "UNMEASURED", "substate": "not-driven", "observed": why, "seen": why}
        out.append(r)
    return out


def _kinds(drivable):
    """The drivable items by what grades them: the checks, the battery, or the audit alone."""
    model = [it for it in drivable if battery.is_model(it)]
    checked = [it for it in drivable if it not in model and it.get("cases")]
    return {"model": model, "checked": checked, "audited": [it for it in drivable if it not in model and it not in checked]}


def _kept_red(was, now):
    """A case the checks graded BROKEN is never downgraded by a rerun whose runner did not
    answer: it stays BROKEN and names the rerun that failed."""
    if (was or {}).get("state") == "BROKEN" and now.get("substate") in ("checks-runner", "no-result"):
        return dict(was, rerun_invalid=f"{now['substate']}: {now.get('observed')}")
    return now


def run_loop(j, a, c, contract, wt, branch, base_sha, slug, note, evidence, stops, seat_kw, mode="slice",
             real=None, review_extra=None, vextra=None, before=None, prior=None, second_reader=True,
             venue_rec=None, finds=None):
    """The loop on `branch` at its tip, then the audit. `mode` is `slice` or `composed`.
    `prior` re-enters with a finished run's rows: {receipts, findings, review, asked}.
    `venue_rec` is a venue the rehearsal left standing at this tip; `finds` the discovery
    seat's, folded into the model items' rows. Returns the dict the landers take: gates,
    preexisting, review_report, receipts, venue, served_sha (the venue record's own),
    codex_in, rounds, check_rows, audit.

    Every boundary is journaled as it happens (round-start, lane-done, intent and done around
    the fix, loop) and the position is the reducer's; a pass that re-enters takes it from
    there, so nothing measured is measured again and nothing landed is landed again."""
    note, prior, finds = note or {}, prior or {}, list(finds or [])
    composed = mode == "composed"
    codex_in = {"fact": reader.NO_SECOND_READER, "path": None}
    v, venue, served_tip = venue_rec, None, gitops.head_of(wt) if venue_rec else None
    venue_facts = {}
    changed = gitops.changed_paths(wt, base_sha)
    proven = policy.proven_by_test(contract["items"], note, wt)
    by_test = {r["item"] for r in proven}
    drivable = [it for it in contract["items"] if it["item"] not in by_test]
    kinds = _kinds(drivable)
    k = int(((c.get("battery") or {}).get("k")) or LOOP_K)
    timeout_min = seat_kw.get("timeout_min") or policy.SEAT_FUSE_MIN
    case_rule = {x.get("id"): it["item"] for it in kinds["checked"] for x in it.get("cases") or [] if isinstance(x, dict)}
    if proven:
        j.append("proven-by-test", items=sorted(by_test))
    skip_drive = a.no_venue or not drivable or not adapter.declared()
    if drivable and not a.no_venue and not adapter.declared():
        j.append("sized", stand_in=False, why=adapter.not_offered("venue"))
    if real is None:
        real = bool(a.real_llm) or policy.needs_real_model_for(contract["items"], changed)
    if skip_drive and not a.no_venue and not drivable:
        j.append("sized", stand_in=False, why="every item is proven by a test the builder named")
    if not skip_drive:
        why = policy.real_model_reason(contract["items"], changed) if real and not a.real_llm \
            else ("--real-llm" if a.real_llm else "no item needs the model's judgment")
        j.append("model", real=real, why=why)
        if real:
            # the doors this loop's venues drive on the real model, and why, for the audit's report
            j.append("real-model", why=why or "the caller drives on the real model",
                     doors=[it["item"] for it in drivable])
    st, resuming = _enter(j, c, reducer.replay(j.events(), branch), wt, branch, prior, proven, repair=bool(getattr(a, "repair", None)))
    receipts, findings, review_report = st.receipts, st.findings, st.review
    codex_in = st.codex_in or codex_in
    preexisting = st.preexisting
    venue = st.venue
    tip_before = st.tip_before or base_sha
    rows = {r["case"]: r for r in _last_lane(j, branch, "checks").get("rows") or []}
    scores = {i: (r, {}) for i, r in (_last_lane(j, branch, "battery").get("scores") or {}).items()}
    # a round already started at the tree's tip is re-entered: its finished lanes stand
    n = st.round - 1 if resuming and st.phase in ("measuring", "fixing") and st.tip == gitops.head_of(wt) else st.round
    if resuming and st.phase == "pushing":
        gitops.push(j, wt, branch, a.shadow, f"push-r{st.round}")
    handed = []
    # the loop's end is journaled after the audit, so a run killed inside the audit resumes there
    ending = None
    suite_at = {}  # the tips the whole heavy suite measured in this loop (ruling 18)
    loop_id = sum(1 for e in j.events() if e.get("kind") == "loop-open" and e.get("branch") == branch)
    gates = []
    stands = resuming and st.phase == "ended"
    if stands:
        # the ended loop's gates stand for the landing; the audit's fast gates at the tip join them
        gates = list(((_last_lane(j, branch, "gates").get("got") or {}).get("gates")) or [])
        # the venue is down; its tenant is the one the last round's rows were graded on
        tenant = next((r["tenant_id"] for r in receipts if r.get("tenant_id")), None)
        venue_facts = {"tenant_id": tenant} if tenant else {}
    while not stops and not stands:
        n += 1
        if integrate.expect(j, wt, branch, contract["items"]):
            # a commit the runner never made: journaled, its rows invalidated, the round starts here
            st = reducer.replay(j.events(), branch)
            receipts = st.receipts
        tip = gitops.head_of(wt)
        first = n == 1 and not prior
        # the tip a round measures after a fix is that fix's tip (a hand commit is journaled apart)
        fixed_by = tip if n > 1 else None
        same = st.round == n and st.tip == tip
        if not same:
            j.append("round-start", branch=branch, round=n, tip=tip, tip_before=tip_before,
                     touched=gitops.changed_paths(wt, tip_before, tip) if n > 1 else None)
            st = reducer.replay(j.events(), branch)
        got = {}
        venue_lost = False

        def gates_lane():
            # every gate on this tip, each through admission; a red proven at the base is pre-existing
            nonlocal preexisting
            lane = same and st.lane("gates")
            if lane:
                got.update(lane["got"])
                return
            fast = gating.measure(j, wt, slug, base_sha, gitops.changed_paths(wt, base_sha), c, f"gates-r{n}")
            # per round only the every-tip lane is heavy; the whole suite waits for a quiet loop (ruling 18)
            heavy = gating.full_suite(j, wt, slug, base_sha, c, every_tip_only=True)
            got["heavy"], got["preexisting"] = gating.settle_heavy(j, wt, base_sha, slug, heavy, preexisting)
            got["fast"], got["gates"] = list(fast), list(fast) + list(got["heavy"])
            j.append("lane-done", branch=branch, round=n, tip=tip, lane="gates", got=got)

        def review_lane():
            # the whole diff once, with the second reader beside it; after the tree moved, what moved
            nonlocal review_report, findings, codex_in
            lane = same and st.lane("review")
            if lane:
                findings = _review_stood(j, findings, stops)
                return
            if first:
                facts = dict(_full_read(contract, c, wt, base_sha, tip, composed, finds), **(review_extra or {}))
                rv, findings, codex_in = _first_review(j, a, contract, note, wt, base_sha, seat_kw, evidence, facts,
                                                       second_reader, codex_in, stops)
                review_report = rv.get("report") or review_report
                got["read"] = rv.get("report")
            elif last_read_tip(j, tip_before) != tip and not _moved_by_landing_alone(c, wt, last_read_tip(j, tip_before)):
                rv, findings = _next_read(j, c, contract, note, wt, base_sha, seat_kw, tip, stops,
                                          last_read_tip(j, tip_before))
                if rv["result"] == "ok":
                    review_report = rv.get("report") or review_report
                    got["read"] = rv.get("report")
            j.append("lane-done", branch=branch, round=n, tip=tip, lane="review", review=review_report, findings=findings,
                     codex_in=codex_in)

        def checks_lane():
            nonlocal rows
            lane = same and st.lane("checks")
            if lane:
                rows = {r["case"]: r for r in lane.get("rows") or []}
                return
            ids = None
            if not kinds["checked"]:
                return
            if not venue:
                why = f"stand-in did not come up: {str((v or {}).get('error'))[:200]}"
                cases = [x for it in kinds["checked"] for x in it.get("cases") or [] if ids is None or x.get("id") in ids]
                for x in cases:
                    lost = dict(rows.get(x["id"]) or {}, schema=schemas.CHECK_ROW, rule=case_rule[x["id"]], case=x["id"],
                                state="UNMEASURED", substate="checks-runner", observed=why, line=None)
                    rows[x["id"]] = _kept_red(rows.get(x["id"]), lost)
            else:
                identity = policy.identity(tip, contract, venue_facts, j.fact("harness"))
                for r in checks.run(c, wt, kinds["checked"], venue_facts, Path(evidence) / "checks" / f"r{n}", identity,
                                    ids=ids, composed=composed, timeout_min=CHECKS_MIN, note=j.append):
                    bad = schemas.check_check_row(r)
                    if bad:
                        j.append("check-row-refused", case=r.get("case"), problems=bad)
                        r = dict(r, state="UNMEASURED", substate="no-result", observed="; ".join(bad))
                    rows[r["case"]] = _kept_red(rows.get(r["case"]), r)
            j.append("lane-done", branch=branch, round=n, tip=tip, lane="checks", rows=list(rows.values()),
                     asked=ids, **{s.lower(): sum(1 for r in rows.values() if r.get("state") == s)
                                   for s in ("WORKS", "BROKEN", "UNMEASURED")})

        def battery_lane():
            lane = same and st.lane("battery")
            if lane:
                scores.update({i: (r, {}) for i, r in (lane.get("scores") or {}).items()})
                return
            wanted = kinds["model"]
            if not wanted or not venue or battery.new_only(c):
                # scope new: the battery runs once, at the audit, on the cases the change added
                return
            scores.update(run_battery(j, c, wanted, venue_facts, wt, evidence, base_sha, k, timeout_min))
            j.append("lane-done", branch=branch, round=n, tip=tip, lane="battery", asked=[it["item"] for it in wanted],
                     scores={i: r for i, (r, _says) in scores.items()})

        def measure_lane():
            # the checks and the battery on one venue at this tip
            nonlocal v, venue, venue_facts, served_tip, venue_lost
            if not (same and st.lane("checks") and st.lane("battery")):
                v = standin.ensure(j, slug, wt, v, served_tip, real_llm=real, key=f"venue-up-r{n}")
                venue = (v or {}).get("venue")
                if not venue:
                    venue_lost = True
                    j.append("venue", state="failed", round=n, why=(v or {}).get("error"))
                else:
                    venue_facts, served_tip = standin.venue_facts(v), tip
            _at_once(checks_lane, battery_lane)
            if venue:
                # the venue goes down the moment the rows are in, so the fix seat starts on a
                # machine without it; the next round stands it again
                standin.venue_down_keep(j, venue, wt, f"venue-down-r{n}")
                v = None

        measures = not skip_drive and (kinds["checked"] or kinds["model"])
        _at_once(gates_lane, review_lane, *([measure_lane] if measures else []))
        gates, preexisting = got["gates"], got["preexisting"]
        red_gates = gating.gates_to_fix(gates, preexisting)
        if stops:
            break
        if approve_checks(j, c, wt, branch, got.get("read")):
            tip = gitops.head_of(wt)
            if venue_facts and not skip_drive:
                # the cases whose check the review just approved grade now, on the same tip
                unapproved = [r["case"] for r in rows.values() if r.get("substate") == "unapproved"]
                if unapproved:
                    v = standin.ensure(j, slug, wt, v, served_tip, real_llm=real, key=f"venue-up-r{n}-approved")
                    if (v or {}).get("venue"):
                        identity = policy.identity(tip, contract, venue_facts, j.fact("harness"))
                        for r in checks.run(c, wt, kinds["checked"], standin.venue_facts(v),
                                            Path(evidence) / "checks" / f"r{n}-approved", identity, ids=unapproved,
                                            composed=composed, timeout_min=CHECKS_MIN, note=j.append):
                            rows[r["case"]] = r
                        standin.venue_down_keep(j, v["venue"], wt, f"venue-down-r{n}-approved")
                        v = None
                        j.append("lane-done", branch=branch, round=n, tip=tip, lane="checks", rows=list(rows.values()),
                                 asked=unapproved)
        if not skip_drive:
            receipts = fold_receipts(j, contract, kinds, rows, scores, None, proven, finds, evidence, venue_facts,
                                     carried=prior.get("receipts"))
            j.append("lane-done", branch=branch, round=n, tip=tip, lane="drive", receipts=receipts, venue=venue,
                     **_counts(receipts))

        def open_reds():
            """Every red this round, each journaled once, less the ones that ended by name (Q8)."""
            now = {}
            for r in rows.values():
                if r.get("state") == "BROKEN":
                    now[r["case"]] = {"kind": "check", "item": r.get("rule"), "case": r["case"], "line": r.get("line")}
            for item, (row, _says) in scores.items():
                if row.get("state") == "BROKEN":
                    b = row.get("battery") or {}
                    # the line is the problem's name across rounds (Q8), so the moving pass^k and case list ride beside it
                    now[f"battery:{item}"] = {"kind": "battery", "item": item, "set": b.get("set"),
                                              "line": f"golden set {b.get('set')} under the bar {b.get('bar')}",
                                              "detail": f"pass^k {b.get('pass_k')}; failed: {', '.join(b.get('failed') or [])}"}
            for g in red_gates:
                now[f"gate:{g.get('name')}"] = dict(g, kind="gate", line=" ".join(str(x) for x in g.get("red_ids") or [])
                                                    or f"exit {g.get('true_exit')}")
            for f in findings:
                now[str(f.get("id"))] = dict(f, kind="finding", line=f.get("summary") or f.get("title"))
            watched = {e.get("case") for e in j.events() if e.get("kind") == "red-line" and e.get("branch") == branch}
            ended = recurrence.ended_ids(j, branch)
            for rid in sorted(set(now) | watched):
                if rid not in ended:
                    recurrence.note(j, rid, (now.get(rid) or {}).get("line"), n, fixed_by, branch=branch,
                                    red=rid in now)
            return {rid: red for rid, red in now.items() if not recurrence.end_if_stuck(j, rid, branch)}

        reds = open_reds()
        named = stop_ids.fixer_briefs(j)
        if not reds and not named and not suite_at.get(tip):
            # the loop is quiet: the whole suite once at this tip; a red it finds is one more round
            heavy_all = gating.full_suite(j, wt, slug, base_sha, c)
            heavy_all, preexisting = gating.settle_heavy(j, wt, base_sha, slug, heavy_all, preexisting)
            suite_at[tip] = True
            names = {g.get("name") for g in heavy_all}
            gates = [g for g in gates if g.get("name") not in names] + list(heavy_all)
            got["heavy"], got["gates"], got["preexisting"] = list(heavy_all), list(gates), preexisting
            j.append("lane-done", branch=branch, round=n, tip=tip, lane="gates", got=got)
            red_gates = gating.gates_to_fix(gates, preexisting)
            reds = open_reds()
        if not reds and not named:
            over = sorted(recurrence.ended_ids(j, branch))
            ending = dict(branch=branch, round=n, result="venue-failed" if venue_lost else "ended" if over else "clean",
                     ended=over or None,
                     why="the venue never came up; every row it owed is UNMEASURED with the venue's error"
                     if venue_lost else "every red left ended by name: still red after two fixes" if over
                     else "nothing red and no finding open")
            break
        if integrate.expect(j, wt, branch, contract["items"]):
            st = reducer.replay(j.events(), branch)
            receipts = st.receipts
            continue
        # one fix round by the builder relaunched, on the whole list (A16)
        op = f"fix-r{n}"
        if not (st.fix_open and st.fix_open.get("op") == op):
            j.intent(op, branch=branch, round=n, tip=tip, given={rid: red.get("line") for rid, red in reds.items()})
        handed = [red for red in reds.values() if red["kind"] == "finding"]
        failing = [{k_: red.get(k_) for k_ in ("item", "case", "set", "line") if red.get(k_)}
                   for red in reds.values() if red["kind"] in ("check", "battery")]
        v, served_tip = _venue_for_fix(j, slug, wt, v, served_tip, real, skip_drive, f"venue-up-fix-r{n}")
        fix = fixing.fix_pass(j, contract, note, wt, base_sha, seat_kw, findings=handed or None, reds=failing or None,
                              gates=[red for red in reds.values() if red["kind"] == "gate"] or None, extra={
                                  **_venue_fact(v),
                                  **discovery.finds_as_fix_facts(finds),
                                  "Named by the conductor: fix first": (named + (seating.answered_findings(handed) or []))
                                  or None,
                                  "Round": n})
        j.append("fix-round", stage=f"r{n}", result=fix["result"])
        stop_ids.mark_routed(j, named, op)
        new_tip = seating.left_at(fix, wt)
        moved_nothing = fix["result"] != "ok" or new_tip == tip
        j.done(op, branch=branch, round=n, result=fix["result"], tip_out=new_tip)
        st = reducer.replay(j.events(), branch)
        if moved_nothing:
            ending = dict(branch=branch, round=n, result="venue-failed" if venue_lost else "moved-nothing",
                     fix=fix["result"], why="the venue never came up" if venue_lost
                     else "the fix left the tip where it was; what is left rides the PR by name")
            break
        integrate.expect(j, wt, branch, contract["items"])
        gitops.push(j, wt, branch, a.shadow, f"push-r{n}")
        tip_before = tip

    audit = {"false_greens": [], "false_reds": [], "receipts": []}
    if (v or {}).get("venue"):
        # the builder's venue from the last fix round; the audit stands its own, blind
        standin.venue_down_keep(j, v["venue"], wt, "venue-down-loop-end")
        v = None
    if not stops and not skip_drive:
        audit, rows, scores, gates, venue = _audit(j, a, c, contract, wt, branch, base_sha, slug, note, evidence,
                                                    seat_kw, kinds, rows, scores, gates, real, vextra, before,
                                                    composed, timeout_min, loop_id)
    if ending:
        j.append("loop", **ending)
    if not skip_drive:
        receipts = fold_receipts(j, contract, kinds, rows, scores, audit["receipts"], proven, finds, evidence,
                                 venue_facts, carried=prior.get("receipts"))
    elif n == 1:
        have = {r.get("item") for r in receipts}
        receipts += [{"item": it["item"], "state": "UNMEASURED", "substate": "not-driven",
                      "observed": "filed as proven by a test; no test in the builder's note names this item and nothing drove it",
                      "seen": "no test names this item"}
                     for it in contract["items"] if it.get("proof") == "test" and it["item"] not in have]
    receipts = policy.regrade_grey_by_test(receipts, contract["items"], note, wt)
    receipts = policy.relabel_diverged(receipts, note, contract.get("rulings"))
    receipts = policy.note_beside(receipts, note)
    j.phase("drive", rounds=n, venue=venue, skipped=bool(skip_drive), **{k_.lower(): val for k_, val in _counts(receipts).items()})
    j.phase("gates", red=len(gating.gates_red(gates, preexisting)), preexisting=[p["gate"] for p in preexisting])
    return {"gates": gates, "preexisting": preexisting, "review_report": review_report, "receipts": receipts,
            "venue": venue, "served_sha": audit.get("served_sha") or venue_facts.get("served_sha"),
            "codex_in": codex_in, "rounds": n, "check_rows": list(rows.values()),
            "audit": {k_: audit[k_] for k_ in ("false_greens", "false_reds")}}


def _audit(j, a, c, contract, wt, branch, base_sha, slug, note, evidence, seat_kw, kinds, rows, scores, gates, real,
           vextra, before, composed, timeout_min, loop_id):
    """The blind audit after the loop's last round (Q2, Q9): one seat drives every item once,
    the battery runs at k=3, and the disagreements with the checks are journaled. Its reds go
    to the builder for one more round; then the checks rerun on their cases, the missed sets
    run again and the audit re-drives only its own reds (Q8). Returns (audit, rows, scores,
    gates, venue name). Each step is journaled under this loop's id, so a resume drives,
    fixes and re-drives nothing twice."""
    key = f"audit-{branch}-{loop_id}"
    asked = [it["item"] for it in kinds["model"] + kinds["checked"] + kinds["audited"]]
    order = {i: n for n, i in enumerate(it["item"] for it in contract["items"])}
    asked.sort(key=lambda i: order.get(i, len(order)))

    def drive_once(ids, key):
        """Every case of `ids` driven once by one audit seat on one venue, the battery first on
        that venue. The seat pages its own work in its progress file and is relaunched on it at
        the context ceiling. Returns (rows, battery scores, the venue record, its facts)."""
        k = int(((c or {}).get("battery") or {}).get("k") or LOOP_K) if battery.new_only(c) else AUDIT_K
        model = [it for it in kinds["model"] if it["item"] in set(ids)]
        v = standin.venue_up(j, slug, wt, real_llm=real, key=key)
        if not v.get("venue"):
            why = f"stand-in did not come up: {str(v.get('error'))[:200]}"
            j.append("venue", state="failed", stage="audit", why=why)
            return ([{"item": i, "state": "UNMEASURED", "substate": "stack-failed", "observed": why, "seen": why}
                     for i in ids], {}, None, {})
        vf = _facts_for_drive(contract, c, note, v, vextra, before)
        vf["Mode"] = "audit: drive every item below once, blind to the diff and to the checks"
        got = run_battery(j, c, model, vf["The venue"], wt, evidence, base_sha, k, timeout_min)
        standin.model_on(j, v, wt, real)
        vf["Contract items"] = [it for it in contract["items"] if it["item"] in set(ids)]
        res = drive_mod.run(j, vf, v, wt, base_sha, contract["issue"], seat_kw, c, ids, scores=got)
        report = res.get("report") or {}
        now = policy.identity(gitops.head_of(wt), contract, vf["The venue"], j.fact("harness"))
        return policy.stamp_served(report.get("items") or [], report, now), got, v, standin.venue_facts(v)

    def rerun_checks(ids, venue_facts):
        out = {}
        if not ids or not venue_facts:
            return out
        identity = policy.identity(gitops.head_of(wt), contract, venue_facts, j.fact("harness"))
        for r in checks.run(c, wt, kinds["checked"], venue_facts, Path(evidence) / "checks" / "audit", identity, ids=ids,
                            composed=composed, timeout_min=CHECKS_MIN, note=j.append):
            recurrence.note(j, r["case"], r.get("line"), "audit", gitops.head_of(wt), branch=branch,
                            red=r.get("state") == "BROKEN")
            out[r["case"]] = r
        return out

    def first_drive():
        seen, got, v, vf = drive_once(asked, "venue-up-audit")
        if (v or {}).get("venue"):
            standin.venue_down_keep(j, v["venue"], wt, "venue-down-audit")
        return {"seen": seen, "scores": {i: r for i, (r, _says) in got.items()}, "venue": (v or {}).get("venue"),
                "served_sha": (vf or {}).get("served_sha")}

    drove = j.op(f"{key}-drive", first_drive)
    seen, venue, served_sha = drove["seen"], drove["venue"], drove.get("served_sha")
    scores.update({i: (r, {}) for i, r in drove["scores"].items()})
    dis = policy.audit_disagreements(list(rows.values()), seen)
    # a checked case whose rule the audit never drove is UNMEASURED by it; named so the denominator reads honest (Q9)
    reached = {r.get("item") for r in seen}
    j.append("audit-disagreements", branch=branch, false_greens=len(dis["false_greens"]), false_reds=len(dis["false_reds"]),
             cases=len(seen), disagreed=dis, excluded=sorted(cid for cid, r in rows.items() if r.get("rule") not in reached))
    ended = recurrence.ended_ids(j, branch)
    ended_items = {r.get("rule") for cid, r in rows.items() if cid in ended}
    # the battery's rows stand in the seat's report for the model items, so its misses are here too
    reds = [r for r in seen if r.get("state") == "BROKEN" and r.get("item") not in ended_items]
    if reds:
        tip = gitops.head_of(wt)
        done = reducer.landed(j, "fix-audit", tip, leg_key=key)
        if done is None:
            j.intent("fix-audit", key=key, branch=branch, tip=tip)
            own, _ = _venue_for_fix(j, slug, wt, None, None, real, False, "venue-up-fix-audit")
            fix = fixing.fix_pass(j, contract, note, wt, base_sha, seat_kw, reds=[
                {"item": r.get("item"), "line": r.get("observed")} for r in reds], extra={
                **_venue_fact(own),
                "Round": "the audit"})
            if (own or {}).get("venue"):
                standin.venue_down_keep(j, own["venue"], wt, "venue-down-fix-audit")
            j.append("fix-round", stage="audit", result=fix["result"])
            done = j.done("fix-audit", key=key, branch=branch, tip_in=tip, tip_out=seating.left_at(fix, wt), result=fix["result"])
        if done.get("tip_out") != done.get("tip_in"):
            integrate.expect(j, wt, branch, contract["items"])
            gitops.push(j, wt, branch, a.shadow, "push-audit")
            fast = gating.measure(j, wt, slug, base_sha, gitops.changed_paths(wt, base_sha), c, "gates-audit")
            # the fast gates measured on the fixed tip stand over the loop's rows of the same name
            gates = list({g["name"]: g for g in list(gates) + list(fast)}.values())
            again = [r.get("item") for r in reds]

            def redrive():
                redriven, got2, v, vf2 = drive_once(again, "venue-up-audit-redrive")
                reran = rerun_checks([x["id"] for it in kinds["checked"] if it["item"] in set(again)
                                      for x in it.get("cases") or []], vf2)
                if (v or {}).get("venue"):
                    standin.venue_down_keep(j, v["venue"], wt, "venue-down-audit-end")
                return {"seen": redriven, "scores": {i: r for i, (r, _says) in got2.items()}, "rows": reran,
                        "venue": (v or {}).get("venue"), "served_sha": (vf2 or {}).get("served_sha")}

            again_drove = j.op(f"{key}-redrive", redrive)
            scores.update({i: (r, {}) for i, r in again_drove["scores"].items()})
            for cid, r in again_drove["rows"].items():
                rows[cid] = _kept_red(rows.get(cid), r)
            seen = policy.merge_redrive(seen, again_drove["seen"], again)
            venue = again_drove["venue"] or venue
            served_sha = again_drove.get("served_sha") or served_sha
    j.append("audit", branch=branch, asked=asked, reds=[r.get("item") for r in reds], false_greens=len(dis["false_greens"]),
            false_reds=len(dis["false_reds"]))
    return dict(dis, receipts=seen, served_sha=served_sha), rows, scores, gates, venue


def review_loop(j, a, c, contract, wt, branch, base_sha, note, seat_kw, stops, findings_in=reader.NO_SECOND_READER,
                finds=None):
    """A slice inside a wave: the whole-diff review sets the work list, the builder relaunched
    takes it, and each read after a fix reads the diff since its last read (Q5), until no
    finding is open, every open one ended by name (still open after two fixes, Q8), or a fix
    moves nothing. No venue and no gates here; the composed branch is where this slice's rows
    can be true. Returns (review report, open findings)."""
    findings, review_report = [], None
    n = 0
    while not stops:
        n += 1
        tip = gitops.head_of(wt)
        if n == 1:
            review, findings = review_once(j, contract, note, wt, base_sha, seat_kw,
                                       {"FINDINGS_IN": findings_in, **_full_read(contract, c, wt, base_sha, tip, False, finds)},
                                       stops)
            review_report = review.get("report")
            approve_checks(j, c, wt, branch, review_report)
            j.phase("review", round=n, findings=len((review.get("report") or {}).get("findings") or []), fix=len(findings))
        else:
            review, findings = _next_read(j, c, contract, note, wt, base_sha, seat_kw, tip, stops,
                                          last_read_tip(j, base_sha))
            if review["result"] == "ok":
                review_report = review.get("report") or review_report
        ended = recurrence.ended_ids(j, branch)
        for f in findings:
            if str(f.get("id")) not in ended:
                recurrence.note(j, str(f.get("id")), f.get("summary") or f.get("title"), n, tip if n > 1 else None,
                                branch=branch, red=True)
        starts = [f for f in findings if not recurrence.end_if_stuck(j, str(f.get("id")), branch)]
        if stops or not starts:
            over = sorted(recurrence.ended_ids(j, branch))
            j.append("review-loop", round=n, result="stopped" if stops else "ended" if over else "clean",
                     open=[f.get("id") for f in findings], ended=over or None)
            break
        fix = fixing.fix_pass(j, contract, note, wt, base_sha, seat_kw, findings=starts, extra={
            **discovery.finds_as_fix_facts(finds),
            "Round": n})
        j.append("fix-round", stage=f"review-r{n}", result=fix["result"])
        if fix["result"] != "ok" or seating.left_at(fix, wt) == tip:
            j.append("review-loop", round=n, result="moved-nothing", fix=fix["result"])
            break
        gitops.push(j, wt, branch, a.shadow, f"push-review-r{n}")
    return review_report, findings
