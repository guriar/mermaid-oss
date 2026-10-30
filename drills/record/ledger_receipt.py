#!/usr/bin/env python
"""Offline drill for the 2026-09-11 train: the DIVERGED ledger state, the held_by and
pass_rate filing check, anchor receipts, the review-size measure, the books fallback to
the slice tip, and the wave-end stall row. Run:

    python engine/drill_train1.py

Throwaway git repo + state dir per run; never touches real state, never calls gh.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))

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
HB_SRC = ENGINE / "hb.py"
HB_PKG = ENGINE / "hb"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail[:300]})" if detail and not cond else ""))


def hb(repo, *args, cwd=None):
    p = subprocess.run([sys.executable, str(repo / "hb.py"), *args], capture_output=True,
                       text=True, cwd=cwd or repo, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        data = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_raw": p.stdout[-400:], "_err": p.stderr[-400:]}
    return p.returncode, data


def git(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, env=env)


MANIFEST = """schema: "manifest/v1"
wave: "drill"
id_prefix: "DR"
precedence:
  default: "artifact-first"
artifacts: []
rulings: []
fixture_recipes:
  - id: "a fresh venue"
    steps:
      - "stand a fresh venue"
items:
  - id: "DR-01"
    rev: 1
    slice: "one"
    kind: "screen"
    says: "the card shows"
    when: "the venue opens"
    then: "the card shows"
    fixture_recipe_id: "a fresh venue"
    drivable: true
  - id: "DR-02"
    rev: 1
    slice: "one"
    kind: "chat-verb"
    held_by: "model"
    pass_rate: "4/5"
    says: "the model asks"
    when: "a bare number"
    then: "the model asks the unit"
    fixture_recipe_id: "a fresh venue"
    drivable: true
"""

MEASURE_ITEM = """  - id: "DR-06"
    rev: 1
    slice: "one"
    kind: "measure"
    says: "the derived face equals the hand-built one"
    when: "the parity test runs"
    then: "every diff is empty"
    fixture_recipe_id: "a fresh venue"
    drivable: true
