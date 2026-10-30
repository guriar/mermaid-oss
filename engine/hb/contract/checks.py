"""hb.run.checks - the builder's checks, run by rule and case id (Q1, A14).

A check lives in the product, one file per rule under the folder the product config names
(`paths.checks`), and holds each of its rule's cases by id in two halves: `setup`, what the
agent did to make the situation, replayable, and `assert`, at the case's boundary. The
product's one runner (`checks.command`, placeholders {ids} {venue} {out}) runs the cases it is
given against a stand-in, one rule at a time, and prints one JSON line per case:

    {"case": "CB-08", "ok": true}
    {"case": "CB-09", "ok": false, "line": "the line that failed"}

Every case of the contract ends as one `check-row/v1` row (hb.run.schemas): WORKS, BROKEN with
the failing line, or UNMEASURED by name. A check is approved at a hash (`approved.json` in the
checks folder, path to sha256); an edited or never approved check still runs, and its row reads
UNMEASURED `unapproved` with what the check said, until the reviewer approves it (Q1).
docs/checks-contract.md is the product's side of this.
"""
import hashlib
import json
import re
import shlex
from pathlib import Path

from hb.runner import hbcall
from hb.runner.schemas import CHECK_ROW

from hb.rig.common import die

APPROVED = "approved.json"
RESULTS = "check-rows.jsonl"


def folder(c, root):
    checks_dir = ((c or {}).get("paths") or {}).get("checks")
    if not checks_dir:
        die("harness-config.json paths.checks is not set", 2)
    return Path(root) / str(checks_dir)


def command(c):
    """The runner command as argv, or None when the product names none."""
    cmd = str(((c or {}).get("checks") or {}).get("command") or "").strip()
    return shlex.split(cmd) if cmd else None


def digest(path):
    """sha256 of the file with line endings as LF, so a pin answers the same on every host."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _rel(path, root):
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def approvals(where):
    p = Path(where) / APPROVED
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def approve(c, root, paths):
    """Pin each check file at its hash now; returns {path: sha256} of what was pinned."""
    where = folder(c, root)
    pinned = approvals(where)
    done = {}
    for name in paths:
        f = Path(name) if Path(name).is_absolute() else Path(root) / name
        if not f.is_file():
            raise ValueError(f"{name} is not a file")
        if where.resolve() not in f.resolve().parents:
            raise ValueError(f"{name} is not under the checks folder {_rel(where, root)}")
        done[_rel(f, root)] = digest(f)
    pinned.update(done)
    where.mkdir(parents=True, exist_ok=True)
    (where / APPROVED).write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return done


def check_file(where, rule_id):
    """The one file that holds a rule's checks, named for the rule, or None."""
    if not Path(where).is_dir():
        return None
    for f in sorted(Path(where).iterdir()):
        if f.is_file() and f.stem == rule_id and f.name != APPROVED:
            return f
    return None


def holds_case(text, case_id):
    """True when the file names the case, as written or with `_` for `-` (a function name)."""
    for name in {case_id, case_id.replace("-", "_")}:
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            return True
    return False


def _argv(cmd, ids, venue_path, out_dir):
    fill = {"{ids}": ",".join(ids), "{venue}": str(venue_path), "{out}": str(out_dir)}
    argv = []
    for token in cmd:
        for key, value in fill.items():
            token = token.replace(key, value)
        argv.append(token)
    return argv


def parse_results(text):
    """{case id: (ok, failing line)} from the runner's JSON lines; any other line is progress."""
    got = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "case" in row and isinstance(row.get("ok"), bool):
            got[str(row["case"])] = (row["ok"], str(row.get("line") or "").strip() or None)
    return got


def _row(rule, case, state, substate, observed, identity, line=None, check=None, sha=None, said=None):
    return {"schema": CHECK_ROW, "rule": rule, "case": case.get("id"), "boundary": case.get("boundary"),
            "after_compose": bool(case.get("after_compose")), "state": state, "substate": substate, "line": line,
            "check": check, "check_sha256": sha, "said": said, "observed": observed, "identity": identity}


