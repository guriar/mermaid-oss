#!/usr/bin/env python
"""Offline lifecycle drill for the venue seam: owner, names, idempotent up,
resumable down, orphan sweep, model choice, receipt identity, seats. Run:

    python engine/drill_venue.py

Throwaway git repo + state dir per run. Nothing here needs Docker, a backend, or
the network: the legs are stubbed with HB_VENUE_STUB=1, which records the argv
each leg WOULD have run from the venue block the drill writes, so the drill reads
what the product's venue.up, venue.down and venue.sign_in would have been told
without running any of them.
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
import re
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
PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"\n        {detail}"))


def hb(repo, *args, env=None):
    p = subprocess.run([sys.executable, str(repo / "hb.py"), *args], capture_output=True, text=True,
                       cwd=repo, encoding="utf-8", errors="replace", env={**os.environ, **(env or {})})
    last = (p.stdout or "").strip().splitlines()
    try:
        return p.returncode, json.loads(last[-1]) if last else {}
    except (ValueError, IndexError):
        return p.returncode, {"raw": (p.stdout or "")[-400:], "err": (p.stderr or "")[-400:]}


def git(repo, *args, env=None):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


# the interpreter the drill runs under, forward slashes as a command string is written
PY = sys.executable.replace(chr(92), '/')

CONFIG = {
    "repo": "drill/drill", "default_branch": "main",
    "state_dir": ".context/harness", "engine_dir": ".claude/harness",
    "commands_dir": ".claude/commands",
    "deploy": {"workflow_on_main": "Deploy", "readiness": "run-conclusion", "frontends": []},
    "ci": {"gate_check": "CI Gate", "watch_backoff_s": [1],
           "outage_probe": "https://api.github.com/rate_limit",
           "status_page": "https://www.githubstatus.com/api/v2/status.json"},
    "tiers": {}, "watchdog": {"stale_min": 15, "ago_red_s": 90, "max_slice_wall_h": 3,
                              "context_window_by_model": {"opus": 1000000}},
    "review_gate": {"enabled": True, "critical_paths": [], "size_hold": {"file_lines": 30, "function_lines": 10}},
    "dep_parity": {"requirements": "req.txt", "venv_python": sys.executable, "critical_pins": ["pip"]},
    "qa": {"mock_llm": {"script": "mock.py", "base_url": "", "min_turn_window_s": 8}},
    "parallel": {"builder_slots": 3},
    # the product's venue commands, python one-liners: a stub venue records their argv and runs none
    "venue": {"capabilities": f"{PY} -c print([])", "list": f"{PY} -c print([])",
              "up": f"{PY} -c pass up --name {{name}} --worktree {{worktree}} {{options}}",
              "down": f"{PY} -c pass down --name {{name}} {{keep}}",
              "sign_in": f"{PY} -c pass sign-in --name {{name}}"},
}
UP = lambda name, wt: [PY, "-c", "pass", "up", "--name", name, "--worktree", wt]
DOWN = lambda name, *keep: [PY, "-c", "pass", "down", "--name", name, *keep]
SIGN_IN = lambda name: [PY, "-c", "pass", "sign-in", "--name", name]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-venue-drill-"))
    repo = tmp / "repo"
    repo.mkdir()
    genv = {**os.environ, "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@d",
            "GIT_COMMITTER_NAME": "d", "GIT_COMMITTER_EMAIL": "d@d"}
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("hb.py\nhb/\n.claude/\n.context/\n", encoding="utf-8")
    (repo / "README.md").write_text("drill\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed", env=genv)
    git(repo, "branch", "-M", "main")
    shutil.copy(HB_SRC, repo / "hb.py")
    shutil.copytree(HB_PKG, repo / "hb", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "harness-config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (repo / ".context" / "harness" / "slices").mkdir(parents=True)

    stub = {"HB_VENUE_STUB": "1"}
    venues = repo / ".context" / "harness" / "venues"

    print("drill V0 - the venue refuses what it cannot measure")
    rc, d = hb(repo, "venue", "up", "--owner", "slice-conductor", env=stub)
    check("up without --issue is a usage refusal", rc == 3 and d.get("verdict") == "usage", str(d)[:200])

    print("drill V1 - id shape, the record, and the venue.up argv")
    issue = 9001
    rc, d = hb(repo, "venue", "up", "--issue", str(issue), "--owner", "slice-conductor", env=stub)
    vid = d.get("venue", "")
    check("up hands back a venue id of the ruled shape",
          rc == 0 and re.fullmatch(rf"v-{issue}-1-[0-9a-f]{{6}}", vid or ""), f"rc={rc} {str(d)[:300]}")
    rec = json.loads((venues / f"{vid}.json").read_text(encoding="utf-8")) if vid else {}
    check("the record lands at .context/harness/venues/<id>.json",
          bool(rec) and rec.get("id") == vid, str(rec)[:200])
    check("the record names its owner, its clock and its state",
          rec.get("owner") == "slice-conductor" and rec.get("tz") == "UTC"
          and rec.get("state") == "up", str({k: rec.get(k) for k in ('owner', 'tz', 'state')}))
    check("the venue's name is keyed by issue and attempt, so a kept database finds the next venue",
          rec.get("name") == f"venue-{issue}-1", rec.get("name"))
    check("the venue carries a browser folder of its own",
          str(rec.get("browser_profile", "")).replace("\\", "/").endswith(f"venues/{vid}/browser/profile"),
          rec.get("browser_profile"))
    check("every receipt this verb prints names the venue",
          d.get("venue") == vid and rec.get("id") == vid, str(d)[:200])
    venue_leg = next((l for l in rec.get("legs", []) if l["leg"] == "venue"), {})
    check("the venue leg recorded the venue.up argv it would have run, filled for this venue",
          venue_leg.get("status") == "stub" and venue_leg.get("cmd") == UP(f"venue-{issue}-1", rec.get("worktree")),
          str(venue_leg)[:400])
    check("the legs are venue, browser, session, in that order",
          [l["leg"] for l in rec.get("legs", [])] == ["venue", "browser", "session"],
          str([l["leg"] for l in rec.get("legs", [])]))
    session = next((l for l in rec.get("legs", []) if l["leg"] == "session"), {})
    check("the session leg recorded the venue.sign_in argv for this venue",
          session.get("cmd") == SIGN_IN(f"venue-{issue}-1"), str(session)[:300])
    check("the record names this venue's own storage state",
          str(rec.get("storage_state", "")).replace("\\", "/").endswith(f"venues/{vid}/browser/storage-state.json"),
          rec.get("storage_state"))

    print("drill V2 - model choice: mock unless --real-llm, and it is in the record")
    check("without --real-llm the record says mock", rec.get("model") == "mock", rec.get("model"))
    rc2, d2 = hb(repo, "venue", "up", "--issue", "9002", "--owner", "wave-conductor", "--real-llm", env=stub)
    rec2 = json.loads((venues / f"{d2.get('venue')}.json").read_text(encoding="utf-8"))
    check("--real-llm is recorded as the venue's model", rec2.get("model") == "real", str(rec2.get("model")))

    print("drill V3 - a second up for the same issue and attempt returns the existing venue")
    rc3, d3 = hb(repo, "venue", "up", "--issue", str(issue), "--owner", "slice-conductor", env=stub)
    check("the second up is not a second venue",
          rc3 == 0 and d3.get("verdict") == "existing" and d3.get("venue") == vid, str(d3)[:300])
    check("no second record was written for that issue and attempt",
          len([p for p in venues.glob(f"v-{issue}-1-*.json")]) == 1,
          str([p.name for p in venues.glob('v-*.json')]))

    print("drill V4 - down is resumable after a crash mid-down, and idempotent after it")
    rcd, dd = hb(repo, "venue", "down", "--venue", vid, env={**stub, "HB_VENUE_STUB_CRASH": "browser"})
    rec = json.loads((venues / f"{vid}.json").read_text(encoding="utf-8"))
    check("a crash mid-down leaves a partial record, not a clean one",
          rcd != 0 and rec.get("state") == "down-partial" and rec.get("down_steps") == ["venue"],
          f"rc={rcd} {str(rec.get('state'))} {rec.get('down_steps')}")
    rcd2, dd2 = hb(repo, "venue", "down", "--venue", vid, env=stub)
    rec = json.loads((venues / f"{vid}.json").read_text(encoding="utf-8"))
    check("the next down resumes from the step that crashed",
          rcd2 == 0 and dd2.get("verdict") == "down"
          and [s["step"] for s in dd2.get("steps", []) if s.get("status") == "already-done"] == ["venue"],
          str(dd2)[:300])
    check("the venue ends down with its database dropped",
          rec.get("state") == "down" and dd2.get("db") == "dropped", str(rec.get("state")))
    rcd3, dd3 = hb(repo, "venue", "down", "--venue", vid, env=stub)
    check("a third down is idempotent", rcd3 == 0 and dd3.get("verdict") == "already-down", str(dd3)[:200])
    rcd4, dd4 = hb(repo, "venue", "down", "--venue", "v-1-1-aaaaaa", env=stub)
    check("down on an unknown id refuses", rcd4 == 2 and dd4.get("verdict") == "unknown-venue", str(dd4)[:200])

    print("drill V4b - a venue stopped with its database kept is dropped by the next plain down")
    rc, dk = hb(repo, "venue", "up", "--issue", "9004", "--owner", "slice-conductor", env=stub)
    kept_id = dk.get("venue")
    rck, ddk = hb(repo, "venue", "down", "--venue", kept_id, "--keep-db", env=stub)
    rec = json.loads((venues / f"{kept_id}.json").read_text(encoding="utf-8"))
    check("down --keep-db records the database as kept", rck == 0 and ddk.get("db") == "kept" and rec.get("db") == "kept", str(ddk)[:200])
    kept_step = next((s for s in ddk.get("steps", []) if s["step"] == "venue"), {})
    check("down --keep-db recorded the venue.down argv with {keep} filled",
          kept_step.get("cmd") == DOWN(rec["name"], "--keep"), str(kept_step)[:300])
    rcp, ddp = hb(repo, "venue", "down", "--venue", kept_id, env=stub)
    rec = json.loads((venues / f"{kept_id}.json").read_text(encoding="utf-8"))
    check("a plain down after it runs the venue step again and drops the database",
          rcp == 0 and ddp.get("verdict") == "down" and rec.get("db") == "dropped"
          and [s["step"] for s in ddp.get("steps", []) if s.get("status") != "already-done"] == ["venue"], str(ddp)[:300])
    plain_step = next((s for s in ddp.get("steps", []) if s["step"] == "venue"), {})
    check("the plain down recorded the venue.down argv with {keep} empty",
          plain_step.get("cmd") == DOWN(rec["name"]), str(plain_step)[:300])
    rcp2, ddp2 = hb(repo, "venue", "down", "--venue", kept_id, env=stub)
    check("and the down after that is idempotent", rcp2 == 0 and ddp2.get("verdict") == "already-down", str(ddp2)[:200])

    print("drill V5 - list and sweep: an orphan by age")
    rc, d5 = hb(repo, "venue", "up", "--issue", "9005", "--owner", "slice-conductor", env=stub)
    old = d5.get("venue")
    rec5 = json.loads((venues / f"{old}.json").read_text(encoding="utf-8"))
    rec5["created"] = "2020-01-01T00:00:00Z"
    (venues / f"{old}.json").write_text(json.dumps(rec5), encoding="utf-8")
    rc, d7 = hb(repo, "venue", "up", "--issue", "9007", "--owner", "slice-conductor", env=stub)
    fresh = d7.get("venue")

    rcl, dl = hb(repo, "venue", "list", env=stub)
    ids = {r["venue"] for r in dl.get("venues", [])}
    check("list names every venue on the host", rcl == 0 and {old, fresh} <= ids, str(ids)[:300])
    row = next(r for r in dl["venues"] if r["venue"] == old)
    check("a listed venue carries owner, age and its name",
          row.get("owner") and row.get("age_hours", 0) > 40000 and row.get("name") == "venue-9005-1", str(row)[:300])

    rcs, ds = hb(repo, "venue", "sweep", "--older-than-hours", "12", env=stub)
    picked = {c["row"]["venue"]: c["reasons"] for c in ds.get("candidates", [])}
    check("sweep finds the aged orphan", old in picked and any("age" in r for r in picked.get(old, [])), str(picked)[:300])
    check("a fresh venue is NOT swept", fresh not in picked, str(picked)[:300])
    check("sweep without --apply is a dry run", ds.get("verdict") == "dry-run"
          and not ds.get("swept"), str(ds)[:200])
    rcs2, ds2 = hb(repo, "venue", "sweep", "--older-than-hours", "12", "--apply", env=stub)
    after = json.loads((venues / f"{old}.json").read_text(encoding="utf-8"))
    check("sweep --apply takes the orphan down",
          ds2.get("verdict") == "swept" and after.get("state") == "down", str(after.get("state")))

    print("drill V7 - seats: one signed-in browser per concurrent verifier")
    rcs8, d8s = hb(repo, "venue", "seats", "--venue", fresh, "--count", "3", env=stub)
    seats8 = d8s.get("seats") or []
    check("seats hands back three seats", rcs8 == 0 and d8s.get("verdict") == "ok" and len(seats8) == 3, str(d8s)[:300])
    for key in ("browser_profile", "storage_state"):
        check(f"each seat has its own {key}", len({s.get(key) for s in seats8}) == 3, str([s.get(key) for s in seats8]))
    check("each seat signs in through venue.sign_in on its own",
          all([l["leg"] for l in s.get("legs", [])] == ["session"] and s["legs"][0].get("cmd") == SIGN_IN("venue-9007-1")
              for s in seats8), str([s.get("legs") for s in seats8])[:400])
    rcs9, d9s = hb(repo, "venue", "seats", "--venue", "v-1-1-aaaaaa", "--count", "2", env=stub)
    check("seats on an unknown venue refuses", rcs9 == 2 and d9s.get("verdict") == "unknown-venue", str(d9s)[:200])

    print("drill V8 - the seam refuses a config it cannot run, before any seat starts")
    from hb.venue import adapter
    saved = (adapter.capabilities, adapter.block)
    base = {"capabilities": "cap", "up": "up {name}", "down": "down {name}", "list": "list"}
    try:
        adapter.capabilities = lambda c=None: ("states", "ui")
        problems = adapter.check_config({"venue": dict(base, up="up {name} {tenant}")})
        check("a placeholder the engine does not fill is named", any("{tenant}" in p for p in problems), str(problems))
        problems = adapter.check_config({"venue": dict(base, ui={"driver": {"args": []}})})
        check("states declared without its commands is named per key", any("venue.state_build" in p for p in problems), str(problems))
        check("ui declared with a driver that has no command is named", any("ui.driver has no command" in p for p in problems), str(problems))
        adapter.capabilities = lambda c=None: ()
        problems = adapter.check_config({"venue": dict(base, reuse={"web/": "reload"})})
        check("reuse mapping to reload without a reload command is named", any("no venue.reload" in p for p in problems), str(problems))
        problems = adapter.check_config({"venue": dict(base, reuse={"web/": "restart"})})
        check("a reuse verdict outside keep, reload, rebuild is named", any("restart" in p for p in problems), str(problems))
        check("a clean block has no problems", adapter.check_config({"venue": base}) == [])
        adapter.capabilities = lambda c=None: ("ui",)
        lines = adapter.not_offered_lines({"venue": base})
        check("the record names each capability not offered and what ran nothing",
              len(lines) == 5 and not any("not offered: ui" in l for l in lines)
              and any(l.startswith("not offered: model: the battery") for l in lines), str(lines))
        check("no venue block is one line naming the venue itself",
              bool(adapter.not_offered_lines({})) and adapter.not_offered_lines({})[0].startswith("not offered: venue"))
        check("the receipt is the last nonempty line only", adapter._last_json('{"a": 1}\nnot json\n') is None
              and adapter._last_json('noise\n{"a": 1}\n\n') == {"a": 1})
        adapter.block = lambda c=None: dict(base, state_build="build {state} {name}")
        try:
            adapter.argv("state_build", name="n")
            missing_refused = False
        except SystemExit as ex:
            missing_refused = ex.code == 2
        check("a required placeholder with no value is a refusal, never an empty argument", missing_refused)
        check("an optional placeholder may be empty", adapter.argv("down", name="n") == ["down", "n"])
    finally:
        adapter.capabilities, adapter.block = saved

    print(f"\n{'=' * 52}\n  {len(PASS)} passed · {len(FAIL)} failed · {len(SKIP)} unmeasured"
          + (f"  -> {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'=' * 52}")
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
