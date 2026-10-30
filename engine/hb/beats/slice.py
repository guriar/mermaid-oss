"""hb.run.slice - the slice door.

`hb run slice #N [--wave <slug>]`. The contract is resolved behind the issue number and
checked against the tree before any seat starts. The builder has its own venue (Q3), stood
when admission has room, and is handed the slice issue verbatim, the contract whole and every
ruling; it plans its own work in its progress file. A builder that dies, or that reaches the
runtime's context ceiling, is relaunched on that progress file and the commit log, never from
item one.

Typed by the owner (standalone): then the one loop (hb.run.loop) on its own branch,
gates, review, drive, fix, until nothing is red or a round moves nothing; every item ends as
a ledger row, the slice ships red with its rows, and its PR body leads with the red rows.

Spawned by a wave (`--wave`): after the build, a review loop and the quick checks, then it
ends `built` with its builder's venue down, no testers, no suite and no PR. Its rows can only be true on the
composed branch, so the wave door measures them there (the owner's ruling: six slices
cut from one frozen start each ran a loop that could not converge on sibling-owned reds).
"""
import re
from pathlib import Path

from hb.venue import adapter
from hb.rig.common import branch_setting, canon, cfg, out, _P, report_text, state_dir
from hb.contract import contract as resolver
from hb.contract import policy
from hb.record import control
from hb.runner import stops as stop_ids
from hb.rig import gitops
from hb.runner import hbcall
from hb.runner import seating
from hb.gates import gating
from hb.venue import standin
from hb.beats import landing
from hb.record import record
from hb.beats import launch
from hb.beats import loop
from hb.runner import reader
from hb.contract import rehearsal
from hb.contract import battery
from hb.beats import discovery
from hb.beats import standing
from hb.record import manager_notes
from hb.beats.loop import open_findings
from hb.record.journal import LANDED, Journal, new_run_id


BUILD_ATTEMPTS = 3


def resumed(j):
    """What a resumed run already holds: the before phase, the build note when the build
    ended ok, and how many times it stopped."""
    import json as _json
    out = {"before": False, "note": None, "stops": 0}
    attempt = None
    for e in j.events():
        k = e.get("kind")
        if k == "phase" and e.get("phase") == "before":
            out["before"] = True
        if k == "phase" and e.get("phase") == "stopped":
            out["stops"] += 1
        if k == "seat" and e.get("seat") == "build" and e.get("state") == "ok":
            attempt = e.get("attempt")
        if k == "build" and e.get("result") == "ok" and attempt:
            rp = j.seat_dir("build", attempt) / "report.json"
            if rp.exists():
                try:
                    out["note"] = _json.loads(report_text(rp))
                except ValueError:
                    pass
    return out


def _prior_final(run_id):
    """What a finished run left for a repair: its base sha and tip, the builder's note folded
    across its seats, the before captures, and the receipts the run's drives graded. Every verify
    seat and the receipts the run landed with are folded oldest to newest, row by item: the full
    drive (a shard's seat, or the merged report over the shards) lays the rows down and each
    repair round's re-drive, which carries only the items it drove, overwrites its own rows and
    no other. A repair starts from the fold; an item no row speaks for is owed a drive."""
    import json as _json
    pj = Journal(run_id)
    if not pj.path.exists():
        return {"error": f"no run {run_id} under the runs directory"}
    ph = pj.read_phase()
    note = {"items": [], "decisions": [], "files": [], "needs_authority": [], "tests": [], "changelog": "", "docs": [],
            "for_user": [], "underneath": []}
    receipts, folded = [], {}
    seats = pj.dir / "seats"
    reports = [d / "report.json" for d in seats.iterdir()] if seats.exists() else []
    # the rows the run landed with sit in the same fold: the runner writes some itself (a
    # stand-in that never came up grades every row UNMEASURED with no verify seat), and a
    # repair that read only the seats called them nothing to do
    reports.append(pj.dir / "evidence" / "receipts.json")
    for rp in sorted((p for p in reports if p.exists()), key=lambda p: p.stat().st_mtime):
        try:
            rep = _json.loads(report_text(rp))
        except ValueError:
            continue
        name = rp.parent.name
        if name.startswith("build-"):
            for k in ("items", "decisions", "files", "needs_authority", "tests", "docs", "for_user", "underneath"):
                note[k] += rep.get(k) or []
            note["changelog"] = rep.get("changelog") or note["changelog"]
        elif (name.startswith("verify") or name == "evidence") and rep.get("items") and \
                not any(r.get("substate") == "before-capture" for r in rep["items"]):
            for r in rep["items"]:
                if r.get("item"):
                    folded[r["item"]] = r
    order = {it.get("item"): n for n, it in enumerate(note["items"]) if isinstance(it, dict)}
    receipts = sorted(folded.values(), key=lambda r: order.get(r.get("item"), len(order)))
    before = {}
    for r in receipts:
        png = pj.dir / "evidence" / "before" / f"{r.get('item')}-desktop.png"
        if png.exists():
            before[r.get("item")] = str(png)
    return {"base_sha": ph.get("base_sha"), "tip": ph.get("tip"), "note": note, "receipts": receipts,
            "before": before}


