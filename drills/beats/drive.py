#!/usr/bin/env python
"""Drills for the drive: one blind audit seat, and the battery that grades every model-held
item. Offline and in process: the seat and the venue are stood in by functions, the journal
is a real one.

Run: python engine/drill_drive.py [names]

Drills:
  model_item_needs_a_battery  a model item with no battery reads UNMEASURED no-battery,
                  whatever the seat wrote; no seat grades a model item WORKS
  audit_page      the one seat sits on the audit page: blind, every item once, no trials
  one_seat        one seat drives every item it is given with the venue's ui driver; a venue
                  without ui reads not offered; a failed seat leaves its items UNMEASURED;
                  scores handed in are not run again
  battery         a battery item is graded from the runner's case lines against its bar, never
                  from the tester: k of k is WORKS with the score; below the bar is BROKEN with
                  the failing cases named; a crash is UNMEASURED environment; no runner or an
                  unknown set is UNMEASURED "no battery" by name;
                  output short of the contract is malformed; a find on a WORKS row is
                  BROKEN; a crashed or silent tester never erases the battery's row
  battery_record  the previous score is read only from a record of the same set, k and
                  cases on this branch's lineage; the record prints pass^k against it
  success_block   a `## Success` block parses like GOAL, with its due date from the merge
"""
import json
import sys
import tempfile
import threading
import os
# the drive drills measure seats side by side, so starts are not spaced here
os.environ["HB_SETTLE_S"] = "0"
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ROOT / "drills"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# the drills measure the engine, never this host's memory: admission reads these, not the host
os.environ.setdefault("HB_MEMORY_FREE_GB", "64")
os.environ.setdefault("HB_MEMORY_TOTAL_GB", "32")
os.environ.setdefault("HB_SETTLE_S", "0.2")
os.environ.setdefault("HB_SETTLE_MAX_S", "1")
os.environ.setdefault("HB_FORCE_AFTER_S", "1")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{(': ' + str(detail)[:400]) if detail and not cond else ''}")


def _state_dir_to(tmp):
    """Point the harness state dir at a temp folder for in-process journals."""
    from hb.rig import common as common
    from hb.record import journal as jm
    saved = (common.state_dir, jm.state_dir)
    common.state_dir = lambda c=None: tmp / ".context" / "harness"
    jm.state_dir = common.state_dir
    return saved


def _restore_state_dir(saved):
    from hb.rig import common as common
    from hb.record import journal as jm
    common.state_dir, jm.state_dir = saved


def _receipt(item, state="WORKS", substate=None, **extra):
    r = {"item": item, "state": state, "substate": substate, "observed": "seen", "seen": "seen",
         "situation": "venue", "paths_tried": ["happy"], "scenario": None, "capture": None,
         "plan": "plan.md", "tenant_id": "tenant-1", "evidence": ["plan.md"]}
    r.update(extra)
    return r


# ── model items ────────────────────────────────────────────────────────────


def drill_model_item_needs_a_battery(tmp):
    """Ruling 14: a chat item's number comes from its battery, never from a tester. A model
    item with no `battery:` was graded WORKS from the tester's own trials; it now reads
    UNMEASURED no-battery by name, and the tester's brief is discovery for it."""
    from hb.gates import verdict
    saved = _state_dir_to(tmp)
    try:
        items = [{"item": "M-01", "held_by": "model", "pass_rate": "4/5", "then": "the pull card is right",
                  "files": [], "drivable": True}, {"item": "S-01", "files": [], "drivable": True}]
        env = _Env(rows={"M-01": {"trials": ["works", "works", "works"]}})
        j, res = _run_drive(tmp, env, items, c={}, name="drill-model-no-battery")
        row = next(r for r in res["report"]["items"] if r["item"] == "M-01")
        v = verdict.classify(row)
        check("model item: no battery reads UNMEASURED no-battery, never the tester's WORKS",
              row["state"] == "UNMEASURED" and row["substate"] == "no-battery" and row["observed"].startswith("no battery:")
              and v["signature"] == "no-battery" and verdict.is_red(row), (row, v))
        check("model item: the plain item is graded as the tester saw it",
              next(r for r in res["report"]["items"] if r["item"] == "S-01")["state"] == "WORKS")
        check("model item: the audit seat is handed no discovery brief and no trials rule (the discovery seat breaks it)",
              "Discovery for model items" not in env.calls[0]["facts"]
              and not any("Trials" in k for k in env.calls[0]["facts"]), list(env.calls[0]["facts"]))
        from hb.contract.contract import _shape
        it = _shape("M-01", "w", "t", "chat-verb", {"held_by": "model", "pass_rate": "4/5", "battery": "pull"})
        check("model item: the contract item carries held_by, pass_rate and battery", it.get("held_by") == "model"
              and it.get("pass_rate") == "4/5" and it.get("battery") == "pull", it)
    finally:
        _restore_state_dir(saved)


