#!/usr/bin/env python
"""Drills for the one loop (hb.run.loop): the checks run case by case, the battery under its
ceiling, the reds to the builder relaunched, the cap per problem, the audit and its count of
disagreements; and the gates, the kept venue and the context the runner records. In process,
with the seats, the checks runner, the battery runner, the gates and the venue faked.

Run: python drills/beats/loop.py [names]
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
from fixture.common import PASS, FAIL, check  # noqa: E402,F401

from hb.beats import loop
from hb.beats import drive
from hb.venue import standin
from hb.runner import adapters
from hb.contract import policy
from hb.runner import seating
from hb.gates import gating
from hb.rig import gitops
from hb.runner import reader
from hb.contract import checks
from hb.contract import battery
from hb.runner import fixing
from hb.record.journal import Journal
from hb.venue import adapter  # noqa: E402
from hb.venue import admission  # noqa: E402
from hb.venue import machine  # noqa: E402
from hb.rig import versions  # noqa: E402
from hb.rig import common as common
from hb.record import journal as jm


def _receipt(item, state, substate=None, observed="seen"):
    return {"item": item, "state": state, "substate": substate, "observed": observed, "seen": observed,
            "situation": "venue", "paths_tried": ["happy"], "plan": "plan.md", "tenant_id": "tenant-1", "evidence": ["plan.md"]}


def _scripted_receipt(item, st):
    """A scripted state reads `BROKEN`, `UNMEASURED/verifier-failed`, or {state, observed}."""
    if isinstance(st, dict):
        return _receipt(item, *str(st.get("state", "WORKS")).split("/"), observed=st.get("observed", "seen"))
    return _receipt(item, *str(st).split("/"))


def _rule(item, cases, **extra):
    return {"item": item, "drivable": True, "kind": "api", "files": ["src/x.py"], "when": "w", "then": "t",
            "cases": [{"id": c, "when": "w", "then": "t", "boundary": "wire"} if isinstance(c, str) else c for c in cases],
            **extra}


CONTRACT = {"goal": "g", "issue": "#7", "rulings": "- R1: T-01 opens the drawer\n\n- R2: T-02 hides the row",
            "items": [_rule("T-01", ["T-01-a"], kind="screen", files=["web/z.tsx"]),
                      _rule("T-02", ["T-02-a"])]}
SIX = {"goal": "g", "issue": "#7", "rulings": "",
       "items": [_rule(f"R-0{i}", [f"R-0{i}-a"]) for i in range(1, 7)]}
A = types.SimpleNamespace(shadow=True, no_venue=False, real_llm=False, seat_timeout_min=1)
BATTERY_CFG = {"battery": {"command": "fake-runner"}}
# what every run_loop reads of the product config: the checks folder and the ledger file
BASE_CFG = {"paths": {"checks": "checks/", "ledger_file": "docs/ledger/delivery.jsonl"},
            "branches": {"landing": "dev", "promotion_base": "main"}}
# where the drill's product keeps its model's code (config `model.paths`)
MODEL_PATHS = ["src/model/"]
# the drill's product config as the engine reads it from disk: its own stop kinds, and its test
# runner's and database's faults as signatures, anchored the way the runner writes them
PRODUCT = {"stops": {"extra": ["wages", "tenant"]}, "gates": {"signatures": [
    {"pattern": r"^\[gw\d+\] node down", "cause": "worker-crash", "recovery": "room"},
    {"pattern": r"^E\s+psycopg2?\.OperationalError: could not connect to server", "cause": "db-starting",
     "recovery": "lease"},
    {"pattern": r"^E\s+(?:sqlalchemy\.exc\.OperationalError: \(psycopg2\.OperationalError\) |psycopg2?\.OperationalError: )"
                r"(?:server closed the connection unexpectedly|SSL SYSCALL error: EOF detected|the server terminated abnormally)",
     "cause": "db-dropped", "recovery": "lease"}]}}
# the verdict reads the product's signatures from the config on disk; this drill has none on disk
from hb.gates import verdict as _verdict  # noqa: E402
_verdict.cfg_or_empty = lambda: PRODUCT


def _model(item, case_set):
    return {"item": item, "drivable": True, "kind": "chat-verb", "held_by": "model", "battery": case_set,
            "pass_rate": "4/5", "files": ["src/model/x.py"], "when": "w", "then": "the pull card is right"}


class Fakes:
    """Seats, the checks runner, the battery runner, gates, venue and git faked around the
    loop; `script` says what each answers:

      checks    one {case: failing line} per call of the checks runner, in order; a case
                not named passes
      battery   {item: [WORKS|BROKEN, ...]} one per run of the item's set
      usd       what each battery run says it spent, or None for a runner that prints none
      audits    one {item: state} per audit drive; an item not named WORKS
      reviews   one findings list per review seat
      fix_moves whether a fix seat commits (bool or a function of the fix count)
      host_free_gb  the host's free memory in GB as a function of the venue keys asked so far;
                set, every fake venue takes its grant through the real admission door
      drive_s   how long a fake drive of one page holds its lane (default 0.05)
      offers    the capabilities the venue declares (default ui and model)
    """

    def __init__(self, repo, script):
        self.repo, self.script = repo, script
        self.seats, self.venues, self.tip = [], 0, script.get("tip", 0)
        self.ups, self.downs, self.keeps, self.threads = [], [], [], []
        self.checked, self.batteries, self.reals = [], [], []

    def __enter__(self):
        s = self

        def sit(j, role, facts, wt, base_sha, issue, label=None, **kw):
            label = label or role
            s.seats.append((label, label, facts))
            if label == "fix":
                if s.script.get("fix_raises") == len([x for x in s.seats if x[0] == "fix"]):
                    raise RuntimeError("the runner was killed inside the fix seat")
                if s.script.get("fix_lands_then_dies") == len([x for x in s.seats if x[0] == "fix"]):
                    s.tip += 1
                    raise RuntimeError("the runner was killed after the fix landed")
                moves = s.script.get("fix_moves", True)
                if callable(moves):
                    moves = moves(len([x for x in s.seats if x[0] == "fix"]))
                if moves:
                    s.tip += 1
                return {"result": "ok", "report": {"schema": "build-note/v1", "items": []}, "seat": f"fix-{len(s.seats)}"}
            if role == "review":
                queued = s.script.setdefault("reviews", [])
                findings = queued.pop(0) if queued else []
                if findings == "crashed":
                    return {"result": "crashed", "report": None, "seat": "review-x"}
                rep = findings if isinstance(findings, dict) else {"findings": findings}
                n = j.next_attempt("review")
                (j.seat_dir("review", n) / "report.json").write_text(json.dumps(rep), encoding="utf-8")
                return {"result": "ok", "report": rep, "seat": f"review-{n}"}
            if role == "discovery":
                return {"result": "ok", "report": {"finds": s.script.get("finds") or []}}
            return {"result": "ok", "report": {}}

        def drive_run(j, vfacts, v, wt, base_sha, issue, seat_kw, c, ids, scores=None):
            time.sleep(s.script.get("drive_s", 0.05))
            s.threads.append(threading.current_thread().name)
            s.seats.append(("verify", "verify", dict(vfacts, asked=list(ids))))
            queued = s.script.setdefault("audits", [])
            st = queued.pop(0) if queued else {}
            rows = [_scripted_receipt(i, st.get(i, "WORKS")) for i in ids]
            items = [it for it in vfacts.get("Contract items") or [] if it["item"] in set(ids)]
            return {"result": "ok", "report": {"served_sha": f"tip{s.tip}", "items": battery.fold(rows, items, scores or {}, j)}}

        def checks_run(c, root, items, venue, out_dir, identity, ids=None, composed=False, timeout_min=30, note=None):
            n = len(s.checked)
            s.checked.append({"ids": list(ids) if ids else None, "composed": composed, "venue": venue})
            plan = s.script.get("checks") or []
            lines = plan[n] if n < len(plan) else {}
            rows = []
            for it in items:
                for case in it.get("cases") or []:
                    if ids and case["id"] not in ids:
                        continue
                    if case.get("after_compose") and not composed:
                        rows.append(checks._row(it["item"], case, "UNMEASURED", "after-compose", "after compose", identity))
                        continue
                    line = lines.get(case["id"])
                    rows.append(checks._row(it["item"], case, "BROKEN" if line else "WORKS", "failed" if line else None,
                                            line or "passed", identity, line=line, check=f"checks/{it['item']}.py",
                                            sha="s", said="BROKEN" if line else "WORKS"))
            return rows

        def run_one(j, c, item, venue, wt, evidence, timeout_min, base_sha):
            s.batteries.append((item["item"], item.get("k")))
            plan = s.script.setdefault("battery", {}).setdefault(item["item"], [])
            state = plan.pop(0) if plan else "WORKS"
            out = Path(evidence) / "battery" / item["item"]
            out.mkdir(parents=True, exist_ok=True)
            summary = {"cases": 2, "k": item.get("k")}
            if s.script.get("usd") is not None:
                summary["usd"] = s.script["usd"]
            (out / "runner.out").write_text(json.dumps(summary) + "\n", encoding="utf-8")
            failed = [] if state == "WORKS" else ["pull-02"]
            row = {"item": item["item"], "state": state, "substate": None if state == "WORKS" else "failed",
                   "observed": f"battery {item['battery']}", "seen": "battery", "evidence": [str(out / "runner.out")],
                   "tenant_id": "tenant-1", "battery": {"set": item["battery"], "pass_k": 1.0 if not failed else 0.5,
                                                  "bar": "4/5", "failed": failed, "k": item.get("k")}}
            j.append("battery-run", item=item["item"], set=item["battery"], state=state, k=item.get("k"))
            return row, {}

        def venue_up(j, slug, wt, real_llm=False, key="venue-up", attempt=1, give_up=None):
            s.venues += 1
            s.ups.append(key)
            s.reals.append((key, real_llm))
            release, t0 = None, time.time()
            if s.script.get("host_free_gb"):
                # the fake venue takes the real grant, so a drill measures admission's decision, not a script's
                release = admission.hold("venue", f"{j.run_id} {key}", note=j.append, sample=lambda: None, give_up=give_up)
                if release is None:
                    return {"venue": None, "no_room": True, "admission_wait_s": round(time.time() - t0, 2), "error": "no room"}
            if s.venues <= s.script.get("first_venues_fail", 0):
                return {"venue": None, "error": "the venue answered unhealthy"}
            if release:
                standin._VENUE_LEASES[f"v-{s.venues}"] = release
            return {"venue": f"v-{s.venues}", "served_sha": f"served{s.venues}",
                    "record": {"browser_profile": "p", "tz": "UTC", "tenant_id": "tenant-1"},
                    "admission_wait_s": round(time.time() - t0, 2)}

        def measure(j, wt, slug, base_sha, changed, c, round_key, heavy=False, only=None):
            reds = s.script.get("gate_red_rounds", set())
            n = int(round_key.split("-r")[1].split("-")[0]) if "-r" in round_key else 0
            return [{"name": "suite" if heavy else "tsc", "true_exit": 1 if n in reds else 0}]

        self.saved = [(seating, "sit", seating.sit), (drive, "run", drive.run), (standin, "venue_up", standin.venue_up),
                      (standin, "venue_down_keep", standin.venue_down_keep), (standin, "venue_down", standin.venue_down),
                      (gating, "measure", gating.measure), (gating, "settle_heavy", gating.settle_heavy),
                      (gitops, "push", gitops.push), (gitops, "changed_paths", gitops.changed_paths),
                      (gitops, "head_of", gitops.head_of), (gitops, "in_history", gitops.in_history),
                      (reader, "codex_read", reader.codex_read), (reader, "wanted", reader.wanted),
                      (checks, "run", checks.run), (battery, "run_one", battery.run_one),
                      (common, "state_dir", common.state_dir), (jm, "state_dir", jm.state_dir),
                      (machine, "state_dir", machine.state_dir), (machine, "settings", machine.settings), (admission, "read_free", admission.read_free),
                      (admission, "read_total", admission.read_total), (adapter, "block", adapter.block),
                      (adapter, "capabilities", adapter.capabilities), (common, "cfg", common.cfg)]
        # the venue's capabilities and the product's model paths, as the config would say them
        adapter.block = lambda c=None: {"ui": {"driver": {"command": "drv", "args": ["{session}"]}}}
        adapter.capabilities = lambda c=None: tuple(s.script.get("offers", ("ui", "model")))
        common.cfg = lambda: {"model": {"paths": MODEL_PATHS}, "branches": {"landing": "dev", "promotion_base": "main"}}
        if s.script.get("host_free_gb"):
            # a 32 GB host whose free memory the script sets; fast polls, and no forced start, so the
            # drill reads admission's decision and never this host's clock
            admission.read_total = lambda: 32 * (1 << 30)
            admission.read_free = lambda: int(s.script["host_free_gb"](s.ups) * (1 << 30))
            machine.settings = lambda c=None: {**machine.DEFAULTS, "poll_s": 0.05, "settle_s": 0.05, "settle_max_s": 1,
                                               "force_after_s": 600}
        seating.sit = sit
        if not s.script.get("real_drive"):
            drive.run = drive_run
        checks.run = checks_run
        battery.run_one = run_one
        standin.venue_up = venue_up
        standin.venue_down_keep = lambda j, v, wt, key: (standin._release_venue(v), s.keeps.append((v, len(s.seats))))
        standin.venue_down = lambda j, v, wt, key="venue-down": (standin._release_venue(v), s.downs.append(v))
        gating.measure = measure
        gating.settle_heavy = lambda j, wt, base_sha, slug, gates, pre: (gates, pre)
        gitops.push = lambda j, wt, branch, shadow, key: None
        # `changed_since` names what moved after a given tip (base..HEAD stays the product's change)
        gitops.changed_paths = lambda wt, base, to="HEAD": (s.script.get("changed_since", {}).get(base, s.script.get("changed", ["src/x.py"]))
                                                             if to == "HEAD" else s.script.get("touched", []))
        gitops.head_of = lambda wt: f"tip{s.tip}"
        gitops.in_history = lambda wt, sha: True
        reader.wanted = lambda a: False
        common.state_dir = lambda c=None: self.repo / "state"
        jm.state_dir = common.state_dir
        machine.state_dir = common.state_dir  # machine binds the name at import; the real grant's state goes under the drill's repo
        return self

    def __exit__(self, *exc):
        for mod, name, val in self.saved:
            setattr(mod, name, val)

    def roles(self):
        return [x[0] for x in self.seats]

    def fixes(self):
        return [x[2] for x in self.seats if x[0] == "fix"]


def _journal(name):
    j = Journal(name)
    if not j.fact("harness"):
        j.phase("opened", beat="slice", harness=versions.harness_version())
    return j


def run_loop(repo, script, name, contract=None, c=None, **kw):
    with Fakes(repo, script) as f:
        j = _journal(name)
        stops = []
        st = loop.run_loop(j, A, {**BASE_CFG, **(c or {})}, contract or CONTRACT, repo, "b", "base", "7", {}, repo / "ev",
                           stops, {}, **kw)
        return st, f, j, stops


def _events(j, kind):
    return [e for e in j.events() if e.get("kind") == kind]


def _by_item(receipts):
    return {r["item"]: r for r in receipts}


# --- the loop: checks, battery, builder warm, cap per problem, one audit ---


def drill_a_rule_nobody_checked_never_reads_green(repo):
    """The builder wrote no check for a rule's cases: the checks' rows read UNMEASURED no-check.
    A rule the blind audit drove takes the audit's row with the missing check named; a rule
    nobody graded stays UNMEASURED, never green. The ledger takes the battery's own reasons."""
    from hb.record.ledger import _ledger_state_check
    rows = {"R-01-a": checks._row("R-01", {"id": "R-01-a", "boundary": "wire"}, "UNMEASURED", "no-check", "no check for R-01-a", {}),
            "R-02-a": checks._row("R-02", {"id": "R-02-a", "boundary": "wire"}, "UNMEASURED", "no-check", "no check for R-02-a", {})}
    kinds = {"checked": [_rule("R-01", ["R-01-a"]), _rule("R-02", ["R-02-a"]), _rule("R-03", ["R-03-a"])], "model": [], "audited": []}
    audit = [_receipt("R-01", "WORKS"), _receipt("R-02", "BROKEN", "failed", observed="the card drew no total")]
    from types import SimpleNamespace
    j = SimpleNamespace(append=lambda *a, **k: None)
    out = {r["item"]: r for r in loop.fold_receipts(j, {"items": []}, kinds, rows, {}, audit, [], [], repo / "ev-nocheck", {})}
    check("no check: the audit's WORKS stands, the missing check named",
          out["R-01"]["state"] == "WORKS" and "no check" in str(out["R-01"].get("checks_gap")), out["R-01"])
    check("no check: the audit's BROKEN stands with its words",
          out["R-02"]["state"] == "BROKEN" and "no total" in str(out["R-02"].get("observed")), out["R-02"])
    check("no check: a rule neither the checks nor the audit graded stays UNMEASURED",
          out["R-03"]["state"] == "UNMEASURED", out["R-03"])
    check("no check: the ledger takes no-battery and battery-runner as reasons",
          _ledger_state_check("UNMEASURED", "no-battery") is None and _ledger_state_check("UNMEASURED", "battery-runner") is None)


