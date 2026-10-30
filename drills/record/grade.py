#!/usr/bin/env python
"""Drill for `hb run grade`: the three numbers come from the journal alone. A journal shaped
like a slice that hit the context ceiling, was relaunched, read by the second reader and
reviewed grades as 150 of 161 model minutes with two instruments and no crash; a journal that
ends without a final phase grades as one crash naming the stage and the traceback's last frame
from the launcher's log; a resume by hand and a harness move are hand touches with their minute.

    python drills/record/grade.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))

import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from hb.record import grade  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""))


T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def at(minute):
    return (T0 + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"run_id": "r1", "v": 2, **e}) for e in events) + "\n", encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-grade-drill-"))

    print("drill G1 - a relaunched build, a second reader and a review grade as 150 of 161 minutes")
    j = tmp / "runs" / "r1" / "journal.jsonl"
    write(j, [
        {"ts": at(0), "kind": "phase", "phase": "opened", "beat": "slice", "harness": "abc"},
        {"ts": at(6), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"},
        {"ts": at(127), "kind": "seat", "seat": "build", "attempt": 1, "state": "context", "minutes": 120.7, "why": "its context reached 601560 tokens"},
        {"ts": at(127), "kind": "relaunched", "seat": "build", "attempt": 1, "why": "context", "tokens": 601560},
        {"ts": at(127), "kind": "seat", "seat": "build", "attempt": 2, "state": "started"},
        {"ts": at(149), "kind": "seat", "seat": "build", "attempt": 2, "state": "ok", "minutes": 22.2},
        {"ts": at(149), "kind": "phase", "phase": "build", "tip": "t1"},
        {"ts": at(150), "kind": "seat", "seat": "codex", "attempt": 1, "state": "ok", "minutes": 0.5},
        {"ts": at(156), "kind": "seat", "seat": "review", "attempt": 1, "state": "ok", "minutes": 6.6},
        {"ts": at(156), "kind": "phase", "phase": "review", "round": 1, "findings": 5, "fix": 5},
        {"ts": at(161), "kind": "seat", "seat": "fix", "attempt": 1, "state": "started"},
    ])
    g = grade.read(j)
    check("model minutes are the seats' own minutes summed", g["model_min"] == 150.0, g["model_min"])
    check("wall minutes run from the first event to the last", g["wall_min"] == 161.0, g["wall_min"])
    check("the grade sums the minutes waited on admission and names the three largest waits by workload",
          isinstance(g.get("wait_min"), float) and isinstance(g.get("top_waits"), list) and len(g["top_waits"]) <= 3
          and all(set(w) == {"workload", "minutes"} for w in g["top_waits"]), str({k: g.get(k) for k in ("wait_min", "top_waits")}))
    names = [i["instrument"] for i in g["instruments"]]
    check("the context relaunch and the second reader are instruments that fired, with outcomes",
          any(n.startswith("build relaunched") for n in names) and "second reader" in names
          and any("attempt 2 ok in 22.2 min" in i["outcome"] for i in g["instruments"]), str(g["instruments"]))
    check("no crash while the run is still going", g["crashes"] == [] or g["final"] is None and len(g["crashes"]) == 1, str(g["crashes"]))
    text = grade.render(g)
    check("the rendering carries the three numbers", "model 150.0 of 161.0 wall minutes" in text and "instruments fired" in text
          and "hand touches: 0" in text, text)

    print("drill G2 - a journal that ends without a final phase grades as one crash with the last frame")
    j2 = tmp / "runs" / "20260101-120000-slice-9" / "journal.jsonl"
    write(j2, [
        {"ts": at(0), "kind": "phase", "phase": "opened", "beat": "slice", "harness": "abc", "ref": "#9", "wave": "w"},
        {"ts": at(6), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"},
        {"ts": at(108), "kind": "seat", "seat": "build", "attempt": 1, "state": "ok", "minutes": 102.6},
        {"ts": at(108), "kind": "phase", "phase": "build", "tip": "t1"},
        {"ts": at(116), "kind": "op", "key": "discovery-issue-9", "state": "started"},
        {"ts": at(137), "kind": "seat", "seat": "discovery", "attempt": 1, "state": "ok", "minutes": 21.3},
    ])
    log = tmp / "runs" / "20260101-115900-wave-w" / "slice-9.out"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('Traceback (most recent call last):\n  File "x/slice.py", line 421, in <lambda>\n    finds = run()\n'
                   '  File "x/battery.py", line 343, in propose_golden\n    held = {c for c in proposed.values()}\n'
                   "AttributeError: 'list' object has no attribute 'values'\n", encoding="utf-8")
    g2 = grade.read(j2, grade.launcher_log_for(j2.parent))
    c = (g2["crashes"] or [{}])[0]
    check("one crash, in the stage that was open, after the model minutes before it",
          len(g2["crashes"]) == 1 and c.get("stage") == "discovery-issue-9" and c.get("model_minutes_before") == 123.9, str(c))
    check("the traceback's last frame is named from the launcher's log",
          (c.get("traceback") or {}).get("in") == "propose_golden" and (c.get("traceback") or {}).get("line") == 343
          and "AttributeError" in (c.get("traceback") or {}).get("error", ""), str(c.get("traceback")))

    print("drill G3 - a resume by hand and a harness move are hand touches with their minute")
    j3 = tmp / "runs" / "r3" / "journal.jsonl"
    write(j3, [
        {"ts": at(0), "kind": "phase", "phase": "opened", "beat": "hotfix", "harness": "aaa"},
        {"ts": at(10), "kind": "seat", "seat": "build", "attempt": 1, "state": "ok", "minutes": 9.0},
        {"ts": at(40), "kind": "phase", "phase": "opened", "beat": "hotfix", "harness": "bbb"},
        {"ts": at(40), "kind": "harness-migrated", "opened_on": "aaa", "now_on": "bbb"},
        {"ts": at(50), "kind": "phase", "phase": "merged", "state": "mergeable"},
    ])
    g3 = grade.read(j3)
    touches = g3["hand_touches"]
    check("each hand touch is one line with its minute", len(touches) == 2 and touches[0]["minute"] == 40.0
          and touches[0]["touch"] == "resumed by hand" and "another harness version" in touches[1]["touch"], str(touches))
    check("a run that reached a final phase has no crash", g3["crashes"] == [] and g3["final"] == "merged", str(g3))

    print(f"\n{'=' * 52}\n  {len(PASS)} passed · {len(FAIL)} failed" + (f"  -> {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'=' * 52}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