def drill_battery_runs_only_the_cases_a_change_adds(tmp):
    """Scope new (the owner's bound on spend): the battery picks only the golden cases the tip
    holds and the base does not, hands the runner exactly those, and a set the change added
    nothing to reads UNMEASURED by name, never run."""
    import subprocess
    from types import SimpleNamespace
    from hb.contract import battery
    repo = tmp / "golden-repo"
    (repo / "evals").mkdir(parents=True, exist_ok=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a],
                                  capture_output=True, text=True)
    g("init", "-q")
    base = {"k": 3, "families": ["pull", "order"], "cases": [{"id": "p-1", "family": "pull"}, {"id": "o-1", "family": "order"}]}
    (repo / "evals" / "golden.json").write_text(json.dumps(base), encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "base")
    base_sha = g("rev-parse", "HEAD").stdout.strip()
    tip = dict(base, cases=base["cases"] + [{"id": "p-2", "family": "pull"}, {"id": "p-3", "family": "pull"}])
    (repo / "evals" / "golden.json").write_text(json.dumps(tip), encoding="utf-8")
    g("commit", "-qam", "tip")
    c = {"battery": {"command": "python run.py", "golden": "evals/golden.json", "scope": "new"}}
    check("scope new: the config's scope is read", battery.new_only(c) and not battery.new_only({"battery": {}}))
    check("scope new: only the cases the change added to the set are picked",
          battery.new_case_ids(repo, c, base_sha, "pull") == ["p-2", "p-3"], battery.new_case_ids(repo, c, base_sha, "pull"))
    check("scope new: a set the change left alone has none", battery.new_case_ids(repo, c, base_sha, "order") == [])
    argv = battery._argv(["python", "run.py"], {"battery": "pull", "ids": ["p-2", "p-3"]}, {}, 1, tmp / "out")
    check("scope new: the runner is handed exactly those cases", argv[argv.index("--ids") + 1] == "p-2,p-3", argv)
    j = SimpleNamespace(append=lambda *a, **k: None, dir=tmp, run_id="drill-new")
    row, _ = battery.run_one(j, c, {"item": "M-02", "held_by": "model", "battery": "order", "ids": []}, {}, repo, tmp, 1, base_sha)
    check("scope new: a set with no added case reads UNMEASURED by name and never runs",
          row["state"] == "UNMEASURED" and row["substate"] == "no-battery" and "adds no golden case" in row["observed"], row)


def drill_venue_facts_name_the_venue_env(tmp):
    """The battery and the checks runner reach the venue through what its receipt said: the
    venue's facts flatten the record and the receipt's `facts`, and the battery's argv carries them."""
    from hb.contract import battery
    from hb.venue import standin
    env_file = tmp / "venue.env"
    vf = standin.venue_facts({"venue": "v-9-1", "origin": "http://localhost:5", "tenant_id": "tenant-9",
                              "record": {"name": "venue-9-1", "facts": {"env_file": str(env_file)}}})
    check("venue facts: the venue's address is its origin", vf.get("origin") == "http://localhost:5", vf)
    check("venue facts: the venue's env file and tenant are named", vf.get("env_file") == str(env_file)
          and vf.get("tenant_id") == "tenant-9", vf)
    argv = battery._argv(["python", "run.py"], {"battery": "pull"}, vf, 1, tmp / "out")
    check("venue facts: the battery is handed the env file, the address and the tenant",
          "--env-file" in argv and "--origin" in argv and "--tenant-id" in argv, argv)