def drill_a_red_is_its_id_and_ends_after_two_fixes(repo):
    """Q8: the behaviour is the red's id (a check case, a gate, a finding). A case still red
    after two fixes ends by name though its failing line moved each round; a finding that went
    green and a new finding each get another round."""
    st, f, j, _ = run_loop(repo, {"checks": [{"T-02-a": "got 3"}, {"T-02-a": "got 2"}, {"T-02-a": "got 1"}, {}]}, "loop-moving")
    ended = _events(j, "ended-by-name")
    check("red id: two fix rounds, then the case ends by name though its line moved",
          len(f.fixes()) == 2 and [e["case"] for e in ended] == ["T-02-a"], (f.roles(), ended))
    check("red id: the case ships red", _by_item(st["receipts"])["T-02"]["state"] == "BROKEN", st["receipts"])
    # ruling 18: after the goal is met only a P1 opens another round; the new finding here is one
    one, two = _finding("F1", "src/x.py:3"), dict(_finding("F2", "src/x.py:9"), severity="P1")
    with tempfile.TemporaryDirectory(prefix="hb-loop-") as tmp:
        other = Path(tmp) / "repo"
        (other / "ev").mkdir(parents=True)
        (other / "state").mkdir(parents=True)
        st2, f2, j2, _ = run_loop(other, {"reviews": [[one], {"findings": [dict(one, status="fixed"), two]},
                                                      {"findings": [dict(two, status="fixed")]}]}, "loop-new-red")
        ended2 = _events(j2, "ended-by-name")
        ends2 = [e.get("result") for e in _events(j2, "loop")]
    check("red id: a finding that went green and a new one each get a round; nothing ends by name",
          len(f2.fixes()) == 2 and not ended2 and ends2[-1:] == ["clean"], (f2.roles(), ended2, ends2))
    three = dict(_finding("F3", "src/x.py:12"), severity="P2")
    with tempfile.TemporaryDirectory(prefix="hb-loop-") as tmp:
        other = Path(tmp) / "repo"
        (other / "ev").mkdir(parents=True)
        (other / "state").mkdir(parents=True)
        st3, f3, j3, _ = run_loop(other, {"reviews": [[one], {"findings": [dict(one, status="fixed"), three]}]}, "loop-p2-rides")
        rides = [e for e in j3.events() if e.get("kind") == "findings-ride"]
        ends3 = [e.get("result") for e in _events(j3, "loop")]
    check("ruling 18: a new P2 after the goal is met rides the record; no fix round opens for it",
          len(f3.fixes()) == 1 and rides and "F3" in rides[-1].get("ids", []) and ends3[-1:] == ["clean"], (f3.roles(), rides, ends3))