def _walked_tips(branch):
    """The tips every finished run on `branch` drove and graded: a run's final phase names
    the commit its Walk status was posted on. A repair that names an older run must not read
    a tip a later run already walked as moved."""
    import json as _json
    tips = set()
    runs = state_dir() / "runs"
    for pp in runs.glob("*/phase.json") if runs.exists() else []:
        try:
            ph = _json.loads(pp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if ph.get("branch") == branch and ph.get("phase") in LANDED and ph.get("tip"):
            tips.add(ph["tip"])
    return tips


def venue_for_build(j, a, contract, slug, wt, own, key, changed):
    """The builder's own venue (Q3): the one standing, kept or reloaded at the tip, else a fresh
    one. Under admission's line the venue's grant waits, so a wave of three goes serial on its
    own and no builder works blind (Q3, A15).
    Returns {"v": venue record, "tip": the tip it serves} or {} for no venue."""
    if a.no_venue:
        return {}
    v = standin.ensure(j, slug, wt, own.get("v"), own.get("tip"), real_llm=bool(a.real_llm) or
                       policy.needs_real_model_for(contract["items"], changed), key=f"venue-up-build-{key}")
    if not (v or {}).get("venue"):
        j.append("venue", state="failed", stage="build", at=key, why=(v or {}).get("error"))
        return {}
    return {"v": v, "tip": gitops.head_of(wt)}


def builder_venue_down(j, wt, own):
    """The builder's venue goes down with its data kept, so the drive after the build
    stands on the situation the builder made (Q3: leased and swept when the builder ends)."""
    if (own.get("v") or {}).get("venue"):
        standin.venue_down_keep(j, own["v"]["venue"], wt, "venue-down-build")


def build(j, contract, wt, base_sha, seat_kw, ruled=None, own=None):
    """One builder seat on the whole contract, relaunched on its own progress file and the
    commit log when it dies. `ruled` is {stop id: stop and answer} when the build
    runs again because its authority stops were answered by id; `own` is the builder's own
    venue, when one stands. Returns (result, note)."""
    last = None
    venue = standin.venue_facts((own or {}).get("v")) if (own or {}).get("v") else None
    env = {"HB_BROWSER_PROFILE": str(venue.get("browser_profile") or "")} if venue else None
    for attempt in range(1, BUILD_ATTEMPTS + 1):
        facts = {}
        if ruled:
            facts["Ruled since the build stopped"] = {
                "what": "the build stopped on a question only the owner could answer, and each stop below "
                        "carries the answer; the commit log shows what is built: change what the ruling "
                        "changes, build what it unblocks, and do not raise the answered question again",
                "stops": ruled}
        if attempt > 1:
            facts["Resume"] = (f"attempt {attempt}: the builder before you ended {last}; the tree, the commit log "
                               "and your progress file are as it left them")
        if venue:
            facts["The venue"] = venue
        res = seating.sit(j, "build", seating.builder_facts(contract, facts), wt, base_sha, contract["issue"],
                          extra_env=env, **seat_kw)
        j.append("build", attempt=attempt, result=res["result"], tip=gitops.head_of(wt))
        if res["result"] == "ok":
            return res, res["report"]
        last = res["result"]
    return res, None


def cited_evidence(r, evidence, fallback=None):
    """The receipt's capture and evidence files that exist on disk. The verifier writes them
    relative to the run's evidence directory, so a relative path is resolved there; the
    `fallback` file is cited when none exists."""
    cited = [str(r["capture"])] if r.get("capture") else []
    cited += [str(x) for x in (r.get("evidence") or []) if isinstance(x, str)]
    found = []
    for path in cited:
        p = Path(path)
        if not p.is_absolute() and evidence is not None and not p.exists():
            p = Path(evidence) / path
        if p.exists() and str(p) not in found:
            found.append(str(p))
    if not found and fallback and Path(fallback).exists():
        found.append(str(fallback))
    return found


def _revs_at_tip(contract, wt):
    """Each item's rev as the manifest in the run's own tree carries it at the tip. The
    contract was read at the base commit, and a branch that revised an item's words bumped
    its rev after; a row booked at the base rev reads the revised item as undelivered."""
    revs = {it["item"]: it.get("rev") or 1 for it in contract["items"]}
    if not contract.get("manifest"):
        return revs
    from hb.contract.manifest import _manifest_items, _manifest_read
    doc, err = _manifest_read(Path(str(contract["manifest"])).stem, Path(wt))
    if err:
        return revs
    for it in _manifest_items(doc):
        if isinstance(it, dict) and str(it.get("id")) in revs and it.get("rev"):
            revs[str(it["id"])] = it["rev"]
    return revs


def _ledger_rows(j, contract, receipts, wt, tip, venue, slug, wave, evidence=None, served_sha=None):
    """One ledger row per item through `hb ledger receipt` then `hb ledger append`;
    a refused row is journaled, never dropped. A slice outside a wave books under the
    wave slug `standalone`, the ledger's own name for it. `served_sha` is the venue
    record's own, never a probe; absent, the row carries none."""
    refused = []
    n = contract["issue"].lstrip("#")
    wave_slug = wave or "standalone"
    if not receipts:
        j.append("ledger", rows=0, refused=[], why="no receipts: the drive was skipped or the stand-in never came up")
        return refused
    revs = _revs_at_tip(contract, wt)
    for r in receipts:
        args = ["ledger", "receipt", "--issue", n, "--item", str(r.get("item")), "--rev", str(revs.get(r.get("item"), 1)),
                "--state", str(r.get("state")), "--sha", r.get("served_sha") or tip, "--slug", slug, "--source", "slice",
                "--wave", wave_slug]
        if r.get("tenant_id"):
            args += ["--tenant-id", str(r["tenant_id"])]
        elif r.get("no_venue"):
            args += ["--no-venue", str(r["no_venue"])]
        if served_sha:
            args += ["--served-sha", str(served_sha)]
        if r.get("substate"):
            args += ["--substate", str(r["substate"])]
        if r.get("ruling"):
            args += ["--ruling", str(r["ruling"])]
        if venue:
            args += ["--venue", str(venue)]
        for k in ("observed", "situation", "plan"):
            if r.get(k):
                val = str(r[k])
                if k == "observed" and r.get("carried"):
                    val = "carried, not re-driven after the repair diff: " + val
                args += [f"--{k}", val]
        for path in cited_evidence(r, evidence):
            args += ["--evidence", path]
        rc, js, text = hbcall.hb(args, cwd=wt, timeout=120)
        if rc != 0:
            refused.append({"item": r.get("item"), "why": (js.get("error") if isinstance(js, dict) else None) or text[-200:]})
    # --issues scopes the fold to THIS slice: without it the append folds every pending
    # receipts file under the wave, and `standalone` is every standalone slice ever run
    # (run 20260922-010153 committed 23 rows of three other issues)
    rc, js, text = hbcall.hb(["ledger", "append", "--issue", n, "--issues", n, "--slug", slug, "--wave", wave_slug],
                       cwd=wt, timeout=120)
    if rc != 0:
        refused.append({"item": "append", "why": (js.get("error") if isinstance(js, dict) else None) or text[-200:]})
    j.append("ledger", rows=len(receipts) - len([x for x in refused if x["item"] != "append"]), refused=refused)
    ledger = _P("ledger_file")
    rc, st, _ = gitops.git(wt, "status", "--porcelain", "--", ledger)
    if (st or "").strip():
        gitops.git(wt, "add", ledger)
        gitops.git(wt, "-c", "user.name=hb", "-c", "user.email=hb@local", "commit", "-q", "-m",
             f"ledger: rows for {contract['issue']} at {tip[:9]}")
    return refused


def _changelog(j, c, contract, note, wt, tip):
    """The slice's changelog lines, committed on its branch: every PR says what it did."""
    lines = (note or {}).get("changelog_lines") or [x for x in [(note or {}).get("changelog")] if x]
    path = _P("changelog_file")
    written = manager_notes.write_changelog(wt, contract, lines, path)
    j.append("changelog", lines=written, file=path)
    if written:
        gitops.git(wt, "add", path)
        gitops.git(wt, "-c", "user.name=hb", "-c", "user.email=hb@local", "commit", "-q", "-m",
                   f"changelog: what {contract['issue']} changes, at {tip[:9]}")


def run_slice(a):
    c = cfg()
    root = canon()
    j = Journal(a.resume) if a.resume else Journal(new_run_id("slice", a.ref))
    j.open_run("slice", migrate=a.migrate_harness, ref=a.ref, shadow=bool(a.shadow), runtime=a.runtime, wave=a.wave)
    if not re.match(r"^#?\d+$", a.ref.strip()):
        j.phase("stopped", why="the slice door opens by issue number")
        return out({"verdict": "stopped", "why": "the slice door opens by issue number", "run_id": j.run_id}, 2)
    n = a.ref.strip().lstrip("#")
    evidence = state_dir() / "runs" / j.run_id / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    stops = []
    seat_kw = dict(runtime=a.runtime, timeout_min=a.seat_timeout_min, shadow=a.shadow, evidence=evidence,
                   rate_limit_wait_min=a.rate_limit_wait_min)

    # worktree: a slice branch, off dev or off the wave branch
    base_ref = None
    if a.wave:
        wave_branch = f"{branch_setting(c, 'integration_prefix')}{a.wave}"
        rc_o, _, _ = gitops.git(root, "rev-parse", "--verify", "--quiet", f"origin/{wave_branch}")
        # a shadow wave never pushed its branch; the local one is the base then
        base_ref = f"origin/{wave_branch}" if rc_o == 0 else wave_branch
    slug = j.fact("slug") or f"issue-{n}"
    tree = gitops.worktree(j, slug, root, c, a.shadow, branch_prefix="slice/", dir_prefix="run-slice-", base_ref=base_ref, prepare=adapter.prepare_worktree)
    if tree.get("error"):
        j.phase("stopped", why=tree["error"])
        return out({"verdict": "stopped", "why": tree["error"], "run_id": j.run_id}, 2)
    wt, branch, base_sha = Path(tree["worktree"]), tree["branch"], tree["base_sha"]
    j.phase("worktree", worktree=str(wt), branch=branch, base_sha=base_sha, wave=a.wave)

    # contract, checked against the worktree's tree before any seat
    # read inside the run's own tree at the base commit, never the main checkout, which
    # may sit behind the landing branch or carry another session's edits
    contract = resolver.for_run(j, a.ref, evidence, wt, require_issue=True)
    if contract.get("problems"):
        why = "the contract cannot be run as filed: " + "; ".join(str(p) for p in contract["problems"][:6])
        j.phase("stopped", why=why, problems=contract["problems"])
        return out({"verdict": "stopped", "stage": "contract", "why": why, "problems": contract["problems"],
                    "run_id": j.run_id, "contract": contract.get("contract_path")}, 2)
    if contract.get("studio"):
        # the sitting's frozen screens are the slice's starting code (hb.run.rules `studio`)
        default = branch_setting(c, "landing")
        moved = gitops.start_from_studio(j, wt, contract["studio"], base_ref or f"origin/{default}", default, base_sha)
        if moved.get("error"):
            why = moved["error"] + (f": {', '.join(moved['conflict'])}" if moved.get("conflict") else "")
            stop_ids.raise_stop(j, stops, "other", stop_ids.other_id(why), why)
            j.phase("stopped", why=why, stops=stops)
            return out({"verdict": "stopped", "stage": "studio", "why": why, "stops": stops, "run_id": j.run_id}, 2)
        base_sha = moved["base_sha"]
        j.phase("worktree", worktree=str(wt), branch=branch, base_sha=base_sha, wave=a.wave, studio=moved["studio"])
    j.phase("contract", slug=slug, issue=contract["issue"], goal=contract["goal"], source=contract["source"],
            items=[it["item"] for it in contract["items"]])
    launch.announce(j, a, c, contract)
    if a.resume and standing.saved(j):
        ctx = standing.saved(j)
        return standing.resume(j, a, c, contract, _lander(j, a, c, contract, ctx, evidence), seat_kw)

    if a.brief and a.repair:
        # a repair brief names what no row holds; it is a stop with an id, routed to the first fixer
        stop_ids.record_conductor(j, a.brief)
    elif a.brief and a.brief.strip() not in control.answers(j.dir):
        # the owner's brief is an answer every seat that starts after it reads
        with open(control.path(j.dir), "a", encoding="utf-8") as f:
            f.write(f"answer: {a.brief.strip()}\n")
    if a.repair:
        # a finished run's branch re-entered at its red rows: no build, no whole-branch
        # review; the repair loop and the suite, then the same landing
        prior = _prior_final(a.repair)
        if prior.get("error"):
            j.phase("stopped", why=prior["error"])
            return out({"verdict": "stopped", "why": prior["error"], "run_id": j.run_id}, 2)
        base_sha = prior["base_sha"] or base_sha
        # the branch as origin holds it is the state: a dev merge by hand after a sibling
        # landed moves the tip, and the repair re-enters that tip, not the one the run left
        sync = {} if a.shadow else gitops.sync_to_origin(wt, branch)
        if sync.get("error"):
            j.phase("stopped", why=sync["error"])
            return out({"verdict": "stopped", "why": sync["error"], "run_id": j.run_id}, 2)
        tip = gitops.head_of(wt)
        # a BROKEN row and a row nobody could measure are both owed a drive again
        grey = {it["item"] for it in contract["items"] if policy.grey_at_filing(it)}
        reds = [r.get("item") for r in prior["receipts"] if r.get("state") == "BROKEN"]
        reds += policy.owed_unmeasured(prior["receipts"], contract["items"])
        # a tip the last drive never saw owes every driven row a walk again: the Walk status
        # names one commit, and the rows carried from an older one would vouch for a tree
        # nobody drove; test-proven and grey rows carry, the fixer sits only if a row breaks
        walked = _walked_tips(branch) | ({prior["tip"]} if prior["tip"] else set())
        moved = bool(walked) and tip not in walked
        redrive = [r.get("item") for r in prior["receipts"]
                   if moved and r.get("item") not in grey and r.get("by") != "test" and r.get("item") not in reds]
        touched = gitops.changed_paths(wt, prior["tip"], tip) if moved else []
        # a finding the second reader's late gate adopted after the PR is owed like a red row
        findings, review = open_findings(Journal(a.repair))
        j.phase("repair", prior_run=a.repair, prior_tip=prior["tip"], tip=tip, moved=moved, synced=sync.get("moved", False),
                base_sha=base_sha, reds=reds, redrive=redrive, touched=len(touched), findings=len(findings))
        if not reds and not findings and not redrive:
            j.phase("stopped", why=f"nothing to repair: run {a.repair} left no blocking red row and no serious finding, "
                                   f"and the tip has not moved since its drive")
            return out({"verdict": "stopped", "why": "nothing to repair", "run_id": j.run_id}, 2)
        st = loop.run_loop(j, a, c, contract, wt, branch, base_sha, slug, prior["note"], evidence, stops, seat_kw,
                           before=prior["before"], second_reader=False,
                           prior={"receipts": prior["receipts"], "findings": findings, "review": review,
                                  "asked": redrive, "tip": prior["tip"]})
        return _ship(j, a, c, contract, wt, branch, base_sha, slug, evidence, prior["note"], prior["before"], stops, st, seat_kw)

    # before captures at the base commit; a slice inside a wave takes none, the wave
    # captures at its own base
    prior = resumed(j)
    if a.resume:
        # gates are keyed on the tip they measured; a resume re-runs only what a moved tree needs
        j.append("resume", stops_before=prior["stops"], built=bool(prior["note"]))
    before = {} if a.wave else loop.capture_before(j, a, contract["items"], wt, slug, base_sha, evidence, seat_kw,
                                                   contract["issue"])

    # the build: one builder on the whole contract, relaunched on its progress file
    ruled = stop_ids.ruled_authority(j, prior["note"]) if prior["note"] else {}
    ruled = {sid: v for sid, v in ruled.items() if sid not in stop_ids.routed(j)}
    own = {}
    if prior["note"] and not ruled:
        note = prior["note"]
    else:
        own = venue_for_build(j, a, contract, slug, wt, own, "1", gitops.changed_paths(wt, base_sha))
        res, note = build(j, contract, wt, base_sha, seat_kw, ruled=ruled, own=own)
        stop_ids.mark_routed(j, [{"id": sid} for sid in ruled], "build")
        if note is None:
            builder_venue_down(j, wt, own)
            j.phase("stopped", why=f"build: {res['result']} after {BUILD_ATTEMPTS} attempts", problems=res.get("problems"))
            return out({"verdict": "stopped", "stage": "build", "why": res["result"],
                        "problems": res.get("problems"), "run_id": j.run_id}, 2)
    stop_ids.authority(j, stops, note)
    # one op, so a runner killed after discovery's fix round sits no second real-model seat
    finds = [] if stops else j.op(f"discovery-{slug}", lambda: _discover(j, a, c, contract, wt, branch, base_sha, slug,
                                                                         own, seat_kw, note))
    builder_venue_down(j, wt, own)
    j.phase("build", tip=gitops.head_of(wt))
    if stops:
        j.phase("stopped", why="needs authority", stops=stops)
        path = record.write_local(j, wt, contract, evidence, base_sha, note, stops, before=before)
        return out({"verdict": "stopped", "stage": "authority", "stops": stops, "run_id": j.run_id, "record": path}, 2)

    gitops.push(j, wt, branch, a.shadow, "push-build")
    if a.wave:
        return _built(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, stops, seat_kw, finds)
    reh = rehearsal.before_drive(j, a, c, contract, wt, branch, base_sha, slug, seat_kw,
                                 real=bool(a.real_llm) or policy.needs_real_model_for(
                                     contract["items"], gitops.changed_paths(wt, base_sha)))
    st = loop.run_loop(j, a, c, contract, wt, branch, base_sha, slug, note, evidence, stops, seat_kw, before=before,
                       venue_rec=reh["venue"], finds=finds)
    return _ship(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, before, stops, st, seat_kw)


def _discover(j, a, c, contract, wt, branch, base_sha, slug, own, seat_kw, note):
    """The discovery seat on the builder's own venue after the build, when the contract has
    model items (Q2). Returns the finds, which the loop folds into the model items' rows;
    `own` is the builder's venue, stood again at the tip when the build left none."""
    if not any(battery.is_model(it) for it in contract["items"]):
        return []
    stood = venue_for_build(j, a, contract, slug, wt, own, "discovery", gitops.changed_paths(wt, base_sha))
    own.clear()
    own.update(stood)
    if not (own.get("v") or {}).get("venue"):
        j.append("discovery-finds", n=0, slice=slug, why="no venue stood for the discovery seat")
        return []
    return discovery.run_after_build(j, c, contract, wt, base_sha, slug, own["v"], seat_kw)


def _built(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, stops, seat_kw, finds=None):
    """A slice inside a wave ends here: the review loop, the quick checks recorded and never
    a stop, the report, and the verdict `built`. The wave measures its rows on the composed
    branch, where they can be true."""
    codex_in = {"fact": reader.NO_SECOND_READER}
    if reader.wanted(a):
        codex_in = reader.codex_read(j, wt, base_sha, contract, evidence, a.seat_timeout_min)
    review_report, open_ = loop.review_loop(j, a, c, contract, wt, branch, base_sha, note, seat_kw, stops,
                                            findings_in=codex_in["fact"], finds=finds)
    quick = []
    if not stops:
        quick = gating.measure(j, wt, slug, base_sha, gitops.changed_paths(wt, base_sha), c, "gates-built")
        red = [g["name"] for g in gating.gates_red(quick)]
        j.append("quick-checks", red=red, why="recorded for the wave's fix list; a slice in a wave never stops on a gate")
    gitops.push(j, wt, branch, a.shadow, "push-built")
    tip = gitops.head_of(wt)
    import json as _json
    (evidence / "receipts.json").write_text(_json.dumps({"issue": contract["issue"], "tip": tip, "items": []}, indent=2),
                                            encoding="utf-8")
    path = record.write_local(j, wt, contract, evidence, base_sha, note, stops, gates=quick, review=review_report,
                              codex_fact=codex_in["fact"])
    final = "stopped" if stops else "built"
    j.phase(final, tip=tip, stops=stops, wave=a.wave, open_findings=[f.get("id") for f in open_],
            quick_red=[g["name"] for g in gating.gates_red(quick)])
    return out({"verdict": final, "run_id": j.run_id, "branch": branch, "tip": tip, "wave": a.wave, "pr": None,
                "shadow": bool(a.shadow), "stops": stops, "receipts": {}, "open_findings": [f.get("id") for f in open_],
                "record": path, "journal": str(j.path)}, 0 if not stops else 2)


def _lander(j, a, c, contract, ctx, evidence):
    """The landing a standing pass calls: the same rows, report, PR refresh and Walk."""
    return lambda st, note, stops: _land(j, a, c, contract, Path(ctx["worktree"]), ctx["branch"], ctx["base_sha"],
                                         ctx["slug"], evidence, note, ctx["before"], stops, st)


def _ship(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, before, stops, st, seat_kw):
    res = _land(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, before, stops, st)
    ctx = standing.context("slice", a.ref, wt, branch, base_sha, slug, note, before, st, res)
    return standing.after_ship(j, a, c, contract, ctx, _lander(j, a, c, contract, ctx, evidence), res,
                               0 if not stops else 2, seat_kw)


def _land(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, before, stops, st):
    """The slice's booking (its receipts file, the changelog, a ledger row per item), then the
    one lander (landing.land): the same for a built run, a repaired one and a standing pass."""
    receipts, venue = st["receipts"], st["venue"]

    def book():
        # the rows as this run leaves them: a wave carries them instead of driving them again
        import json as _json
        here = str(evidence)
        kept = [dict(r, capture=str(Path(here) / r["capture"]) if r.get("capture") and not Path(str(r["capture"])).is_absolute() else r.get("capture"),
                     evidence=cited_evidence(r, evidence)) for r in receipts]
        (evidence / "receipts.json").write_text(_json.dumps({"issue": contract["issue"], "tip": gitops.head_of(wt), "items": kept},
                                                            indent=2, ensure_ascii=False), encoding="utf-8")
        # every item ends as a ledger row
        tip = gitops.head_of(wt)
        if not a.wave:
            _changelog(j, c, contract, note, wt, tip)
        return {"refused": _ledger_rows(j, contract, receipts, wt, tip, venue, slug, a.wave, evidence=evidence,
                                        served_sha=st.get("served_sha"))}
    return landing.land(j, a, c, contract, wt, branch, base_sha, slug, evidence, st, "slice", note, before, stops,
                        book=book, opens=not a.wave)