# ── the one seat ───────────────────────────────────────────────────────────


class _Env:
    """The seat and the venue stood in, with the calls recorded."""

    def __init__(self, fail_shards=(), fail_seats=False, foreign=None, rows=None, omit=()):
        self.foreign = foreign
        # items the stand-in tester leaves out of its report
        self.omit = set(omit)
        # extra keys the stand-in tester writes on named items (a battery item's finds or trials)
        self.rows = rows or {}
        self.calls = []
        self.lock = threading.Lock()
        self.fail_shards = dict.fromkeys(fail_shards, 0)
        self.fail_seats = fail_seats
        self.live, self.peak = 0, 0
        self.seated = []

    def seat(self, j, role, facts, wt, base_sha, issue, extra_env=None, label=None, **kw):
        label = label or role
        ids = [it["item"] for it in facts.get("Contract items") or []]
        with self.lock:
            self.calls.append({"label": label, "items": ids, "profile": (extra_env or {}).get("HB_BROWSER_PROFILE"),
                               "venue": facts.get("The venue"), "facts": facts, "tools": kw.get("tools")})
            self.live += 1
            self.peak = max(self.peak, self.live)
        attempt = j.next_attempt(label)
        j.append("seat", seat=label, attempt=attempt, state="started")
        with self.lock:
            self.live -= 1
        k = label.split(".")[-1]
        if k in self.fail_shards and self.fail_shards[k] < 2:
            self.fail_shards[k] += 1
            j.append("seat", seat=label, attempt=attempt, state="crashed")
            return {"result": "crashed", "report": None}
        j.append("seat", seat=label, attempt=attempt, state="ok")
        rep = {"schema": "verify-report/v2", "issue": issue, "run_id": j.run_id, "attempt": attempt, "seat": "verify",
               "base_sha": base_sha, "head_sha": "tip", "venue": "v-1", "served_sha": "tip",
               "items": [_receipt(i, **self.rows.get(i, {})) for i in ids if i not in self.omit]
               + ([_receipt(self.foreign, "BROKEN", "failed")] if self.foreign and self.foreign not in ids else [])}
        return {"result": "ok", "report": rep}


# the venue block the adapter reads: a browser driver whose args name the session and origin
DRIVER = {"command": "drv", "args": ["--storage-state", "{session}", "--origin", "{origin}"]}


def _run_drive(tmp, env, items, c=None, name="drill-drive", scores=None, venue=None, caps=("ui", "model")):
    from hb.beats import drive
    from hb.runner import seating
    from hb.venue import adapter
    from hb.record.journal import Journal
    saved = (seating.sit, adapter.block, adapter.capabilities)
    seating.sit = env.seat
    adapter.block = lambda c=None: {"ui": {"driver": DRIVER}}
    adapter.capabilities = lambda c=None: tuple(caps)
    try:
        j = Journal(name)
        ev = tmp / "evidence"
        ev.mkdir(parents=True, exist_ok=True)
        vfacts = {"Goal": "g", "Contract items": items,
                  "The venue": venue or {"venue": "v-1", "browser_profile": "/v/browser/profile"}}
        res = drive.run(j, vfacts, {"venue": "v-1"}, tmp, "base", "#7", {"evidence": ev}, c or {},
                        [it["item"] for it in items], scores=scores)
        return j, res
    finally:
        seating.sit, adapter.block, adapter.capabilities = saved


# ── state groups at once, on organizations of their own ────────────────────