def drill_the_ceiling_stops_the_battery(repo):
    """Past `battery.ceiling_usd` the battery stops and the rest read "not run: ceiling", never
    green; a runner that prints no spend is journaled unmeasured and never stopped."""
    contract = {"goal": "g", "issue": "#7", "rulings": "",
                "items": [_model("M-01", "a"), _model("M-02", "b"), _model("M-03", "c")]}
    c = {"battery": {"command": "fake-runner", "ceiling_usd": 1.0}}
    st, f, j, _ = run_loop(repo, {"usd": 0.75}, "loop-ceiling", contract=contract, c=c)
    loop_runs = [i for i, k in f.batteries if k == 1]
    check("ceiling: two sets ran, the third did not", loop_runs == ["M-01", "M-02"], f.batteries)
    row = _by_item(st["receipts"])["M-03"]
    check("ceiling: the rest read not run: ceiling, never green",
          row["state"] == "UNMEASURED" and row["observed"].startswith("not run: ceiling"), row)
    check("ceiling: each spend is journaled", [e["usd"] for e in _events(j, "battery-spend")][:2] == [0.75, 0.75],
          _events(j, "battery-spend"))
    with tempfile.TemporaryDirectory(prefix="hb-loop-") as tmp:
        other = Path(tmp) / "repo"
        (other / "ev").mkdir(parents=True)
        (other / "state").mkdir(parents=True)
        _st, f2, j2, _ = run_loop(other, {"usd": None}, "loop-no-spend", contract=contract, c=c)
        spends = _events(j2, "battery-spend")
    check("ceiling: a runner that prints no usd runs every set", [i for i, k in f2.batteries if k == 1] == ["M-01", "M-02", "M-03"],
          f2.batteries)
    check("ceiling: and its spend is journaled unmeasured", spends and all(e.get("state") == "unmeasured" for e in spends), spends)


def drill_loop_clean_first_round(repo):
    """Everything green on the first round: gates, one review, the checks, the audit; no fix."""
    st, f, j, stops = run_loop(repo, {}, "loop-clean")
    check("clean: one review, then the audit", f.roles() == ["review", "verify"], f.roles())
    check("clean: one round, journaled clean", any(e.get("result") == "clean" and e.get("round") == 1 for e in _events(j, "loop")))
    check("clean: every row works", all(r["state"] == "WORKS" for r in st["receipts"]) and len(st["receipts"]) == 2, st["receipts"])
    review = [x[2] for x in f.seats if x[0] == "review"][0]
    check("clean: the first read is the whole diff", "Read" in review and "Diff" in review, list(review))
    check("clean: no stop", not stops)


def drill_a_resume_past_the_landings_commits_re_enters_the_ended_loop(repo):
    """The loop ended clean; the landing committed ledger rows past its tip; a resume finds the
    ended loop and stands on it: no new review, no new round, the audit's rows as they were."""
    st1, f1, j1, _ = run_loop(repo, {"audits": [{"T-01": "BROKEN/failed"}, {}]}, "loop-stands")
    check("stands: the audit's red took a fix that moved the tip", len(f1.fixes()) == 1 and f1.tip == 1, (f1.roles(), f1.tip))
    # a resume journals the compose tip before it enters the loop, as the wave door does
    j1.append("phase", phase="compose", tip="tip0")
    st2, f2, j2, stops = run_loop(repo, {"tip": 2, "changed_since": {"tip1": ["docs/ledger/delivery.jsonl"]}}, "loop-stands")
    check("stands: the second pass seats no review and no fix", f2.roles() == [], f2.roles())
    check("stands: journaled as the ended loop standing at the audit fix's tip",
          any(e.get("head") == "tip2" and e.get("tip") == "tip1" for e in _events(j2, "loop-stands")), _events(j2, "loop-stands"))
    check("stands: one loop-open in the journal", len(_events(j2, "loop-open")) == 1, len(_events(j2, "loop-open")))
    check("stands: the rows are the first pass's", _by_item(st2["receipts"]) == _by_item(st1["receipts"]), st2["receipts"])
    check("stands: the gates are the first pass's", [g["name"] for g in st2["gates"]] == [g["name"] for g in st1["gates"]], st2["gates"])
    check("stands: no stop", not stops)


def drill_loop_red_gate_is_a_fix_item_never_a_stop(repo):
    st, f, j, stops = run_loop(repo, {"gate_red_rounds": {1}}, "loop-gate")
    fail = (f.fixes() or [{}])[0].get(fixing.FAILING_CHECKS) or []
    check("gate: a red gate starts a fix round", len(f.fixes()) == 1 and any(g.get("name") == "tsc" for g in fail), f.roles())
    check("gate: a red gate never stops the run", not stops, stops)
    check("gate: green on the next round, the loop ends clean",
          any(e.get("result") == "clean" for e in _events(j, "loop")) and not gating.gates_red(st["gates"]), st["gates"])


def _finding(fid, decider, severity="P1"):
    return {"id": fid, "class": "broken", "severity": severity, "confidence": 9, "summary": f"{fid} wrong",
            "status": "open", "decider": decider, "quote": {"file": decider.split(":")[0], "text": "x"}}


def drill_a_review_that_never_answered_stops_the_run(repo):
    """The per-tip review is an instrument: a review seat that crashes at the first read, or
    at the read of a fix's tip, leaves that tip unreviewed, and the run stops on it by name;
    the loop never carries an unread tip toward the PR."""
    st, f, j, stops = run_loop(repo, {"reviews": ["crashed"]}, "loop-review-crash")
    check("unreviewed: a first read that never answered stops the run, named",
          any("the diff is unreviewed" in x for x in stops), stops)
    with tempfile.TemporaryDirectory(prefix="hb-loop-") as tmp:
        other = Path(tmp) / "repo"
        (other / "ev").mkdir(parents=True)
        (other / "state").mkdir(parents=True)
        own = _finding("F1", "src/x.py:3")
        _st, f2, _j, stops2 = run_loop(other, {"reviews": [[own], "crashed"]}, "loop-review-crash-after-fix")
    check("unreviewed: a read of the fix's tip that never answered stops the run too",
          "fix" in f2.roles() and any("the diff is unreviewed" in x for x in stops2), (f2.roles(), stops2))


def drill_loop_ends_when_fix_moves_nothing(repo):
    st, f, j, _ = run_loop(repo, {"checks": [{"T-02-a": "x"}], "fix_moves": False}, "loop-nothing")
    check("nothing: one fix round, then the loop ends moved-nothing",
          len(f.fixes()) == 1 and any(e.get("result") == "moved-nothing" for e in _events(j, "loop")), f.roles())
    check("nothing: the red row stays red", _by_item(st["receipts"])["T-02"]["state"] == "BROKEN")


