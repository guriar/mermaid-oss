#!/usr/bin/env python
"""Drill for what a run tells the owner it changed: the build seats' `for_user`,
`underneath` and `changelog` lines survive the join into the PR's first block, and the
changelog lines land once under one `## Unreleased:` section, newest first. Run:

    python engine/hb.py rig drill --here --only record/manager_notes.py

Throwaway folder per run; never touches real state.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))

import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hb.record import firstscreen
from hb.record import manager_notes

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail[:300]})" if detail and not cond else ""))


notes = [
    {"for_user": ["Search reads the dates you mean."], "underneath": ["Readers take two dates."],
     "changelog": "Search reads every report by exact dates."},
    {"for_user": ["Search reads the dates you mean.", "Long lists come a page at a time."],
     "underneath": ["Risky: pages of 25."], "changelog": "Long lists come a page at a time."},
]
told = manager_notes.join(notes)
check("join keeps every plain sentence once, in order",
      told["for_user"] == ["Search reads the dates you mean.", "Long lists come a page at a time."], str(told))
check("join keeps both changelog lines", len(told["changelog_lines"]) == 2, str(told))

fs = firstscreen.build({"items": []}, [], told, None, [], headline="the title")
check("the PR's first block carries the sentences, not only the title",
      fs["for_user"] == told["for_user"] and fs["underneath"] == told["underneath"], str(fs["for_user"]))

with tempfile.TemporaryDirectory() as tmp:
    cl = Path(tmp) / "CHANGELOG.md"
    cl.write_text("# Changelog\n\nIntro.\n\n## Unreleased: hotfixes\n\n- An older line.\n", encoding="utf-8")
    contract = {"goal": "slice: typed ground, one prompt rail, one refusal shape"}
    first = manager_notes.write_changelog(tmp, contract, told["changelog_lines"], "CHANGELOG.md")
    text = cl.read_text(encoding="utf-8")
    check("a section is written above the older ones",
          text.index("## Unreleased: Typed ground") < text.index("## Unreleased: hotfixes"), text)
    check("both lines land under it", all(f"- {x}" in text for x in first) and len(first) == 2, text)
    again = manager_notes.write_changelog(tmp, contract, told["changelog_lines"] + ["A third line."], "CHANGELOG.md")
    text2 = cl.read_text(encoding="utf-8")
    check("a line already there is not written twice", again == ["A third line."], str(again))
    check("one section, not two", text2.count("## Unreleased: Typed ground") == 1, text2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
print(f"TRUE_EXIT={1 if FAIL else 0}")
sys.exit(1 if FAIL else 0)