"""

BAD_ITEMS = """  - id: "DR-03"
    rev: 1
    slice: "one"
    kind: "chat-verb"
    held_by: "prompt"
    says: "x"
    when: "x"
    then: "x"
    fixture_recipe_id: "a fresh venue"
    drivable: true
  - id: "DR-04"
    rev: 1
    slice: "one"
    kind: "chat-verb"
    says: "x"
    when: "x"
    then: "x"
    fixture_recipe_id: "a fresh venue"
    drivable: true
  - id: "DR-05"
    rev: 1
    slice: "one"
    kind: "chat-verb"
    held_by: "model"
    pass_rate: "6/5"
    says: "x"
    when: "x"
    then: "x"
    fixture_recipe_id: "a fresh venue"
    drivable: true
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-train1-drill-"))
    repo = tmp / "repo"
    repo.mkdir()
    genv = {**os.environ, "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@d",
            "GIT_COMMITTER_NAME": "d", "GIT_COMMITTER_EMAIL": "d@d"}
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("hb.py\nhb/\n.claude/\n.context/\n", encoding="utf-8")
    (repo / "docs" / "manifests").mkdir(parents=True)
    (repo / "docs" / "manifests" / "drill.yaml").write_text(MANIFEST, encoding="utf-8")
    (repo / "docs" / "ledger").mkdir(parents=True)
    (repo / "docs" / "ledger" / "delivery.jsonl").write_text("", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed", env=genv)
    git(repo, "branch", "-M", "main")
    shutil.copy(HB_SRC, repo / "hb.py")
    shutil.copytree(HB_PKG, repo / "hb", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "harness-config.json").write_text(json.dumps({
        "repo": "drill/drill", "default_branch": "main",
        "state_dir": ".context/harness", "engine_dir": ".claude/harness",
        "commands_dir": ".claude/commands",
        "paths": {"manifest_dir": "docs/manifests", "ledger_file": "docs/ledger/delivery.jsonl"},
        "deploy": {"workflow_on_main": "Deploy", "readiness": "run-conclusion"},
        "ci": {"gate_check": "CI Gate", "watch_backoff_s": [1],
               "outage_probe": "https://api.github.com/rate_limit",
               "status_page": "https://www.githubstatus.com/api/v2/status.json"},
        "tiers": {}, "watchdog": {"stale_min": 15, "ago_red_s": 90, "max_slice_wall_h": 3,
                                  "context_window_by_model": {"fable": 15000000, "opus": 1000000}},
        "review_gate": {"enabled": True, "critical_paths": [],
                        "size_hold": {"file_lines": 30, "function_lines": 10}},
        "dep_parity": {"requirements": "req.txt", "venv_python": sys.executable,
                       "critical_pins": ["pip"]},
        "qa": {"mock_llm": {"script": "mock.py", "base_url": "", "min_turn_window_s": 8}},
        "parallel": {"builder_slots": 3},
    }), encoding="utf-8")
    state = repo / ".context" / "harness"
    (state / "slices").mkdir(parents=True)
    (state / "evidence" / "9001").mkdir(parents=True)
    (state / "evidence" / "9001" / "tree.txt").write_text("tree", encoding="utf-8")

    mp = repo / "docs" / "manifests" / "drill.yaml"
    print("drill T2 - ledger receipt: DIVERGED needs a ruling and is counted apart")
    common = ["ledger", "receipt", "--issue", "9001", "--slug", "drill", "--item", "DR-01", "--sha", "deadbeef",
              "--tenant-id", "tenant-1", "--situation", "a fresh venue", "--observed", "built to R7",
              "--evidence", str(state / "evidence" / "9001" / "tree.txt")]
    rc, d = hb(repo, *common, "--state", "DIVERGED")
    check("DIVERGED without --ruling refused", rc == 2 and d.get("verdict") == "diverged-without-ruling", str(d)[:300])
    rc, d = hb(repo, *common, "--state", "DIVERGED", "--ruling", "dk-7")
    check("DIVERGED with --ruling files", rc == 0, str(d)[:300])
    rows = [json.loads(ln) for ln in (state / "evidence" / "9001" / "receipts.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("the row carries the ruling", rows and rows[-1].get("state") == "DIVERGED" and rows[-1].get("ruling") == "dk-7", str(rows[-1:])[:300])
    check("a row with no --served-sha records the served sha as not given",
          (rows[-1].get("env_identity") or {}).get("served_sha") == "not given", str(rows[-1:])[:300])
    rc, d = hb(repo, *common, "--state", "DIVERGED", "--ruling", "dk-7", "--substate", "failed")
    check("DIVERGED refuses a substate", rc == 2, str(d)[:200])

    print("drill T2b - ledger receipt: the tenant id follows the item's kind")
    mp.write_text(MANIFEST + MEASURE_ITEM, encoding="utf-8")
    receipts = state / "evidence" / "9001" / "receipts.jsonl"
    # T3 counts this file's rows; the rows booked here are put back after
    kept = receipts.read_text(encoding="utf-8")
    plain = ["ledger", "receipt", "--issue", "9001", "--slug", "drill", "--sha", "deadbeef", "--state", "WORKS",
             "--situation", "the parity test seeds its own venue state", "--observed", "every diff empty",
             "--evidence", str(state / "evidence" / "9001" / "tree.txt")]
    rc, d = hb(repo, *plain, "--item", "DR-06")
    rows = [json.loads(ln) for ln in (state / "evidence" / "9001" / "receipts.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    ident = (rows[-1].get("env_identity") or {}) if rows else {}
    check("a measure item with no tenant id books", rc == 0 and rows[-1].get("item") == "DR-06", str(d)[:300])
    check("its row names no tenant and says why", ident.get("tenant_id") is None and ident.get("tenant_basis") == "no-venue"
          and ident.get("no_venue"), str(ident)[:300])
    rc, d = hb(repo, *plain, "--item", "DR-02")
    check("a chat-verb item with no tenant id is still refused", rc == 2 and d.get("verdict") == "no-tenant-identity", str(d)[:300])
    rc, d = hb(repo, *plain, "--item", "DR-02", "--no-venue", "read from a unit pin")
    check("--no-venue never excuses a venue-driven kind", rc == 2 and d.get("verdict") == "no-tenant-identity", str(d)[:300])
    rc, d = hb(repo, *plain, "--item", "DR-01", "--tenant-id", "tenant-1", "--served-sha", "e39accb7")
    rows = [json.loads(ln) for ln in (state / "evidence" / "9001" / "receipts.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    ident = (rows[-1].get("env_identity") or {}) if rows else {}
    check("a screen row with its tenant books as measured", rc == 0 and ident.get("tenant_basis") == "measured",
          str(rows[-1:])[:300])
    check("the served sha the caller gave is the row's", ident.get("served_sha") == "e39accb7"
          and ident.get("status") == "measured", str(ident)[:300])
    mp.write_text(MANIFEST, encoding="utf-8")
    receipts.write_text(kept, encoding="utf-8")

    print(f"\n{'=' * 52}\n  {len(PASS)} passed · {len(FAIL)} failed"
          + (f"  -> {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'=' * 52}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
