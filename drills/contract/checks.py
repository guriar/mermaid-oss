#!/usr/bin/env python
"""Drill for hb.run.checks and the case boundary (Q1, A14, A18): a fake product with three
rules, two checks and one case with none; a fake runner; an approved check edited after its
approval; a contract with a case that names no boundary. Offline, in a throwaway repo.

Run: python engine/drill_checks.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
os.environ.setdefault("HB_MEMORY_FREE_GB", "64")  # the drills measure the engine, never this host's memory
os.environ.setdefault("HB_MEMORY_TOTAL_GB", "32")
os.environ.setdefault("HB_SETTLE_S", "0.2")
os.environ.setdefault("HB_SETTLE_MAX_S", "1")
os.environ.setdefault("HB_FORCE_AFTER_S", "1")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ROOT / "drills"))
from hb.runner import schemas

PASS, FAIL = [], []

RUNNER = """import json, sys
from pathlib import Path
ids, venue, out = sys.argv[1].split(","), sys.argv[2], Path(sys.argv[3])
assert json.loads(Path(venue).read_text())["venue"] == "drill-venue"
out.mkdir(parents=True, exist_ok=True)
print("starting the browser")
for cid in ids:
    if cid == "B-01":
        print(json.dumps({"case": cid, "ok": False, "line": "expected 200 on POST /pull, got 409"}))
    else:
        print(json.dumps({"case": cid, "ok": True}))
"""

CHECK = """# {rule}: one block per case, setup then assert
def {fn}(page):
    # setup: open the venue and pull two gallons of milk
    # assert ({boundary}): the count reads 2
    pass
"""


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:400]})" if not cond else ""))


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def contract(cases_by_rule):
    return {"schema": "contract/v2", "wave": "w", "states": [], "slices": [{"key": "s", "gets": "a user gets counts"}],
            "rules": [{"id": rid, "rev": 1, "slice": "s", "says": f"rule {rid} holds", "proof": "test", "states": ["all"],
                       "cases": cases} for rid, cases in cases_by_rule.items()]}


def hb(repo, *args):
    p = subprocess.run([sys.executable, str(repo / "hb.py"), *args], capture_output=True, text=True, cwd=repo,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    lines = (p.stdout or "").strip().splitlines()
    try:
        return p.returncode, json.loads(lines[-1]) if lines else {}
    except ValueError:
        return p.returncode, {"_raw": p.stdout, "_err": p.stderr}


def make_repo(tmp):
    repo = tmp / "repo"
    repo.mkdir()
    shutil.copy(ENGINE / "hb.py", repo / "hb.py")
    shutil.copytree(ENGINE / "hb", repo / "hb", ignore=shutil.ignore_patterns("__pycache__"))
    py = Path(sys.executable).as_posix()
    write(repo / ".claude" / "harness-config.json", json.dumps({
        "repo": "drill/drill", "default_branch": "main", "state_dir": ".context/harness",
        "paths": {"checks": "product/checks/", "manifest_dir": "docs/manifests",
                  "ledger_file": "docs/ledger/delivery.jsonl"},
        "checks": {"command": f'"{py}" tools/run_checks.py {{ids}} {{venue}} {{out}}'}}))
    write(repo / "tools" / "run_checks.py", RUNNER)
    write(repo / "product" / "checks" / "R-A-1.py", CHECK.format(rule="R-A-1", fn="A_01", boundary="screen"))
    write(repo / "product" / "checks" / "R-B-1.py", CHECK.format(rule="R-B-1", fn="B_01", boundary="wire"))
    write(repo / "docs" / "manifests" / "w.json", json.dumps(contract({
        "R-A-1": [{"id": "A-01", "when": "a user pulls milk", "then": "the count reads 2", "boundary": "screen"}],
        "R-B-1": [{"id": "B-01", "when": "the pull posts", "then": "the server answers 200", "boundary": "wire"}],
        "R-C-1": [{"id": "C-01", "when": "a supplier is closed", "then": "the order waits", "boundary": "both"},
                  {"id": "C-02", "when": "the sibling slice's card shows", "then": "it carries the count",
                   "boundary": "screen", "after_compose": True}]})))
    git = ["git", "-c", "user.name=d", "-c", "user.email=d@d"]
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    subprocess.run(git + ["add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(git + ["commit", "-q", "-m", "seed"], cwd=repo, capture_output=True)
    return repo


def approve(repo, *paths):
    """The runner's own approval, as the loop pins what the review approved."""
    from hb.contract import checks
    c = json.loads((repo / ".claude" / "harness-config.json").read_text(encoding="utf-8"))
    return {"approved": checks.approve(c, repo, list(paths))}


