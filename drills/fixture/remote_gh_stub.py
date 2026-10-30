#!/usr/bin/env python
"""gh stand-in for the remote gate drills: answers the runner list, the dispatch, the run list,
the run and the artifact download from a scenario file (HB_REMOTE_SCENARIO), and records every
call and every poll there."""
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
scenario = Path(os.environ["HB_REMOTE_SCENARIO"])
sc = json.loads(scenario.read_text(encoding="utf-8"))
sc["calls"] = sc.get("calls", []) + [argv]
path = next((a for a in argv if a.startswith("repos/")), "")
if "/actions/runs/" in path:
    sc["polls"] = sc.get("polls", 0) + 1
scenario.write_text(json.dumps(sc), encoding="utf-8")
if argv[:1] == ["api"] and path.endswith("/actions/runners"):
    if sc.get("down"):
        sys.stderr.write("connect: no route to host\n")
        sys.exit(1)
    print(sc.get("online", 2))
    sys.exit(0)
if argv[:1] == ["api"] and path.endswith("/dispatches"):
    sys.exit(int(sc.get("dispatch_rc", 0)))
if argv[:1] == ["api"] and "/runs?" in path:
    print(json.dumps(sc.get("runs", [])))
    sys.exit(0)
if argv[:1] == ["api"] and "/actions/runs/" in path:
    done = sc["polls"] >= int(sc.get("completes_after", 1))
    print(json.dumps({"status": "completed" if done else "in_progress", "conclusion": "success" if done else None}))
    sys.exit(0)
if argv[:2] == ["run", "download"]:
    files = sc.get("artifact")
    if files is None:
        sys.stderr.write("no artifact matches\n")
        sys.exit(1)
    into = Path(argv[argv.index("--dir") + 1])
    for name, text in files.items():
        (into / name).write_text(text, encoding="utf-8")
    sys.exit(0)
print(json.dumps({"stub": True, "argv": argv}))
sys.exit(0)