def drill_after_compose_cases_run_on_the_composed_branch(repo):
    """A case marked after_compose reads UNMEASURED on a slice and runs on the composed branch."""
    contract = {"goal": "g", "issue": "#7", "rulings": "",
                "items": [_rule("T-01", [{"id": "T-01-a", "boundary": "both", "after_compose": True}])]}
    st, f, _j, _ = run_loop(repo, {}, "loop-slice-compose", contract=contract)
    check("compose: the slice's checks run with composed False", [x["composed"] for x in f.checked] == [False], f.checked)
    check("compose: on the slice the case is unmeasured", _by_item(st["receipts"])["T-01"]["state"] == "UNMEASURED")
    with tempfile.TemporaryDirectory(prefix="hb-loop-") as tmp:
        other = Path(tmp) / "repo"
        (other / "ev").mkdir(parents=True)
        (other / "state").mkdir(parents=True)
        st2, f2, _j2, _ = run_loop(other, {}, "loop-composed", contract=contract, mode="composed")
    check("compose: the composed loop runs it", [x["composed"] for x in f2.checked] == [True]
          and _by_item(st2["receipts"])["T-01"]["state"] == "WORKS", (f2.checked, st2["receipts"]))


def drill_a_broken_check_survives_a_rerun_that_did_not_run(repo):
    """A case the checks graded BROKEN keeps BROKEN when the rerun's runner never answered."""
    with Fakes(repo, {"checks": [{"T-02-a": "the row is listed"}]}) as f:
        j = _journal("loop-broken-survives")
        real = checks.run

        def flaky(c, root, items, venue, out_dir, identity, ids=None, **kw):
            rows = real(c, root, items, venue, out_dir, identity, ids=ids, **kw)
            flaky.calls += 1
            if flaky.calls > 1:
                rows = [dict(r, state="UNMEASURED", substate="checks-runner", line=None, observed="the runner exited 3")
                        for r in rows]
            return rows
        flaky.calls = 0
        checks.run = flaky
        f.script["fix_moves"] = lambda k: k == 1
        st = loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
    row = _by_item(st["receipts"])["T-02"]
    check("survives: the rule stays BROKEN, the case names the rerun that did not run",
          row["state"] == "BROKEN" and any(str(r.get("rerun_invalid")).startswith("checks-runner")
                                           for r in st["check_rows"]), (row, st["check_rows"]))


def drill_loop_resume_continues_the_round(repo):
    """A runner killed inside the fix seat resumes at that round: the measured lanes stand, so
    the checks are not run again for it, and one fix seat finishes the round."""
    script = {"checks": [{"T-02-a": "x"}], "fix_raises": 1}
    with Fakes(repo, script) as f:
        j = _journal("loop-resume")
        try:
            loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
            crashed = False
        except RuntimeError:
            crashed = True
        before = len(f.checked)
        script["fix_raises"] = None
        st = loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
    check("resume: the first pass died in the fix seat after round one's checks", crashed and before == 1, (crashed, before))
    check("resume: re-entered at round one", any(e.get("round") == 1 for e in _events(j, "loop-resume")), _events(j, "loop-resume"))
    check("resume: round one's checks stood; round two ran once", len(f.checked) == 2, f.checked)
    reads = [x[2] for x in f.seats if x[0] == "review"]
    check("resume: round one's review stood across the resume; round two reads only the fix",
          len(reads) == 2 and "Read" in reads[0] and "Diff since your last read" in reads[1], [list(r) for r in reads])
    check("resume: both rows end green", {r["item"]: r["state"] for r in st["receipts"]} == {"T-01": "WORKS", "T-02": "WORKS"})


def drill_audit_resumes_where_it_was_killed(repo):
    """A runner killed after the audit's fix landed and before its done resumes inside the
    audit: the first drive stands, the fix is reconciled from the tip and never sat again, and
    only the audit's red is driven again on the fixed tip."""
    script = {"audits": [{"T-02": "BROKEN/failed"}, {}], "fix_lands_then_dies": 1}
    with Fakes(repo, script) as f:
        j = _journal("audit-resume")
        try:
            loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
            crashed = False
        except RuntimeError:
            crashed = True
        script["fix_lands_then_dies"] = None
        st = loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
    drives = [x[2]["asked"] for x in f.seats if x[0] == "verify"]
    check("audit resume: the first pass died with the fix landed and no loop end journaled",
          crashed and not [e for e in _events(j, "loop") if e.get("result") != "venue-retry"][:-1], _events(j, "loop"))
    check("audit resume: one audit drive of everything, then only its red", drives == [["T-01", "T-02"], ["T-02"]], drives)
    check("audit resume: one fix seat across both passes, its landing reconciled from the tip",
          len(f.fixes()) == 1 and any(e.get("op") == "fix-audit" and e.get("reconciled") for e in _events(j, "done")),
          (f.roles(), _events(j, "done")))
    check("audit resume: the red reads green after the re-drive", _by_item(st["receipts"])["T-02"]["state"] == "WORKS",
          st["receipts"])


def drill_review_loop(repo):
    """A slice inside a wave: the whole-diff read, the builder on its findings, the read of the
    diff since the last read; no venue, no checks, no tester."""
    finding = _finding("F1", "src/x.py:3")
    with Fakes(repo, {"reviews": [[finding], {"findings": [dict(finding, status="fixed")]}]}) as f:
        j = _journal("review-loop")
        _report, open_ = loop.review_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", {}, {}, [],
                                          finds=[{"item": "T-01", "said": "pull two", "wrong": "no card"}])
    check("review loop: review, fix, the next read", f.roles() == ["review", "fix", "review"], f.roles())
    check("review loop: the next read is the diff since the last read",
          "Diff since your last read" in [x[2] for x in f.seats][2], list([x[2] for x in f.seats][2]))
    check("review loop: the fix carries the discovery finds", "DISCOVERY_FINDS" in f.fixes()[0], list(f.fixes()[0]))
    check("review loop: nothing open at the end", open_ == [], open_)
    check("review loop: no venue and no checks", f.venues == 0 and not f.checked and "verify" not in f.roles())


def drill_audit_disagreements_count_both_ways(repo):
    """The count compares per case: a check green the audit saw red is a false green, a check
    red the audit saw green a false red; a BROKEN row naming one case counts only that case."""
    rows = [checks._row("R", {"id": "R-a", "boundary": "wire"}, "WORKS", None, "ok", {}),
            checks._row("R", {"id": "R-b", "boundary": "wire"}, "WORKS", None, "ok", {}),
            checks._row("S", {"id": "S-a", "boundary": "wire"}, "BROKEN", "failed", "x", {}, line="x")]
    audit = [_receipt("R", "BROKEN", "failed", observed="R-b: the row is listed"), _receipt("S", "WORKS")]
    dis = policy.audit_disagreements(rows, audit)
    check("audit: one false green on the case the row names, one false red",
          [x["case"] for x in dis["false_greens"]] == ["R-b"] and [x["case"] for x in dis["false_reds"]] == ["S-a"], dis)
    folded = {r["item"]: r for r in policy.receipts_from_checks(rows, [{"item": "R"}, {"item": "S"}, {"item": "T"}])}
    check("audit: the fold is one receipt per rule, red naming its case and line",
          folded["S"]["state"] == "BROKEN" and folded["S"]["observed"] == "case S-a: x" and folded["R"]["state"] == "WORKS"
          and "T" not in folded, folded)


# --- the lanes ---


def drill_a_change_to_the_models_code_drives_on_the_real_model(repo):
    """A diff under the product's model code (config `model.paths`, src/model/ here) is judged
    only on the real model, though no item is a chat verb: the audit's venues go up real and
    the journal names the doors and the path; a diff elsewhere stays on the fake."""
    st, f, j, _ = run_loop(repo, {"changed": ["src/model/tools/pull.py"]}, "real-by-path")
    said = _events(j, "real-model")
    audit_ups = [real for key, real in f.reals if "audit" in key]
    check("model code: the audit's venues go up on the real model",
          audit_ups and all(audit_ups), str(f.reals))
    check("model code: the journal names the doors driven real and the path that asked",
          len(said) == 1 and said[0].get("doors") == ["T-01", "T-02"]
          and "src/model/tools/pull.py" in str(said[0].get("why")), str(said))
    st, f, j, _ = run_loop(repo, {"changed": ["src/services/x.py"]}, "fake-by-path")
    check("model code: a diff elsewhere drives on the fake, with no real-model line",
          not any(real for _, real in f.reals) and not _events(j, "real-model"), str(f.reals))


