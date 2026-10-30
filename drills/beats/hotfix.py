#!/usr/bin/env python
"""Drills for the hotfix's short audit (Q11) on the fake runtime and a stubbed stand-in.

    python engine/drill_hotfix.py

Drills:
  audit_red_relaunches_the_builder  a red row takes one fix pass, the red is driven again, and
                      the PR tip is measured after the fix
  an_audit_seat_that_crashes_measures_nothing  a crashed audit leaves its rows UNMEASURED
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
import shutil
import sys
import tempfile
from pathlib import Path

from fixture.common import HB, HERE, PASS, FAIL, check, journal, last_json, make_repo, seats, sh, stub_venue  # noqa: E402,F401

sys.path.insert(0, str(ROOT / "drills"))

TOUCH = {"touch": ["src/x.py"]}
WORKS = {"item": "HF-01", "state": "WORKS", "substate": None, "seen": "it worked", "observed": "it worked",
         "situation": "fresh venue", "capture": None, "tenant_id": "tenant-fake", "evidence": ["plan.md"]}
BROKEN = dict(WORKS, state="BROKEN", substate="failed", seen="it shows twice", observed="the widget shows twice")

# a seat's own script: reads the brief on stdin, writes what the drill asserts on beside the evidence
SEAT_SCRIPT = r'''
import re, sys
from pathlib import Path
brief = sys.stdin.read()
ev = Path(re.search(r"Evidence directory \(write scratch and captures here\): `([^`]+)`", brief).group(1))
side = sys.argv[1]
items = re.findall(r'"item": "([A-Z]+-\d+)"', brief.split("# Inputs", 1)[1].split("## Envelope", 1)[0])
(ev / side).mkdir(parents=True, exist_ok=True)
for item in dict.fromkeys(items):
    (ev / side / f"{item}-desktop.png").write_bytes(b"png")
with open(ev / f"{side}-asked.txt", "a", encoding="utf-8") as f:
    f.write(",".join(dict.fromkeys(items)) + "\n")
'''





def run(repo, script):
    seat_py = repo / ".context" / "seat.py"
    seat_py.parent.mkdir(parents=True, exist_ok=True)
    seat_py.write_text(SEAT_SCRIPT, encoding="utf-8")
    for role, side in (("build", "before"), ("verify", "after")):
        for key in [k for k in script if k.split(".")[0] == role]:
            script[key] = dict(script[key], exec=[str(seat_py), side])
    sp = repo / ".context" / "fake-script.json"
    sp.write_text(json.dumps(script), encoding="utf-8")
    rc, o, e = sh([sys.executable, HB, "run", "hotfix", "the widget shows twice", "--shadow", "--rate-limit-wait-min", "0"],
                  cwd=str(repo), env={"FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(FIXTURE / "gh_stub.py"),
                                      "HB_VENUE_STUB": "1"})
    js = last_json(o)
    evidence = repo / ".context" / "harness" / "runs" / str(js.get("run_id")) / "evidence"
    return js, journal(repo, js.get("run_id", "")), evidence, o + e


def drill_audit_red_relaunches_the_builder(repo):
    js, evs, ev, raw = run(repo, {"build": {"mode": "ok", **TOUCH}, "fix": {"mode": "ok", **TOUCH},
                                  "verify.1": {"mode": "ok", "report": {"items": [BROKEN]}},
                                  "verify.2": {"mode": "ok", "report": {"items": [WORKS]}}})
    fixes = [e for e in evs if e.get("kind") == "fix-round" and e.get("stage") == "audit"]
    check("audit red: one fix pass by the builder relaunched", len(fixes) == 1 and len(seats(evs, "fix")) >= 1,
          [e.get("seat") for e in seats(evs)])
    asked = (ev / "after-asked.txt").read_text(encoding="utf-8").split() if (ev / "after-asked.txt").exists() else []
    check("audit red: the red alone is driven again", asked == ["HF-01", "HF-01"], asked)
    check("audit red: the re-driven row stands WORKS", (js.get("receipts") or {}).get("WORKS") == 1, raw[-600:])
    # HS-06: the audit's fix moved the tip, and every instrument CI runs is measured again there
    fixed = next((e.get("tip_out") or e.get("tip") for e in reversed(evs) if e.get("kind") == "seat"
                  and e.get("seat") == "fix" and e.get("state") == "ok"), None)
    tips = [e for e in evs if e.get("kind") == "pr-tip"]
    last = tips[-1] if tips else {}
    check("audit red: the PR tip is measured after the audit's fix, the heavy gate's TRUE_EXIT row in the record",
          fixed and str(last.get("tip")).startswith(str(fixed)[:9]) and str(js.get("tip", "")).startswith(str(last.get("tip"))[:9])
          and [h.get("name") for h in last.get("heavy") or []] == ["suite"] and (last.get("heavy") or [{}])[0].get("true_exit") == 0,
          str(last) + str(fixed))
    full = [e.get("key") for e in evs if e.get("kind") == "op" and str(e.get("key", "")).startswith("gates-full@")
            and e.get("state") == "complete"]
    check("audit red: the heavy gate ran under the fixed tip's own key", any(str(last.get("tip"))[:9] in k for k in full), str(full))
    rec = (ev / "record.md").read_text(encoding="utf-8") if (ev / "record.md").exists() else ""
    check("audit red: the record carries the heavy gate's TRUE_EXIT line", "- suite: TRUE_EXIT=0" in rec, rec[-600:])


def drill_an_audit_seat_that_crashes_measures_nothing(repo):
    """The audit reads its seat's report; a seat that crashes, respawned once and crashing again,
    left no report, and its items read UNMEASURED by name, never the builder's WORKS."""
    js, evs, ev, raw = run(repo, {"build": {"mode": "ok", **TOUCH}, "verify": {"mode": "crash"}})
    crashed = [e for e in seats(evs, "verify") if e.get("state") not in ("started", "ok")]
    check("audit crash: the seat crashed and was respawned once", len(crashed) == 2, [e.get("state") for e in seats(evs, "verify")])
    rec = js.get("receipts") or {}
    check("audit crash: the row reads UNMEASURED, never WORKS", rec.get("UNMEASURED") == 1 and not rec.get("WORKS"), str(rec) + raw[-300:])


DRILLS = [drill_audit_red_relaunches_the_builder,
          drill_an_audit_seat_that_crashes_measures_nothing]


def main():
    only = sys.argv[1:] or None
    for d in DRILLS:
        name = d.__name__.replace("drill_", "")
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="hb-hotfix-drill-"))
        try:
            d(stub_venue(make_repo(tmp)))
        except Exception as ex:
            FAIL.append(f"{name}: raised {ex.__class__.__name__}: {ex}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for p in PASS:
        print("PASS", p)
    for f in FAIL:
        print("FAIL", f)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={0 if not FAIL else 1}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
