#!/usr/bin/env python
"""Drill suite for the engine's long-standing seams: the docket, the changelog fold,
the worktree sweep, the ledger's goal rows, the gate clock and the retired verbs. Run:

    python engine/drills.py

Creates an isolated throwaway git repo + state dir per run; never touches real state.
Network-dependent behaviours (ci-watch outage classification, deploy-watch) are
exercised live instead, and are named LIVE-ONLY at the end.
"""
import sys
from pathlib import Path

ALONE = True  # times the host's own waits; runs after the parallel set
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
import time
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


def copy_hb(repo):
    """The engine is an entry file plus the package of seams beside it. Copying
    only the entry gives the drill repo a script that cannot import itself, so
    every drill would measure the copy failing, not the engine."""
    shutil.copy(HB_SRC, repo / "hb.py")
    shutil.copytree(HB_PKG, repo / "hb", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    # the guard is where hb reads the namespaces it may delete from, two levels up from the package
    shutil.copytree(HB_PKG.parent.parent / "hooks", repo.parent / "hooks", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail[:400]})" if detail and not cond else ""))


def hb(repo, *args, expect_rc=None, env_extra=None):
    p = subprocess.run([sys.executable, str(repo / "hb.py"), *args],
                       capture_output=True, text=True, cwd=repo,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env_extra or {})})
    try:
        data = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_raw": p.stdout, "_err": p.stderr}
    if expect_rc is not None and p.returncode != expect_rc:
        print(f"    [rc {p.returncode} != {expect_rc}] {p.stderr[-200:]}")
    return p.returncode, data


