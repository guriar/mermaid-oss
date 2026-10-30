"""Shared drill helpers: a temp product repo with a fake runtime, the hb entry, PASS and FAIL,
and the one `check` every drill prints with. Lifted out of the runner drill so no drill
imports another (MO-04).
"""
import json
from types import SimpleNamespace
import os
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path
os.environ.setdefault("HB_MEMORY_FREE_GB", "64")  # the drills measure the engine, never this host's memory
os.environ.setdefault("HB_MEMORY_TOTAL_GB", "32")
os.environ.setdefault("HB_SETTLE_S", "0.2")
os.environ.setdefault("HB_SETTLE_MAX_S", "1")
os.environ.setdefault("HB_FORCE_AFTER_S", "1")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
HERE = ENGINE
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
HB = str(HERE / "hb.py")
PASS, FAIL = [], []


def sh(cmd, cwd=None, env=None, timeout=600):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=e, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def last_json(text):
    for line in reversed(text.splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {}


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop_pid(pid):
    import signal
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError, TypeError):
        pass


def make_repo(tmp):
    """A product repo with one commit on dev, pushed to a bare origin, plus the
    harness config the runner reads. Returns the checkout path."""
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    sh(["git", "init", "-q", "--bare", str(origin)])
    sh(["git", "init", "-q", "-b", "dev", str(repo)])
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    (repo / "apps" / "backend").mkdir(parents=True)
    (repo / "apps" / "backend" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "harness-config.json").write_text(json.dumps({
        "repo": "drill/drill", "state_dir": ".context/harness",
        "engine_dir": ".claude/harness", "commands_dir": ".claude/commands",
        "seats": {**{r: {"runtime": "fake"} for r in ("build", "review", "fix", "verify", "triage")},
                  "codex": {"runtime": "none"}},
        "gates": [{"name": "trivial", "cmd": ["python", "-c", "print('gate ok')"]},
                  {"name": "suite", "cmd": ["python", "-c", "print('suite ok')"], "heavy": True}],
        # every path the engine reads: the engine carries no default for a product fact
        "paths": {"manifest_dir": "docs/manifests", "ledger_file": "docs/ledger/delivery.jsonl",
                  "decisions_dir": "docs/decisions", "design_dir": "design", "checks": "checks/",
                  "hotfix_dir": ".context/hotfix", "worktrees_dir": ".claude/worktrees", "sittings_dir": ".context/sittings", "changelog_file": "CHANGELOG.md",
                  "release_doc": "docs/release.md", "slicing_doc": "docs/slicing.md", "studio_doc": "docs/studio.md"},
        "branches": {"landing": "dev", "promotion_base": "main", "integration_prefix": "wave/", "studio_prefix": "studio/",
                     "deletable_prefixes": ["slice/", "hotfix/", "fix/"]},
        "landing": {"default_mode": "pr", "branch_prefix": "hotfix/", "status_context": "Walk"},
        "queue": {"labels": {"wave": "wave:", "autonomy": "autonomy:"}},
        "pictures": {"viewports": [{"name": "desktop", "width": 1280, "height": 800}, {"name": "phone", "width": 375, "height": 812}]},
        "ci": {"gate_check": "CI Gate", "script_prefixes": []},
        "stops": {"extra": []},
        # a detached run starts the page when its port is quiet; never the owner's page
        "page": {"port": free_port()},
    }, indent=2), encoding="utf-8")
    (repo / ".gitignore").write_text(".context/\n.claude/worktrees/\n", encoding="utf-8")
    # a product checkout has an identity; a bare CI runner does not, and every merge the engine makes needs one
    g("config", "user.name", "drill"); g("config", "user.email", "drill@example.invalid")
    g("add", "-A"); g("commit", "-q", "-m", "init")
    g("remote", "add", "origin", str(origin))
    g("push", "-q", "-u", "origin", "dev")
    return repo


# the stub venue's one command: `capabilities <a,b>` answers that list, every other verb (a state
# check of any state among them) an empty receipt and exit 0
VENUE_STUB = ("import json, sys\n"
              "verb, args = sys.argv[1], sys.argv[2:]\n"
              "if verb == 'capabilities':\n    print(json.dumps(args[0].split(',') if args and args[0] else []))\n"
              "else:\n    print(json.dumps({}))\n")
