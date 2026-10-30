#!/usr/bin/env python
"""Drill of the loop's stopping rule (ruling 18): what a finding costs is decided once in policy, and merge
readiness is five facts computed from receipts. Pins: a sub-P1 finding rides after the goal is met and a
P1 never does; a finding quoted outside the round's paths rides unless it is a P1; a nonwaivable one still
stops; the score counts the five facts and the finish state is mergeable only at five of five."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from hb.contract import policy  # noqa: E402
from hb.record import firstscreen  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  ({str(detail)[:300]})"))


def finding(fid, severity="P1", file="src/x.py", cls="broken", status="open"):
    return {"id": fid, "severity": severity, "class": cls, "status": status, "title": fid,
            "summary": f"{fid} wrong", "quote": {"file": file, "line": 1, "text": "x"}}


def main():
    print("drill S1 - what a finding costs is decided once")
    fs = [finding("A", "P1"), finding("B", "P2"), finding("C", "P2", cls="nonwaivable"), finding("D", "P1", status="fixed")]
    fix, stop, ride = policy.route_findings(fs, {}, goal_met=False)
    check("before the goal is met every open finding is the builder's, the nonwaivable one a stop",
          [f["id"] for f in fix] == ["A", "B"] and [f["id"] for f in stop] == ["C"] and ride == [], (fix, stop, ride))
    fix, stop, ride = policy.route_findings(fs, {}, goal_met=True)
    check("after the goal is met a P2 rides, a P1 is still fixed, the nonwaivable one still stops",
          [f["id"] for f in fix] == ["A"] and [f["id"] for f in stop] == ["C"] and [f["id"] for f in ride] == ["B"]
          and "below P1" in ride[0]["rides"], (fix, stop, ride))
    fix, stop, ride = policy.route_findings([finding("E", "P2", file="web/far.tsx"), finding("F", "P1", file="web/far.tsx")],
                                            {}, goal_met=False, scope={"src/x.py"})
    check("a sub-P1 finding quoted outside the round's paths rides; a P1 there is fixed",
          [f["id"] for f in fix] == ["F"] and [f["id"] for f in ride] == ["E"] and "outside" in ride[0]["rides"], (fix, ride))
    fix, stop, ride = policy.route_findings([finding("C", "P2", cls="nonwaivable")], {"c": "waived, ship it"}, goal_met=True)
    check("an answered nonwaivable finding carries its answer to the builder, whatever the goal says",
          [f["id"] for f in fix] == ["C"] and fix[0].get("answer") == "waived, ship it" and not stop, (fix, stop))
    check("the goal is met only when the review says so", policy.goal_met({"goal_verdict": {"met": True}})
          and not policy.goal_met({"goal_verdict": {"met": False}}) and not policy.goal_met(None))

    print("drill S2 - merge readiness is five facts from receipts")
    items = [{"item": "T-01", "kind": "screen"}]
    works = [{"item": "T-01", "state": "WORKS"}]
    review = {"goal_verdict": {"met": True, "misses": []}, "findings": [finding("B", "P2")]}
    green = [{"name": "lint", "true_exit": 0}]
    sc = firstscreen.score(works, items, review, green, (), (), "published", True)
    check("five of five on a green run, with the riding finding named in the goal fact's receipt",
          sc["n"] == 5 and sc["of"] == 5 and "riding for the owner: B" in sc["facts"][0]["receipt"], sc)
    sc = firstscreen.score(works, items, dict(review, findings=[finding("A", "P1")]), green, (), (), "published", True)
    check("an open P1 makes the goal fact false and names it", sc["n"] == 4 and not sc["facts"][0]["ok"] and "P1 open: A" in sc["facts"][0]["receipt"], sc)
    sc = firstscreen.score(works, items, review, [{"name": "suite", "true_exit": 1, "red_ids": ["t::x"]}], (), (), "published", True)
    check("a red gate makes the gate fact false and names the gate", sc["n"] == 4 and not sc["facts"][1]["ok"] and "suite" in sc["facts"][1]["receipt"], sc)
    sc = firstscreen.score([{"item": "T-01", "state": "BROKEN"}], items, review, green, (), (), "published", True)
    check("a red row makes the row fact false", sc["n"] == 4 and not sc["facts"][2]["ok"] and "1 item red" in sc["facts"][2]["receipt"], sc)
    sc = firstscreen.score(works, items, review, green, (), ["[S-1] a stop"], "published", True)
    check("an open stop makes the owner fact false", sc["n"] == 4 and not sc["facts"][3]["ok"], sc)
    sc = firstscreen.score(works, items, review, green, (), (), "unpublished", True)
    check("an unpublished record makes the record fact false", sc["n"] == 4 and not sc["facts"][4]["ok"] and sc["facts"][4]["receipt"] == "record not published", sc)

    print("drill S3 - the finish state is mergeable only at five of five, and says the false fact")
    fs5 = firstscreen.finish_state([], works, items, "published", gates=green, review=review)
    check("five of five is mergeable, with the score carried", fs5["merge"] == "eligible" and fs5["score"]["n"] == 5 and fs5["why"] == [], fs5)
    fs4 = firstscreen.finish_state([], works, items, "published", gates=green, review=dict(review, findings=[finding("A", "P1")]))
    check("an open P1 is not mergeable and the why names it", fs4["merge"] == "not-eligible" and fs4["why"] == ["P1 open: A"], fs4)
    fs3 = firstscreen.finish_state([], works, items, "unpublished", gates=green, review=review)
    check("the old words hold: an unpublished record says so, and only so", fs3["why"] == ["record not published"], fs3)
    text = "\n".join(firstscreen.markdown(firstscreen.build({"items": items, "goal": "g"}, works, {}, review, green)))
    check("the first screen prints the score and its five lines",
          "Merge readiness 5/5" in text and text.count("- yes:") == 5, text[:600])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
