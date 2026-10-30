"""hb.run.schemas - what a seat's report must look like before the runner reads it.

Every report carries one envelope: schema, issue, run_id, attempt, seat, base_sha,
head_sha. The runner refuses a report whose head_sha is not the tree's HEAD at the
moment the seat returned, because a valid-looking report for an older commit is the
shape that shipped unreviewed code before. No third-party validator: the shapes are
small and the refusals must name the field in plain words.
"""


SEAT_SCHEMAS = {
    "build": "build-note/v1",
    "review": "review-findings/v3",
    "verify": "verify-report/v2",
}

ENVELOPE = ("schema", "issue", "run_id", "attempt", "seat", "base_sha", "head_sha")

SEVERITIES = ("P0", "P1", "P2")
VERIFY_STATES = ("WORKS", "BROKEN", "UNMEASURED")
BROKEN_SUBSTATES = ("failed", "mismatch", "not-built", "infeasible")
UNMEASURED_SUBSTATES = ("not-driven", "verifier-failed", "stack-failed", "before-capture")
BUILD_STATUSES = ("built", "built-differently", "left-out", "blocked")
# The stop list: a run stops for these and nothing else. A builder names one under
# `needs_authority`; a reviewer's `nonwaivable` finding is one of them; a red row whose words
# name one stops the run (verdict). Every other question the builder decides, and the PR shows
# it under "decided for you". These five every product has; config `stops.extra` adds its own.
STOP_KINDS = ("money", "migration", "dependency", "contradiction", "auth")
# `question` is the soft channel: the builder keeps going on its own reading and the
# question rides the report and the PR under "Yours to rule"; it never stops a run.
QUESTION = "question"


def stop_kinds():
    """The five stop kinds plus the product's `stops.extra`, read from the config once per call."""
    from hb.rig.common import cfg_or_empty
    extra = ((cfg_or_empty() or {}).get("stops") or {}).get("extra") or []
    return STOP_KINDS + tuple(str(k) for k in extra if str(k) not in STOP_KINDS)


def _need(obj, key, problems, where, kind=None):
    if key not in obj:
        problems.append(f"{where}: missing `{key}`")
        return None
    v = obj[key]
    if kind is not None and not isinstance(v, kind):
        problems.append(f"{where}: `{key}` must be {kind.__name__}")
        return None
    return v


def check_envelope(report, seat, run_id, attempt, head_sha, base_sha=None):
    """Problems with the envelope, as plain sentences. Empty means it holds."""
    problems = []
    if not isinstance(report, dict):
        return ["the report is not a JSON object"]
    for k in ENVELOPE:
        if k not in report:
            problems.append(f"envelope: missing `{k}`")
    if problems:
        return problems
    want = SEAT_SCHEMAS.get(seat)
    if report["schema"] != want:
        problems.append(f"envelope: schema is `{report['schema']}`, the {seat} seat writes `{want}`")
    if report["seat"] != seat:
        problems.append(f"envelope: seat is `{report['seat']}`, expected `{seat}`")
    if str(report["run_id"]) != str(run_id):
        problems.append(f"envelope: run_id `{report['run_id']}` is not this run `{run_id}`")
    if int(report["attempt"]) != int(attempt):
        problems.append(f"envelope: attempt {report['attempt']} is not this attempt {attempt}")
    if head_sha and not str(head_sha).startswith(str(report["head_sha"])[:7]) \
            and not str(report["head_sha"]).startswith(str(head_sha)[:7]):
        problems.append(f"envelope: head_sha `{report['head_sha']}` is not the tree's HEAD `{head_sha}`; "
                        "the report describes a different commit")
    if base_sha and str(report["base_sha"])[:7] != str(base_sha)[:7]:
        problems.append(f"envelope: base_sha `{report['base_sha']}` is not the run's base `{base_sha}`")
    return problems


