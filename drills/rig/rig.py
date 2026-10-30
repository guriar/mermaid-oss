#!/usr/bin/env python
"""Drill for the rig: `rig drill` quotes every drill's TRUE_EXIT line and refuses on a red or an
unmeasured one; a run started from a plugin cache pins itself into the versions home and keeps
that engine when the cache changes under it; a run resumed on another pin refuses, and
--migrate-harness journals the move and owes every receipt. Run:

    python drills/rig/rig.py

Throwaway git repo, plugin copy and versions home per run; never touches real state, never calls gh.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

from fixture.common import PASS, FAIL, check, make_repo, run_hotfix, journal, last_json  # noqa: E402,F401

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def hb(entry, repo, *args, env=None):
    p = subprocess.run([sys.executable, str(entry), *args], capture_output=True, text=True, cwd=repo,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    return p.returncode, last_json(p.stdout) or {"_raw": p.stdout[-400:], "_err": p.stderr[-400:]}, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-rig-drill-"))
    home = tmp / "home"
    env = {"HB_VERSIONS_DIR": str(home)}
    repo = make_repo(tmp / "repo")

    print("drill R1 - rig drill quotes TRUE_EXIT lines and refuses on red")
    rc, d, _ = hb(ENGINE / "hb.py", repo, "rig", "drill", "--from", str(ROOT), env={**env, "HB_RIG_STUB_DRILLS": "green"})
    check("without rig.source_branch the drill refuses naming the key", rc == 2 and "rig.source_branch" in str(d.get("why")), str(d)[:300])
    config_path = repo / ".claude" / "harness-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["rig"] = {"source_branch": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
                                                      capture_output=True, text=True).stdout.strip()}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    g = ["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d"]
    subprocess.run(g + ["commit", "-q", "-m", "rig.source_branch", "--", str(config_path)], check=True, capture_output=True)
    subprocess.run(g + ["push", "-q", "origin", "dev"], check=True, capture_output=True)
    rc, d, _ = hb(ENGINE / "hb.py", repo, "rig", "drill", "--from", str(ROOT), env={**env, "HB_RIG_STUB_DRILLS": "green"})
    rows = d.get("drills") or []
    check("a green pass is green and names every drill under drills/<foundation>/",
          rc == 0 and d.get("verdict") == "green" and rows and all(r["drill"].startswith("drills/") for r in rows), str(d)[:300])
    check("every row quotes a TRUE_EXIT line, never a number",
          all(str(r.get("TRUE_EXIT", "")).startswith("TRUE_EXIT=") for r in rows), str(rows[:2]))
    rc, d, _ = hb(ENGINE / "hb.py", repo, "rig", "drill", "--from", str(ROOT), env={**env, "HB_RIG_STUB_DRILLS": "red"})
    check("a red drill refuses with exit 1 and quotes the lines", rc == 1 and d.get("verdict") == "red"
          and all(str(r.get("TRUE_EXIT", "")).startswith("TRUE_EXIT=") for r in d.get("drills") or []), str(d)[:300])

    print("drill R2 - the runner reads the printed line: UNMEASURED, SKIPPED and a missing line are never green")
    from hb.rig import rig
    check("bare TRUE_EXIT=0 is green", rig.read_true_exit("x\nTRUE_EXIT=0\n") == ("green", "TRUE_EXIT=0"))
    check("TRUE_EXIT=0 UNMEASURED is unmeasured", rig.read_true_exit("TRUE_EXIT=0 UNMEASURED no product\n")[0] == "unmeasured")
    check("TRUE_EXIT=0 SKIPPED is skipped", rig.read_true_exit("TRUE_EXIT=0 SKIPPED windows-only\n")[0] == "skipped")
    check("SKIPPED without a reason is red", rig.read_true_exit("TRUE_EXIT=0 SKIPPED\n")[0] == "red")
    check("a missing line is red", rig.read_true_exit("all passed\n") == ("red", "TRUE_EXIT=missing"))
    check("the last line wins", rig.read_true_exit("TRUE_EXIT=0\nTRUE_EXIT=1\n")[0] == "red")
    alone = [d for d in rig.discover_drills(ROOT) if rig._alone(ROOT, d)]
    check("the drills that time the host declare ALONE and are found by glob",
          "drills/venue/admission.py" in alone and "drills/rig/rig.py" in rig.discover_drills(ROOT), str(alone))

    print("drill R5 - the structure check reads `from hb import <foundation>` as an import of it")
    from hb.rig import structure
    fake = tmp / "fake-hb"
    for name in structure.FOUNDATIONS:
        (fake / name).mkdir(parents=True)
    (fake / "rig" / "helper.py").write_text("from hb import beats\n", encoding="utf-8")
    problems = structure.check(fake)
    check("rig importing beats through `from hb import beats` is against the arrow",
          any("rig -> beats" in p for p in problems), str(problems))
    (fake / "rig" / "helper.py").write_text("from hb import rig\n", encoding="utf-8")
    check("the same form inside the arrow passes", structure.check(fake) == [], str(structure.check(fake)))

    print("drill R3 - a run from a plugin cache pins itself and keeps its engine when the cache changes")
    plugin = tmp / "plugin"
    for part in ("engine", "roles", "hooks", "templates", "commands"):
        shutil.copytree(ROOT / part, plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
    script = {"build": {"mode": "ok", "touch": ["src/x.py"]}}
    rc, js, raw = run_hotfix(repo, script, hb=str(plugin / "engine" / "hb.py"), env_extra=env)
    rid = js.get("run_id", "")
    opened = next((e for e in journal(repo, rid) if e.get("kind") == "phase" and e.get("phase") == "opened"), {})
    sha = opened.get("harness")
    check("the run journals a frozen version, not `working`", rc == 0 and sha and sha != "working" and len(sha) == 12,
          str(opened)[:200] + raw[-300:])
    frozen = home / "versions" / str(sha) / "engine" / "hb.py"
    check("the frozen copy is laid out once under the versions home", frozen.is_file()
          and (frozen.parent.parent / ".complete").exists() and (frozen.parent.parent / "roles" / "builder.md").is_file(), str(frozen))
    (plugin / "roles" / "builder.md").write_text("# changed under the run\n", encoding="utf-8")
    rc2, js2, raw2 = run_hotfix(repo, script, hb=str(plugin / "engine" / "hb.py"), extra=("--resume", rid), env_extra=env)
    opened2 = [e for e in journal(repo, rid) if e.get("kind") == "phase" and e.get("phase") == "opened"]
    moved = [e for e in journal(repo, rid) if e.get("kind") == "harness-migrated"]
    check("a resume after the cache changed re-enters on the version the run opened on",
          rc2 == 0 and js2.get("verdict") != "harness-moved" and not moved
          and all(e.get("harness") == sha for e in opened2), str(js2)[:300] + raw2[-300:])

    print("drill R4 - a run resumed on another pin refuses; --migrate-harness journals the move")
    pin = lambda s: str(home / "versions" / s / "engine" / "hb.py")
    repo2 = make_repo(tmp / "pinned")
    rc, js, raw = run_hotfix(repo2, script, env_extra={**env, "HB_PINNED_ENTRY": pin("aaaaaaaaaaaa")})
    rid = js.get("run_id", "")
    opened = next((e for e in journal(repo2, rid) if e.get("kind") == "phase" and e.get("phase") == "opened"), {})
    check("a run journals the harness version it opened on", opened.get("harness") == "aaaaaaaaaaaa", str(opened)[:200] + raw[-200:])
    rc, js2, raw = run_hotfix(repo2, script, extra=("--resume", rid), env_extra={**env, "HB_PINNED_ENTRY": pin("bbbbbbbbbbbb")})
    check("a resume on another pin refuses, naming both versions",
          rc == 2 and js2.get("verdict") == "harness-moved" and js2.get("opened_on") == "aaaaaaaaaaaa"
          and js2.get("now_on") == "bbbbbbbbbbbb", str(js2)[:300] + raw[-200:])
    rc, js3, raw = run_hotfix(repo2, script, extra=("--resume", rid, "--migrate-harness"),
                              env_extra={**env, "HB_PINNED_ENTRY": pin("bbbbbbbbbbbb")})
    moved = [e for e in journal(repo2, rid) if e.get("kind") == "harness-migrated"]
    check("--migrate-harness journals the move and the run goes on",
          moved and moved[0].get("opened_on") == "aaaaaaaaaaaa" and moved[0].get("now_on") == "bbbbbbbbbbbb"
          and js3.get("verdict") in ("mergeable", "run-complete"), str((moved, js3.get("verdict")))[:300] + raw[-200:])
    from hb.record import reducer
    st = reducer.replay([{"kind": "loop-open", "branch": "b", "receipts": [{"item": "T-01", "state": "WORKS", "identity": {"harness": "a"}}]},
                         {"kind": "harness-migrated", "opened_on": "a", "now_on": "b"}], "b")
    check("the reducer owes every receipt taken before the move",
          st.receipts[0].get("state") == "UNMEASURED" and "moved" in str(st.receipts[0].get("observed")), str(st.receipts))

    print(f"\n{'=' * 52}\n  {len(PASS)} passed · {len(FAIL)} failed"
          + (f"  -> {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'=' * 52}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
