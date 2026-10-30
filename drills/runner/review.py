#!/usr/bin/env python
"""Drill for the second reader's Claude fallback (hb.run.reader). In process, on a throwaway
git repo, with the seat launches faked.

Run: python engine/drill_review.py
"""
import json
import subprocess
import sys
import tempfile
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

from hb.venue import machine  # noqa: E402
from hb.runner import adapters
from hb.runner import reader
from hb.record.journal import Journal
from hb.rig import common as common
from hb.record import journal as jm

UNITS = """def case_units(cases):
    return cases * 12


def bag_units(bags):
    return bags * 5


def keg_units(kegs):
    return kegs * 30
"""
PRICES = """def case_price(cases):
    return cases * 40


def bag_price(bags):
    return bags * 9
"""
ORDERS = """from units import case_units


def order_total(cases):
    return case_units(cases)
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def seed(repo):
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "drill@example.invalid")
    git(repo, "config", "user.name", "drill")
    for name, text in (("units.py", UNITS), ("prices.py", PRICES), ("orders.py", ORDERS)):
        (repo / name).write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return git(repo, "rev-parse", "HEAD")


def fake_launch(launched):
    def launch(name, role, brief_path, cwd, out_dir, model="", **kw):
        launched.append((name, model, Path(brief_path).read_text(encoding="utf-8")))
        if name == "codex":
            (out_dir / "stderr.txt").write_text("\nERROR: You've hit your usage limit. Try again at 3:00 AM.\nmore\n",
                                                encoding="utf-8")
            return {"exit_class": "rate-limited", "stderr": str(out_dir / "stderr.txt"), "usage": None}
        (out_dir / "stdout.txt").write_text(json.dumps({"type": "result", "result": "units.py:2: doubles the case"}) + "\n",
                                            encoding="utf-8")
        return {"exit_class": "ok", "usage": None}
    return launch


def drill_codex_usage_falls_to_claude(repo, base):
    launched = []
    state = repo / ".context" / "harness"
    conf = {"seats": {"codex": {"runtime": "codex", "model": "gpt"}, "review": {"runtime": "claude", "model": "opus-r"}},
            "state_dir": str(state)}
    saved = (adapters.probe, adapters.launch, adapters.cfg, reader.cfg, machine.cfg, machine.state_dir,
             common.state_dir, jm.state_dir)
    adapters.probe = lambda name, role=None: (True, "ok")
    adapters.launch = fake_launch(launched)
    adapters.cfg = reader.cfg = machine.cfg = lambda *a, **k: conf
    machine.state_dir = common.state_dir = jm.state_dir = lambda c=None: state
    try:
        j = Journal("drill-review-fallback")
        got = reader.codex_read(j, repo, base, {"rulings": "", "items": []}, repo, 1)
        events = j.events()
    finally:
        (adapters.probe, adapters.launch, adapters.cfg, reader.cfg, machine.cfg, machine.state_dir,
         common.state_dir, jm.state_dir) = saved
    check("second reader: a Codex usage error, then Claude on the review seat's model, same brief",
          [(n, m) for n, m, _ in launched] == [("codex", "gpt"), ("claude", "opus-r")]
          and launched[0][2] == launched[1][2], str([(n, m) for n, m, _ in launched]))
    check("second reader: the Claude read is handed on with no wait for the plan's reset", got.get("runtime") == "claude"
          and Path(got["path"]).read_text(encoding="utf-8") == "units.py:2: doubles the case"
          and not any(e.get("kind") == "wait" for e in events), f"{got}")
    words = [e.get("words") for e in events if e.get("kind") == "second-read" and e.get("state") == "fallback"]
    check("second reader: the fallback is journaled with the first error line",
          words == ["second-read fallback: claude why: ERROR: You've hit your usage limit. Try again at 3:00 AM."],
          str(words))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        drill_codex_usage_falls_to_claude(repo, seed(repo))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed" + (f": {FAIL}" if FAIL else ""))
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