def check_build(report):
    p = []
    items = _need(report, "items", p, "build-note", list) or []
    for i, it in enumerate(items):
        w = f"build-note items[{i}]"
        if not isinstance(it, dict):
            p.append(f"{w}: not an object"); continue
        _need(it, "item", p, w, str)
        st = _need(it, "status", p, w, str)
        if st and st not in BUILD_STATUSES:
            p.append(f"{w}: status `{st}` not in {BUILD_STATUSES}")
        before = _need(it, "before", p, w, str)
        if before is not None and not before.strip():
            p.append(f"{w}: `before` is empty; the observed-before line is required")
        if st and st != "built" and not (it.get("because") or "").strip():
            p.append(f"{w}: status `{st}` needs a `because`")
        if it.get("see") is not None and not isinstance(it.get("see"), str):
            p.append(f"{w}: `see` must be a string: where in the product a verifier finds this item")
    for k in ("decisions", "files", "needs_authority", "tests"):
        _need(report, k, p, "build-note", list)
    for k in ("for_user", "underneath"):
        if report.get(k) is not None and not (isinstance(report[k], list) and all(isinstance(x, str) for x in report[k])):
            p.append(f"build-note: `{k}` must be a list of sentences")
    authority_kinds = stop_kinds() + (QUESTION,)
    for i, na in enumerate(report.get("needs_authority") or []):
        if not isinstance(na, dict) or na.get("kind") not in authority_kinds:
            p.append(f"build-note needs_authority[{i}]: kind must be one of {authority_kinds}")
    for i, f in enumerate(report.get("files") or []):
        w = f"build-note files[{i}]"
        if not isinstance(f, dict):
            p.append(f"{w}: not an object"); continue
        for k in ("path", "owns", "expensive", "deleted"):
            _need(f, k, p, w, str)
    return p


def check_review(report):
    p = []
    if report.get("scope") is not None and not isinstance(report.get("scope"), str):
        p.append("review-findings: `scope` is what you read, in words")
    gv = _need(report, "goal_verdict", p, "review-findings", dict)
    if gv is not None:
        if not isinstance(gv.get("met"), bool):
            p.append("review-findings goal_verdict.met must be true or false")
        if not isinstance(gv.get("misses"), list):
            p.append("review-findings goal_verdict.misses must be a list")
    fs = _need(report, "findings", p, "review-findings", list) or []
    ids = set()
    for i, f in enumerate(fs):
        w = f"review-findings findings[{i}]"
        if not isinstance(f, dict):
            p.append(f"{w}: not an object"); continue
        fid = _need(f, "id", p, w, str)
        if fid in ids:
            p.append(f"{w}: duplicate id `{fid}`")
        ids.add(fid)
        sev = _need(f, "severity", p, w, str)
        if sev and sev not in SEVERITIES:
            p.append(f"{w}: severity `{sev}` not in {SEVERITIES}")
        has_q = isinstance(f.get("quote"), dict) and f["quote"].get("file")
        has_a = isinstance(f.get("absence"), dict) and f["absence"].get("where")
        if not has_q and not has_a:
            p.append(f"{w}: needs a `quote` or an `absence`")
        if f.get("status") not in ("open", "fixed"):
            p.append(f"{w}: status must be open or fixed")
        if not (f.get("title") or "").strip():
            p.append(f"{w}: `title` is empty; one plain sentence of what is wrong, for the owner")
    for key in ("approved_checks", "accepted_golden"):
        if report.get(key) is not None and not isinstance(report.get(key), list):
            p.append(f"review-findings: `{key}` is a list")
    return p


def _text(obj, key):
    return isinstance(obj.get(key), str) and bool(obj[key].strip())


