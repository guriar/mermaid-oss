#!/usr/bin/env python
"""Drill for the PR's first screen numbers (Q6, Q9): a fake journal with seats of every role,
helpers and skills, an audit's disagreements, the owner's touches and battery spend prints
every field in the ruled order; a run with no battery spend reads unmeasured. Run:

    python engine/drill_prbody.py

Throwaway git repo per run; never touches real state.
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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hb.record import firstscreen
from hb.record import prbody
from hb.runner import usage

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail[:400]})" if detail and not cond else ""))


class FakeJournal:
    def __init__(self, evs, facts=None):
        self._evs, self._facts = evs, facts or {}

    def events(self):
        return self._evs

    def fact(self, key, default=None):
        return self._facts.get(key, default)


def seat(label, minutes, ts, **usage_):
    return {"kind": "seat", "seat": label, "attempt": 1, "state": "ok", "minutes": minutes, "ts": ts, "usage": usage_}


def tool_use(uid, name, **inp):
    return {"type": "tool_use", "id": uid, "name": name, "input": inp}


def stream_with_skills(path):
    """A builder's stream: two skills it invoked (one twice), one helper with its totals, and
    a skill the helper invoked, which is the helper's and not the seat's."""
    lines = [
        {"type": "assistant", "message": {"content": [tool_use("s1", "Skill", skill="diagnosing-bugs")]}},
        {"type": "assistant", "message": {"content": [tool_use("a1", "Agent", prompt="drive")]}},
        {"type": "assistant", "parent_tool_use_id": "a1",
         "message": {"content": [tool_use("s9", "Skill", skill="a-helpers-own")], "usage": {"output_tokens": 5}}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a1"}]},
         "tool_use_result": {"totalTokens": 900, "totalDurationMs": 120000}},
        {"type": "assistant", "message": {"content": [tool_use("s2", "Skill", skill="ponytail")]}},
        {"type": "assistant", "message": {"content": [tool_use("s3", "Skill", skill="diagnosing-bugs")]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def git_repo(tmp):
    """A branch with one commit past its base by each of a seat, the runner and the session."""
    repo = tmp / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@d", "GIT_COMMITTER_EMAIL": "d@d"}

    def commit(message, who="d"):
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=repo, capture_output=True,
                       env={**env, "GIT_COMMITTER_NAME": who})

    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    commit("base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    commit("an item", who="hb seat")
    commit("ledger rows", who="hb")
    commit("a hand fix", who="the session")
    return repo, base


def drill_usage(tmp):
    stream = tmp / "stdout.txt"
    stream_with_skills(stream)
    helped = usage.helpers(stream)
    check("usage: a seat's skills are read off its stream, once each, in order, never a helper's",
          bool(helped) and helped["skills_invoked"] == ["diagnosing-bugs", "ponytail"], str(helped))
    check("usage: the helper count, tokens and minutes stand beside the skills",
          bool(helped) and helped["calls"] == 1 and helped["minutes"] == 2.0 and helped["tokens"] == 900, str(helped))
    only_skill = tmp / "skill-only.txt"
    only_skill.write_text(json.dumps({"type": "assistant", "message": {"content": [
        tool_use("s1", "Skill", skill="code-review")]}}) + "\n", encoding="utf-8")
    one = usage.helpers(only_skill)
    check("usage: a seat that invoked a skill and spawned no helper is still recorded",
          bool(one) and one["calls"] == 0 and one["skills_invoked"] == ["code-review"], str(one))
    return helped


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-prbody-drill-"))
    helped = drill_usage(tmp)

    repo, base = git_repo(tmp)
    counted = prbody.session_commits(repo, base)
    check("human: only commits neither a seat nor the runner made count", counted == 1, str(counted))
    check("human: a branch that cannot be read is unmeasured, never zero", prbody.session_commits(None, base) is None)

    evs = [{"kind": "phase", "phase": "opened", "ts": "2026-01-01T00:00:00+00:00"},
           seat("build", 300, "2026-01-01T05:00:00+00:00", peak_context_tokens=410000, helpers=dict(helped, minutes=30.0)),
           seat("review", 60, "2026-01-01T06:00:00+00:00", helpers={"calls": 0, "skills_invoked": ["code-review"]}),
           seat("codex", 30, "2026-01-01T06:10:00+00:00"),
           seat("discovery", 45, "2026-01-01T07:00:00+00:00"),
           seat("fix", 60, "2026-01-01T08:00:00+00:00"),
           seat("audit", 90, "2026-01-01T09:30:00+00:00"),
           seat("merge-12", 12, "2026-01-01T09:40:00+00:00"),
           {"kind": "control", "verb": "answer", "text": "A-1: yes", "ts": "2026-01-01T09:41:00+00:00"},
           {"kind": "phase", "phase": "opened", "ts": "2026-01-01T10:00:00+00:00"},
           {"kind": "audit-disagreements", "false_greens": 1, "false_reds": 0, "cases": 42,
            "ts": "2026-01-01T10:30:00+00:00"},
           {"kind": "phase", "phase": "standing", "ts": "2026-01-01T11:00:00+00:00"}]
    al = prbody.journal_allowance(FakeJournal(evs, {"worktree": str(repo), "base_sha": base}))
    text = "\n".join(prbody.numbers_lines(al))
    print("\n" + text)
    want = ["Seat hours: build 6.0 h, review 1.5 h, discovery 0.8 h, audit 1.5 h, merge 0.2 h",
            "Helper hours: 0.5 h", "Skills invoked: build: diagnosing-bugs, ponytail; review: code-review",
            "Peak context: 410k tokens", "False greens: 1 (check green, audit red); false reds 0; 42 cases compared",
            "Human: 1 answers, 1 resumes, 1 session commits", "Battery spend: unmeasured", "Wall clock: 10.7 h"]
    at = [text.find(w) for w in want]
    check("first screen: every field prints, in the ruled order",
          all(x >= 0 for x in at) and at == sorted(at), str(list(zip(want, at))))
    check("first screen: no fix and no verify line; a fix round is the builder's",
          " fix " not in text and "verify" not in text, text)
    check("first screen: a run with no battery spend reads unmeasured", "Battery spend: unmeasured" in text)

    spent = evs + [{"kind": "battery-spend", "usd": 1.25}, {"kind": "battery-spend", "usd": 0.5}]
    text2 = "\n".join(prbody.numbers_lines(prbody.journal_allowance(FakeJournal(spent))))
    check("first screen: battery spend sums its events", "Battery spend: $1.75" in text2, text2)
    bare = "\n".join(prbody.numbers_lines(prbody.journal_allowance(FakeJournal(evs[:2]))))
    check("first screen: no audit and no branch read unmeasured by name",
          "False greens: unmeasured" in bare and "session commits unmeasured" in text2, bare + text2)

    audited = prbody.journal_allowance(FakeJournal(evs + [{"kind": "audit-disagreements", "false_greens": 0, "false_reds": 0,
                                                            "cases": 3, "excluded": [], "disagreed": {"false_greens": [], "false_reds": []}}]))
    with_audit = "\n".join(firstscreen.markdown(firstscreen.build({"items": []}, [], {}, None, [], audit=audited["audit"])))
    without = "\n".join(firstscreen.markdown(firstscreen.build({"items": []}, [], {}, None, [], audit=None)))
    check("first screen: a run with no audit prints 'no audit: unmeasured', never zero",
          "no audit: unmeasured" in without and "no audit: unmeasured" not in with_audit and "drove: 3" in with_audit,
          without + with_audit)

    print(f"\n{'=' * 52}\n  {len(PASS)} passed · {len(FAIL)} failed"
          + (f"  -> {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'=' * 52}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