# the product's merge-hazard check, stubbed: two marker files in one tree are two heads of one chain
HAZARDS_STUB = ("import sys\nfrom pathlib import Path\n"
                "marks = sorted(p.name for p in Path(sys.argv[1]).glob('src/hazard-*.txt'))\n"
                "if len(marks) > 1:\n    print('two heads: ' + ', '.join(marks))\n")
# the venue's states catalog (config `venue.states`), relative to the repo; the drill writes it
VENUE_STATES = "tools/venue-states.json"


def stub_venue(repo, capabilities=("ui", "model"), states=False, hazards=False, model_paths=None, commit=True):
    """Declare a venue on make_repo's config whose every command is the stub script (HB_VENUE_STUB
    stands the venue itself). The scripts live beside the repo, so they never enter its tree.
    `states` adds the state doors and the catalog, `hazards` the stub `gates.merge_hazards`,
    `model_paths` the config `model.paths`; `commit` commits and pushes the config, for a drill
    whose run cuts its worktree from origin before anything else commits."""
    stubs = repo.parent / "stubs"
    stubs.mkdir(parents=True, exist_ok=True)
    (stubs / "venue_stub.py").write_text(VENUE_STUB, encoding="utf-8")
    py = Path(sys.executable).as_posix()
    call = f"{py} {(stubs / 'venue_stub.py').as_posix()}"
    path = repo / ".claude" / "harness-config.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    verbs = ["up", "down", "list", "set_model", "say"] + (["state_build", "state_check", "state_undo"] if states else [])
    c["venue"] = {k: f"{call} {k}" for k in verbs}
    c["venue"]["capabilities"] = f"{call} capabilities {','.join(capabilities)}"
    c["venue"]["ui"] = {"driver": {"command": "drv", "args": ["{session}"]}}
    if states:
        c["venue"]["states"] = VENUE_STATES
    if model_paths:
        c["model"] = {"paths": list(model_paths)}
    if hazards:
        (stubs / "hazards.py").write_text(HAZARDS_STUB, encoding="utf-8")
        c["gates"] = {"commands": c["gates"], "merge_hazards": f"{py} {(stubs / 'hazards.py').as_posix()} {{worktree}}"}
    path.write_text(json.dumps(c, indent=2), encoding="utf-8")
    if commit:
        g = ["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d"]
        sh(g + ["commit", "-q", "-m", "drill config", "--", str(path)])
        sh(g + ["push", "-q", "origin", "dev"])
    return repo


def run_hotfix(repo, script, ref="the widget shows twice", extra=(), env_extra=None, hb=None):
    sp = repo / ".context" / "fake-script.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(script), encoding="utf-8")
    env = {"FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(FIXTURE / "gh_stub.py")}
    env.update(env_extra or {})
    rc, o, e = sh([sys.executable, hb or HB, "run", "hotfix", ref, "--shadow", "--no-venue",
                   "--rate-limit-wait-min", "0", *extra], cwd=str(repo), env=env)
    return rc, last_json(o), o + e


def run_dir(repo, run_id):
    d = repo / ".context" / "harness" / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def journal(repo, run_id):
    p = repo / ".context" / "harness" / "runs" / run_id / "journal.jsonl"
    if not run_id or not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def seats(evs, seat=None):
    return [e for e in evs if e.get("kind") == "seat" and (seat is None or e.get("seat") == seat)]


def _heavy_measured(j, repo):
    """A `pr-tip` at the repo's head, as gating.pr_tip journals it, so a drill of the PR's own
    mechanics opens a PR on a tip a heavy gate measured."""
    from hb.rig import gitops
    j.append("pr-tip", tip=gitops.head_of(repo), heavy=[{"name": "suite", "true_exit": 0, "unmeasured": None}], parity=[])


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{(': ' + str(detail)) if detail and not cond else ''}")


TOUCH = {"touch": ["src/x.py"]}
