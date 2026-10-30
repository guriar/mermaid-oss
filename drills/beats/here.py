#!/usr/bin/env python
"""Drill of the instruments as verbs (ruling 17): a session opens a worktree, works there, and asks
for a review of its own diff against its own ask, with no contract. Pins: the worktree receipt and
its idempotence (MO-44), the review receipt's honesty line and findings (MO-45), and that the
verbs journal into one run per worktree (MO-49)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "drills"))

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

from fixture.common import HB, PASS, FAIL, ROOT, check, journal, last_json, make_repo, stub_venue  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def hb(cwd, *args, env=None):
    p = subprocess.run([sys.executable, HB, *args], capture_output=True, text=True, cwd=str(cwd),
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    return p.returncode, last_json(p.stdout) or {"_raw": p.stdout[-400:], "_err": p.stderr[-400:]}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-here-drill-"))
    repo = make_repo(tmp)

    print("drill H1 - worktree open: a worktree off the landing branch, once")
    rc, d = hb(repo, "worktree", "open", "Breaks Double Count")
    wt = Path(d.get("worktree") or "")
    check("open: the receipt names the worktree, the branch here/<slug> and the base sha",
          rc == 0 and d.get("verdict") == "open" and d.get("branch") == "here/breaks-double-count"
          and len(d.get("base_sha") or "") == 40 and wt.is_dir(), str(d)[:300])
    check("open: the run is one per slug", d.get("run_id") == "here-breaks-double-count", d.get("run_id"))
    rc2, d2 = hb(repo, "worktree", "open", "breaks-double-count")
    check("open: a second call returns the same worktree, never a rival",
          rc2 == 0 and d2.get("worktree") == d.get("worktree") and d2.get("reused") is True, str(d2)[:200])
    evs = journal(repo, d.get("run_id") or "")
    check("open: the run journals its worktree op once",
          sum(1 for e in evs if e.get("kind") == "op" and e.get("key") == "worktree" and e.get("state") == "complete") == 1,
          str([e.get("kind") for e in evs]))

    print("drill H2 - review --here: one reviewer seat on this worktree's diff against the ask")
    (wt / "docs" / "fix.md").write_text("breaks are counted once\n", encoding="utf-8")
    g = ["git", "-C", str(wt), "-c", "user.email=d@d", "-c", "user.name=d"]
    subprocess.run(g + ["add", "docs/fix.md"], capture_output=True)
    subprocess.run(g + ["commit", "-qm", "the fix", "--", "docs/fix.md"], capture_output=True)
    script = {"review": {"mode": "ok", "report": {"findings": [
        {"id": "F1", "axis": "goal", "severity": "P1", "class": "broken", "confidence": 8, "status": "open",
         "title": "x returns a constant", "summary": "the fix hard-codes the answer",
         "rule": "the ask", "quote": {"file": "docs/fix.md", "line": 1, "text": "breaks are counted once"}}]}}}
    sp = tmp / "fake-script.json"
    sp.write_text(json.dumps(script), encoding="utf-8")
    rc, r = hb(wt, "review", "--here", "breaks are counted twice in the cost function", "--runtime", "fake",
               "--rate-limit-wait-min", "0", env={"FAKE_SCRIPT": str(sp)})
    check("review: the seat read and the receipt carries its findings",
          rc == 0 and r.get("verdict") == "ok" and [f.get("id") for f in r.get("findings") or []] == ["F1"], str(r)[:400])
    check("review: the receipt says no contract was supplied",
          r.get("contract") == "no contract supplied; completeness not measured", r.get("contract"))
    check("review: the receipt names the ask, the base sha and the tip it read",
          r.get("ask", "").startswith("breaks are counted") and r.get("base_sha") == d.get("base_sha")
          and len(r.get("tip") or "") == 40 and r.get("tip") != r.get("base_sha"), str({k: r.get(k) for k in ("ask", "base_sha", "tip")}))
    check("review: the fix-class finding is listed to fix", r.get("to_fix") == ["F1"], str(r.get("to_fix")))
    check("review: it journals into the worktree's run, not a new one", r.get("run_id") == d.get("run_id"), r.get("run_id"))
    evidence = repo / ".context" / "harness" / "runs" / d.get("run_id", "x") / "evidence" / "review-here.json"
    check("review: the receipt is written beside the run's evidence", evidence.exists(), str(evidence))

    print("drill H3 - review --here outside a worktree hb opened refuses")
    rc, r = hb(repo, "review", "--here", "anything", "--runtime", "fake")
    check("review: the product checkout itself is refused, naming the door to open first",
          rc == 2 and "hb worktree open" in str(r.get("why")), str(r)[:200])

    print("drill H6 - a checkout with no origin opens off its own landing branch, and a refused open is retried")
    solo = tmp / "solo"
    solo.mkdir()
    gs = ["git", "-C", str(solo), "-c", "user.email=d@d", "-c", "user.name=d"]
    subprocess.run(["git", "init", "-q", "-b", "dev", str(solo)], capture_output=True)
    (solo / ".claude").mkdir()
    shutil.copy(repo / ".claude" / "harness-config.json", solo / ".claude" / "harness-config.json")
    (solo / ".gitignore").write_text(".context/\n.claude/worktrees/\n", encoding="utf-8")
    (solo / "README.md").write_text("solo\n", encoding="utf-8")
    subprocess.run(gs + ["add", "-A"], capture_output=True)
    subprocess.run(gs + ["commit", "-qm", "init"], capture_output=True)
    rc, bad = hb(solo, "worktree", "open", "solo-fix", "--base", "no-such-ref")
    check("no origin: a base that does not exist is a refusal", rc == 2 and "unreadable" in str(bad.get("why")), str(bad)[:200])
    rc, good = hb(solo, "worktree", "open", "solo-fix")
    check("no origin: the next open tries again and branches off the local landing branch",
          rc == 0 and good.get("verdict") == "open" and good.get("reused") is False and Path(good.get("worktree") or "").is_dir(), str(good)[:300])

    print("drill H4 - audit --here: the venue up through the venue block, one audit seat, rows in the receipt")
    stub_venue(repo, capabilities=("ui",))
    script["verify"] = {"mode": "ok", "report": {"items": [
        {"item": "ASK", "state": "WORKS", "substate": None, "seen": "breaks counted once", "observed": "once",
         "situation": "fresh venue", "paths_tried": ["happy"], "scenario": None, "capture": None,
         "plan": "plan.md", "tenant_id": "tenant-fake", "evidence": ["plan.md"]}]}}
    sp.write_text(json.dumps(script), encoding="utf-8")
    rc, au = hb(wt, "audit", "--here", "--runtime", "fake", "--rate-limit-wait-min", "0",
                env={"FAKE_SCRIPT": str(sp), "HB_VENUE_STUB": "1"})
    check("audit: one seat drove the change and the receipt carries its rows",
          rc == 0 and au.get("verdict") == "ok" and [(r.get("item"), r.get("state")) for r in au.get("rows") or []] == [("ASK", "WORKS")],
          str(au)[:400])
    check("audit: the ask defaults to the last review's", au.get("ask", "").startswith("breaks are counted"), au.get("ask"))
    check("audit: it journals into the worktree's run", au.get("run_id") == d.get("run_id"), au.get("run_id"))
    evs = journal(repo, d.get("run_id") or "")
    check("audit: the venue went up and came down in the journal",
          any(e.get("kind") == "op" and str(e.get("key", "")).startswith("venue-up-audit") and e.get("state") == "complete" for e in evs)
          and any(e.get("kind") == "op" and str(e.get("key", "")).startswith("venue-down-audit") for e in evs),
          str(sorted({e.get("key") for e in evs if e.get("kind") == "op"})))

    print("drill H5 - land --shadow: the record and the PR body from the receipts, through the one lander")
    rc, ld = hb(wt, "land", "--shadow", env={"FAKE_SCRIPT": str(sp), "HB_GH_STUB": str(ROOT / "drills" / "fixture" / "gh_stub.py")})
    run_dir = repo / ".context" / "harness" / "runs" / (d.get("run_id") or "x")
    record = (run_dir / "evidence" / "record.md").read_text(encoding="utf-8") if (run_dir / "evidence" / "record.md").exists() else ""
    body = (run_dir / "evidence" / "pr-body.md").read_text(encoding="utf-8") if (run_dir / "evidence" / "pr-body.md").exists() else ""
    check("land: the record and the PR body are written by the beats' own lander",
          rc == 0 and record.startswith("# Record of run here-") and "## Record" in body, str(ld)[:300] + body[:200])
    check("land: the receipt counts the audit's WORKS row", (ld.get("receipts") or {}).get("WORKS") == 1, str(ld.get("receipts")))
    check("land: the receipt says no contract was supplied", ld.get("contract") == "no contract supplied; completeness not measured",
          ld.get("contract"))
    check("land: shadow pushed nothing and opened no PR", ld.get("shadow") is True and not ld.get("pr"), str(ld)[:200])


    print("drill H7 - the git guard owns here/: a landed here/ branch is the harness's litter to sweep")
    guard = ROOT / "hooks" / "git_guard.py"

    def ask(cmd):
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        p = subprocess.run([sys.executable, str(guard)], input=json.dumps(payload), capture_output=True, text=True,
                           cwd=str(repo), env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)})
        return p.returncode
    check("guard: `git branch -D here/<slug>` passes when here/<slug> is not the current branch",
          ask("git branch -D here/breaks-double-count") == 0, "refused")
    check("guard: `git push origin --delete here/<slug>` passes", ask("git push origin --delete here/breaks-double-count") == 0, "refused")
    check("guard: hotfix/ stays protected", ask("git branch -D hotfix/x") == 2, "allowed")
    check("guard: a redirection after the branch is not read as a second branch",
          ask("git push origin --delete here/breaks-double-count 2>&1 | tail -1") == 0, "refused")
    outside = tmp / "no-harness-repo"
    outside.mkdir(exist_ok=True)
    p_out = subprocess.run([sys.executable, str(guard)], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push origin main"},
                                                                          "cwd": str(outside)}),
                           capture_output=True, text=True, cwd=str(outside), env={**os.environ, "CLAUDE_PROJECT_DIR": str(outside)})
    check("guard: a repo with no harness config is nobody's business, even a push to main", p_out.returncode == 0, p_out.stderr[-200:])

    print(f"\n{'=' * 52}\n  {len(PASS)} passed · {len(FAIL)} failed" + (f"  -> {FAIL}" if FAIL else "  SUITE GREEN") + f"\n{'=' * 52}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