def check_verify(report):
    p = []
    _need(report, "venue", p, "verify-report", str)
    _need(report, "served_sha", p, "verify-report", str)
    items = _need(report, "items", p, "verify-report", list) or []
    for i, it in enumerate(items):
        w = f"verify-report items[{i}]"
        if not isinstance(it, dict):
            p.append(f"{w}: not an object"); continue
        _need(it, "item", p, w, str)
        st = _need(it, "state", p, w, str)
        if st == "DIVERGED":
            p.append(f"{w}: a verifier never writes DIVERGED; the runner assigns it from a ruling")
        elif st and st not in VERIFY_STATES:
            p.append(f"{w}: state `{st}` not in {VERIFY_STATES}")
        sub = it.get("substate")
        if st == "BROKEN" and sub not in BROKEN_SUBSTATES:
            p.append(f"{w}: BROKEN needs a substate in {BROKEN_SUBSTATES}")
        if st == "UNMEASURED" and sub not in UNMEASURED_SUBSTATES:
            p.append(f"{w}: UNMEASURED needs a substate in {UNMEASURED_SUBSTATES}")
        if st == "WORKS" and sub:
            p.append(f"{w}: WORKS carries no substate")
        if not (it.get("observed") or "").strip():
            p.append(f"{w}: `observed` is empty")
        # the PR body prints `seen`, never a sentence mined from `observed`: a re-drive's
        # narration opened with "Re-driven because catalog.py changed" and that became
        # what the owner read as the result
        if not (it.get("seen") or "").strip():
            p.append(f"{w}: `seen` is empty; one plain sentence of what was seen, for the owner")
        if st in ("WORKS", "BROKEN") and not (it.get("situation") or "").strip():
            p.append(f"{w}: `situation` is required on WORKS and BROKEN")
        # The ledger refuses a WORKS row with no tenant and no evidence path; the report
        # carries both so the refusal lands on the seat, not on the ledger after the drive.
        ev = it.get("evidence")
        if ev is not None and (not isinstance(ev, list) or not all(isinstance(x, str) for x in ev)):
            p.append(f"{w}: `evidence` must be a list of file paths")
        if st == "WORKS" and not (it.get("tenant_id") or "").strip() and not (it.get("no_venue") or "").strip():
            p.append(f"{w}: WORKS needs `tenant_id`, the tenant the substance read was taken against, "
                     "or `no_venue` saying why nothing was read from a venue (a CI or tooling item)")
        if st == "WORKS" and not (ev or it.get("capture") or it.get("video")):
            # Graded, never refused: refusing the report re-ran a whole drive shard for one row.
            it["state"], it["substate"] = "UNMEASURED", "not-driven"
            it["observed"] = (it.get("observed") or "") + " [graded UNMEASURED: WORKS carried no evidence path]"
        if it.get("video") is not None and not isinstance(it.get("video"), str):
            p.append(f"{w}: `video` must be a path or null")
        found = it.get("found")
        if found is not None and (not isinstance(found, list) or not all(isinstance(x, str) for x in found)):
            p.append(f"{w}: `found` must be a list of sentences, each a case and what went wrong")
    return p


_BODY_CHECKS = {
    "build": check_build,
    "review": check_review,
    "verify": check_verify,
}


def check_report(report, seat, run_id, attempt, head_sha, base_sha=None):
    """Every problem with a seat's report, envelope first. Empty means valid."""
    problems = check_envelope(report, seat, run_id, attempt, head_sha, base_sha)
    if problems:
        return problems
    if seat == "build":
        return check_build(report)
    return _BODY_CHECKS[seat](report)


# ── check-row/v1: one case's row from `hb checks run` (hb.run.checks) ─────

CHECK_ROW = "check-row/v1"
CHECK_UNMEASURED = ("no-check", "unapproved", "after-compose", "no-runner", "no-result", "checks-runner")
CHECK_ROW_KEYS = ("schema", "rule", "case", "boundary", "state", "substate", "line", "check", "observed", "identity")


def check_check_row(row):
    """Problems with one check row, as plain sentences. Empty means it holds."""
    if not isinstance(row, dict):
        return ["check-row: not an object"]
    p = [f"check-row: missing `{k}`" for k in CHECK_ROW_KEYS if k not in row]
    if p:
        return p
    where = f"check-row {row['case']}"
    if row["schema"] != CHECK_ROW:
        p.append(f"{where}: schema is `{row['schema']}`, not `{CHECK_ROW}`")
    if row["state"] not in VERIFY_STATES:
        p.append(f"{where}: state `{row['state']}` not in {VERIFY_STATES}")
    if row["state"] == "BROKEN" and not str(row.get("line") or "").strip():
        p.append(f"{where}: BROKEN needs the failing `line`")
    if row["state"] == "UNMEASURED" and row["substate"] not in CHECK_UNMEASURED:
        p.append(f"{where}: UNMEASURED substate `{row['substate']}` not in {CHECK_UNMEASURED}")
    if not isinstance(row["identity"], dict):
        p.append(f"{where}: `identity` is the tip, contract_hash, harness and venue")
    return p


# ── discovery-report/v1 (Q2) ─────────────────────────────────────────────────
# Registered after the shared tables so this block stays one hunk of its own.
SEAT_SCHEMAS["discovery"] = "discovery-report/v1"


def check_discovery(report):
    """A find is a user's sentence and what went wrong; an empty list is the verdict
    "nothing new", never a missing one."""
    p = []
    _need(report, "served_sha", p, "discovery-report", str)
    finds = _need(report, "finds", p, "discovery-report", list) or []
    for i, f in enumerate(finds):
        w = f"discovery-report finds[{i}]"
        if not isinstance(f, dict):
            p.append(f"{w}: not an object"); continue
        for key in ("item", "said", "wrong"):
            if not _text(f, key):
                p.append(f"{w}: `{key}` is empty")
        if f.get("capture") is not None and not isinstance(f.get("capture"), str):
            p.append(f"{w}: `capture` must be a path or null")
    return p


_BODY_CHECKS["discovery"] = check_discovery
