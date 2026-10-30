#!/usr/bin/env python
"""gh stand-in for the runner drills: answers the few gh calls the beat makes offline."""
import json, sys
argv = sys.argv[1:]
if argv[:2] == ["pr", "list"]:
    print("[]"); sys.exit(0)
if argv[:2] == ["pr", "create"]:
    print("https://example.invalid/pr/1"); sys.exit(0)
if argv[:2] == ["pr", "view"]:
    # a standing run asks for its PR's state; the drills' PRs are merged at once
    print(json.dumps({"state": "MERGED"})); sys.exit(0)
if argv[:2] == ["issue", "view"]:
    print(json.dumps({"title": "stub issue", "body": "## Rulings\nR1: stub\n\nwhen: x\nthen: y", "labels": []})); sys.exit(0)
print(json.dumps({"stub": True, "argv": argv})); sys.exit(0)