def drill_loop_round_lanes_at_once(repo):
    """The gates, the review and the checks of a round start together on one tip; the venue
    comes up while the gates run; a red gate cancels neither the review nor the checks. The
    gates lane holds until the review seat and the venue have started: run one after the other,
    they never would, and the drill reads that from the flags, not from the clock."""
    review_in, venue_in, seen = threading.Event(), threading.Event(), {}

    def gates_wait(fn):
        def wrapped(*a, **kw):
            if "-r1" in str(a[6] if len(a) > 6 else kw.get("round_key")) and "gates" not in seen:
                seen["gates"] = (review_in.wait(30), venue_in.wait(30))
            return fn(*a, **kw)
        return wrapped

    def flag(event, fn):
        def wrapped(*a, **kw):
            event.set()
            return fn(*a, **kw)
        return wrapped

    with Fakes(repo, {"gate_red_rounds": {1}}) as f:
        gating.measure, seating.sit = gates_wait(gating.measure), flag(review_in, seating.sit)
        standin.venue_up = flag(venue_in, standin.venue_up)
        j = _journal("loop-lanes")
        loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
    review_while, venue_while = seen.get("gates", (False, False))
    check("lanes: the review starts while the gates run", review_while, seen)
    check("lanes: the venue comes up while the gates run", venue_while, seen)
    check("lanes: round one's red gate cancelled neither the review nor the checks",
          f.roles()[0] == "review" and f.checked and "fix" in f.roles(), f.roles())


def drill_loop_second_reader_beside_review(repo):
    """The second reader reads beside the review, never before it; its claims pass a closing
    quote gate and join the list."""
    sr = {"id": "SR-1", "class": "broken", "severity": "P1", "confidence": 9, "summary": "T-02 leaks", "status": "open"}
    review_in, marks = threading.Event(), {}

    def codex_read(j, wt, base_sha, contract, evidence, timeout_min):
        # the read holds until the review has started; read one after the other it never would
        marks["review_began_during_the_read"] = review_in.wait(30)
        return {"fact": "read this file raw: x", "path": "x"}

    saved = (reader.codex_read, reader.gate)
    reader.codex_read = codex_read
    reader.gate = lambda j, wt, base_sha, contract, codex_in, seat_kw: [sr]
    try:
        with Fakes(repo, {}) as f:
            reader.wanted = lambda a: True
            reader.codex_read = codex_read
            inner = seating.sit

            def sit(j, role, facts, wt, base_sha, issue, label=None, **kw):
                if role == "review":
                    review_in.set()
                return inner(j, role, facts, wt, base_sha, issue, label=label, **kw)
            seating.sit = sit
            j = _journal("loop-reader")
            st = loop.run_loop(j, A, BASE_CFG, CONTRACT, repo, "b", "base", "7", {}, repo / "ev", [], {})
    finally:
        reader.codex_read, reader.gate = saved
    review = [x for x in f.seats if x[0] == "review"][0][2]
    check("reader: the review starts while the second read runs", marks.get("review_began_during_the_read") is True, marks)
    check("reader: the reviewer is told the read is beside it", review.get("FINDINGS_IN") == loop.READ_BESIDE,
          str(review.get("FINDINGS_IN")))
    fix = f.fixes()
    check("reader: the gated claim joins the fix list",
          fix and any(x.get("id") == "SR-1" for x in fix[0].get(fixing.FINDINGS_FOR_ME) or []), f.roles())
    check("reader: codex_in reaches the landers", st["codex_in"].get("path") == "x", st["codex_in"])


def drill_a_lost_venue_reads_unmeasured_never_green(repo):
    """A round whose venue never came up measured nothing: every case it owed reads UNMEASURED
    with the venue's error, never green, and the loop ends saying the venue never came up."""
    st, f, j, _ = run_loop(repo, {"first_venues_fail": 99, "fix_moves": False}, "loop-lost-venue")
    ends = [e.get("result") for e in _events(j, "loop")]
    check("lost venue: the loop ends venue-failed, by name", ends[-1:] == ["venue-failed"], ends)
    rows = [r for r in st["receipts"] if r.get("item") in ("T-01", "T-02")]
    check("lost venue: no row the venue owed reads green",
          rows and all(r.get("state") != "WORKS" for r in rows), [(r.get("item"), r.get("state"), r.get("observed")) for r in rows])


def drill_a_venue_without_ui_or_model_measures_nothing_it_cannot(repo):
    """A venue that declares neither `ui` nor `model` sits no drive seat and runs no battery:
    each row it owed reads UNMEASURED with `not offered: <capability>` as its observed, and the
    pictures at the base take no seat."""
    contract = {"goal": "g", "issue": "#7", "rulings": "",
                "items": [_rule("T-01", []), _model("M-01", "pull")]}
    st, f, j, _ = run_loop(repo, {"offers": (), "real_drive": True}, "loop-not-offered", contract=contract, c=BATTERY_CFG)
    rows = _by_item(st["receipts"])
    check("not offered: no audit seat sat, no battery ran", "verify" not in f.roles() and not f.batteries, (f.roles(), f.batteries))
    check("not offered: the drive's row reads not offered: ui, UNMEASURED",
          rows["T-01"]["state"] == "UNMEASURED" and rows["T-01"]["observed"] == "not offered: ui", rows["T-01"])
    check("not offered: the model item's row reads not offered: model, UNMEASURED",
          rows["M-01"]["state"] == "UNMEASURED" and rows["M-01"]["observed"] == "not offered: model", rows["M-01"])
    with Fakes(repo, {"offers": ()}) as f:
        j = _journal("pictures-not-offered")
        before = loop.capture_before(j, A, CONTRACT["items"], repo, "7", "base", repo / "ev", {}, "#7")
    check("not offered: the pictures at the base take no seat and say why",
          before == {} and not f.seats and any(e.get("why") == "not offered: ui" for e in _events(j, "sized")),
          _events(j, "sized"))


def drill_the_served_sha_is_the_venue_records(repo):
    """The ledger names the commit the venue said it served, from the venue's own receipt, never
    a probe: the loop hands the landers the audit venue's served sha."""
    st, f, j, _ = run_loop(repo, {}, "loop-served-sha")
    check("served sha: the loop's answer carries the audit venue's served sha",
          str(st.get("served_sha") or "").startswith("served"), st.get("served_sha"))


def drill_an_every_tip_gate_runs_at_the_tip_beside_the_suite(repo):
    """A gate the config marks `"at": "every-tip"` is heavy: the full suite at a tip runs it as
    the config writes it, beside the config's own suite, each once."""
    saved = gitops.changed_paths
    gitops.changed_paths = lambda wt, base, to="HEAD": ["src/engine/solver.py"]
    green = (0, "10 passed in 1s", 0)
    try:
        suite = {"name": "unit-tests", "heavy": True,
                 "cmd": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "auto", "--dist", "worksteal"]}
        engine = {"name": "engine-lane", "at": "every-tip", "cmd": ["python", "-m", "pytest", "-q", "-m", "engine_heavy"]}
        with GateHost(repo, {"pytest": [green, green, green]}) as h:
            rows = gating.full_suite(_opened("every-tip"), repo, "w", "base", {"gates": {"commands": [suite, engine]}, "branches": {"landing": "dev"}})
    finally:
        gitops.changed_paths = saved
    cmds = {r["name"]: r.get("cmd") or [] for r in rows}
    check("every tip: the marked gate runs as written, beside the suite",
          cmds.get("engine-lane") == ["python", "-m", "pytest", "-q", "-m", "engine_heavy"]
          and sorted(cmds) == ["engine-lane", "unit-tests"], str(cmds))
    check("every tip: each ran once", [n for n, _ in h.calls].count("pytest") == 2, str(h.calls))


def drill_a_hand_commit_inside_a_seat_window_is_named(repo):
    """A commit made by hand while a seat sat was absorbed into the seat's tip out, as if the
    seat had made it. Every seat commits as `gitops.SEAT_COMMITTER`; a commit on the first-
    parent line of the window with any other committer is named, and a seat's merge of others'
    commits is not."""
    import subprocess
    w = repo / "window"
    w.mkdir()
    base = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_EMAIL": "c@c"}

    def git(*a, who="owner"):
        return subprocess.run(["git", "-C", str(w), *a], capture_output=True, text=True,
                              env={**base, "GIT_COMMITTER_NAME": who}).stdout.strip()
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "base")
    tip_in = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "side")
    git("commit", "-q", "--allow-empty", "-m", "a sibling's work")
    git("checkout", "-q", "main")
    git("commit", "-q", "--allow-empty", "-m", "the seat's fix", who=gitops.SEAT_COMMITTER)
    git("commit", "-q", "--allow-empty", "-m", "a hand fix")
    git("merge", "-q", "--no-ff", "side", "-m", "the seat merges the sibling", who=gitops.SEAT_COMMITTER)
    foreign = gitops.foreign_commits(w, tip_in, git("rev-parse", "HEAD"))
    check("seat window: the hand commit is named, the seat's own commits and its merge's side are not",
          len(foreign) == 1 and foreign[0].endswith("a hand fix"), str(foreign))


