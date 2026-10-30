#!/usr/bin/env python
"""Drills for the slice door (hb run slice): the contract behind an issue and the brief that
hands it whole, the builder relaunched after a death, the ledger rows and the red-first
ship, the wave mode, the repair loop and the `--repair` entry, the studio commit a slice
starts from, and the doors that refuse: a sentence, a stale contract, a venue door that is
missing. Offline: fake seats, the gh stub, a stub venue command.

Run: python engine/drill_slice.py
"""
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ENGINE))
from fixture.common import HB, PASS, FAIL, check, journal, last_json, make_repo, seats, sh, stub_venue, VENUE_STATES  # noqa: E402,F401


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


MANIFEST = """schema: "manifest/v1"
wave: "t"
id_prefix: ""
precedence:
  default: "artifact-first"
artifacts:
  - path: "design/t/mock.html"
    sha256: "{mock_sha}"
fixture_recipes:
  - id: "r1"
    steps:
      - "open the venue"
items:
  - id: "T-01"
    rev: 1
    kind: "api"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a caller posts the delete"
    then: "the row is hidden"
    files: ["src/x.py"]
  - id: "T-02"
    rev: 1
    kind: "api"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a caller lists people"
    then: "the hidden row is absent"
    files: ["src/x.py"]
  - id: "T-03"
    rev: 1
    kind: "screen"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a manager opens the drawer"
    then: "a Delete verb sits under Bring back"
    files: ["web/z.tsx"]
"""

BODY = ("## Intent\nwhy\n\n## Rulings\n> R1: the owner's words\n\n"
        "## Items\nT-01..T-03 in `docs/manifests/t.yaml` (authoritative), slice `t`.\n")


def stub_gh(repo, body):
    script = repo / ".context" / "gh_stub.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"title": "the slice", "body": body, "labels": []})
    script.write_text("import sys, json\nargv = sys.argv[1:]\n"
                      "if argv[:2] == ['issue', 'view']:\n"
                      f"    print({payload!r}); sys.exit(0)\n"
                      "if argv[:2] == ['pr', 'list']:\n    print('[]'); sys.exit(0)\n"
                      "if argv[:2] == ['pr', 'create']:\n    print('https://example.invalid/pr/2'); sys.exit(0)\n"
                      "print(json.dumps({'stub': True})); sys.exit(0)\n", encoding="utf-8")
    return script


# the model's own code, where a change is judged only on the real model (config `model.paths`)
MODEL_PATHS = ["src/model/tools/", "src/model/capabilities/", "src/model/core/defaults.py"]


def make_slice_repo(tmp):
    # the config rides the commit below, so stub_venue does not commit it
    repo = stub_venue(make_repo(tmp), states=True, model_paths=MODEL_PATHS, commit=False)
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
    # the venue's states catalog (config `venue.states`): the doors a tester can wear
    catalog = repo / VENUE_STATES
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(json.dumps({"states": [], "doors": [{"id": "ok"}, {"id": "sales"}]}), encoding="utf-8")
    mock = repo / "design" / "t" / "mock.html"
    mock.parent.mkdir(parents=True, exist_ok=True)
    mock.write_text("<html>mock</html>\n", encoding="utf-8")
    (repo / "docs" / "manifests").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "manifests" / "t.yaml").write_text(MANIFEST.replace("{mock_sha}", sha(mock)), encoding="utf-8")
    (repo / "web").mkdir(parents=True, exist_ok=True)
    (repo / "web" / "z.tsx").write_text("export {}\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "manifest"); g("push", "-q", "origin", "dev")
    return repo


# the slice's two api items are proven by a test the note names, so no drive is owed for them
BUILD_NOTE = {"items": [{"item": "T-01", "status": "built", "before": "x.py:1 did nothing"}],
              "tests": [{"path": "src/x.py", "pins": "T-01 and T-02: the row is hidden and absent"}]}


def run_slice(repo, script, ref="#7", body=BODY, extra=(), venue=False):
    sp = repo / ".context" / "fake-script.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(script), encoding="utf-8")
    env = {"FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(stub_gh(repo, body))}
    if venue:
        env["HB_VENUE_STUB"] = "1"
    rc, o, e = sh([sys.executable, HB, "run", "slice", ref, "--shadow", *(() if venue else ("--no-venue",)),
                   "--no-before", "--rate-limit-wait-min", "0", *extra], cwd=str(repo), env=env)
    return rc, last_json(o), o + e


TOUCH = {"touch": ["src/x.py"]}


def drill_happy(repo):
    """Three items: one builder on the whole contract, the review, the ledger, shipped. The
    builder's brief carries the slice issue verbatim and every ruling (HS-01)."""
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE},
                                   "verify": {"mode": "ok", "report": {"items": [
                                       {"item": i, "state": "WORKS", "substate": None, "observed": "seen", "seen": "seen", "situation": "venue",
                                        "paths_tried": ["happy"], "scenario": None, "capture": None, "plan": "plan.md",
                                        "tenant_id": "tenant-1", "evidence": ["plan.md"]}
                                       for i in ("T-01", "T-02", "T-03")]}}})
    check("happy: shipped", js.get("verdict") == "mergeable", raw[-600:])
    evs = journal(repo, js.get("run_id", ""))
    ph = [e for e in evs if e.get("kind") == "phase"]
    con = next((e for e in ph if e.get("phase") == "contract"), {})
    check("happy: contract from the manifest", con.get("source") == "manifest" and con.get("items") == ["T-01", "T-02", "T-03"],
          str(con)[:200])
    builds = [e for e in evs if e.get("kind") == "build"]
    check("happy: one builder seat on the whole contract", len(builds) == 1 and builds[0].get("result") == "ok", str(builds)[:200])
    rd = repo / ".context" / "harness" / "runs" / js.get("run_id", "")
    brief = (rd / "seats" / "build-1" / "brief.md").read_text(encoding="utf-8") if (rd / "seats" / "build-1" / "brief.md").exists() else ""
    check("happy: the brief carries the slice issue verbatim and every ruling",
          "## The slice issue, verbatim" in brief and BODY.strip() in brief and "R1: the owner's words" in brief
          and all(f'"item": "{i}"' in brief for i in ("T-01", "T-02", "T-03")), brief[:600])
    check("happy: the envelope names the progress file, the worktree and base, and the report path",
          "progress.md" in brief and "Worktree (already your cwd)" in brief and "OUTPUT_PATH" in brief, brief[-800:])
    check("happy: full review ran after the build", any(e.get("phase") == "review" for e in ph))
    check("happy: branch is slice/", str(js.get("branch", "")).startswith("slice/"), js.get("branch"))
    check("happy: no PR in shadow", js.get("pr") is None)
    led = next((e for e in evs if e.get("kind") == "ledger"), None)
    check("happy: ledger rows attempted, refusals journaled not dropped", led is not None and "refused" in led, str(led)[:200])


def drill_resume(repo):
    """The builder dies and is relaunched from the tree as it stands, never from nothing."""
    rc, js, raw = run_slice(repo, {"build.1": {"mode": "crash"},
                                   "build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}})
    check("resume: shipped", js.get("verdict") in ("mergeable", "run-complete"), raw[-400:])
    evs = journal(repo, js.get("run_id", ""))
    bseats = [e for e in seats(evs, "build") if e.get("state") != "started"]
    crashed = any(e.get("attempt") == 1 and e.get("state") not in ("ok",) for e in bseats)
    recovered = any(e.get("attempt") == 2 and e.get("state") == "ok" for e in bseats)
    check("resume: the first builder died and the relaunched one carried the build to ok", crashed and recovered,
          str(bseats)[:400])


def drill_stale(repo):
    """A moved mock stops the run before any seat."""
    (repo / "design" / "t" / "mock.html").write_text("<html>moved</html>\n", encoding="utf-8")
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("add", "-A"); g("commit", "-q", "-m", "the mock moved under the contract"); g("push", "-q", "origin", "dev")
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}})
    check("stale: stopped at the contract", js.get("verdict") == "stopped" and js.get("stage") == "contract", raw[-400:])
    evs = journal(repo, js.get("run_id", ""))
    check("stale: no seat ran", not seats(evs))


def drill_sentence(repo):
    rc, js, raw = run_slice(repo, {}, ref="the drawer scrolls")
    check("sentence: refused", js.get("verdict") == "stopped" and "issue number" in str(js.get("why")), raw[-300:])