def drill_one_seat(tmp):
    """The seat drives with the venue's own browser driver, filled with its session and origin;
    a venue without `ui` sits no seat and every row reads not offered; a seat that fails
    leaves every item it was asked UNMEASURED with why, never the rows it never wrote."""
    saved = _state_dir_to(tmp)
    try:
        items = [{"item": i, "files": [], "drivable": True} for i in ("A-01", "A-02", "B-01")]
        env = _Env()
        j, res = _run_drive(tmp, env, items, name="drill-one-seat",
                            venue={"venue": "v-1", "browser_profile": "/v/browser/profile",
                                   "storage_state": "/v/session.json", "origin": "http://127.0.0.1:5"})
        check("one seat: on the venue's own profile", env.calls[0]["profile"] == "/v/browser/profile", env.calls[0]["profile"])
        check("one seat: its browser tools are the venue's driver, filled with the session and the origin",
              env.calls[0]["tools"] == {"ui": {"command": "drv", "args": ["--storage-state", "/v/session.json",
                                                                          "--origin", "http://127.0.0.1:5"], "env": {}}},
              env.calls[0]["tools"])
        env = _Env()
        j, res = _run_drive(tmp, env, items, name="drill-one-seat-no-ui", caps=("model",))
        check("one seat: a venue without ui sits no seat; every row reads not offered, UNMEASURED",
              not env.calls and res["result"] == "not-offered"
              and all(r["state"] == "UNMEASURED" and r["observed"] == "not offered: ui" for r in res["report"]["items"]),
              (env.calls, res))
        env = _Env(fail_shards=("verify",))
        j, res = _run_drive(tmp, env, items, name="drill-one-seat-fails")
        check("one seat: a failed seat leaves every item UNMEASURED verifier-failed with why",
              res["result"] == "crashed" and all(r["state"] == "UNMEASURED" and r["substate"] == "verifier-failed"
                                                 and "crashed" in r["observed"] for r in res["report"]["items"]), res)
    finally:
        _restore_state_dir(saved)


# ── the battery ────────────────────────────────────────────────────────────

RUNNER = """
import json, sys
a = sys.argv
cs = a[a.index("--set") + 1]
k = int(a[a.index("--k") + 1]) if "--k" in a else 2
if cs == "unknown":
    print(json.dumps({"unknown_set": cs})); sys.exit(4)
if cs == "usage":
    sys.stderr.write("usage: runner --set SET"); sys.exit(2)
if cs == "storm":
    sys.stderr.write("429 from the model, three times"); sys.exit(3)
if cs == "crash":
    raise RuntimeError("boom")
print("runner: starting")
pairs = () if cs == "zero" else (("pull-01", "pull two gallons of milk"),) if cs == "dropper" else (
    ("pull-01", "pull two gallons of milk"), ("pull-02", "take 3 lb beans for the bar"))
for cid, say in pairs:
    for t in range(1, (1 if cs == "onetry" else k) + 1):
        bad = cs == "weak" and cid == "pull-02" and t == k
        row = {"case": cid, "say": say, "try": t, "call_ok": not bad, "card_ok": True, "state_ok": True,
               "reply_judge": "ok", "ms": 900}
        if cs == "limited" and cid == "pull-02":
            row.update(call_ok=False, rate_limited=True)
        print(json.dumps(row))
if cs != "nosummary":
    print(json.dumps({"cases": 0 if cs == "zero" else 2, "k": k, "pass_k": 0.5 if cs == "weak" else 1.0,
                      "ask_rate": 0.0, "extra_calls": 0}))
"""


def _battery_cfg(tmp):
    runner = tmp / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    # the runner takes its admission grant in the drill's own state folder
    return {"battery": {"command": f"{Path(sys.executable).as_posix()} {runner.as_posix()}"},
            "state_dir": str(tmp / ".context" / "harness"), "machine": {"poll_s": 0.05}}


def _battery_items(case_set):
    return [{"item": "B-01", "held_by": "model", "battery": case_set, "pass_rate": "4/5", "then": "the pull card is right",
             "files": [], "drivable": True},
            {"item": "S-01", "files": [], "drivable": True}]