def drill_kept_reader_needs_its_own_tip(repo):
    """A seat that only reads the tree is kept only at the tip it read; a builder's is kept
    while its tip is in the history."""
    saved = (gitops.head_of, gitops.in_history, common.state_dir, jm.state_dir)
    try:
        common.state_dir = lambda c=None: repo / "state"
        jm.state_dir = common.state_dir
        gitops.in_history = lambda wt, sha: True
        gitops.head_of = lambda wt: "tip-new"
        j = Journal("kept-exact")
        for label in ("verify.1", "fix"):
            (j.seat_dir(label, 1) / "report.json").write_text("{}", encoding="utf-8")
            j.append("seat", seat=label, attempt=1, state="ok", key="K", **{"pass": "old"}, tip_in="tip-old", tip_out="tip-old")
        check("kept: a tester of an older tip is not kept", seating.kept(j, "verify.1", "K", repo, exact=True) is None)
        check("kept: a build seat whose tip is in the history is kept", seating.kept(j, "fix", "K", repo) is not None)
        check("kept: a fix seat is kept only at the tip it left; the reducer reconciles its landed work",
              "fix" in seating.EXACT and seating.kept(j, "fix", "K", repo, exact=True) is None)
        gitops.head_of = lambda wt: "tip-old"
        seating._HANDED.clear()
        check("kept: a tester of this very tip is kept", seating.kept(j, "verify.1", "K", repo, exact=True) is not None)
        check("kept: testers, reviewers and the second reader read only",
              set(seating.READS_ONLY) == {"verify", "discovery", "review", "codex"})
    finally:
        gitops.head_of, gitops.in_history, common.state_dir, jm.state_dir = saved


def drill_venue_kept_by_the_products_reuse_table(repo):
    """The product's `venue.reuse` table decides: a change under a `keep` prefix keeps the
    standing venue; one under a `rebuild` prefix, or under no prefix, brings a fresh one."""
    calls = {"up": 0, "down": 0}
    saved = (standin.venue_up, standin.venue_down_keep, gitops.changed_paths, gitops.head_of, common.state_dir, jm.state_dir,
             adapter.block)
    try:
        adapter.block = lambda c=None: {"reuse": {"web/": "keep", "web/package.json": "rebuild", "src/": "rebuild"}}
        check("reuse: a web path keeps", adapter.reuse(["web/a.tsx"]) == "keep")
        check("reuse: a src path beside it rebuilds", adapter.reuse(["web/a.tsx", "src/x.py"]) == "rebuild")
        check("reuse: the longest prefix wins", adapter.reuse(["web/package.json"]) == "rebuild")
        check("reuse: a path no prefix names rebuilds", adapter.reuse(["other/x"]) == "rebuild")
        standin.venue_up = lambda j, slug, wt, real_llm=False, key="venue-up", attempt=1: calls.__setitem__("up", calls["up"] + 1) or {"venue": "v-new"}
        standin.venue_down_keep = lambda j, v, wt, key: calls.__setitem__("down", calls["down"] + 1)
        gitops.head_of = lambda wt: "tip2"
        common.state_dir = lambda c=None: repo / "state"
        jm.state_dir = common.state_dir
        j = Journal("kept-venue")
        gitops.changed_paths = lambda wt, a, b="HEAD": ["web/a.tsx"]
        v = standin.ensure(j, "7", repo, {"venue": "v-1"}, "tip1")
        check("kept venue: a keep change keeps the venue", v == {"venue": "v-1"} and calls == {"up": 0, "down": 0}, str((v, calls)))
        check("kept venue: journaled kept", any(e.get("kind") == "venue" and e.get("state") == "kept" for e in j.events()))
        gitops.changed_paths = lambda wt, a, b="HEAD": ["src/x.py"]
        v = standin.ensure(j, "7", repo, {"venue": "v-1"}, "tip1")
        check("kept venue: a rebuild change takes the old venue down and brings a fresh one",
              v == {"venue": "v-new"} and calls == {"up": 1, "down": 1}, str((v, calls)))
    finally:
        (standin.venue_up, standin.venue_down_keep, gitops.changed_paths, gitops.head_of, common.state_dir, jm.state_dir,
         adapter.block) = saved


def drill_usage_carries_context(repo):
    """The seat's usage carries the cache fields and the fullest context of any one turn."""
    stream = repo / "stdout.txt"
    lines = [{"type": "system"},
             {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 5000}}},
             {"type": "assistant", "message": {"usage": {"input_tokens": 12, "cache_read_input_tokens": 240000, "cache_creation_input_tokens": 100}}},
             {"type": "result", "total_cost_usd": 1.5, "usage": {"input_tokens": 22, "output_tokens": 3000, "cache_read_input_tokens": 330000, "cache_creation_input_tokens": 5100}}]
    stream.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    u = adapters.read_usage("claude-json", stream, repo / "stderr.txt")
    check("usage: totals with the cache fields", u["output_tokens"] == 3000 and u["cache_read_input_tokens"] == 330000 and u["cost_usd"] == 1.5, str(u))
    check("usage: the peak context is the fullest turn", u["peak_context_tokens"] == 240112 and u["turns"] == 2, str(u))


def drill_authority_kinds(repo):
    """The always-wait list: auth always stops, the product's `stops.extra` (wages, tenant here)
    stop beside it; a question never does."""
    note = {"needs_authority": [{"kind": "wages", "text": "shows a rate"}, {"kind": "question", "text": "which word"},
                                {"kind": "auth", "text": "touches sessions"}, {"kind": "tenant", "text": "tenant filter"}]}
    saved = common.cfg_or_empty
    try:
        common.cfg_or_empty = lambda: PRODUCT
        kinds = [na["kind"] for na in policy.authority_stops(note)]
        common.cfg_or_empty = lambda: {}
        bare = [na["kind"] for na in policy.authority_stops(note)]
    finally:
        common.cfg_or_empty = saved
    check("authority: wages, auth and tenant stop; a question does not", kinds == ["wages", "auth", "tenant"], str(kinds))
    check("authority: without stops.extra only the five every product has stop", bare == ["auth"], str(bare))


def drill_size_tripwire_prints_on_the_pr(repo):
    """What a change pushed past the size limit is named on the PR's first screen; nothing grown, no block."""
    from hb.record import firstscreen
    contract = {"items": [{"item": "T-01", "then": "the card shows"}]}
    reason = "src/services/big.py grows from 580 to 640 lines (limit 600); a module that size owns more than one concern"
    fs = firstscreen.build(contract, [_receipt("T-01", "WORKS")], {}, None, [], size={"hold": True, "reasons": [reason]})
    md = " ".join(firstscreen.markdown(fs))
    check("size: the first screen carries the reason", fs["size"] == [reason], str(fs.get("size")))
    check("size: the PR names it under its own heading", "## Grew past the size limit" in md and reason in md, md[:400])
    check("size: it does not change the verdict", fs["verdict"] == firstscreen.READY, fs["verdict"])
    quiet = firstscreen.build(contract, [_receipt("T-01", "WORKS")], {}, None, [], size={"hold": False, "reasons": []})
    check("size: nothing grown prints no block", "size limit" not in " ".join(firstscreen.markdown(quiet)))
    unmeasured = firstscreen.build(contract, [_receipt("T-01", "WORKS")], {}, None, [])
    check("size: no measure prints no block", unmeasured["size"] == [])


class GateHost:
    """measure() for real, with run-gate, the lease, the machine slot and git faked: `answers`
    maps a word of the gate's command to a list of (exit, artifact text, seconds[, red ids]),
    one per call; the red ids are what run-gate prints as RED_NODEID lines."""

    def __init__(self, repo, answers):
        import threading
        self.repo, self.answers, self.calls, self.live, self.peak = repo, answers, [], 0, 0
        self.lock = threading.Lock()

    def __enter__(self):
        from hb.runner import hbcall
        from hb.venue import machine
        from contextlib import nullcontext
        self.saved = [(hbcall, "hb", hbcall.hb), (gating, "lease_with_retry", gating.lease_with_retry),
                      (machine, "slot", machine.slot), (gitops, "head_of", gitops.head_of),
                      (common, "state_dir", common.state_dir), (jm, "state_dir", jm.state_dir),
                      (machine, "state_dir", machine.state_dir), (machine, "settings", machine.settings)]
        host = self

        def hb(args, cwd, timeout=1800, env=None):
            cmd = args[args.index("--") + 1:]
            name = next(n for n in host.answers if n in cmd)
            with host.lock:
                host.calls.append((name, list(cmd)))
                host.live += 1
                host.peak = max(host.peak, host.live)
                code, text, secs, *reds = host.answers[name].pop(0)
                art = host.repo / f"gate-{name}-{len(host.calls)}.txt"
            time.sleep(secs)
            with host.lock:
                host.live -= 1
            art.write_text(text + f"\nTRUE_EXIT={code}\n", encoding="utf-8")
            red_lines = "".join(f"RED_NODEID={r}\n" for r in (reds[0] if reds else []))
            return code, {}, f"GATE_OUTPUT={art}\n{red_lines}TRUE_EXIT={code}\n"
        hbcall.hb = hb
        gating.lease_with_retry = lambda wt, slug, note=None, **kw: {"url": "toy://db"}
        machine.slot = lambda *a, **kw: nullcontext()
        gitops.head_of = lambda wt: "tip0"
        common.state_dir = lambda c=None: self.repo / "state"
        jm.state_dir = machine.state_dir = common.state_dir
        # admission runs for real on the drill's fixed reading, with no config file to ask, and
        # settles at once so the drills read gates.parallel and never a start's settle wait
        machine.settings = lambda c=None: {**machine.DEFAULTS, **((c or {}).get("machine") or {}),
                                           "poll_s": 0.05, "settle_s": 0.05, "force_after_s": 0.5}
        return self

    def __exit__(self, *exc):
        for mod, name, val in self.saved:
            setattr(mod, name, val)