def run_checks(repo, venue, ids=None, composed=False):
    """The runner's own checks run over the contract's cases, as the loop runs them."""
    from hb.contract import checks
    from hb.contract import rules
    c = json.loads((repo / ".claude" / "harness-config.json").read_text(encoding="utf-8"))
    doc, _why = rules.read("w", repo)
    v = json.loads(venue)
    identity = {"tip": "t", "contract_hash": doc["_sha256"][:12], "harness": "working", "venue": v.get("venue")}
    out_dir = repo / ".context" / "checks" / "w"
    return {"rows": checks.run(c, repo, rules.as_items(doc), v, out_dir, identity, ids, composed)}


def by_case(d):
    return {r["case"]: r for r in d.get("rows") or []}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-checks-drill-"))
    repo = make_repo(tmp)
    venue = json.dumps({"venue": "drill-venue", "backend_url": "http://localhost:1"})

    print("drill 1: the contract with boundaries is signable")
    rc, d = hb(repo, "contract", "check", "--wave", "w")
    check("bounded contract is signable as contract/v2", rc == 0 and d.get("cases_era") == "contract/v2", d)

    print("drill 2: two checks approved, the runner prints two rows and one no-check")
    d = approve(repo, "product/checks/R-A-1.py", "product/checks/R-B-1.py")
    check("approve pins two files", len(d.get("approved") or {}) == 2, d)
    d = run_checks(repo, venue)
    rows = by_case(d)
    for cid, r in rows.items():
        print(f"    {cid} {r['state']} {r['substate'] or ''} {r['line'] or ''}".rstrip())
    check("A-01 WORKS", rows.get("A-01", {}).get("state") == "WORKS", rows.get("A-01"))
    check("B-01 BROKEN with the failing line", rows.get("B-01", {}).get("state") == "BROKEN"
          and rows["B-01"]["line"] == "expected 200 on POST /pull, got 409", rows.get("B-01"))
    check("C-01 UNMEASURED no-check", (rows.get("C-01", {}).get("state"), rows.get("C-01", {}).get("substate"))
          == ("UNMEASURED", "no-check"), rows.get("C-01"))
    check("C-02 waits for the composed branch", rows.get("C-02", {}).get("substate") == "after-compose", rows.get("C-02"))
    check("every row carries the identity tuple", all(set(r["identity"]) == {"tip", "contract_hash", "harness", "venue"}
                                                      and r["identity"]["venue"] == "drill-venue" for r in rows.values()), rows)
    check("every row holds check-row/v1", not [p for r in rows.values() for p in schemas.check_check_row(r)],
          [p for r in rows.values() for p in schemas.check_check_row(r)])
    check("the rows are written to disk", (repo / ".context" / "checks" / "w" / "check-rows.jsonl").is_file())

    print("drill 3: an approved check edited after approval reads unapproved")
    f = repo / "product" / "checks" / "R-A-1.py"
    f.write_text(f.read_text(encoding="utf-8") + "    assert True  # loosened\n", encoding="utf-8")
    d = run_checks(repo, venue, ["A-01", "B-01"])
    rows = by_case(d)
    check("edited A-01 reads UNMEASURED unapproved", (rows.get("A-01", {}).get("state"), rows.get("A-01", {}).get("substate"))
          == ("UNMEASURED", "unapproved") and rows["A-01"]["said"] == "WORKS", rows.get("A-01"))
    check("untouched B-01 still BROKEN", rows.get("B-01", {}).get("state") == "BROKEN", rows.get("B-01"))
    check("--ids narrows to two rows", set(rows) == {"A-01", "B-01"}, rows)
    approve(repo, "product/checks/R-A-1.py")
    d = run_checks(repo, venue, ["A-01"])
    check("re-approved A-01 WORKS again", by_case(d).get("A-01", {}).get("state") == "WORKS", d)
    d = run_checks(repo, venue, ["C-02"], composed=True)
    check("composed run reaches C-02 and finds no check", by_case(d).get("C-02", {}).get("substate") == "no-check", d)

    print("drill 4: hb contract check refuses a case with no boundary")
    write(repo / "docs" / "manifests" / "w.json", json.dumps(contract({
        "R-A-1": [{"id": "A-01", "when": "w", "then": "t", "boundary": "screen"}],
        "R-B-1": [{"id": "B-01", "when": "w", "then": "t"}]})))
    rc, d = hb(repo, "contract", "check", "--wave", "w")
    named = [p for p in d.get("problems") or [] if "B-01" in p and "boundary" in p]
    print(f"    {named[0] if named else d}")
    check("refused, naming the rule and the case", rc == 1 and named and "R-B-1" in named[0], d)

    print("drill 5: an older contract with no boundary anywhere is reported, not refused")
    write(repo / "docs" / "manifests" / "w.json", json.dumps(contract({
        "R-A-1": [{"id": "A-01", "when": "w", "then": "t"}]})))
    rc, d = hb(repo, "contract", "check", "--wave", "w")
    check("older cases read as contract/v1 cases, no checks", rc == 0
          and d.get("cases_era") == "contract/v1 cases, no checks", d)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  {len(PASS)} passed, {len(FAIL)} failed" + (f": {FAIL}" if FAIL else ""))
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