def drill_battery(tmp):
    from hb.gates import verdict
    saved = _state_dir_to(tmp)
    try:
        c = _battery_cfg(tmp)
        # k of k: WORKS with the score, whatever the tester wrote
        env = _Env(rows={"B-01": {"state": "BROKEN", "substate": "failed"}})
        j, res = _run_drive(tmp, env, _battery_items("pull"), c=c, name="drill-battery-ok")
        row = next(r for r in res["report"]["items"] if r["item"] == "B-01")
        check("battery: k of k is WORKS with the score in the row", row["state"] == "WORKS"
              and row.get("battery", {}).get("pass_k") == 1.0 and row["battery"]["set"] == "pull", row)
        check("battery: the tester's own state never grades a battery item", row.get("battery") and row["state"] == "WORKS", row)

        # below the bar: BROKEN with the failing cases named
        env = _Env()
        j, res = _run_drive(tmp, env, _battery_items("weak"), c=c, name="drill-battery-weak")
        row = next(r for r in res["report"]["items"] if r["item"] == "B-01")
        check("battery: below the bar is BROKEN/failed naming the failing cases", row["state"] == "BROKEN"
              and row["substate"] == "failed" and row["battery"]["failed"] == ["pull-02"] and "pull-02" in row["observed"], row)

        # a crash, a 429 storm, a refused flag, and output short of the contract: environment, never BROKEN
        for cs, word in (("crash", "crashed"), ("storm", "rate limited"), ("nosummary", "malformed"),
                         ("usage", "refused its arguments"), ("zero", "zero case lines"), ("onetry", "another count of tries"),
                         ("dropper", "the summary says 2 cases"), ("limited", "was rate limited")):
            env = _Env()
            j, res = _run_drive(tmp, env, _battery_items(cs), c=c, name=f"drill-battery-{cs}")
            row = next(r for r in res["report"]["items"] if r["item"] == "B-01")
            v = verdict.classify(row)
            check(f"battery: a runner whose output {word} is UNMEASURED, the environment's, by name",
                  row["state"] == "UNMEASURED" and v["cause_class"] == "environment" and v["signature"] == "battery-runner"
                  and not verdict.is_fixable(row) and word in row["observed"], (row, v))

        # no runner on the product, or a set the runner does not know: UNMEASURED "no battery" by name
        for cfg_, cs, why in (({}, "pull", "no runner"), (c, "unknown", "unknown case set")):
            env = _Env()
            j, res = _run_drive(tmp, env, _battery_items(cs), c=cfg_, name=f"drill-battery-{why[:4]}")
            row = next(r for r in res["report"]["items"] if r["item"] == "B-01")
            v = verdict.classify(row)
            check(f"battery: {why} reads UNMEASURED no battery by name",
                  row["state"] == "UNMEASURED" and row["observed"].startswith("no battery:") and v["signature"] == "no-battery"
                  and v["outcome"] == "UNMEASURED" and verdict.is_red(row), (row, v))
            ev = [e for e in j.events() if e.get("kind") == "battery-run"][-1]
            check(f"battery: {why} is journaled as no-battery", ev["state"] == "no-battery" and ev["why"] == why, ev)

        # a tester find on a WORKS row is a product red naming the find, and the reducer folds it
        found = ["'pull the usual for the bar': the card asked which bar instead of using the one station"]
        env = _Env(rows={"B-01": {"state": "UNMEASURED", "substate": "not-driven", "found": found}})
        from hb.record.journal import Journal
        Journal("drill-battery-find").append("loop-open", branch="b")
        j, res = _run_drive(tmp, env, _battery_items("pull"), c=c, name="drill-battery-find")
        row = next(r for r in res["report"]["items"] if r["item"] == "B-01")
        check("battery: a find on a WORKS row makes it BROKEN naming the find", row["state"] == "BROKEN"
              and row["found"] == found and found[0] in row["observed"] and verdict.is_fixable(row), row)
        from hb.runner.schemas import check_verify
        bad = check_verify({"venue": "v", "served_sha": "s", "items": [_receipt("B-01", found="one string")]})
        check("battery: the report refuses a find that is not a list of sentences", any("found" in p for p in bad), bad)

        # the battery's row stands when the tester seat crashes, and when it leaves the item out
        for env, how in ((_Env(fail_shards=("verify",)), "crashes"), (_Env(omit=("B-01",)), "leaves it out")):
            j, res = _run_drive(tmp, env, _battery_items("pull"), c=c, name=f"drill-battery-seat-{how[:5]}")
            rows = {r["item"]: r for r in res["report"]["items"]}
            check(f"battery: a tester seat that {how} never erases the battery's row",
                  rows.get("B-01", {}).get("state") == "WORKS" and rows["B-01"].get("battery", {}).get("pass_k") == 1.0
                  and "S-01" in rows, (res.get("result"), rows))
    finally:
        _restore_state_dir(saved)