SUITE = {"name": "backend-tests", "cmd": ["python", "-m", "pytest", "-q", "-n", "auto", "src/tests"], "heavy": True}


def _opened(name):
    j = Journal(name)
    j.phase("opened", beat="wave", ref="w")
    return j


def drill_environment_red_is_recovered_not_fixed(repo):
    """A suite whose artifact names an environment fault (MemoryError) has an invalid verdict:
    it is journaled, run once more after its recovery, and never handed to a fix seat; the
    rerun that measures is the gate's result. A crash with no product line twice is UNMEASURED
    by name with its signature, never green, never a fix item and never replayed at base."""
    mem = (1, "E   MemoryError\n1 failed, 900 passed in 400s", 0)
    with GateHost(repo, {"pytest": [mem, (0, "901 passed in 400s", 0)]}) as h:
        j = _opened("env-memory")
        rows = gating.measure(j, repo, "w", "base", [], {}, "gates-full", heavy=True, only=[SUITE])
    row = rows[0]
    check("env red: rerun once, the same command", len(h.calls) == 2 and h.calls[0][1] == h.calls[1][1], str(h.calls))
    check("env red: the rerun that measures is the result, the recovery named, nothing red",
          row["true_exit"] == 0 and row.get("recovered_from") == "memory" and not gating.gates_red(rows), row)
    vs = [e for e in j.events() if e.get("kind") == "verdict"]
    check("env red: both classifications journaled: invalid environment memory with its condition, then the rerun",
          [(v["attempt"], v["valid"], v["cause_class"], v["signature"]) for v in vs]
          == [(1, False, "environment", "memory"), (2, True, "product", None)]
          and vs[0]["recovery"] == "room from admission" and vs[1].get("rerun") is True, vs)
    check("env red: the recovery is journaled with its condition",
          any(e.get("kind") == "gate-recovery" and e.get("signature") == "memory" for e in j.events()))
    crash = (3221225725, "collecting ... \n[gw0] node down", 0)
    with GateHost(repo, {"pytest": [crash, (124, "run-gate: TIMEOUT after 60min; process tree killed", 0)]}) as h:
        j = _opened("env-crash")
        rows = gating.measure(j, repo, "w", "base", [], {}, "gates-full", heavy=True, only=[SUITE])
    row = rows[0]
    check("crash twice: UNMEASURED by name with the signature, the rerun and what it showed",
          row["true_exit"] is None and row["unmeasured"].startswith("worker-crash (environment): rerun once after")
          and row["unmeasured"].endswith("still timeout"), row)
    check("crash twice: never a fix item, and named on the landing",
          not gating.gates_to_fix(rows) and gating.unmeasured_gate_names(rows) == ["backend-tests"], rows)
    gating.settle_heavy(j, repo, "base", "w", rows, [])
    check("crash twice: never replayed at base, never pre-existing",
          not gating.known_reds(j) and not any(e.get("kind") == "replay-at-base" for e in j.events()))
    with GateHost(repo, {"pytest": [(1, "3 failed, 10 passed in 9s", 0)]}) as h:
        rows = gating.measure(_opened("plain-red"), repo, "w", "base", [], {}, "gates-full", heavy=True, only=[SUITE])
    check("plain red: a red with a product line is the product's: run once, a fix item",
          len(h.calls) == 1 and [g["name"] for g in gating.gates_to_fix(rows)] == ["backend-tests"], h.calls)


def drill_the_same_red_after_recovery_is_the_products(repo):
    """The signature that comes back with a product line after its recovery condition was met is
    the product's own red: valid, BROKEN, a fix item, and no third run."""
    down = (1, "E   psycopg.OperationalError: could not connect to server\n1 failed, 900 passed in 400s", 0)
    with GateHost(repo, {"pytest": [down, down]}) as h:
        j = _opened("env-recurs")
        rows = gating.measure(j, repo, "w", "base", [], {}, "gates-full", heavy=True, only=[SUITE])
    v = rows[0].get("verdict") or {}
    check("recurs: two runs, no third", len(h.calls) == 2, h.calls)
    check("recurs: BROKEN, valid, the product's, named as the recurrence",
          rows[0]["true_exit"] == 1 and v.get("valid") is True and v.get("outcome") == "BROKEN"
          and v.get("cause_class") == "product" and str(v.get("signature")).startswith("db-starting recurred"), rows[0])
    check("recurs: a fix item, and read the same by a later reader with no rerun context",
          [g["name"] for g in gating.gates_to_fix(rows)] == ["backend-tests"] and not gating.unmeasured_gate_names(rows), rows)


def drill_a_dropped_database_is_the_environments(repo):
    """A database that closed its connection mid-suite is the environment's, recovered by a lease
    that answers, never a product red; and a rerun recovered from one environment signature that
    fails on a different one is UNMEASURED by the new name, never BROKEN."""
    from hb.gates import verdict as vd
    lines = ("E   sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) server closed the connection unexpectedly",
             "E   psycopg2.OperationalError: SSL SYSCALL error: EOF detected",
             "E   psycopg2.OperationalError: the server terminated abnormally")
    for i, line in enumerate(lines):
        art = repo / f"db-dropped-{i}.txt"
        art.write_text(line + "\n1 failed, 900 passed in 400s\nTRUE_EXIT=1\n", encoding="utf-8")
        v = vd.classify({"name": "backend-tests", "cmd": SUITE["cmd"], "true_exit": 1, "artifact": str(art)})
        check(f"db-dropped: {line[4:40]} is the environment's, recovered by a lease that answers",
              v["signature"] == "db-dropped" and v["cause_class"] == "environment" and v["outcome"] == "UNMEASURED"
              and v["recovery"] == vd.DB_ANSWERS, str(v))
    for prefix in ("", "receipts."):
        other = repo / f"db-dropped-other-{prefix}txt"
        other.write_text(f"E   {prefix}OperationalError: terminated abnormally while writing the receipt file\n"
                         "1 failed, 900 passed in 400s\nTRUE_EXIT=1\n", encoding="utf-8")
        v = vd.classify({"name": "backend-tests", "cmd": SUITE["cmd"], "true_exit": 1, "artifact": str(other)})
        check(f"db-dropped: {prefix}OperationalError that is not the database driver's is the product's red",
              v["signature"] != "db-dropped" and v["outcome"] == "BROKEN" and v["cause_class"] == "product", str(v))
    busy = (1, "E   psycopg.OperationalError: could not connect to server\n1 failed, 900 passed in 400s", 0)
    dropped = (1, lines[0] + "\n1 failed, 900 passed in 400s", 0)
    with GateHost(repo, {"pytest": [busy, dropped]}) as h:
        rows = gating.measure(_opened("env-switch"), repo, "w", "base", [], {}, "gates-full", heavy=True, only=[SUITE])
    v = rows[0].get("verdict") or {}
    check("db-dropped after db-starting: two runs, UNMEASURED by the new signature, never BROKEN, no fix item",
          len(h.calls) == 2 and rows[0]["true_exit"] is None and v.get("outcome") == "UNMEASURED"
          and v.get("signature") == "db-dropped" and not gating.gates_to_fix(rows)
          and str(rows[0].get("unmeasured")).endswith("still db-dropped"), rows[0])