def backdate(path, minutes):
    t = time.time() - minutes * 60
    os.utime(path, (t, t))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-drills-"))
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=repo,
                   capture_output=True, env={**os.environ, "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@d",
                                             "GIT_COMMITTER_NAME": "d", "GIT_COMMITTER_EMAIL": "d@d"})
    copy_hb(repo)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "harness-config.json").write_text(json.dumps({
        "repo": "drill/drill", "default_branch": "main",
        "state_dir": ".context/harness", "engine_dir": ".claude/harness",
        "commands_dir": ".claude/commands",
        "paths": {"manifest_dir": "docs/manifests", "ledger_file": "docs/ledger/delivery.jsonl"},
        "branches": {"landing": "dev", "promotion_base": "main"},
        "deploy": {"workflow_on_main": "Deploy", "readiness": "run-conclusion"},
        "ci": {"gate_check": "CI Gate", "watch_backoff_s": [1], "outage_probe": "https://api.github.com/rate_limit",
               "status_page": "https://www.githubstatus.com/api/v2/status.json"},
        "tiers": {}, "watchdog": {"stale_min": 15, "ago_red_s": 90, "max_slice_wall_h": 3},
        "review_gate": {"enabled": True, "critical_paths": []},
        "dep_parity": {"requirements": "req.txt", "venv_python": sys.executable,
                        "critical_pins": ["pip"]},
        "qa": {"mock_llm": {"script": "mock.py", "base_url": "", "min_turn_window_s": 8}},
    }))
    sd = repo / ".context" / "harness"
    (sd / "slices").mkdir(parents=True)
    (sd / "waves").mkdir(parents=True)

    print("drill 18: docket: the owner queue, born with a cause block (docket/v3)")
    cause = ["--cause", "engine/hb/cli.py:80 lists the verb as retired",
             "--desired", "the walk files it under the lane the ruling names",
             "--cause-class", "defect", "--lane", "slice",
             "--plain", "The owner would notice nothing yet; doing nothing leaves the queue lying."]
    rc, d = hb(repo, "docket", "add", "--type", "PROPOSAL", "--title", "Supersede ADR drill",
               "--rec", "approve verbatim", "--wave", "drill", "--discovered-by", "#90", *cause)
    check("add returns id + counts", d.get("added", "").startswith("dk-")
          and d.get("open_by_type", {}).get("PROPOSAL") == 1, str(d)[:400])
    pid = d.get("added")
    rc, d = hb(repo, "docket", "add", "--type", "PROPOSAL", "--title", "no cause",
               "--plain", "The owner sees a stale queue.", expect_rc=2)
    check("an item with no measured cause is refused", rc == 2
          and "cause block rejected" in d.get("error", ""), str(d)[:400])
    rc, d = hb(repo, "docket", "add", "--type", "PROPOSAL", "--title", "same line again",
               "--cause", "engine/hb/cli.py:80 lists the verb as retired",
               "--desired", "one cause, one item", "--cause-class", "defect", "--lane", "slice",
               "--plain", "The owner would notice nothing; a second entry for one fault splits the record.",
               expect_rc=2)
    check("a second OPEN item at the same file:line is refused as a duplicate cause",
          rc == 2 and d.get("verdict") == "duplicate-cause" and pid in d.get("open_items", []), str(d)[:400])
    rc, d = hb(repo, "docket", "verdict", "--id", pid, "--verdict", "parked", expect_rc=2)
    check("parked without --revisit refused (anti-graveyard)", rc == 2 and "revisit" in d.get("error", ""), str(d)[:400])
    rc, d = hb(repo, "docket", "verdict", "--id", pid, "--verdict", "parked",
               "--revisit", "when onboarding feature unparks")
    check("parked with revisit ok", rc == 0 and d.get("status") == "parked", str(d)[:400])
    rc, d = hb(repo, "docket", "verdict", "--id", "dk-0", "--verdict", "approved", expect_rc=3)
    check("verdict on unknown id refused", rc != 0, str(d)[:400])
    rc, d = hb(repo, "docket", "retype", "--id", pid, "--type", "RULE")
    check("retype works (migration sitting tool)",
          (d.get("changed") or {}).get("type") == "RULE", str(d)[:400])

    print("drill 20: worktree-gc: a squash-landed branch with no merged PR read is kept, never swept on a guess")
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, capture_output=True)
    env_git = {**os.environ, "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@d",
               "GIT_COMMITTER_NAME": "d", "GIT_COMMITTER_EMAIL": "d@d"}
    subprocess.run(["git", "checkout", "-q", "-b", "slice/93-x"], cwd=repo, capture_output=True)
    (repo / "landed.txt").write_text("content\n")
    subprocess.run(["git", "add", "landed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "slice work"], cwd=repo, capture_output=True, env=env_git)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, capture_output=True)
    (repo / "landed.txt").write_text("content\n")  # the squash: same content, different SHA
    subprocess.run(["git", "add", "landed.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "squash: slice work (Issue: #93)"], cwd=repo,
                   capture_output=True, env=env_git)
    subprocess.run(["git", "fetch", "origin", "-q"], cwd=repo, capture_output=True)
    wt_gc = tmp / "wt-93"
    subprocess.run(["git", "worktree", "add", "-q", str(wt_gc), "slice/93-x"], cwd=repo, capture_output=True)
    rc, d = hb(repo, "worktree-gc", "--apply")
    check("a squash-landed branch gh could not read as merged is KEPT (unmerged), not removed",
          any("wt-93" in x and "unmerged" in x for x in d.get("kept_reasons", [])) and not d.get("removed"), str(d)[:400])

    print("drill 33: docket add: no lost entries under concurrent writers")
    before = len(json.loads((sd / "docket.json").read_text(encoding="utf-8"))["entries"])
    procs = [subprocess.Popen([sys.executable, str(repo / "hb.py"), "docket", "add",
                               "--type", "REVIEW", "--title", f"concurrent {i}", "--plain", "The owner notices nothing.",
                               "--cause", f"engine/hb/x.py:{i + 1} reads the wrong row", "--desired", "the right row",
                               "--cause-class", "defect", "--lane", "slice", "--wave", f"conc{i}"],
                              cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for i in range(6)]
    for p in procs:
        p.wait()
    after = json.loads((sd / "docket.json").read_text(encoding="utf-8"))["entries"]
    check("6 concurrent docket adds all landed with unique ids", len(after) == before + 6
          and len({e["id"] for e in after}) == len(after), f"{before}->{len(after)}")

    print("\nLIVE-ONLY (exercised in the pilot, not drillable offline): ci-watch "
          "green/failed/outage, deploy-watch, snap and login; the gh paths are "
          "drilled offline via HB_GH_STUB, only its live gh transport stays pilot-verified.")
    print(f"\n{'='*52}\n  {len(PASS)} passed · {len(FAIL)} failed"
          + (f"  → {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'='*52}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


def seed_repo(prefix):
    """A throwaway repo with hb.py and the smallest config hb reads. Returned so a
    drill group can stand on its own tree instead of a shared one - a group that
    cannot run when an earlier drill crashes proves nothing about its own code."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    copy_hb(repo)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "harness-config.json").write_text(json.dumps({
        "repo": "drill/drill", "default_branch": "main",
        "state_dir": ".context/harness", "engine_dir": ".claude/harness",
        "commands_dir": ".claude/commands",
        "paths": {"manifest_dir": "docs/manifests", "ledger_file": "docs/ledger/delivery.jsonl"},
        "branches": {"landing": "dev", "promotion_base": "main"},
        "deploy": {"workflow_on_main": "Deploy", "readiness": "run-conclusion"},
        "tiers": {}, "watchdog": {"stale_min": 15, "ago_red_s": 90, "max_slice_wall_h": 3},
    }), encoding="utf-8")
    return tmp, repo


def goal_body_stub(repo, body):
    """A gh stub that answers `issue view ... -q .body` with BODY verbatim, which is
    what `_wave_goal_text` reads. The seam is HB_GH_STUB (drill 34's)."""
    p = repo / "goal_gh_stub.py"
    p.write_text("import sys\n"
                 "body = " + repr(body) + "\n"
                 "args = sys.argv[1:]\n"
                 "if args[:2] == ['issue', 'view']:\n"
                 "    sys.stdout.write(body)\n", encoding="utf-8")
    return {"HB_GH_STUB": str(p)}


def drill_goal_rows():
    print("drill G: the ledger accepts the anchor's GOAL ids")
    tmp, repo = seed_repo("hb-drills-goal-")
    genv = goal_body_stub(repo, "\n".join([
        "## GOAL", "",
        "- **G1 (one day, every door)** when a user opens any door then today is the venue's",
        "- **G2 (no fake green)** when a gate is unmeasured then the row says so",
        "", "## Slices", "- nothing here declares a G id",
    ]))
    ev = repo / "capture.txt"
    ev.write_text("a capture", encoding="utf-8")
    man = repo / "docs" / "manifests"
    man.mkdir(parents=True)
    (man / "goaldrill.yaml").write_text("slug: goaldrill\nrev: 3\nitems:\n"
                                        "  - id: W1-01\n    rev: 1\n    says: a thing\n",
                                        encoding="utf-8")
    common = ["--issue", "9800", "--anchor", "9800", "--wave", "goaldrill",
              "--sha", "deadbeef", "--tenant-id", "tenant-drill",
              "--situation", "a fresh venue with one door open",
              "--evidence", str(ev), "--source", "composed"]
    rc, d = hb(repo, "ledger", "receipt", "--item", "G1", "--state", "WORKS", *common,
               env_extra=genv)
    check("G1 receipt files with kind goal at the manifest's top-level rev",
          rc == 0 and d.get("verdict") == "receipt-written"
          and d.get("item") == "G1" and d.get("rev") == 3, str(d))
    row = json.loads((repo / ".context" / "harness" / "evidence" / "9800"
                      / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    check("the row carries kind=goal and the anchor", row.get("kind") == "goal"
          and row.get("anchor") == "9800", str(row))
    rc, d = hb(repo, "ledger", "receipt", "--item", "G9", "--state", "WORKS", *common,
               env_extra=genv, expect_rc=2)
    check("G9 is refused unknown-goal", rc == 2 and d.get("verdict") == "unknown-goal"
          and d.get("known_ids") == ["G1", "G2"], str(d))
    rc, d = hb(repo, "ledger", "receipt", "--item", "G1", "--state", "WORKS",
               "--issue", "9800", "--wave", "goaldrill", "--sha", "deadbeef",
               "--tenant-id", "tenant-drill", "--situation", "s", "--evidence", str(ev),
               env_extra=genv, expect_rc=2)
    check("a G item with no --anchor is refused", rc == 2
          and d.get("verdict") == "goal-without-anchor", str(d))
    rc, d = hb(repo, "ledger", "append", "--wave", "goaldrill", "--anchor", "9800",
               env_extra=genv)
    check("the G row flows through append into the ledger", rc == 0
          and d.get("appended") == 1, str(d))
    led = (repo / "docs" / "ledger" / "delivery.jsonl")
    ev_row = json.loads(led.read_text(encoding="utf-8").splitlines()[0]) if led.exists() else {}
    check("the appended event says kind goal", ev_row.get("item") == "G1"
          and ev_row.get("kind") == "goal", str(ev_row))
    shutil.rmtree(tmp, ignore_errors=True)


def drill_gate_clock():
    print("drill TZ: run-gate runs the child under UTC by default")
    tmp, repo = seed_repo("hb-drills-tz-")

    def gate(*extra):
        p = subprocess.run([sys.executable, str(repo / "hb.py"), "run-gate", "--issue", "9801",
                            *extra, "--",
                            sys.executable, "-c",
                            "import os;print('TZ_SEEN=' + str(os.environ.get('TZ')))"],
                           capture_output=True, text=True, cwd=repo,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        art = [ln.split("=", 1)[1] for ln in p.stdout.splitlines()
               if ln.startswith("GATE_OUTPUT=")]
        return p, (Path(art[0]).read_text(encoding="utf-8") if art else "")

    p, blob = gate()
    check("default gate: the child sees TZ=UTC", "TZ_SEEN=UTC" in blob, blob[:400])
    check("default gate: the artifact header carries TZ=UTC",
          any(ln == "TZ=UTC" for ln in blob.splitlines()), blob[:400])
    check("default gate: TRUE_EXIT=0", "TRUE_EXIT=0" in blob, blob[-200:])
    p, blob = gate("--tz", "machine")
    check("--tz machine: no TZ override reaches the child",
          "TZ_SEEN=UTC" not in blob, blob[:400])
    check("--tz machine: the artifact says so", "TZ=machine" in blob, blob[:400])
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    drill_goal_rows()
    drill_gate_clock()
    main()
