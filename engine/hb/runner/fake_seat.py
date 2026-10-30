#!/usr/bin/env python
"""A stand-in seat for the runner's drills. It reads the brief on stdin like a real
CLI, then behaves as the drill script says for its role and attempt, so the state
machine can be exercised without a model: a valid report, a stale one, a crash, a
rate limit, a timeout.

Env from the runner: HB_SEAT_ROLE, HB_OUTPUT_PATH, HB_RUN_ID, HB_ATTEMPT, HB_BASE_SHA,
HB_ISSUE. Env from the drill: FAKE_SCRIPT, a JSON file keyed `<role>.<attempt>` or
`<role>` (or `<label>.<attempt>` / `<label>` for a seat journaled under its own label),
each `{"mode": "ok|invalid|crash|ratelimit|usagelimit|timeout", "report": {...}, "touch": ["path", ...],
"write": {path: text}, "exec": ["script.py", ...]}`. `touch` makes the build seat commit
real changes so the runner has a diff to measure; `exec` runs a drill's script in the cwd.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def sh(*args):
    return subprocess.run(list(args), capture_output=True, text=True).stdout.strip()


def main():
    brief = sys.stdin.read()
    role = os.environ.get("HB_SEAT_ROLE", "build")
    label = os.environ.get("HB_SEAT_LABEL", role)
    attempt = int(os.environ.get("HB_ATTEMPT", "1"))
    out = Path(os.environ["HB_OUTPUT_PATH"])
    script = {}
    sp = os.environ.get("FAKE_SCRIPT")
    if sp and Path(sp).exists():
        script = json.loads(Path(sp).read_text(encoding="utf-8"))
    # a seat journaled under a name of its own (a merge seat is a build seat named merge-<n>)
    # is scripted by that name first; a drive shard (verify.2) is still scripted by its role
    own = label.split(".")[0] != role
    spec = ((script.get(f"{label}.{attempt}") or script.get(label) if own else None)
            or script.get(f"{role}.{attempt}") or script.get(role) or {"mode": "ok"})
    issue = os.environ.get("HB_ISSUE", "#0").lstrip("#")
    mode = spec.get("mode", "ok")
    if spec.get("stderr"):
        sys.stderr.write(spec["stderr"] + "\n")
    if mode == "ratelimit":
        sys.stderr.write("API error: rate limit exceeded, resets in 2 minutes\n")
        sys.exit(1)
    if mode == "usagelimit":
        # the stream a Claude seat prints when the plan's window is spent (run 20261007-184837)
        words = spec.get("words") or "You've hit your weekly limit · resets Oct 11, 6am (America/Chicago)"
        for line in ({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1791716400}},
                     {"type": "assistant", "error": "rate_limit", "api_error": "usage_limit_reached",
                      "message": {"content": [{"type": "text", "text": words}]}},
                     {"type": "result", "is_error": True, "api_error_status": 429, "api_error": "usage_limit_reached",
                      "result": words, "total_cost_usd": 0}):
            print(json.dumps(line))
        sys.exit(1)
    if mode == "crash":
        sys.stderr.write("boom\n")
        sys.exit(3)
    if mode == "timeout":
        time.sleep(float(spec.get("sleep", 30)))
        sys.exit(0)
    for rel in spec.get("touch") or []:
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        # one marker line, once: two fake builds of one file end identical, so two slices
        # touching it merge clean in the wave drill instead of conflicting on line counts
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        if "# fake" not in text:
            p.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "# fake\n", encoding="utf-8")
        sh("git", "add", rel)
    for rel, text in (spec.get("write") or {}).items():
        # a whole file as the drill wrote it: a changelog heading, a manifest line;
        # `{issue}` in the path or the text is the seat's issue, so two slices write apart
        rel, text = rel.replace("{issue}", issue), text.replace("{issue}", issue)
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        sh("git", "add", rel)
    for rel in spec.get("touch_rows") or []:
        # one row per issue, so two slices of a wave drill collide on the append-only file
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        row = json.dumps({"issue": os.environ.get("HB_ISSUE", "#0"), "fake": True})
        if row not in text:
            p.write_text(text + ("\n" if text and not text.endswith("\n") else "") + row + "\n", encoding="utf-8")
        sh("git", "add", rel)
    if spec.get("touch") or spec.get("touch_rows") or spec.get("write"):
        sh("git", "-c", "user.email=fake@seat", "-c", "user.name=fake", "commit", "-q", "--allow-empty", "-m",
           f"fake {role} {attempt}")
    if spec.get("exec"):
        # a drill's own script does the seat's work in its cwd, reading the brief on stdin
        subprocess.run([sys.executable, *spec["exec"]], input=brief, text=True, encoding="utf-8")
    head = sh("git", "rev-parse", "HEAD")
    if mode == "invalid":
        head = "0000000deadbeef"
    base = {
        "issue": os.environ.get("HB_ISSUE", "#0"), "run_id": os.environ.get("HB_RUN_ID", ""),
        "attempt": attempt, "seat": role, "base_sha": os.environ.get("HB_BASE_SHA", ""),
        "head_sha": head,
    }
    # a review answers at the scope its brief asked for, as a real reviewer does
    asked = re.search(r"^## Scope\s+(\w+)", brief, re.M)
    scope = asked.group(1) if asked else "full"

    bodies = {
        "build": {"schema": "build-note/v1", "goal": "fake goal",
                  "items": [{"item": "HF-01", "status": "built", "before": "x.py:1 did nothing"}],
                  "decisions": [], "files": [{"path": "x.py", "owns": "nothing", "expensive": "none",
                                              "deleted": "none"}],
                  "needs_authority": [], "tests": [], "changelog": "fake", "docs": []},
        "review": {"schema": "review-findings/v3", "scope": scope,
                   "attested_tip": head, "goal_verdict": {"met": True, "misses": []},
                   "findings": [], "codex": {"read": False, "adopted": 0, "refuted": 0}},
        "verify": {"schema": "verify-report/v2", "venue": "v-fake", "served_sha": head,
                   "items": [{"item": "HF-01", "state": "WORKS", "substate": None,
                              "seen": "it worked", "observed": "it worked", "situation": "fresh venue",
                              "paths_tried": ["happy"], "scenario": None, "capture": None,
                              "plan": "plan.md", "tenant_id": "tenant-fake", "evidence": ["plan.md"]}]},
        "discovery": {"schema": "discovery-report/v1", "served_sha": head, "finds": []},
        "demo": {"schema": "demo/v1", "brief_words": len(brief.split())},
    }
    body = bodies[role]
    body.update(spec.get("report") or {})
    body.update(base)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(json.dumps({"fake": True, "role": role, "attempt": attempt, "mode": mode}))


if __name__ == "__main__":
    main()
