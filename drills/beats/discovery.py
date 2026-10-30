#!/usr/bin/env python
"""Drill for discovery and the audit page (Q2, Q4). Run:

    python engine/drill_discovery.py

A throwaway product repo with a golden file; a fake discovery seat returns two finds. The
finds reach the builder's next brief, the golden set grows by two proposed cases while its
frozen cases keep every byte, the item's row counts them, and the audit brief carries no
diff and no checks.
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
os.environ.setdefault("HB_SETTLE_S", "0")

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ROOT / "drills"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PASS, FAIL = [], []

FINDS = [{"item": "M-01", "said": "pulled 2 of the blue", "wrong": "the card pulled two boxes, not two units",
          "capture": "finds/M-01-blue.png"},
         {"item": "M-01", "said": "grab the red... no wait, three", "wrong": "the card kept the first amount"}]
GOLDEN = {"schema": "battery-golden/v1", "families": {"pull": {"bar": 0.99}},
          "cases": [{"id": "a-pull", "family": "pull", "steps": [{"say": "pulled 3 whole"}], "expect": {"card": {}}},
                    {"id": "a-case-pull", "family": "pull", "steps": [{"say": "grab a case of cups"}], "expect": {}}]}
CONTRACT = {"issue": "#7", "goal": "the pull card is right",
            "items": [{"item": "M-01", "held_by": "model", "battery": "pull", "then": "the pull lands in the unit said"},
                      {"item": "S-01", "then": "the list shows"}]}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""))


def make_repo(tmp):
    repo = tmp / "repo"
    repo.mkdir()
    (repo / ".claude").mkdir()
    (repo / ".claude" / "harness-config.json").write_text(json.dumps({
        "repo": "drill/drill", "default_branch": "dev", "state_dir": ".context/harness",
        "seats": {"discovery": {"runtime": "fake"}, "build": {"runtime": "fake"}},
        "battery": {"golden": "golden.json"}}), encoding="utf-8")
    (repo / "golden.json").write_text(json.dumps(GOLDEN, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".context/\n", encoding="utf-8")
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a],
                                  capture_output=True, text=True)
    g("init", "-q", "-b", "dev")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return repo, g("rev-parse", "HEAD").stdout.strip()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-drill-discovery-"))
    repo, base = make_repo(tmp)
    os.chdir(repo)  # the engine reads the product config from the checkout it runs in
    script = repo / ".context" / "fake-script.json"
    script.parent.mkdir(parents=True)
    script.write_text(json.dumps({"discovery": {"report": {"finds": FINDS}}}), encoding="utf-8")
    os.environ["FAKE_SCRIPT"] = str(script)
    from hb.contract import battery
    from hb.runner import briefs
    from hb.beats import discovery
    from hb.runner import schemas
    from hb.record.journal import Journal
    c = json.loads((repo / ".claude" / "harness-config.json").read_text(encoding="utf-8"))
    before = (repo / "golden.json").read_text(encoding="utf-8")
    j = Journal("drill-discovery", c)
    ev = j.dir / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    seat_kw = dict(runtime="fake", timeout_min=5, shadow=True, evidence=ev, rate_limit_wait_min=0)

    finds = discovery.run_after_build(j, c, CONTRACT, repo, base, "drill", {"origin": "http://venue"}, seat_kw)
    check("discovery: the fake seat's two finds come back, each tagged with its case set",
          len(finds) == 2 and all(f["set"] == "pull" for f in finds), finds)
    line = [e for e in j.events() if e.get("kind") == "discovery-finds"]
    check("discovery: the journal says discovery-finds n=2 with 2 proposed",
          len(line) == 1 and line[0].get("n") == 2 and line[0].get("proposed") == 2, line)
    seat_brief = next((j.dir / "seats").glob("discovery-*/brief.md")).read_text(encoding="utf-8")
    check("discovery: the seat's brief is the discovery page, the model item only and its golden cases",
          seat_brief.startswith("# Discovery") and "a-pull: pulled 3 whole" in seat_brief
          and '"S-01"' not in seat_brief, seat_brief[-600:])

    facts = discovery.finds_as_fix_facts(finds)
    build_brief = briefs.render("build", {"Goal": CONTRACT["goal"], **facts},
                                {"issue": "#7", "run_id": j.run_id, "attempt": 2, "base_sha": base, "cwd": str(repo),
                                 "evidence_dir": str(ev), "output_path": str(ev / "r.json")})
    check("fix facts: DISCOVERY_FINDS carries both finds into the builder's next brief",
          len(facts.get("DISCOVERY_FINDS") or []) == 2 and "## DISCOVERY_FINDS" in build_brief
          and all(f["said"] in build_brief for f in FINDS), facts)
    check("fix facts: no finds, no key", discovery.finds_as_fix_facts([]) == {})

    after = (repo / "golden.json").read_text(encoding="utf-8")
    golden = json.loads(after)
    proposed = golden.get("proposed", {}).get("pull") or []
    check("golden: the set grows by two proposed cases in the frozen cases' shape",
          len(proposed) == 2 and all(p["status"] == "proposed" and p["family"] == "pull" and p["steps"][0]["say"]
                                     for p in proposed), proposed)
    frozen_text = before.rstrip()[:-1].rstrip()
    check("golden: every byte before the proposed list is the file as it was, frozen cases included",
          after.startswith(frozen_text) and golden["cases"] == GOLDEN["cases"], after[:200])
    again, _ = battery.propose_golden(finds, repo, c)
    check("golden: the same finds proposed again add nothing", again == 0
          and (repo / "golden.json").read_text(encoding="utf-8") == after)
    (repo / "golden.json").write_text(json.dumps(GOLDEN), encoding="utf-8")
    n, why = battery.propose_golden(finds, repo, c)
    check("golden: a file the writer cannot reproduce is left alone and says why",
          n == 0 and "frozen" in str(why) and (repo / "golden.json").read_text(encoding="utf-8") == json.dumps(GOLDEN), why)

    rows = battery.fold([], CONTRACT["items"], {}, j, finds)
    m = next(r for r in rows if r["item"] == "M-01")
    check("fold: the model item's row keeps the proposed count", m.get("proposed") == 2, m)

    # a discovery seat that lies: one that crashes twice finds nothing and says why; one that
    # reports a find on an item the contract has no model item for has that find dropped
    script.write_text(json.dumps({"discovery": {"report": {"finds": FINDS}}, "discovery.2": {"mode": "crash"},
                                  "discovery.3": {"mode": "crash"},
                                  "discovery.4": {"report": {"finds": [dict(FINDS[0], item="X-99")]}}}), encoding="utf-8")
    lost = discovery.run_after_build(j, c, CONTRACT, repo, base, "drill-crash", {"origin": "http://venue"}, seat_kw)
    said = [e for e in j.events() if e.get("kind") == "discovery-finds" and e.get("slice") == "drill-crash"]
    check("discovery lies: a seat that crashed finds nothing and the journal says why",
          lost == [] and said and "discovery seat:" in str(said[-1].get("why")), said)
    foreign = discovery.run_after_build(j, c, CONTRACT, repo, base, "drill-foreign", {"origin": "http://venue"}, seat_kw)
    check("discovery lies: a find on an item that is no model item of the contract is dropped", foreign == [], foreign)

    bad = {"schema": "discovery-report/v1", "served_sha": "x", "finds": [{"item": "M-01", "said": "", "wrong": "w"}]}
    check("schema: a find with no sentence is refused by name",
          any("`said` is empty" in p for p in schemas.check_discovery(bad)), schemas.check_discovery(bad))

    audit = briefs.render("audit", {"Contract items": CONTRACT["items"], "The venue": {"origin": "http://venue"}},
                          {"issue": "#7", "run_id": j.run_id, "attempt": 1, "cwd": str(repo),
                           "evidence_dir": str(ev), "output_path": str(ev / "a.json")})
    check("audit: the brief renders with no diff, no checks and no plan file",
          audit.startswith("# Audit") and "diff --git" not in audit and "## Checks" not in audit
          and "plan.md" not in audit and "verify-report/v2" in audit, audit[:300])
    roles = ROOT / "roles"
    check("roles: discovery is under half a page, audit under a page, verifier is gone",
          len((roles / "discovery.md").read_text(encoding="utf-8").splitlines()) < 45
          and len((roles / "audit.md").read_text(encoding="utf-8").splitlines()) < 90
          and not (roles / "verifier.md").exists())

    os.chdir(ENGINE)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  {len(PASS)} passed · {len(FAIL)} failed" + (f"  -> {FAIL}" if FAIL else ""))
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