def drill_a_second_memory_fault_is_still_the_hosts(repo):
    """A MemoryError twice, with pytest's `1 failed` beside it, never measured: the memory
    signature wins over the count, the gate is UNMEASURED by name and no fix seat is handed
    it; each crashed run's peak is a floor, so the rerun took the host alone; and a green
    test named for a fault names no cause, since an artifact is read by its failure lines."""
    from hb.gates import verdict as vd
    mem = (1, "E   MemoryError\n1 failed, 900 passed in 400s", 0)

    with GateHost(repo, {"pytest": [mem, mem]}) as h:
        j = _opened("env-memory-twice")
        rows = gating.measure(j, repo, "w", "base", [], {}, "gates-full", heavy=True, only=[SUITE])
    v = rows[0].get("verdict") or {}
    check("memory twice: UNMEASURED environment memory, never the product's for a failed-count line",
          rows[0].get("true_exit") is None and v.get("valid") is False and v.get("cause_class") == "environment"
          and v.get("signature") == "memory" and "memory" in str(rows[0].get("unmeasured")), rows[0])
    check("memory twice: no fix item", not gating.gates_to_fix(rows) and gating.unmeasured_gate_names(rows) == ["backend-tests"], rows)
    ends = [e for e in j.events() if e.get("kind") == "admission" and e.get("state") == "released" and e.get("workload") == "gate:backend-tests"]
    check("memory twice: both crashed runs released their grant", len(ends) == 2, str(ends))

    art = repo / "named-for-memory.txt"
    art.write_text("tests/test_out_of_memory_banner.py::test_out_of_memory_banner FAILED\nE   assert banner == 'x'\n"
                   + "\n".join(f"line {i}" for i in range(80)) + "\n1 failed, 1 passed in 2s\nTRUE_EXIT=1\n", encoding="utf-8")
    row = {"name": "backend-tests", "cmd": SUITE["cmd"], "true_exit": 1, "artifact": str(art)}
    check("a test named for a fault is the product's red, not an environment signature",
          vd.classify(row)["cause_class"] == "product" and vd.classify(row)["outcome"] == "BROKEN", str(vd.classify(row)))


def drill_verdict_reads_machine_text(repo):
    """A tester's sentence named `Connection refused` and the verdict read the venue restarting,
    so a valid BROKEN went to recovery and was never fixed; a failing test named for a memory
    fault read `memory` twice and ended UNMEASURED. A signature reads only what a machine wrote:
    a receipt's substate, a gate's own reason and an artifact's process lines."""
    from hb.gates import verdict as vd
    said = {"item": "T-01", "state": "BROKEN", "substate": "failed",
            "observed": "the card shows Connection refused instead of the pull", "seen": "Connection refused on the card"}
    v = vd.classify(said)
    check("machine text: a tester's sentence never names an environment signature",
          v["valid"] and v["outcome"] == "BROKEN" and v["cause_class"] == "product" and vd.is_fixable(said), str(v))
    crashed = {"item": "T-02", "state": "UNMEASURED", "substate": "verifier-failed", "observed": "the seat died"}
    check("machine text: the substate still names a seat crash", vd.classify(crashed)["signature"] == "seat-crash")
    stop = {"item": "T-03", "state": "BROKEN", "substate": "failed", "observed": "another org's rows showed: tenant isolation broken"}
    check("machine text: a tester's words name no stop; only a seat's report does",
          vd.classify(stop)["signature"] is None and vd.is_fixable(stop), str(vd.classify(stop)))
    art = repo / "named-memoryerror.txt"
    art.write_text("FAILED tests/test_upload.py::test_upload_reports_memoryerror_cleanly - AssertionError: MemoryError\n"
                   "E   AssertionError: assert 'MemoryError' in ''\n"
                   "E    +  where '' = upload_banner()\n1 failed, 900 passed in 400s\nTRUE_EXIT=1\n", encoding="utf-8")
    row = {"name": "backend-tests", "cmd": SUITE["cmd"], "true_exit": 1, "artifact": str(art)}
    gv = vd.classify(row)
    check("machine text: a failing test named for a memory fault is the product's red, fixable",
          gv["cause_class"] == "product" and gv["outcome"] == "BROKEN" and vd.is_fixable(row), str(gv))
    real = repo / "real-memoryerror.txt"
    real.write_text("collecting ...\nTraceback (most recent call last):\n  File \"x.py\", line 1\nMemoryError\n"
                    "1 failed, 900 passed in 400s\nTRUE_EXIT=1\n", encoding="utf-8")
    check("machine text: a traceback's own MemoryError line is the host's room",
          vd.classify({"name": "backend-tests", "cmd": SUITE["cmd"], "true_exit": 1, "artifact": str(real)})["signature"] == "memory")


def drill_a_red_vitest_passing_alone_is_unmeasured_never_green(repo):
    """A vitest file that failed in the whole gate and passed alone set the gate's exit to 0,
    outside the verdict, outside admission and outside the op, so every resume asked again and
    an order-dependent failure read green. Its files run alone inside the gate's own measure,
    admitted like the gate; passing alone reads UNMEASURED order-dependent with both artifacts,
    never green and never a fix item; failing alone stays the product's red."""
    from hb.gates import verdict as vd
    vitest = {"name": "vitest-changed", "cmd": ["npx", "vitest", "run", "--changed", "origin/dev"], "heavy": False,
              "isolate": "npx vitest run {id}"}
    red = (1, " FAIL   dom  src/a/__tests__/X.test.tsx > case\n Tests  1 failed", 0, ["src/a/__tests__/X.test.tsx"])
    with GateHost(repo, {"vitest": [red, (0, " Tests  1 passed", 0)]}) as h:
        j = _opened("vitest-order")
        rows = gating.measure(j, repo, "w", "base", ["web/a.tsx"], {}, "gates-r1", only=[vitest])
    row = rows[0]
    v = vd.classify(row)
    check("order-dependent: the failing file ran alone, by name, under the gate's own measure",
          len(h.calls) == 2 and h.calls[1][1][-1] == "src/a/__tests__/X.test.tsx"
          and any(e.get("kind") == "gate-alone" for e in j.events()), str(h.calls))
    check("order-dependent: UNMEASURED by name with both artifacts, never green, never a fix item",
          row["true_exit"] != 0 and v["outcome"] == "UNMEASURED" and v["signature"] == "order-dependent"
          and row.get("alone_artifact") and not gating.gates_to_fix(rows) and gating.unmeasured_gate_names(rows) == ["vitest-changed"],
          str((row, v)))
    with GateHost(repo, {"vitest": [red, (1, " FAIL   dom  src/a/__tests__/X.test.tsx > case", 0)]}) as h:
        rows = gating.measure(_opened("vitest-real"), repo, "w", "base", ["web/a.tsx"], {}, "gates-r1",
                              only=[vitest])
    check("order-dependent: a file that fails alone too is the product's red, a fix item",
          [g["name"] for g in gating.gates_to_fix(rows)] == ["vitest-changed"], str(rows))


DRILLS = [drill_a_rule_nobody_checked_never_reads_green, drill_a_red_is_its_id_and_ends_after_two_fixes, drill_the_ceiling_stops_the_battery, drill_loop_clean_first_round,
          drill_a_resume_past_the_landings_commits_re_enters_the_ended_loop, drill_loop_red_gate_is_a_fix_item_never_a_stop,
          drill_a_review_that_never_answered_stops_the_run,
          drill_loop_ends_when_fix_moves_nothing,
          drill_after_compose_cases_run_on_the_composed_branch, drill_a_broken_check_survives_a_rerun_that_did_not_run,
          drill_loop_resume_continues_the_round, drill_review_loop, drill_audit_resumes_where_it_was_killed,
          drill_audit_disagreements_count_both_ways,
          drill_a_change_to_the_models_code_drives_on_the_real_model,
          drill_loop_round_lanes_at_once, drill_loop_second_reader_beside_review, drill_a_lost_venue_reads_unmeasured_never_green,
          drill_a_venue_without_ui_or_model_measures_nothing_it_cannot, drill_the_served_sha_is_the_venue_records,
          drill_an_every_tip_gate_runs_at_the_tip_beside_the_suite,
          drill_a_hand_commit_inside_a_seat_window_is_named,
          drill_kept_reader_needs_its_own_tip, drill_venue_kept_by_the_products_reuse_table, drill_usage_carries_context,
          drill_authority_kinds, drill_size_tripwire_prints_on_the_pr, drill_environment_red_is_recovered_not_fixed,
          drill_the_same_red_after_recovery_is_the_products, drill_a_dropped_database_is_the_environments,
          drill_a_second_memory_fault_is_still_the_hosts,
          drill_verdict_reads_machine_text, drill_a_red_vitest_passing_alone_is_unmeasured_never_green]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    only = sys.argv[1:] or None
    for d in DRILLS:
        if only and d.__name__.replace("drill_", "") not in only and d.__name__ not in only:
            continue
        with tempfile.TemporaryDirectory(prefix="hb-loop-") as tmp:
            repo = Path(tmp) / "repo"
            (repo / "ev").mkdir(parents=True)
            (repo / "state").mkdir(parents=True)
            try:
                d(repo)
            except Exception as ex:  # a drill that raises is a failed drill, named
                import traceback
                check(f"{d.__name__}: raised {ex.__class__.__name__}: {ex}", False, traceback.format_exc()[-600:])
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAIL", f)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