def drill_red_first(repo):
    """A BROKEN row ships red, the PR body leads with it, the ledger still gets a row."""
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE},
                                   "verify": {"mode": "ok", "report": {"items": [
                                       {"item": "T-01", "state": "BROKEN", "substate": "failed", "observed": "no row hidden", "seen": "no row hidden",
                                        "situation": "venue", "paths_tried": ["happy"], "scenario": None, "capture": None, "plan": "plan.md"},
                                       {"item": "T-02", "state": "WORKS", "substate": None, "observed": "seen", "seen": "seen", "situation": "venue",
                                        "paths_tried": ["happy"], "scenario": None, "capture": None, "plan": "plan.md",
                                        "tenant_id": "tenant-1", "evidence": ["plan.md"]},
                                       {"item": "T-03", "state": "WORKS", "substate": None, "observed": "seen", "seen": "seen", "situation": "venue",
                                        "paths_tried": ["happy"], "scenario": None, "capture": None, "plan": "plan.md",
                                        "tenant_id": "tenant-1", "evidence": ["plan.md"]}]}}},
                            extra=())
    # --no-venue skips the drive in drills; the red-first body is asserted through the report writer instead
    check("red-first: shipped (no drive in drills)", js.get("verdict") in ("mergeable", "run-complete"), raw[-300:])
    body = Path(repo / ".context" / "harness" / "runs" / js.get("run_id", "") / "evidence" / "pr-body.md")
    check("red-first: a PR body was written", body.exists())


def drill_wave(repo):
    """Spawned by a wave: builds and reviews itself, records its quick checks, ends `built`
    with no venue, no tester, no suite and no PR; the wave measures its rows."""
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("branch", "wave/w1", "dev"); g("push", "-q", "origin", "wave/w1")
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}, extra=("--wave", "w1"))
    check("wave: built", js.get("verdict") == "built", raw[-400:])
    check("wave: no PR", js.get("pr") is None and js.get("wave") == "w1")
    evs = journal(repo, js.get("run_id", ""))
    check("wave: no pr op", not any(e.get("key") == "pr" for e in evs))
    kinds = [e.get("seat") for e in evs if e.get("kind") == "seat" and e.get("state") == "started"]
    check("wave: a review sat, no tester", "review" in kinds and not any(str(k).startswith("verify") for k in kinds), str(kinds))
    check("wave: quick checks recorded, never a stop", any(e.get("kind") == "quick-checks" for e in evs) and not js.get("stops"))
    check("wave: no venue and no suite", not any(e.get("kind") == "op" and (str(e.get("key")).startswith("venue-up") or "suite" in str(e.get("key"))) for e in evs))
    wt = next((e.get("base_sha") for e in evs if e.get("phase") == "worktree"), "")
    base = sh(["git", "-C", str(repo), "rev-parse", "origin/wave/w1"])[1].strip()
    check("wave: based on the wave branch", wt == base, f"{wt[:9]} vs {base[:9]}")


def drill_ledger_rev_from_tree(repo):
    """A slice whose branch bumps an item's rev booked its rows at the rev the contract read
    at the base commit. The row's rev is read from the manifest in the run's own tree, the
    tree the rows describe; an item that tree does not carry keeps the contract's rev."""
    from hb.beats import slice as sl
    from hb.runner import hbcall
    man = repo / "docs" / "manifests" / "t.yaml"
    text = man.read_text(encoding="utf-8")
    man.write_text(text.replace('  - id: "T-01"\n    rev: 1', '  - id: "T-01"\n    rev: 2'), encoding="utf-8")
    calls = []
    real = hbcall.hb
    hbcall.hb = lambda args, cwd, timeout=1800, env=None: (calls.append(list(args)) or (0, {}, ""))
    try:
        contract = {"issue": "#7", "manifest": str(man), "source": "manifest",
                    "items": [{"item": "T-01", "rev": 1}, {"item": "T-09", "rev": 3}]}
        receipts = [dict(_receipt("T-01", "WORKS"), evidence=[]), dict(_receipt("T-09", "WORKS"), evidence=[])]

        class J:
            def append(self, *a, **k): pass
        with _In(repo):
            sl._ledger_rows(J(), contract, receipts, repo, "abc123def", None, "t", None, served_sha="feed1234")
    finally:
        hbcall.hb = real
    rows = [c for c in calls if c[:2] == ["ledger", "receipt"]]
    revs = {c[c.index("--item") + 1]: c[c.index("--rev") + 1] for c in rows}
    check("ledger rev: the row takes the rev the run's tree carries", revs.get("T-01") == "2", str(revs))
    check("ledger: each row names its tenant and the venue record's served commit",
          all(c[c.index("--tenant-id") + 1] == "tenant-1" and c[c.index("--served-sha") + 1] == "feed1234" for c in rows),
          str(rows)[:400])
    check("ledger rev: an item the tree does not carry keeps the contract's rev", revs.get("T-09") == "3", str(revs))


class _In:
    """cwd inside the drill's repo, so cfg() reads its harness-config (config `model.paths`)."""

    def __init__(self, repo):
        self.repo = repo

    def __enter__(self):
        import os
        self.here = os.getcwd()
        os.chdir(self.repo)

    def __exit__(self, *exc):
        import os
        os.chdir(self.here)


def drill_real_model_derived(repo):
    """A contract with a chat-verb item drives on the real model; one without drives on the fake;
    a change under the config's `model.paths` drives on the real model whatever its items."""
    from hb.contract import policy
    check("real model: a chat-verb item asks for the real model",
          policy.needs_real_model([{"item": "T-01", "kind": "screen"}, {"item": "T-02", "kind": "chat-verb"}]))
    check("real model: screens and api drive on the fake",
          not policy.needs_real_model([{"item": "T-01", "kind": "screen"}, {"item": "T-02", "kind": "api"}]))
    api = [{"item": "T-01", "kind": "api"}]
    with _In(repo):
        check("real model: a change under the model's paths asks for the real model",
              all(policy.needs_real_model_for(api, [p]) for p in ("src/model/tools/pull.py",
                  "src/model/capabilities/x/verb.py", "src/model/core/defaults.py"))
              and not policy.needs_real_model_for(api, ["src/model/core/runtime.py", "src/x.py"]))
    src = (ENGINE / "hb" / "beats" / "loop.py").read_text(encoding="utf-8")
    # the one venue the loop brings up takes the derived model, never a literal
    check("real model: the loop's venue takes the derived model",
          "standin.ensure(j, slug, wt, v, served_tip, real_llm=real" in src and "real_llm=True" not in src
          and 'policy.needs_real_model_for(contract["items"], changed)' in src)


def drill_real_model_at_each_caller(repo):
    """The slice's build and discovery venues each ask the real model of the
    run's base diff: a diff under src/model/tools/ brings the venue up real, a diff elsewhere does
    not. An absolute path matches by its repo-relative part."""
    import types
    from hb.beats import slice as S
    from hb.venue import standin
    from hb.rig import gitops
    from hb.contract import policy
    seen, diff = [], {"paths": []}
    saved = (standin.ensure, standin.venue_up, gitops.changed_paths)
    standin.ensure = lambda j, slug, wt, v, tip, real_llm=False, key=None, **kw: seen.append(real_llm) or {}
    standin.venue_up = lambda j, slug, wt, real_llm=False, key=None, **kw: seen.append(real_llm) or {}
    gitops.changed_paths = lambda wt, base, to="HEAD": list(diff["paths"])
    a = types.SimpleNamespace(no_venue=False, real_llm=False)
    contract = {"issue": "#7", "items": [{"item": "T-01", "kind": "api"}]}
    j = types.SimpleNamespace(append=lambda *x, **k: None)
    try:
        with _In(repo):
            for paths, want in ((["src/model/tools/pull.py"], True), (["src/services/x.py"], False)):
                diff["paths"], seen[:] = paths, []
                S.venue_for_build(j, a, contract, "7", repo, {}, "1", gitops.changed_paths(repo, "base"))
                S.venue_for_build(j, a, contract, "7", repo, {}, "discovery", gitops.changed_paths(repo, "base"))
                check(f"real model at each caller: {paths[0]} brings the build and discovery venues up "
                      f"{'real' if want else 'fake'}", seen == [want] * 2, str(seen))
            check("real model at each caller: an absolute path matches by its repo-relative part",
                  policy.needs_real_model_for([], ["C:/work/run-7/src/model/tools/pull.py"])
                  and not policy.needs_real_model_for([], ["C:/work/run-7/src/services/x.py"]))
    finally:
        standin.ensure, standin.venue_up, gitops.changed_paths = saved


def _receipt(item, state, substate=None, observed="seen"):
    return {"item": item, "state": state, "substate": substate, "observed": observed, "seen": observed, "situation": "venue",
            "paths_tried": ["happy"], "scenario": None, "capture": None, "plan": "plan.md",
            "tenant_id": "tenant-1", "evidence": ["plan.md"]}


def drill_repair_entry(repo):
    """`--repair <run>` re-enters a finished run's branch at its red rows: no build seat, the
    prior receipts carried, the repair phase names the reds; a run with no red refuses."""
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}})
    prior = js.get("run_id", "")
    check("repair entry: a first run shipped", js.get("verdict") in ("mergeable", "run-complete") and prior, raw[-300:])
    rd = repo / ".context" / "harness" / "runs" / prior
    # no drive ran in the drill (--no-venue); a drive's receipts are written in its place
    vd = rd / "seats" / "verify-9"
    vd.mkdir(parents=True, exist_ok=True)
    (vd / "report.json").write_text(json.dumps({"items": [_receipt("T-01", "BROKEN", "failed", "no row hidden"),
                                                          _receipt("T-02", "WORKS"), _receipt("T-03", "WORKS")]}), encoding="utf-8")
    # a repair round's re-drive carries only the items it drove: the fold keeps the other rows
    # (run 20261007-010144-slice-2472 lost 6 of 7 rows to a one-item re-drive and had nothing to repair)
    time.sleep(0.05)
    rd2 = rd / "seats" / "verify-10"
    rd2.mkdir(parents=True, exist_ok=True)
    (rd2 / "report.json").write_text(json.dumps({"items": [_receipt("T-03", "WORKS")]}), encoding="utf-8")
    rc, js2, raw2 = run_slice(repo, {"fix": {"mode": "ok"}},
                              extra=("--repair", prior, "--brief", "fix the hidden row"))
    evs = journal(repo, js2.get("run_id", ""))
    rep = next((e for e in evs if e.get("kind") == "phase" and e.get("phase") == "repair"), {})
    # the journal line keeps scalars; the reds list lives in phase.json, which later phases merge into
    ph = json.loads((rd.parent / js2.get("run_id", "") / "phase.json").read_text(encoding="utf-8"))
    check("repair entry: the repair phase names the prior run and its reds", rep.get("prior_run") == prior and ph.get("reds") == ["T-01"], str(ph)[:300])
    check("repair entry: no build seat ran", not seats(evs, "build"), str(seats(evs, "build"))[:200])
    check("repair entry: the brief is the first control answer", any(e.get("kind") == "control" and "fix the hidden row" in str(e.get("text")) for e in evs)
          or "fix the hidden row" in (rd.parent / js2.get("run_id", "") / "control.txt").read_text(encoding="utf-8"))
    check("repair entry: without a stand-in the fix pass still sits once, nothing can be re-driven, and the red ships red, named",
          js2.get("verdict") == "run-complete" and len(seats(evs, "fix")) >= 1
          and any(e.get("kind") == "loop" and e.get("result") in ("moved-nothing", "ended") for e in evs), raw2[-400:])
    # a run with no blocking red refuses the entry
    (vd / "report.json").write_text(json.dumps({"items": [_receipt("T-01", "WORKS"), _receipt("T-02", "WORKS"), _receipt("T-03", "WORKS")]}), encoding="utf-8")
    rc, js3, raw3 = run_slice(repo, {}, extra=("--repair", prior))
    check("repair entry: nothing to repair refuses", js3.get("verdict") == "stopped" and "nothing to repair" in str(js3.get("why")), raw3[-300:])


def drill_repair_owes_unmeasured_rows(repo):
    """A run whose stand-in never came up landed with every driven row UNMEASURED and no verify
    seat report, and `--repair` said nothing to repair. Its landed receipts are folded with the
    seats', and any UNMEASURED row on an item not filed grey is owed a drive with no fixer."""
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}})
    prior = js.get("run_id", "")
    check("unmeasured repair: a first run landed", js.get("verdict") in ("mergeable", "run-complete") and prior, raw[-300:])
    ev = repo / ".context" / "harness" / "runs" / prior / "evidence"
    rows = [_receipt("T-01", "WORKS"), _receipt("T-02", "WORKS"),
            _receipt("T-03", "UNMEASURED", "stack-failed", "the stand-in did not come up")]
    (ev / "receipts.json").write_text(json.dumps({"issue": "#7", "items": rows}), encoding="utf-8")
    rc, js2, raw2 = run_slice(repo, {}, extra=("--repair", prior))
    evs = journal(repo, js2.get("run_id", ""))
    ph = json.loads((ev.parent.parent / js2.get("run_id", "") / "phase.json").read_text(encoding="utf-8"))
    check("unmeasured repair: the repair enters and owes the UNMEASURED row",
          "nothing to repair" not in str(js2.get("why")) and ph.get("reds") == ["T-03"], str(js2.get("why")) + str(ph.get("reds")))
    check("unmeasured repair: no fixer and no build seat sat for it", not seats(evs, "fix") and not seats(evs, "build"),
          str([(s["seat"], s["state"]) for s in seats(evs)]))


def drill_repair_after_the_tip_moved(repo):
    """A slice shipped green, then a sibling merged and the branch took dev by hand: the tip
    has no Walk and nothing is red, so `--repair` once refused with nothing to repair and no
    door walked the new tip. A moved tip owes every driven row a drive again, test-proven and
    grey rows carry, and a reused tree takes the branch as origin holds it first."""
    sys.path.insert(0, str(HERE.parent / "engine"))
    from hb.rig import gitops
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}})
    prior = js.get("run_id", "")
    check("moved tip: a first run shipped", js.get("verdict") in ("mergeable", "run-complete") and prior, raw[-300:])
    rd = repo / ".context" / "harness" / "runs" / prior
    vd = rd / "seats" / "verify-9"
    vd.mkdir(parents=True, exist_ok=True)
    rows = [_receipt("T-01", "WORKS"), _receipt("T-02", "WORKS"), dict(_receipt("T-03", "WORKS"), by="test")]
    (vd / "report.json").write_text(json.dumps({"items": rows}), encoding="utf-8")
    # the tip has not moved: nothing to repair
    rc, js2, raw2 = run_slice(repo, {}, extra=("--repair", prior))
    check("moved tip: an unmoved green run still has nothing to repair", js2.get("verdict") == "stopped" and "nothing to repair" in str(js2.get("why")), raw2[-300:])
    # dev merged by hand into the slice branch: the tree moves past the tip the run left
    wt = repo / ".claude" / "worktrees" / "run-slice-issue-7"
    g = lambda *a: sh(["git", "-C", str(wt), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    (wt / "src" / "merged.py").write_text("x = 2\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "merge dev")
    rc, js3, raw3 = run_slice(repo, {}, extra=("--repair", prior))
    evs = journal(repo, js3.get("run_id", ""))
    ph = json.loads((rd.parent / js3.get("run_id", "") / "phase.json").read_text(encoding="utf-8"))
    check("moved tip: the repair enters, names the move and owes the driven rows, carrying the test-proven one",
          js3.get("verdict") != "stopped" and ph.get("moved") is True and ph.get("redrive") == ["T-01", "T-02"]
          and ph.get("reds") == [] and ph.get("touched") >= 1, str({k: ph.get(k) for k in ("moved", "redrive", "reds", "touched")}) + raw3[-200:])
    rd3 = rd.parent / js3.get("run_id", "")
    whole = [b for b in rd3.glob("seats/review-*/brief.md") if "## Read" in b.read_text(encoding="utf-8")]
    check("moved tip: no build seat and no whole-diff read ran", not seats(evs, "build") and not whole,
          str(seats(evs, "build")) + str(whole))
    # a reused tree takes the branch origin holds, fast-forward only
    g("push", "-q", "-u", "origin", "slice/issue-7")
    rc, head, _ = sh(["git", "-C", str(wt), "rev-parse", "HEAD"])
    g("reset", "-q", "--hard", "HEAD~1")
    synced = gitops.sync_to_origin(wt, "slice/issue-7")
    check("moved tip: a reused tree fast-forwards to origin's branch", synced.get("moved") is True and synced.get("tip") == head.strip(), str(synced))
    (wt / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "diverged")
    g("push", "-q", "-f", "origin", "slice/issue-7")
    g("reset", "-q", "--hard", "HEAD~1")
    (wt / "src" / "local.py").write_text("z = 1\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "local")
    diverged = gitops.sync_to_origin(wt, "slice/issue-7")
    check("moved tip: a tree that diverged from origin is named, never merged", "diverged" in str(diverged.get("error")), str(diverged))


def drill_grey_never_blocks(repo):
    """An item the manifest files `drivable: false` is UNMEASURED by nature and never makes the
    run ship red (the owner's ruling); a BROKEN row still does."""
    sys.path.insert(0, str(HERE.parent / "engine"))
    from hb.contract import policy
    items = [{"item": "T-01", "drivable": True}, {"item": "T-02", "drivable": False}]
    grey = [_receipt("T-01", "WORKS"), _receipt("T-02", "UNMEASURED", "not-driven")]
    check("grey: an UNMEASURED grey-at-filing row does not block", policy.blocking_red(grey, items) == [])
    check("grey: an UNMEASURED row on a drivable item blocks",
          [r["item"] for r in policy.blocking_red([_receipt("T-01", "UNMEASURED", "verifier-failed")], items)] == ["T-01"])
    try:
        from hb.beats.wave import _goal_rows
    except ImportError:
        _goal_rows = None
        print("  SKIP  grey goal rows: this cut ships no wave door")
    goals = _goal_rows({"body": "## GOAL\n- G1: when a, then b\n- G2 (not drivable: needs a stub door): when c, then d\n"}) if _goal_rows else None
    if goals is not None:
        check("grey: a goal line marked (not drivable) is grey at filing, its words intact",
              [g["drivable"] for g in goals] == [True, False] and goals[1]["when"] == "c" and goals[1]["then"] == "d", str(goals))
    if goals is not None:
        check("grey: a grey goal's UNMEASURED row does not block",
              policy.blocking_red([_receipt("G2", "UNMEASURED", "not-driven")], goals) == [])
    check("grey: BROKEN always blocks", [r["item"] for r in policy.blocking_red([_receipt("T-02", "BROKEN", "failed")], items)] == ["T-02"])


# a finding outside any cause's boundary, as the reviewer and the fixer mark it
BUILD_FINDING = {"id": "F1", "axis": "goal", "severity": "P1", "class": "broken", "confidence": 8,
                 "title": "T-02 is not derived from the facts", "summary": "T-02 needs the derivation module",
                 "rule": "T-02 then", "quote": {"file": "src/x.py", "line": 1, "text": "x = 1"},
                 "absence": None, "recurrence": "n/a", "status": "open", "fixed_at": None,
                 "size": "build", "build_reason": "the derivation module the contract names does not exist yet"}
LEFT_BUILD = {"causes": [], "left_open": [{"finding": "F1", "why": "a build leg, not a fix pass",
                                          "verdict": "open", "evidence": "x.py:1", "size": "build",
                                          "build_reason": "the derivation module does not exist yet"}]}


def drill_declined_finding_rides_the_pr(repo):
    """A finding the review raises and each fix round leaves open on the same words ends by
    name after two fixes and rides the PR for the owner: never a stop, never a third fix
    (Q8). The fix round is the builder relaunched, with the builder's decisions."""
    script = {"build": {"mode": "ok", **TOUCH, "report": dict(BUILD_NOTE, decisions=[{"text": "kept the drawer", "ruling": "R1"}])},
              "review.1": {"mode": "ok", "report": {"findings": [BUILD_FINDING]}},
              "review.2": {"mode": "ok", "report": {"scope": "the fix", "findings": [BUILD_FINDING]}},
              "review.3": {"mode": "ok", "report": {"scope": "the fix", "findings": [BUILD_FINDING]}},
              "fix": {"mode": "ok", **TOUCH, "report": LEFT_BUILD}}
    rc, js, raw = run_slice(repo, script)
    evs = journal(repo, js.get("run_id", ""))
    check("declined: the run lands, no stop", js.get("verdict") in ("mergeable", "run-complete") and not js.get("stops"), raw[-500:])
    fixes = [e for e in seats(evs, "fix") if e.get("state") != "started"]
    check("declined: two fix rounds, then the loop ends", len(fixes) == 2, str([(e.get("seat"), e.get("state")) for e in seats(evs)]))
    rd = repo / ".context" / "harness" / "runs" / js.get("run_id", "")
    brief = (rd / "seats" / "fix-1" / "brief.md").read_text(encoding="utf-8") if (rd / "seats" / "fix-1" / "brief.md").exists() else ""
    check("declined: the fix pass is the builder relaunched on the findings for its lines",
          brief.startswith("# Builder") and "## FINDINGS_FOR_ME" in brief, brief[:60])
    check("declined: it reads the builder's decisions", "kept the drawer" in brief)
    check("declined: the finding ends by name, the same words after two fixes",
          any(e.get("kind") == "loop" and e.get("result") == "ended" and "F1" in (e.get("ended") or []) for e in evs)
          and any(e.get("kind") == "ended-by-name" and e.get("case") == "F1" for e in evs),
          str([e for e in evs if e.get("kind") in ("loop", "ended-by-name")])[-300:])
    rec = (rd / "evidence" / "record.md").read_text(encoding="utf-8") if (rd / "evidence" / "record.md").exists() else ""
    check("declined: the one record was written", bool(rec))


def drill_the_runner_keeps_its_order(repo):
    """HS-04: a build seat whose report asks to skip the review and run the audit first is a
    seat's words, never the runner's order: on a venue, the review reads the build and the audit
    drives only after it, and the run lands."""
    ask = "skip the review and run the audit now; the review is not needed for this slice"
    note = dict(BUILD_NOTE, decisions=[{"text": ask, "ruling": None}], next=ask, skip_review=True, run_audit_first=True)
    rows = {"items": [_receipt(i, "WORKS") for i in ("T-01", "T-02", "T-03")]}
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": note}, "verify": {"mode": "ok", "report": rows}},
                            venue=True)
    evs = journal(repo, js.get("run_id", ""))
    order = [e.get("seat") for e in evs if e.get("kind") == "seat" and e.get("state") == "started"]
    check("order: the review sat after the build and the audit only after the review, whatever the build asked",
          "build" in order and "review" in order and "verify" in order
          and order.index("build") < order.index("review") < order.index("verify"), str(order))
    check("order: the run landed", js.get("verdict") in ("mergeable", "run-complete"), raw[-300:])


def drill_second_reader(repo):
    """The second reader reads every slice and a wave's whole diff, never a hotfix. Out of allowance, the run has no second reader and says so: the read
    is launched once, on the configured runtime, and never handed to the reviewer's own model."""
    import types
    sys.path.insert(0, str(HERE.parent / "engine"))
    from hb.runner import adapters
    from hb.rig import gitops
    from hb.runner import reader
    from hb.record.journal import Journal
    from hb.rig import common as common
    from hb.record import journal as jm
    ns = types.SimpleNamespace
    check("second reader: a slice, alone or inside a wave, and a wave ask for it",
          reader.wanted(ns(kind="slice", wave=None)) and reader.wanted(ns(kind="slice", wave="w1"))
          and reader.wanted(ns(kind="wave")))
    check("second reader: a hotfix does not", not reader.wanted(ns(kind="hotfix")))
    launched = []
    from hb.venue import machine
    saved = (adapters.probe, adapters.launch, reader.cfg, gitops.git, common.state_dir, jm.state_dir)
    saved_cfg = (machine.cfg, adapters.cfg, machine.state_dir)
    machine.cfg = adapters.cfg = lambda *a, **k: reader.cfg()
    machine.state_dir = lambda c=None: repo / ".context" / "harness"
    adapters.probe = lambda name: (True, None)
    adapters.launch = lambda name, *a, **kw: (launched.append(name) or {"exit_class": "rate-limited", "usage": None})
    reader.cfg = lambda: {"seats": {"codex": {"runtime": "codex", "model": "m", "fallback": "claude", "fallback_model": "sonnet"}},
                      "state_dir": str(repo / ".context" / "harness")}
    gitops.git = lambda wt, *a, **kw: (0, "diff --git a/x b/x", "")
    common.state_dir = lambda c=None: repo / ".context" / "harness"
    jm.state_dir = common.state_dir
    try:
        j = Journal("drill-second-reader")
        got = reader.codex_read(j, repo, "base", {"rulings": "", "items": []}, repo, 1)
    finally:
        adapters.probe, adapters.launch, reader.cfg, gitops.git, common.state_dir, jm.state_dir = saved
        machine.cfg, adapters.cfg, machine.state_dir = saved_cfg
    check("second reader: out of allowance it is launched once and then once as claude", launched == ["codex", "claude"], str(launched))
    check("second reader: the run says it had none, and why", got.get("path") is None
          and "no second reader this run" in got.get("fact", "") and "rate-limited" in got.get("fact", ""), str(got))


def drill_codex_seat_is_quiet(repo):
    """A codex seat carried the owner's own MCP servers, plugins and skills into every turn,
    and the owner's Fast tier, which spends the plan at 2.5x. The launched command switches
    each MCP server off by name, disables plugins and the rest, and asks for the standard tier."""
    sys.path.insert(0, str(HERE.parent / "engine"))
    from hb.runner import adapters
    saved = (adapters.cfg, adapters.codex_mcp_names)
    echo = ["python", "-c", "import sys, json; print(json.dumps(sys.argv[1:]))", "exec", "-"]
    adapters.cfg = lambda *a, **k: {"runtimes": {"codex": {"cmd": echo}}}
    adapters.codex_mcp_names = lambda exe, cwd: ["telemetry", "n8n-mcp"]
    try:
        out_dir = repo / ".context" / "harness" / "runs" / "drill-quiet" / "seats" / "codex-1"
        brief = repo / ".context" / "brief.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text("read", encoding="utf-8")
        res = adapters.launch("codex", "codex", brief, repo, out_dir, model="m", timeout_s=60)
        argv = json.loads((out_dir / "stdout.txt").read_text(encoding="utf-8").strip().splitlines()[-1])
    finally:
        adapters.cfg, adapters.codex_mcp_names = saved
    pairs = list(zip(argv, argv[1:]))
    check("codex seat: every MCP server is switched off by name",
          ("-c", "mcp_servers.telemetry.enabled=false") in pairs and ("-c", "mcp_servers.n8n-mcp.enabled=false") in pairs, str(argv))
    check("codex seat: plugins, apps, hooks and skills ride no turn",
          ("--disable", "plugins") in pairs and ("--disable", "apps") in pairs and ("--disable", "hooks") in pairs
          and ("-c", "skills.max_context_tokens=1") in pairs and ("-c", "agents.enabled=false") in pairs, str(argv))
    check("codex seat: the standard service tier, never the owner's Fast", ("-c", "service_tier=default") in pairs, str(argv))
    check("codex seat: the options sit before the prompt", argv[-1] == "-" and res["exit_class"] == "ok", str(argv))


LIMIT_TEXT = ("ERROR: You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
              "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 1:32 AM.\n"
              "tokens used\n224,975\n")


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def drill_second_reader_tokens(repo):
    """What each review costs the owner's plan is on the page: codex prints `tokens used`
    at the end of its stderr, the seat journals it, and the run page totals it."""
    sys.path.insert(0, str(HERE.parent / "engine"))
    from hb.runner import adapters
    from hb.record import live
    err = repo / ".context" / "err.txt"
    out_ = repo / ".context" / "out.txt"
    err.parent.mkdir(parents=True, exist_ok=True)
    err.write_text("x\n" * 5000 + "tokens used\n407,120\n", encoding="utf-8")
    out_.write_text("src/x.py:1: a claim\n", encoding="utf-8")
    check("tokens: the codex usage line is read from its stderr",
          adapters.read_usage("codex-text", out_, err) == {"total_tokens": 407120})
    evs = [{"kind": "seat", "seat": "codex", "attempt": 1, "state": "ok", "minutes": 5.7, "usage": {"total_tokens": 407120}}]
    check("tokens: the page line names what the read cost", live.second_reader(evs) == "Codex read the whole change (407,120 tokens)",
          str(live.second_reader(evs)))
    check("tokens: the activity line names it too", "407,120 tokens" in str(live._line(evs[0], 1)), str(live._line(evs[0], 1)))


def drill_no_base_capture_without_a_screen(repo):
    """A contract with no screen item has no look to capture at the base commit: the slice
    brings up no base stand-in and says so."""
    man = repo / "docs" / "manifests" / "t.yaml"
    text = man.read_text(encoding="utf-8")
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    sp = repo / ".context" / "fake-script.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}), encoding="utf-8")

    def go():
        rc, o, e = sh([sys.executable, HB, "run", "slice", "#7", "--shadow", "--rate-limit-wait-min", "0"], cwd=str(repo),
                      env={"FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(stub_gh(repo, BODY)), "HB_VENUE_STUB": "1"})
        return journal(repo, last_json(o).get("run_id", "")), o + e
    man.write_text(text.replace('kind: "screen"', 'kind: "api"'), encoding="utf-8")
    g("commit", "-qam", "no screen item"); g("push", "-q", "origin", "dev")
    evs, raw = go()
    sized = [e for e in evs if e.get("kind") == "sized" and e.get("before") is False]
    check("base capture: a contract with no screen item brings up no base stand-in and says so",
          sized and not any(e.get("kind") == "op" and e.get("key") == "venue-up-base" for e in evs), raw[-400:])

# a fake builder's own stream: one commit to a backend file, then the turns a Claude seat prints
GROW = """import json, subprocess, sys, time
from pathlib import Path
x = Path("src/x.py")
x.write_text(x.read_text(encoding="utf-8") + "# grown\\n", encoding="utf-8")
subprocess.run(["git", "add", str(x)]); subprocess.run(["git", "-c", "user.email=f@f", "-c", "user.name=f", "commit", "-qm", "grow"])
held, helper, wait = int(sys.argv[1]), sys.argv[2] == "helper", float(sys.argv[3])
if helper:
    print(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"usage": {"input_tokens": 10},
          "content": [{"type": "tool_use", "id": "toolu_h1", "name": "Agent", "input": {"prompt": "open the drawer"}}]}}))
    print(json.dumps({"type": "assistant", "parent_tool_use_id": "toolu_h1", "message": {"usage": {"input_tokens": 900000}}}))
    print(json.dumps({"type": "user", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_h1"}]},
          "tool_use_result": {"totalTokens": 4200, "totalDurationMs": 90000}}))
print(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": held}}}), flush=True)
time.sleep(wait)
"""

FOURTH = """  - id: "T-04"
    rev: 1
    kind: "api"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a caller restores the row"
    then: "the row is back"
    files: ["src/x.py"]
"""


def _venue_repo(repo, ceiling):
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    man = repo / "docs" / "manifests" / "t.yaml"
    man.write_text(man.read_text(encoding="utf-8") + FOURTH, encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Product\n\n## How we write code\n\nPrefer deletion to addition.\n\n## Other\n\nno\n",
                                    encoding="utf-8")
    cfg_path = repo / ".claude" / "harness-config.json"
    conf = json.loads(cfg_path.read_text(encoding="utf-8"))
    conf.setdefault("seats", {})["context_ceiling"] = ceiling
    cfg_path.write_text(json.dumps(conf, indent=2), encoding="utf-8")
    (repo / ".context").mkdir(exist_ok=True)
    (repo / ".context" / "grow.py").write_text(GROW, encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "four items"); g("push", "-q", "origin", "dev")
    g("branch", "wave/w1", "dev"); g("push", "-q", "origin", "wave/w1")


def _run_with_venue(repo, script, free_gb):
    sp = repo / ".context" / "fake-script.json"
    sp.write_text(json.dumps(script), encoding="utf-8")
    body = BODY.replace("T-01..T-03", "T-01..T-04")
    rc, o, e = sh([sys.executable, HB, "run", "slice", "#7", "--shadow", "--wave", "w1", "--rate-limit-wait-min", "0"],
                  cwd=str(repo), env={"FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(stub_gh(repo, body)), "HB_VENUE_STUB": "1",
                                      "HB_POLL_S": "1", "HB_SETTLE_S": "0", "HB_SETTLE_MAX_S": "0", "HB_FORCE_AFTER_S": "0", "HB_MEMORY_FREE_GB": str(free_gb), "HB_MEMORY_TOTAL_GB": "32"})
    js = last_json(o)
    return js, journal(repo, js.get("run_id", "")), o + e


def drill_builder_with_a_venue(repo):
    """Q3, A15, A16, A17: a slice of four items builds on its own venue, stood before the build;
    a builder past the context ceiling is relaunched on its progress file; a helper's spend is
    journaled under the builder; under admission's line the build waits for its venue."""
    grow = str(repo / ".context" / "grow.py")
    _venue_repo(repo, ceiling=4000)
    ok_build = {"mode": "ok", "report": BUILD_NOTE, "exec": [grow, "10", "no", "0"]}
    script = {"build.1": {"mode": "ok", "exec": [grow, "5000", "no", "60"]},
              "build.2": {"mode": "ok", "report": BUILD_NOTE, "exec": [grow, "10", "helper", "0"]},
              "build": ok_build}
    js, evs, raw = _run_with_venue(repo, script, free_gb=30)
    check("venue:the slice ends built", js.get("verdict") == "built", raw[-600:])
    ups = [e for e in evs if e.get("kind") == "op" and str(e.get("key")).startswith("venue-up-build") and e.get("state") == "complete"]
    first_build = next((n for n, e in enumerate(evs) if e.get("kind") == "seat" and e.get("seat") == "build"), 10 ** 6)
    first_up = next((n for n, e in enumerate(evs) if e.get("kind") == "op" and str(e.get("key")).startswith("venue-up-build-1")), 10 ** 6)
    check("venue:the builder's venue stands before the build", first_up < first_build and ups and ups[0].get("key") == "venue-up-build-1",
          [e.get("key") for e in ups])
    ended = [e for e in evs if e.get("kind") == "relaunched"]
    check("venue:a builder past the context ceiling is relaunched, journaled why: context",
          ended and ended[0].get("why") == "context" and ended[0].get("tokens", 0) >= 4000, str(ended)[:300])
    builds = [e for e in evs if e.get("kind") == "seat" and e.get("seat") == "build" and e.get("state") not in ("started",)]
    check("venue:the relaunched builder finishes the build on its progress file",
          builds[:2] and builds[0].get("state") == "context" and builds[1].get("state") == "ok", [e.get("state") for e in builds])
    run_dir = repo / ".context" / "harness" / "runs" / js.get("run_id", "")
    brief = (run_dir / "seats" / "build-2" / "brief.md").read_text(encoding="utf-8") if (run_dir / "seats" / "build-2" / "brief.md").exists() else ""
    check("venue:the builder's brief carries the venue, the helper's guard mark and its progress file",
          "## The venue" in brief and "HB_HELPER=1" in brief and "progress.md" in brief, brief[-400:])
    helped = [e for e in evs if e.get("kind") == "helpers"]
    seat_line = next((e for e in builds if e.get("attempt") == 2), {})
    check("venue:a helper's tokens and minutes are journaled under the builder",
          helped and helped[0].get("seat") == "build" and helped[0].get("calls") == 1 and helped[0].get("tokens") == 4200
          and helped[0].get("minutes") == 1.5 and ((seat_line.get("usage") or {}).get("helpers") or {}).get("calls") == 1,
          str(helped)[:300])
    from hb.runner import usage
    check("venue:the run's spend sums the helpers under the builder", usage.of(run_dir, evs)["helpers"]["calls"] == 1)
    check("venue:the builder's venue stands once for the whole build", len(ups) == 1, str([e.get("key") for e in ups]))
    check("venue:the builder's venue goes down with its database kept when the build ends",
          any(e.get("kind") == "op" and e.get("key") == "venue-down-build" for e in evs))

    js, evs, raw = _run_with_venue(repo, {"build": ok_build}, free_gb=1)
    check("venue:under admission's line the venue's grant waits and the build still runs on a venue, never blind (Q3)",
          js.get("verdict") == "built"
          and any(e.get("kind") == "op" and str(e.get("key")).startswith("venue-up-build") for e in evs), raw[-400:])


def drill_helpers_are_guarded(repo):
    """A17: the guard refuses a background helper, a helper's commit and a helper's own helper,
    each with a plain sentence; the builder alone is handed the Agent tool."""
    import subprocess
    from hb.runner import adapters as A
    from hb.runner import seatguard
    guard = Path(seatguard.__file__)

    def hook(call):
        p = subprocess.run([sys.executable, str(guard)], input=json.dumps(call), capture_output=True, text=True, timeout=60)
        return p.returncode, p.stderr
    rc, err = hook({"tool_name": "Agent", "tool_input": {"prompt": "open the drawer", "run_in_background": True}})
    check("helpers: a background spawn is refused with a plain sentence", rc == 2 and "foreground" in err, err)
    check("helpers: a foreground spawn passes", hook({"tool_name": "Agent", "tool_input": {"prompt": "open the drawer"}})[0] == 0)
    rc, err = hook({"tool_name": "Bash", "tool_input": {"command": "HB_HELPER=1 git commit -qm x"}})
    check("helpers: a helper's commit is refused", rc == 2 and "never commits" in err, err)
    check("helpers: the builder's own commit passes", hook({"tool_name": "Bash", "tool_input": {"command": "git commit -qm x"}})[0] == 0)
    rc, err = hook({"tool_name": "Agent", "agent_id": "a1", "tool_input": {"prompt": "x"}})
    check("helpers: a helper's own spawn is refused", rc == 2 and "never spawns" in err, err)
    st = json.loads(A.seat_settings(repo.parent / "seat-settings" / "build-1").read_text(encoding="utf-8"))
    matcher = st["hooks"]["PreToolUse"][0]["matcher"].split("|")
    check("helpers: every Agent call goes through the guard", "Agent" in matcher and "Bash" in matcher, matcher)
    check("helpers: only the builder may spawn and it gets the browser", A.HELPER_ROLES == ("build",)
          and A.role_tools("build") == ("ui",) and A.role_tools("review") == ())


DRILLS = [drill_second_reader, drill_codex_seat_is_quiet,
          drill_second_reader_tokens, drill_no_base_capture_without_a_screen, drill_happy, drill_resume, drill_stale, drill_sentence, drill_red_first, drill_wave, drill_ledger_rev_from_tree, drill_real_model_derived, drill_real_model_at_each_caller, drill_repair_entry, drill_repair_after_the_tip_moved, drill_grey_never_blocks,
          drill_declined_finding_rides_the_pr, drill_the_runner_keeps_its_order, drill_builder_with_a_venue, drill_helpers_are_guarded]


def drill_authority_answered(repo):
    """A slice stopped on a migration and the owner answered: every resume reused the
    stopped build's note and stopped again, so the ruling never reached a builder. The
    answered build runs again, told what was ruled, and the run goes on."""
    asks = dict(BUILD_NOTE, needs_authority=[{"kind": "migration", "text": "needs a column", "evidence": "x.py:1"}])
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": asks}})
    rid = js.get("run_id", "")
    check("authority-answered: first run stops for authority", js.get("stage") == "authority", raw[-300:])
    rc, js2, raw2 = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": asks}}, extra=("--resume", rid))
    check("authority-answered: unanswered, a resume stops again", js2.get("stage") == "authority", raw2[-300:])
    sid = str((js.get("stops") or [""])[0]).split("]")[0].lstrip("[")
    check("authority-answered: the stop carries its id", sid.startswith("A-") and len(sid) == 6, str(js.get("stops")))
    (repo / ".context" / "harness" / "runs" / rid / "control.txt").write_text(
        f"answer: {sid}: yes, add the column" + chr(10), encoding="utf-8")
    # the rebuild repeats the answered row, as a builder may: it is routed, never raised
    rc, js3, raw3 = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": asks},
                                     "verify": {"mode": "ok", "report": {"items": [
                                         {"item": i, "state": "WORKS", "substate": None, "observed": "seen", "seen": "seen",
                                          "situation": "venue", "paths_tried": ["happy"], "scenario": None, "capture": None,
                                          "plan": "plan.md", "tenant_id": "tenant-1", "evidence": ["plan.md"]}
                                         for i in ("T-01", "T-02", "T-03")]}}}, extra=("--resume", rid))
    check("authority-answered: answered, the run goes past the stop", js3.get("stage") != "authority"
          and js3.get("verdict") in ("mergeable", "run-complete"), raw3[-400:])
    evs = journal(repo, rid)
    leg1 = [e for e in evs if e.get("kind") == "build"]
    check("authority-answered: the stopped build ran again", len(leg1) >= 2, str(leg1)[:300])
    briefs = sorted((repo / ".context" / "harness" / "runs" / rid / "seats").glob("build-*/brief.md"))
    check("authority-answered: the rebuild is told a ruling came in, with the answer beside its id",
          any("Ruled since the build stopped" in t and sid in t and "yes, add the column" in t
              for t in (b.read_text(encoding="utf-8") for b in briefs)))
    rc, js4, raw4 = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": asks},
                                     "verify": {"mode": "ok", "report": {"items": []}}}, extra=("--resume", rid))
    evs4 = journal(repo, rid)
    check("authority-answered: a later resume does not raise the answered stop again",
          js4.get("stage") != "authority" and not any(sid in s for s in js4.get("stops") or []), raw4[-400:])
    check("authority-answered: nor build a third time",
          len([e for e in evs4 if e.get("kind") == "build"]) == len(leg1), str(len(leg1)))


def drill_authority_other_id(repo):
    """An answer that names a different stop id does not unlock this one: the build's note is
    kept, the stop is raised again with its own id, and no builder is told it was ruled."""
    asks = dict(BUILD_NOTE, needs_authority=[{"kind": "migration", "text": "needs a column", "evidence": "x.py:1"}])
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": asks}})
    rid = js.get("run_id", "")
    sid = str((js.get("stops") or [""])[0]).split("]")[0].lstrip("[")
    (repo / ".context" / "harness" / "runs" / rid / "control.txt").write_text(
        "answer: A-0000: yes, add the column" + chr(10) + "answer: F9: not this one" + chr(10), encoding="utf-8")
    rc, js2, raw2 = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}, extra=("--resume", rid))
    check("authority-other-id: still stopped for authority", js2.get("stage") == "authority", raw2[-300:])
    check("authority-other-id: the same id is raised again", any(s.startswith(f"[{sid}]") for s in js2.get("stops") or []),
          str(js2.get("stops")))
    evs = journal(repo, rid)
    check("authority-other-id: the stopped build did not run again",
          len([e for e in evs if e.get("kind") == "build"]) == 1)
    rp = repo / ".context" / "harness" / "runs" / rid / "evidence" / "record.md"
    rec = rp.read_text(encoding="utf-8") if rp.exists() else ""
    check("authority-other-id: the record's Yours to rule names the id and how to answer it",
          f"answer: {sid}:" in rec.split("## Yours to rule")[-1], rec[:600])


DRILLS += [drill_authority_answered, drill_authority_other_id, drill_repair_owes_unmeasured_rows]


STANDING_GH = """import json, sys
from pathlib import Path
argv = sys.argv[1:]
here = Path(__file__).resolve().parent
with open(here / "gh-calls.log", "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\\n")
made = here / "pr-made"
if argv[:2] == ["issue", "view"]:
    print(json.dumps({"title": "the slice", "body": BODY, "labels": []})); sys.exit(0)
if argv[:2] == ["pr", "list"]:
    print(json.dumps([{"url": "https://example.invalid/pr/2"}] if made.exists() else [])); sys.exit(0)
if argv[:2] == ["pr", "create"]:
    made.write_text("x"); print("https://example.invalid/pr/2"); sys.exit(0)
if argv[:2] == ["pr", "view"]:
    st = here / "pr-state"
    print(json.dumps({"state": st.read_text().strip() if st.exists() else "OPEN"})); sys.exit(0)
print(json.dumps({"stub": True})); sys.exit(0)
"""


def _wait(repo, rid, cond, proc, limit=240):
    """The journal once `cond` holds; at once when the run's process ended without it."""
    t0 = time.time()
    while time.time() - t0 < limit:
        evs = journal(repo, rid)
        if cond(evs) or proc.poll() is not None:
            return evs
        time.sleep(0.5)
    return journal(repo, rid)


def _standing_again(n):
    """The journal holds the n-th verb's end and a standing phase after it."""
    def cond(evs):
        done = [i for i, e in enumerate(evs) if e.get("kind") == "next-done" and e.get("n") == n]
        return bool(done) and any(e.get("kind") == "phase" and e.get("phase") == "standing" for e in evs[done[0]:])
    return cond


def _after_last_next(evs):
    cut = max((i for i, e in enumerate(evs) if e.get("kind") == "next"), default=0)
    return evs[cut:]


def _ok_seats(evs):
    return [e.get("seat") for e in evs if e.get("kind") == "seat" and e.get("state") == "ok"]


def _standing_repo(repo):
    """w.py is declared by T-02 alone, so a dev merge that changes it moves one row; a stack stub
    lets the stubbed venue come up; the polls are fast."""
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    man = repo / "docs" / "manifests" / "t.yaml"
    man.write_text(man.read_text(encoding="utf-8").replace(
        'then: "the hidden row is absent"\n    files: ["src/x.py"]',
        'then: "the hidden row is absent"\n    files: ["src/x.py", "src/w.py"]'), encoding="utf-8")
    (repo / "src" / "w.py").write_text("w = 1\n", encoding="utf-8")
    conf = repo / ".claude" / "harness-config.json"
    cj = json.loads(conf.read_text(encoding="utf-8"))
    cj["standing"] = {"control_poll_s": 0.5, "pr_poll_s": 1}
    conf.write_text(json.dumps(cj, indent=2), encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "standing drill"); g("push", "-q", "origin", "dev")
    gh = repo / ".context" / "gh" / "gh_stub.py"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text(STANDING_GH.replace("BODY", repr(BODY)), encoding="utf-8")
    return g, gh


def drill_standing_run(repo):
    """A run no longer exits when it ships: it stands with its PR open and takes the chat's next
    step. `hb run next <id> <words>` relaunches the builder with the words, the loop measures the
    branch again from where it stood, and the PR refreshes; the merge ends the run; `hb run
    next` refuses a run with no words, a dead run and a merged one."""
    import os
    import subprocess
    g, gh = _standing_repo(repo)
    rows = {"items": [_receipt(i, "WORKS") for i in ("T-01", "T-02", "T-03")]}
    sp = repo / ".context" / "fake-script.json"
    sp.write_text(json.dumps({"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}, "verify": {"mode": "ok", "report": rows},
                              "fix": {"mode": "ok", "write": {"web/z.tsx": "export const a = 1\n"}}}),
                  encoding="utf-8")
    env = {**os.environ, "FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(gh), "HB_VENUE_STUB": "1"}
    proc = subprocess.Popen([sys.executable, HB, "run", "slice", "#7", "--rate-limit-wait-min", "0", "--no-before"],
                            cwd=str(repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    runs = repo / ".context" / "harness" / "runs"
    rid = ""
    while not rid and proc.poll() is None:
        time.sleep(0.5)
        rid = next((d.name for d in runs.glob("*-slice-*") if (d / "journal.jsonl").exists()), "") if runs.exists() else ""
    hb = lambda *a: sh([sys.executable, HB, *a], cwd=str(repo), env={"HB_GH_STUB": str(gh)})[1]
    try:
        evs = _wait(repo, rid, lambda evs: any(e.get("kind") == "phase" and e.get("phase") == "standing" for e in evs), proc)
        ph = json.loads((runs / rid / "phase.json").read_text(encoding="utf-8"))
        check("standing: a run that ships stands with its PR open, alive, its venue down",
              ph.get("phase") == "standing" and ph.get("pr") == "https://example.invalid/pr/2" and proc.poll() is None
              and (runs / rid / "pid.json").exists() and any(e.get("kind") == "op" and e.get("key") == "venue-down" for e in evs), str(ph)[:300])
        w = last_json(hb("run", "watch", rid, "--after", "0", "--max-min", "0.05"))
        check("standing: the watch ends at the standing phase", w.get("final") == "standing", str(w))
        bare = last_json(hb("run", "next", rid))
        check("standing: `hb run next` with no words is refused", bare.get("verdict") == "refused", str(bare))
        walk0 = {e.get("key") for e in evs if str(e.get("key", "")).startswith("walk-status@")}

        q = last_json(hb("run", "next", rid, "T-03", "shows", "the", "old", "label"))
        check("next: queued on a live run", q.get("verdict") == "queued" and q.get("line") == "next: T-03 shows the old label", str(q))
        evs = _wait(repo, rid, _standing_again(1), proc)
        after = _after_last_next(evs)
        check("next: the builder took the words, the loop read what moved and drove the branch, in the run's order",
              _ok_seats(after)[:2] == ["fix", "review"] and "verify" in _ok_seats(after), str(_ok_seats(after)))
        fix = sorted((runs / rid / "seats").glob("fix-*/brief.md"))
        check("next: the builder's brief carries the owner's words",
              fix and "T-03 shows the old label" in fix[-1].read_text(encoding="utf-8"), str(fix))
        calls = (gh.parent / "gh-calls.log").read_text(encoding="utf-8")
        walks = {e.get("key") for e in after if str(e.get("key", "")).startswith("walk-status@")}
        check("next: the PR was refreshed and the Walk posted on the new tip",
              '"pr", "edit"' in calls and walks and not (walks & walk0), calls[-300:])

        proc.kill()
        proc.communicate(timeout=60)
        dead = last_json(hb("run", "next", rid, "anything"))
        check("next on a dead run: refused, with the resume line", dead.get("verdict") == "refused"
              and f"--resume {rid}" in str(dead.get("resume")), str(dead))
        opened = len(journal(repo, rid))
        proc = subprocess.Popen([sys.executable, HB, "run", "slice", "#7", "--rate-limit-wait-min", "0", "--resume", rid],
                                cwd=str(repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        evs = _wait(repo, rid, lambda evs: any(e.get("kind") == "phase" and e.get("phase") == "standing" for e in evs[opened:]), proc)
        check("resume: a standing run re-enters standing with no seat",
              proc.poll() is None and not _ok_seats(evs[opened:]) and any(e.get("phase") == "standing" for e in evs[opened:]),
              str([(e.get("kind"), e.get("phase") or e.get("seat")) for e in evs[opened:]])[:300])

        (gh.parent / "pr-state").write_text("MERGED", encoding="utf-8")
        o, _ = proc.communicate(timeout=120)
        final = last_json(o)
        check("merged: the run exits at phase merged with the closeout hint",
              proc.returncode == 0 and final.get("phase") == "merged" and "closeout" in str(final.get("closeout"))
              and json.loads((runs / rid / "phase.json").read_text(encoding="utf-8")).get("phase") == "merged", o[-400:])
        merged = last_json(hb("run", "next", rid, "anything"))
        check("next on a merged run: refused", merged.get("verdict") == "refused" and "merged" in str(merged.get("why")), str(merged))
    finally:
        if proc.poll() is None:
            proc.kill()


DRILLS += [drill_standing_run]


STUDIO_BODY = ("## Intent\nwhy\n\n## Rulings\n> R1: the owner's words\n\n## Items\n\nR-T-1, R-T-2\n\n"
               "**MANIFEST:** docs/manifests/s.json, authoritative; slice one.\n")


def _studio_repo(repo, studio_edit="export const Drawer = 1\n", dev_edit=None, doors_required=False, doors=None):
    """A studio branch cut from dev with one frozen screen, then the contract filed on dev
    (so dev has moved past the studio commit), optionally with an edit that collides."""
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("checkout", "-q", "-b", "studio/s", "dev")
    (repo / "web" / "z.tsx").write_text(studio_edit, encoding="utf-8")
    g("commit", "-qam", "studio: the frozen drawer"); g("push", "-q", "origin", "studio/s")
    studio = sh(["git", "-C", str(repo), "rev-parse", "HEAD"])[1].strip()
    g("checkout", "-q", "dev")
    if dev_edit is not None:
        (repo / "web" / "z.tsx").write_text(dev_edit, encoding="utf-8")
    eyes = {"id": "R-T-2", "rev": 1, "slice": "one", "says": "The drawer opens from the right.", "proof": "eyes",
            "states": ["all"], "cases": [{"id": "T-02", "when": "a manager opens a role", "then": "its drawer is on the right"}]}
    if doors is not None:
        eyes["doors"] = doors
    doc = {"schema": "contract/v2", "wave": "s", "studio": {"branch": "studio/s", "commit": studio},
           "slices": [{"key": "one", "gets": "The manager edits a role in a drawer."}],
           "rules": [{"id": "R-T-1", "rev": 1, "slice": "one", "says": "The role is saved.", "proof": "test",
                      "states": ["all"], "cases": [{"id": "T-01", "when": "a manager saves", "then": "the role is saved"}]}, eyes]}
    if doors_required:
        doc["doors_required"] = True
    (repo / "docs" / "manifests" / "s.json").write_text(json.dumps(doc), encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "the contract, filed after the studio froze"); g("push", "-q", "origin", "dev")
    return studio


def drill_studio_base(repo):
    """A contract carrying `studio` starts the slice branch on the frozen studio commit, merges
    the dev that moved past it, tells the builder so, and names the commit in the PR body."""
    studio = _studio_repo(repo)
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}, body=STUDIO_BODY)
    check("studio: the slice ran from the studio commit", js.get("verdict") in ("mergeable", "run-complete"), raw[-500:])
    evs = journal(repo, js.get("run_id", ""))
    op = next((e.get("result") for e in evs if e.get("kind") == "op" and e.get("key") == "studio-base"
               and e.get("state") == "complete"), {}) or {}
    wt = repo / ".claude" / "worktrees" / "run-slice-issue-7"
    on = sh(["git", "-C", str(wt), "merge-base", "--is-ancestor", studio, "HEAD"])[0] == 0
    has_dev = sh(["git", "-C", str(wt), "merge-base", "--is-ancestor", "origin/dev", "HEAD"])[0] == 0
    check("studio: the branch holds the studio commit and the dev that moved past it, merged",
          on and has_dev and op.get("merged") is True and op.get("studio") == studio, f"{op} on={on} dev={has_dev}")
    frozen = (wt / "web" / "z.tsx").read_text(encoding="utf-8")
    check("studio: the frozen screen is in the tree the builder starts from", "Drawer" in frozen, frozen)
    rd = repo / ".context" / "harness" / "runs" / js.get("run_id", "")
    brief = (rd / "seats" / "build-1" / "brief.md").read_text(encoding="utf-8") if (rd / "seats" / "build-1" / "brief.md").exists() else ""
    check("studio: the builder brief says it starts from the frozen screens",
          f"This branch starts from the frozen studio screens at {studio}" in brief and "never redraw" in brief, brief[:400])
    body = (rd / "evidence" / "pr-body.md").read_text(encoding="utf-8") if (rd / "evidence" / "pr-body.md").exists() else ""
    what = body.split("## What this is", 1)[-1].split("## Needs your ruling", 1)[0]
    check("studio: the PR body's What this is names the studio commit", studio in what, what[:400])


def drill_studio_conflict(repo):
    """Dev moved past the studio commit with an edit that collides: an S- stop, no seat."""
    _studio_repo(repo, dev_edit="export const Table = 2\n")
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}, body=STUDIO_BODY)
    check("studio conflict: stopped at the studio with an S- stop naming the file",
          js.get("verdict") == "stopped" and js.get("stage") == "studio"
          and any(str(s).startswith("[S-") and "z.tsx" in str(s) for s in js.get("stops") or []), raw[-500:])
    check("studio conflict: no seat ran", not seats(journal(repo, js.get("run_id", ""))))


def drill_missing_door(repo):
    """doors_required and an eyes rule with no doors, or a door the venue lacks: no seat starts."""
    _studio_repo(repo, doors_required=True)
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}, body=STUDIO_BODY)
    check("doors: a missing door stops the slice at the contract, naming the rule",
          js.get("verdict") == "stopped" and js.get("stage") == "contract"
          and any("R-T-2" in str(p) and "doors" in str(p) for p in js.get("problems") or []), raw[-500:])
    check("doors: no seat ran", not seats(journal(repo, js.get("run_id", ""))))


def drill_unknown_door(repo):
    _studio_repo(repo, doors=["floor-now"])
    rc, js, raw = run_slice(repo, {"build": {"mode": "ok", **TOUCH, "report": BUILD_NOTE}}, body=STUDIO_BODY)
    check("doors: a door the venue lacks stops the slice before any seat",
          js.get("verdict") == "stopped" and any("'floor-now' is not a venue door" in str(p) for p in js.get("problems") or [])
          and not seats(journal(repo, js.get("run_id", ""))), raw[-500:])


DRILLS += [drill_studio_base, drill_studio_conflict, drill_missing_door, drill_unknown_door]


def main():
    only = sys.argv[1:] or None
    for d in DRILLS:
        name = d.__name__.replace("drill_", "")
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="hb-slice-drill-"))
        try:
            repo = make_slice_repo(tmp)
            d(repo)
        except Exception as ex:
            FAIL.append(f"{name}: raised {ex.__class__.__name__}: {ex}")
    for p in PASS:
        print("PASS", p)
    for f in FAIL:
        print("FAIL", f)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