def drill_battery_record(tmp):
    """The previous score was the newest record on any branch: a sibling slice, another k, a
    grown set. It is now the newest record of the same set, k and cases whose tip is an
    ancestor of this run's base; the record prints pass^k against it, and a run that drops a
    case the set held last time is malformed."""
    import os
    import subprocess
    from hb.contract import battery
    repo = tmp / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@d", "GIT_COMMITTER_NAME": "d", "GIT_COMMITTER_EMAIL": "d@d"}
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)  # noqa: E731
    head = lambda: git("rev-parse", "HEAD").stdout.strip()  # noqa: E731
    git("init", "-q", "-b", "dev")
    git("commit", "-q", "--allow-empty", "-m", "seed")
    merged = head()
    git("commit", "-q", "--allow-empty", "-m", "the merged slice")
    base = head()
    git("checkout", "-q", "-b", "sibling", merged)
    git("commit", "-q", "--allow-empty", "-m", "a sibling slice, never merged")
    sibling = head()
    git("checkout", "-q", "dev")
    ids = ["pull-01", "pull-02"]

    def record(name, entry):
        env["GIT_COMMITTER_DATE"] = f"2026-10-1{len(name)}T10:0{name[-1]}:00Z"  # newest last, never one second
        git("checkout", "-q", "--orphan", f"evidence/{name}")
        git("rm", "-rq", "--cached", ".")
        (repo / "evidence" / name).mkdir(parents=True)
        (repo / "evidence" / name / battery.SCORES_FILE).write_text(json.dumps({"pull": entry}), encoding="utf-8")
        git("add", f"evidence/{name}")
        git("commit", "-q", "-m", f"evidence: {name}")
        git("checkout", "-q", "-f", "dev")
    score = {"set": "pull", "k": 2, "case_ids": ids}
    check("record: no earlier score reads None", battery.previous_score(repo, score, base) is None)
    record("r1", {"pass_k": 0.8, "k": 2, "case_ids": ids, "tip": merged})
    record("r2", {"pass_k": 0.1, "k": 3, "case_ids": ids, "tip": merged})
    record("r3", {"pass_k": 0.2, "k": 2, "case_ids": ids + ["pull-03"], "tip": merged})
    record("r4", {"pass_k": 0.3, "k": 2, "case_ids": ids, "tip": sibling})
    prev = battery.previous_score(repo, score, base)
    check("record: the previous score is the same set, k and cases on this branch's lineage, never a newer record "
          "of another k, a grown set or a sibling branch", prev and prev["pass_k"] == 0.8 and prev.get("commit"), prev)
    check("record: a set never scored reads None", battery.previous_score(repo, dict(score, set="receive"), base) is None)
    known = (battery.last_record(repo, "pull", base) or {}).get("case_ids")
    raised = None
    try:
        battery.validate([{"case": "pull-01", "try": t, "call_ok": True, "card_ok": True, "state_ok": True} for t in (1, 2)],
                         {"cases": 1, "k": 2}, 2, known)
    except ValueError as ex:
        raised = str(ex)
    check("record: a run that drops a case the set held on its last record is malformed, by name",
          raised and "pull-03" in raised, raised)
    row = {"item": "B-01", "state": "WORKS", "battery": {"set": "pull", "pass_k": 1.0, "k": 2, "cases": 2, "case_ids": ids,
                                                         "failed": [], "bar": "4/5", "previous": dict(prev, tip="abc123def")},
           "found": ["'pull the usual': asked which station"]}
    sections = dict(battery.report_lines([row, {"item": "S-01", "state": "WORKS"}]))
    line = sections["Battery, pass^k against the last comparable score"][0]
    check("record: pass^k prints against the last comparable score", "pass^k 1.0" in line and "last 0.8 at abc123def" in line, line)
    check("record: a find prints as a proposed golden case",
          sections["Proposed golden cases (tester finds, for the golden file)"] == ["B-01: 'pull the usual': asked which station"], sections)
    ev = tmp / "ev"
    ev.mkdir()
    written = battery.write_scores(ev, [row], "tip1234567", "run-1", base)
    on_disk = json.loads((ev / battery.SCORES_FILE).read_text(encoding="utf-8"))["pull"]
    check("record: the scores file names the set, the score, k, the case ids, the tip and the base",
          on_disk["tip"] == "tip1234567" and on_disk["k"] == 2 and on_disk["case_ids"] == ids and on_disk["base"] == base
          and written["pull"]["pass_k"] == 1.0, on_disk)