def _graded(rule, case, result, approved, check, sha, identity):
    """The row for one case the runner answered."""
    ok, line = result
    said = "WORKS" if ok else "BROKEN"
    if not approved:
        return _row(rule, case, "UNMEASURED", "unapproved", f"the check said {said}" + (f": {line}" if line else "")
                    + "; it is not approved at this hash, so it grades nothing until the reviewer approves it",
                    identity, line, check, sha, said)
    if ok:
        return _row(rule, case, "WORKS", None, f"the check at {check} passed", identity, None, check, sha, said)
    line = line or "the check failed and printed no line"
    return _row(rule, case, "BROKEN", "failed", line, identity, line, check, sha, said)


def run_rule(c, root, rule, cases, venue_path, out_dir, identity, composed, timeout_min, note=None):
    """The rows of one rule's cases: the runner called once for the cases its file holds."""
    where = folder(c, root)
    f = check_file(where, rule)
    text = f.read_text(encoding="utf-8", errors="replace") if f else ""
    check = _rel(f, root) if f else None
    sha = digest(f) if f else None
    approved = bool(f) and approvals(where).get(check) == sha
    rows, to_run = {}, []
    for case in cases:
        cid = case.get("id")
        if case.get("after_compose") and not composed:
            rows[cid] = _row(rule, case, "UNMEASURED", "after-compose", "the case needs a sibling slice's work; "
                             "it runs on the composed branch", identity, check=check, sha=sha)
        elif not f or not holds_case(text, cid):
            rows[cid] = _row(rule, case, "UNMEASURED", "no-check", f"no check for {cid}: "
                             + (f"{check} does not name it" if f else f"no file for {rule} under {_rel(where, root)}"),
                             identity)
        else:
            to_run.append(case)
    cmd = command(c)
    if to_run and not cmd:
        for case in to_run:
            rows[case["id"]] = _row(rule, case, "UNMEASURED", "no-runner", "the product names no checks runner "
                                    "(harness-config `checks.command`)", identity, check=check, sha=sha)
    elif to_run:
        mine = Path(out_dir) / rule
        mine.mkdir(parents=True, exist_ok=True)
        ids = [case["id"] for case in to_run]
        rc, o, e = hbcall.call(_argv(cmd, ids, venue_path, mine), cwd=root, timeout=timeout_min * 60,
                               kind="checks", holder=f"checks {rule}", c=c, note=note)
        (mine / "runner.out").write_text(o or "", encoding="utf-8")
        (mine / "runner.err").write_text(e or "", encoding="utf-8")
        got = parse_results(o)
        for case in to_run:
            cid = case["id"]
            if cid in got:
                rows[cid] = _graded(rule, case, got[cid], approved, check, sha, identity)
            elif rc != 0:
                rows[cid] = _row(rule, case, "UNMEASURED", "checks-runner", f"the checks runner exited {rc} with no "
                                 f"line for {cid}: {(e or o)[-200:].strip()}", identity, check=check, sha=sha)
            else:
                rows[cid] = _row(rule, case, "UNMEASURED", "no-result", f"the checks runner printed no line for {cid}",
                                 identity, check=check, sha=sha)
    return [rows[case.get("id")] for case in cases]


def run(c, root, items, venue, out_dir, identity, ids=None, composed=False, timeout_min=30, note=None):
    """One check-row/v1 row per case of `items` (contract items or rules, each with `cases`),
    narrowed to `ids` when given; `after_compose` cases run only when `composed`. The rows are
    written to `<out_dir>/check-rows.jsonl` and returned."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    venue_path = out_dir / "venue.json"
    venue_path.write_text(json.dumps(venue or {}), encoding="utf-8")
    wanted = set(ids or [])
    rows = []
    for it in items or []:
        cases = [x for x in it.get("cases") or [] if isinstance(x, dict) and (not wanted or x.get("id") in wanted)]
        if cases:
            rows += run_rule(c, root, it.get("item") or it.get("id"), cases, venue_path, out_dir, identity,
                             composed, timeout_min, note)
    (out_dir / RESULTS).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows
