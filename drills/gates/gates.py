#!/usr/bin/env python
"""Drill for the gates' config seams: every product fact a gate needs is read from the
product's config, and an absent key means the engine adds nothing. The preflight adjusts,
notes or refuses a gate; silence is a pass only for a configured tool; a product signature
names a red's cause and a busy lease; a gate's outcome line is its product line; CI's scripts
are read by the configured prefixes; every-tip, fans_out and isolate shape a round; a venue
without `database` leases nothing; the size tripwire skips the configured globs. Run:

    python drills/gates/gates.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "drills"))
from fixture.common import FAIL, PASS, check, make_repo, sh  # noqa: E402
from hb.gates import ciparity, gates, gating, verdict  # noqa: E402

PREFLIGHT = """import json, sys
ask = json.loads(sys.stdin.read())
if "refuse-me" in ask["argv"]:
    print(json.dumps({"refuse": "a full suite with no parallelism"}))
else:
    print(json.dumps({"argv": ask["argv"] + ["--from-preflight"], "env": {"DRILL_DB": "leased"},
                      "notes": ["DB_SOURCE=drill (" + ask["gate"] + ")"]}))
"""
ECHO = "import os, sys; print(os.environ.get('DRILL_DB'), ' '.join(sys.argv[1:]))"


# the interpreter the drill runs under, forward slashes as a command string is written
PY = sys.executable.replace(chr(92), '/')

def _config(repo, **gates_block):
    path = repo / ".claude" / "harness-config.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    c["gates"] = gates_block
    path.write_text(json.dumps(c, indent=2), encoding="utf-8")


def _run_gate(argv, gate_name="drill-gate"):
    """(exit code, stdout, artifact text) of one in-process run-gate."""
    saved_argv = sys.argv
    sys.argv = ["hb", "run-gate", "--issue", "drill", "--", *argv]
    os.environ[gates.GATE_NAME_ENV] = gate_name
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            gates.cmd_run_gate(SimpleNamespace(gateargs=["--", *argv], issue="drill", timeout_min=5, tz=None))
        code = 0
    except SystemExit as ex:
        code = ex.code
    finally:
        sys.argv = saved_argv
        os.environ.pop(gates.GATE_NAME_ENV, None)
    text = buf.getvalue()
    art = next((l.split("=", 1)[1] for l in text.splitlines() if l.startswith("GATE_OUTPUT=")), None)
    return code, text, Path(art).read_text(encoding="utf-8") if art else ""


def drill_preflight(repo):
    _config(repo)
    code, text, _ = _run_gate(["python", "-c", ECHO, "as-given"])
    check("preflight: none configured, the gate runs as given", code == 0 and "None as-given" in text, text[-300:])
    (repo / "preflight.py").write_text(PREFLIGHT, encoding="utf-8")
    _config(repo, preflight=f"{PY} {(repo / 'preflight.py').as_posix()}")
    code, text, art = _run_gate(["python", "-c", ECHO, "adjusted"], gate_name="unit")
    check("preflight: its argv and env are what runs", code == 0 and "leased adjusted --from-preflight" in text, text[-300:])
    check("preflight: its notes and the gate's name are in the artifact", "DB_SOURCE=drill (unit)" in art, art[:300])
    code, text, _ = _run_gate(["python", "-c", ECHO, "refuse-me"])
    check("preflight: a refusal refuses the gate with its reason",
          code == 2 and "refused-by-preflight" in text and "no parallelism" in text, text[-300:])
    _config(repo, preflight=f"{PY} -c \"print('no json here')\"")
    code, text, _ = _run_gate(["python", "-c", ECHO])
    check("preflight: one that answers no JSON refuses, never runs the gate unadjusted",
          code == 2 and "refused-preflight-silent" in text, text[-300:])


def drill_silence_is_a_pass_only_for_a_configured_tool(repo):
    _config(repo)
    code, _, art = _run_gate(["python", "-c", "pass"])
    check("silent: an empty capture is a refusal by default", code == 2 and "EMPTY CAPTURE" in art, art[:200])
    _config(repo, silent_ok_tools=["python"])
    code, _, art = _run_gate(["python", "-c", "pass"])
    check("silent: a configured tool's silence is its pass", code == 0 and "SILENT=true" in art, art[:200])


def _artifact(repo, name, text):
    p = repo / f"{name}.txt"
    p.write_text(text + "\nTRUE_EXIT=1\n", encoding="utf-8")
    return {"name": "suite", "cmd": ["runner"], "true_exit": 1, "artifact": str(p)}


def drill_signatures_come_from_config(repo):
    crash = _artifact(repo, "crash", "collecting ...\n[gw0] node down: worker crashed\n3 passed")
    _config(repo)
    v = verdict.classify(crash)
    check("signatures: with none configured, a red with a result is the product's", v["outcome"] == "BROKEN"
          and v["cause_class"] == "product", str(v))
    _config(repo, signatures=[{"pattern": r"^\[gw\d+\] node down", "cause": "worker-crash", "recovery": "room"},
                              {"pattern": "database system is starting up|accessed by other users", "cause": "db-busy",
                               "recovery": "lease"}])
    v = verdict.classify(crash)
    check("signatures: a configured pattern names the cause and its recovery",
          v["signature"] == "worker-crash" and v["cause_class"] == "environment" and v["recovery"] == verdict.ROOM, str(v))
    named = _artifact(repo, "named", "FAILED tests/test_node_down.py::test_gw0_node_down - assert 1\n1 failed")
    check("signatures: a test named for the fault is not the fault (the product anchors its pattern)",
          verdict.classify(named)["outcome"] == "BROKEN", str(verdict.classify(named)))
    check("signatures: a lease refusal a lease signature names is busy, another is not",
          verdict.lease_busy("the database system is starting up") and not verdict.lease_busy("the migrations failed"))
    memory = _artifact(repo, "memory", "E   MemoryError")
    check("signatures: the host's room stays the engine's own", verdict.classify(memory)["signature"] == "memory")
    cut = _artifact(repo, "cut", "collecting ... 40%")
    check("outcome: a gate whose outcome line is missing never measured",
          verdict.classify(dict(cut, outcome=r"\d+ (passed|failed)"))["outcome"] == "UNMEASURED"
          and verdict.classify(cut)["outcome"] == "BROKEN")


WORKFLOW = """on:
  pull_request:
jobs:
  lint:
    steps:
      - name: Words
        run: python tools/lint/words.py
      - name: Styles
        run: npm run lint:css
"""


def drill_ci_scripts_by_configured_prefix(repo):
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    check("ci-parity: no prefix configured, no step is read", ciparity.derive(repo, "dev")["gates"] == [])
    got = sorted(g["name"] for g in ciparity.derive(repo, "dev", ciparity.prefixes({"ci": {"script_prefixes": ["tools/", "npm run"]}}))["gates"])
    check("ci-parity: a path prefix and a command head each name their script",
          got == ["ci: lint:css", "ci: tools/lint/words.py"], str(got))


def drill_a_round_is_shaped_by_the_gate_keys(repo):
    from hb.record.journal import Journal
    from hb.runner import hbcall
    c = {"state_dir": ".context/harness", "gates": {"commands": [{"name": "unit", "cmd": "python -m unit {workers}", "at": "every-tip", "fans_out": True,
                                 "isolate": "python -m unit --only {id}"}]}}
    listed = gating.default_gates([], c)
    check("every-tip: the gate is heavy, its string command an argv",
          listed[0].get("heavy") is True and listed[0]["cmd"] == ["python", "-m", "unit", "{workers}"], str(listed))
    check("isolate: each red id is its own argument",
          gating.isolate_argv("python -m unit --only {id}", ["a::x", "b::y"]) == ["python", "-m", "unit", "--only", "a::x", "b::y"])
    calls = []
    answers = iter(["unit red\nRED_NODEID=a::x\nTRUE_EXIT=1\n", "alone green\nTRUE_EXIT=0\n"])

    def fake_hb(args, cwd, timeout=1800, env=None):
        calls.append((args[args.index("--") + 1:], dict(env or {})))
        return 0, {}, next(answers)
    saved = hbcall.hb
    hbcall.hb = fake_hb
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        j = Journal("drill-gate-keys", c={"state_dir": str(repo / ".context" / "harness")})
        rows = gating.measure(j, str(repo), "drill", "base", [], c, "gates-full", heavy=True,
                              only=gating.default_gates([], c))
    finally:
        hbcall.hb = saved
        os.chdir(here)
    first = calls[0][0] if calls else []
    check("fans_out: the admitted worker count fills {workers}", first[-1].isdigit(), str(first))
    check("fans_out: the gate's name reaches run-gate", calls and calls[0][1].get(gates.GATE_NAME_ENV) == "unit", str(calls[:1]))
    check("isolate: a red that passes alone is order-dependent, never green, through the isolate command",
          len(calls) == 2 and calls[1][0] == ["python", "-m", "unit", "--only", "a::x"]
          and str(rows[0].get("unmeasured")).startswith("order-dependent"), str((calls, rows)))


def drill_no_database_capability_leases_nothing(repo):
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        got = gating.test_db(str(repo), "lease", "drill")
    finally:
        os.chdir(here)
    check("database: a product with no venue is not offered a lease", got == {"why": "not offered: database"}, str(got))


def drill_size_hold_skips_the_configured_globs(repo):
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    base = g("rev-parse", "HEAD")[1].strip()
    (repo / "gen").mkdir(exist_ok=True)
    (repo / "gen" / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(700)), encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "a generated file")
    path = repo / ".claude" / "harness-config.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        held = gates.size_hold(base, cwd=repo)
        c["review_gate"] = {"size_hold": {"exclude_globs": ["gen/*"]}}
        path.write_text(json.dumps(c), encoding="utf-8")
        skipped = gates.size_hold(base, cwd=repo)
    finally:
        os.chdir(here)
    check("size hold: a file past the limit holds", held["hold"] is True, str(held))
    check("size hold: a configured glob is skipped", skipped["hold"] is False and skipped["largest_file"] is None, str(skipped))


DRILLS = [drill_preflight, drill_silence_is_a_pass_only_for_a_configured_tool, drill_signatures_come_from_config,
          drill_ci_scripts_by_configured_prefix, drill_a_round_is_shaped_by_the_gate_keys,
          drill_no_database_capability_leases_nothing, drill_size_hold_skips_the_configured_globs]


def main():
    only = sys.argv[1:] or None
    for d in DRILLS:
        name = d.__name__.replace("drill_", "")
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="hb-gates-drill-"))
        here = os.getcwd()
        try:
            repo = make_repo(tmp)
            os.chdir(str(repo))
            d(repo)
        except Exception as ex:  # noqa: BLE001 - a raise is a named failure of the drill
            FAIL.append(f"{name}: raised {ex.__class__.__name__}: {ex}")
        finally:
            os.chdir(here)
            shutil.rmtree(tmp, ignore_errors=True)
    for p in PASS:
        print("PASS", p)
    for f in FAIL:
        print("FAIL", f)
    print("drill G-kill: a tree kill takes the child and its workers, never the caller")
    import subprocess, time
    from hb.gates.gates import _kill_tree
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _kill_tree(child.pid)
    deadline = time.time() + 10
    while child.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    check("kill: the child in our own group is gone and this drill is still running", child.poll() is not None)
    own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=(os.name != "nt"))
    _kill_tree(own.pid)
    deadline = time.time() + 10
    while own.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    check("kill: a child in its own group is gone too", own.poll() is not None)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={0 if not FAIL else 1}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