def drill_success_block(tmp):
    from hb.record import success
    from hb.contract import contract
    body = ("## GOAL\n- when a pull is said, then the card is right\n\n## Success\n"
            "- number: pull cards OK with no edit\n  read: Analytics\n  today: 6 of 10\n  target: 9 of 10\n"
            "  when: about two weeks after the merge\n- number: pull pass^k\n  read: the battery\n  today: 0.93\n"
            "  target: 0.99\n  when: 10 days after merge\n\n## Rulings\nnone\n")
    entries = success.parse(contract._section(body, "Success"))
    check("success: every entry parses with its keys", len(entries) == 2 and entries[0]["read"] == "Analytics"
          and entries[0]["target"] == "9 of 10" and entries[1]["read"] == "the battery", entries)
    check("success: the due span is read from the block, two weeks and ten days", entries[0]["after_days"] == 14
          and entries[1]["after_days"] == 10, [e["after_days"] for e in entries])
    check("success: a block without a span reads None", success.parse("- number: x\n  when: whenever")[0]["after_days"] is None)
    words = success.parse("- number: x\n  when: seven days on\n- number: y\n  when: nine weeks\n- number: z\n  when: a fortnight after")
    check("success: seven, nine and a fortnight are spans", [e["after_days"] for e in words] == [7, 63, 14], words)
    bad = success.problems(success.parse("- number: x\n  when: whenever the owner asks"))
    check("success: an entry with no span is refused at parse time, by name", len(bad) == 1 and "`x`" in bad[0], bad)
    check("success: an unreadable merge time dates nothing", success.merged_time("not a time") is None
          and success.merged_time(None) is None)
    check("success: no block parses to nothing", success.parse(contract._section(body, "Nothing")) == [])
    due = success.due(entries, "2026-10-14T12:00:00Z")
    check("success: the due date counts from the merge", due[0]["due"] == "2026-10-28" and due[1]["due"] == "2026-10-24", due)
    text = "\n".join(success.lines(due))
    check("success: the lines carry the number, the source, today, the target and the due date",
          "Analytics" in text and "today 6 of 10" in text and "due 2026-10-28" in text, text)
    rendered = contract.render({"goal": "g", "issue": "#1", "source": "manifest", "items": [], "success": entries})
    check("success: the contract renders the block beside the items", "## Success" in rendered and "Analytics" in rendered)


DRILLS = [drill_model_item_needs_a_battery, drill_battery_runs_only_the_cases_a_change_adds, drill_venue_facts_name_the_venue_env, drill_one_seat, drill_battery, drill_battery_record,
          drill_success_block]


def main():
    only = sys.argv[1:] or None
    for d in DRILLS:
        name = d.__name__.replace("drill_", "")
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="hb-drive-drill-"))
        try:
            d(tmp)
        except Exception as ex:
            import traceback
            FAIL.append(f"{name}: raised {ex.__class__.__name__}: {ex} {traceback.format_exc()[-300:]}")
    for p in PASS:
        print("PASS", p)
    for f in FAIL:
        print("FAIL", f)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
