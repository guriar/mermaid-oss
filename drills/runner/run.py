#!/usr/bin/env python
"""Drills for the runner (hb run hotfix) on the fake runtime.

Each drill builds a throwaway product repo with a bare `origin`, a config that
names the fake runtime for every seat and one trivial gate, then runs the hotfix
beat in shadow with a FAKE_SCRIPT that scripts every seat's behaviour. Asserts
read the run journal and the phase file, never the console.

    python engine/drill_run.py

Drills:
  happy         every seat valid; the run ships in shadow; every phase recorded
  invalid       the build seat writes a stale head_sha once; respawned once; ships
  ratelimit     the review seat rate-limits once; the runner waits (0 min) and retries
  crash-twice   the build seat crashes every time; relaunched its fixed number of times, the run stops at build, named
  authority     the note asks for a migration; the run stops before gates
  nonwaivable   a nonwaivable finding stops the run; no PR
  nonwaivable_answered  an answer naming the finding sends it to the fixer instead
  nonwaivable_other_id  an answer naming another finding leaves this one stopped
  gate_answered  a red gate stops with its G- id; an answer naming it runs the gate again
  every_gate_unmeasured_stops_without_a_fixer  no gate printed a result: a rig stop, no fixer
  empty_tip_stops_before_measuring  an unreadable tip raises a rig fault before any gate runs
  detached_run_outlives_its_launcher  --detach returns the run id at once; the run ends on its own
  ci_parity     a tools/ script the product's CI workflow runs is a gate before the PR
  replay_stale_base  a base copy left behind never stops the replay from measuring
  replay_unmeasured_retried  a replay at base that measured nothing is run again
  seat_long_commands  a Claude seat gets the long ceilings, so a suite run outlives 10 minutes
  fix-round     one broken finding; the builder fixes it; the read after the fix runs; ships
  resume        the journal's completed ops are not repeated on --resume
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
import tempfile
import time
from types import SimpleNamespace

from fixture.common import HERE, HB, PASS, FAIL, sh, last_json, free_port, stop_pid, make_repo, run_hotfix, run_dir, journal, seats, _heavy_measured, check, TOUCH  # noqa: E402,F401


def drill_happy(repo):
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}})
    check("happy: shipped", js.get("verdict") == "mergeable", raw[-600:])
    evs = journal(repo, js.get("run_id", ""))
    phases = [e["phase"] for e in evs if e.get("kind") == "phase"]
    for ph in ("contract", "worktree", "build", "gates", "review", "drive", "mergeable"):
        check(f"happy: phase {ph}", ph in phases, str(phases))
    check("happy: build once, review once", len(seats(evs, "build")) == 2 and len(seats(evs, "review")) == 2,
          str([(s["seat"], s["state"]) for s in seats(evs)]))
    check("happy: no fix seat", not seats(evs, "fix"))
    check("happy: gate measured", any(g.get("true_exit") == 0 for g in
                                      next((e["result"] for e in evs if str(e.get("key", "")).startswith("gates-r1@") and e.get("state") == "complete"), [])))
    check("happy: no push in shadow", not any(e.get("key", "").startswith("push") for e in evs))


def drill_invalid(repo):
    rc, js, raw = run_hotfix(repo, {"build.1": {"mode": "invalid", **TOUCH}, "build.2": {"mode": "ok", **TOUCH}})
    evs = journal(repo, js.get("run_id", ""))
    states = [s["state"] for s in seats(evs, "build")]
    check("invalid: respawned once then ok", states == ["started", "invalid-report", "started", "ok"], str(states))
    check("invalid: problems named head_sha", any("head_sha" in str(s.get("problems")) for s in seats(evs, "build")))
    check("invalid: shipped", js.get("verdict") == "mergeable")


def drill_ratelimit(repo):
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}, "review.1": {"mode": "ratelimit"}})
    evs = journal(repo, js.get("run_id", ""))
    check("ratelimit: classified", any(s["state"] == "rate-limited" for s in seats(evs, "review")))
    check("ratelimit: waited then retried", any(e.get("kind") == "wait" for e in evs)
          and seats(evs, "review")[-1]["state"] == "ok")
    check("ratelimit: shipped", js.get("verdict") == "mergeable")


def drill_crash_twice(repo):
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "crash"}})
    check("crash-twice: stopped at build", js.get("verdict") == "stopped" and js.get("stage") == "build", raw[-400:])
    evs = journal(repo, js.get("run_id", ""))
    from hb.beats import slice as slice_door
    check("crash-twice: the builder is relaunched its fixed number of times, then the run stops",
          len([s for s in seats(evs, "build") if s["state"] == "started"]) == 2 * slice_door.BUILD_ATTEMPTS,
          [s["state"] for s in seats(evs, "build")])


def drill_authority(repo):
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH, "report": {
        "needs_authority": [{"kind": "migration", "text": "needs a column", "evidence": "x.py:1"}]}}})
    check("authority: stopped before gates", js.get("verdict") == "stopped" and js.get("stage") == "authority", raw[-400:])
    evs = journal(repo, js.get("run_id", ""))
    check("authority: no gates ran", not any(str(e.get("key", "")).startswith("gates-build@") for e in evs))


def drill_nonwaivable(repo):
    finding = {"id": "F1", "axis": "correctness", "severity": "P0", "class": "nonwaivable",
               "rule": "tenant isolation", "confidence": 9, "title": "query without tenant scope", "summary": "query without tenant scope",
               "quote": {"file": "src/x.py", "line": 1, "text": "x = 1"}, "absence": None,
               "recurrence": "n/a", "status": "open", "fixed_at": None}
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    "review": {"mode": "ok", "report": {"findings": [finding]}}})
    check("nonwaivable: stopped", js.get("verdict") == "stopped" and any("nonwaivable" in s for s in js.get("stops", [])), raw[-400:])
    evs = journal(repo, js.get("run_id", ""))
    check("nonwaivable: no fixer", not seats(evs, "fix"))
    check("nonwaivable: no pr op", not any(e.get("key") == "pr" for e in evs))


def drill_resume_keeps_build_and_review(repo):
    """A hotfix that stopped at its review for a ruling, resumed with the answer: the builder
    is not sat again on built code, the review of the unchanged tip is met and not read again,
    and the answer goes to the fixer."""
    finding = {"id": "F1", "axis": "correctness", "severity": "P0", "class": "nonwaivable",
               "rule": "tenant isolation", "confidence": 9, "title": "query without tenant scope", "summary": "query without tenant scope",
               "quote": {"file": "src/x.py", "line": 1, "text": "x = 1"}, "absence": None,
               "recurrence": "n/a", "status": "open", "fixed_at": None}
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    "review.1": {"mode": "ok", "report": {"findings": [finding]}}})
    rid = js.get("run_id", "")
    check("resume keeps: the first run stops at its review", js.get("verdict") == "stopped" and rid, raw[-300:])
    (run_dir(repo, rid) / "control.txt").write_text("answer: F1: scope the query by tenant\n", encoding="utf-8")
    fixed = dict(finding, status="fixed", fixed_at="x.py:1 @ tip")
    rc, js2, raw2 = run_hotfix(repo, {"build": {"mode": "crash"},
                                      "fix": {"mode": "ok", **TOUCH, "report": {"fixes": [
                                          {"finding": "F1", "red_first": "test_x.py", "change": "tenant scope", "pinned_by": "test_x.py"}]}},
                                      "review.2": {"mode": "ok", "report": {"scope": "the fix", "findings": [fixed]}}},
                               extra=("--resume", rid))
    evs = journal(repo, rid)
    check("resume keeps: the builder is not sat again", len([x for x in seats(evs, "build") if x["state"] == "started"]) == 1,
          str([x["state"] for x in seats(evs, "build")]))
    check("resume keeps: the review at the unchanged tip stood from the first pass; one whole-diff review, one closing check",
          len([x for x in seats(evs, "review") if x["state"] == "started"]) == 2
          and len([x for x in seats(evs, "review") if x["state"] == "ok"]) == 2, str([e.get("kind") for e in evs][-25:]))
    check("resume keeps: the answered finding went to the fixer and the run shipped",
          any(x["state"] == "ok" for x in seats(evs, "fix")) and js2.get("verdict") == "mergeable", raw2[-400:])


def drill_kept_seat_rule(repo):
    """A seat that ended ok is kept for a later pass that asks the same thing while the tip
    it left is still in the tree's history: handed once, never to the pass that wrote it,
    never for a tip the tree no longer holds, and never for a different ask. Where the seat
    ran (the stand-in) is not part of what it was asked."""
    sys.path.insert(0, str(ENGINE))
    from hb.runner import seating
    rc, head, _ = sh(["git", "-C", str(repo), "rev-parse", "HEAD"])
    head = head.strip()
    d = repo.parent / "krun"
    key = seating.asked({"Scope": "full"})

    def line(attempt, **over):
        sd = d / "seats" / f"review-{attempt}"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "report.json").write_text("{}", encoding="utf-8")
        return dict({"kind": "seat", "seat": "review", "state": "ok", "attempt": attempt, "key": key,
                     "pass": "an-earlier-pass", "tip_in": head, "tip_out": head}, **over)
    evs = [line(1), line(2, tip_out="0" * 40), line(3, **{"pass": seating._PASS})]
    j = SimpleNamespace(dir=d, run_id="krun", events=lambda: evs)
    other = seating.kept(j, "review", seating.asked({"Scope": "the fix"}), repo)
    first = seating.kept(j, "review", key, repo)
    again = seating.kept(j, "review", key, repo)
    check("kept seat: a different ask is not kept", other is None, str(other))
    check("kept seat: the same ask at a tip in the tree's history is kept", bool(first) and first["attempt"] == 1, str(first))
    check("kept seat: handed once; a lost tip and this pass's own seat are not kept", again is None, str(again))
    check("kept seat: the place a seat ran is not what it was asked",
          seating.asked({"a": 1, "The venue": {"port": 1}}) == seating.asked({"a": 1, "The venue": {"port": 2}})
          and seating.asked({"a": 1}) != seating.asked({"a": 2}))


def drill_known_reds(repo):
    """A gate the run proved red before the change does not stop the last check before the
    PR; a red nobody proved still does."""
    sys.path.insert(0, str(ENGINE))
    from hb.gates import gating
    j = SimpleNamespace(events=lambda: [{"kind": "preexisting-red", "gate": "ci: migrate", "evidence": "base-replay.txt"}])
    ran = [{"name": "ci: migrate", "true_exit": 1}, {"name": "ci: boundary", "true_exit": 1}, {"name": "ci: types", "true_exit": 0}]
    red = [g["name"] for g in gating.gates_red(ran, gating.known_reds(j))]
    check("known reds: a proven pre-existing red is passed over, an unproven one is not", red == ["ci: boundary"], str(red))


def drill_first_screen(repo):
    """The first screen's verdict is read off the record: a red row is never Ready, a red goal
    or an open stop is Not ready; a seat's own call is shown as decided, not as a ruling owed."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import firstscreen as fs
    items = [{"item": "A-01", "when": "a user opens the page", "then": "the title reads Sales", "kind": "screen", "proof": "eyes"},
             {"item": "A-02", "when": "a caller lists people", "then": "the hidden row is absent", "kind": "api", "proof": "test"}]
    ok = [{"item": "A-01", "state": "WORKS"}, {"item": "A-02", "state": "WORKS", "substate": "by-test"}]
    red = [{"item": "A-01", "state": "BROKEN", "observed": "the title reads Sale"}, ok[1]]
    goals = [{"item": "G-01", "then": "a user sees sales"}]
    check("first screen: every item works is Ready", fs.verdict(ok, items)["word"] == fs.READY)
    check("first screen: a red item is Known reds", fs.verdict(red, items)["word"] == fs.KNOWN_REDS)
    check("first screen: a red goal row or an open stop is Not ready",
          fs.verdict(ok, items, goals=goals)["word"] == fs.NOT_READY and fs.verdict(ok, items, stops=["[A-1] x"])["word"] == fs.NOT_READY)
    note = {"for_user": ["The Sales page has a title."], "underneath": ["One heading component."],
            "needs_authority": [{"kind": "question", "text": "Should the title be bold? Built it regular.", "evidence": "x"}]}
    built = fs.build({"items": items}, red, note, None, [], allowance={"minutes": 12, "usd": 3, "wall_min": 20})
    check("first screen: a seat's own call is decided for you, never a ruling owed",
          built["decided"] and not built["ruling"] and "Should the title be bold" in built["decided"][0], str(built)[:300])


def drill_run_view(repo):
    """The run page reads a finished run from its journal: where it is on its line, its
    launch card, and what it cost; an answer to a stop nobody raised is refused."""
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}})
    rid = js.get("run_id", "")
    sys.path.insert(0, str(ENGINE))
    from hb.record import view
    from hb.record import page
    saved = view.root
    view.root = lambda: run_dir(repo, rid).parent
    try:
        v = view.run(rid) or {}
        check("run view: a shipped run sits at the end of its line", v.get("state") == "mergeable" and v.get("at") == len(v.get("line") or []),
              str({k: v.get(k) for k in ("state", "at", "line")}))
        check("run view: the launch card is journaled with the run's flags",
              isinstance(v.get("launch"), dict) and "--shadow" in (v["launch"].get("flags") or []), str(v.get("launch")))
        check("run view: steps and nothing owed", (v.get("cost") or {}).get("steps", 0) >= 2 and v.get("needs") == 0, str(v.get("cost")))
        refused = page.answer(rid, "A-none", "yes")
        check("run page: an answer to a stop that is not open is refused", refused.get("ok") is False, str(refused))
    finally:
        view.root = saved


def drill_run_page_plays_the_film_and_opens_pictures(repo):
    """The owner reviews a merged run on the page after the chat is gone: the demo seat's
    film plays there with its summary, the finished list marks it, and the server answers a
    byte range so a long film can seek."""
    import threading
    import urllib.request
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}})
    rid = js.get("run_id", "")
    sys.path.insert(0, str(ENGINE))
    from hb.record import view
    from hb.record import page
    demo = run_dir(repo, rid) / "evidence" / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    (demo / "film.webm").write_bytes(bytes(range(256)) * 40)
    (demo / "summary.md").write_text("Filmed on dev.", encoding="utf-8")
    (demo / "launch.json").write_text(json.dumps({"verdict": "filmed"}), encoding="utf-8")
    saved = view.root
    view.root = lambda: run_dir(repo, rid).parent
    try:
        v = view.run(rid) or {}
        f = v.get("demo") or {}
        check("film: the run view plays the demo seat's film with its summary",
              f.get("video") == f"/file/{rid}/demo/film.webm" and f.get("summary") == "Filmed on dev.", str(f))
        row = view.row_of(rid)
        check("film: the finished list marks the run as having a film", row.get("film") is True, str(row)[:200])
        port = free_port()
        srv = page.ThreadingHTTPServer(("127.0.0.1", port), page.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/file/{rid}/demo/film.webm", headers={"Range": "bytes=100-199"})
            with urllib.request.urlopen(req, timeout=10) as r:
                body, status, crange = r.read(), r.status, r.headers.get("Content-Range")
            check("film: a byte range is answered 206 with exactly those bytes",
                  status == 206 and crange == "bytes 100-199/10240" and body == (bytes(range(256)) * 40)[100:200],
                  f"{status} {crange} {len(body)}")
        finally:
            srv.shutdown()
        html = page.PAGE.read_text(encoding="utf-8")
        check("pictures: every before and after picture opens full size on click",
              html.count("data-zoom>Before") == 1 and html.count("data-zoom>After") == 1 and 'id="zoom"' in html, "")
    finally:
        view.root = saved

def _iso(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _write_journal(d, events):
    (d / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def drill_live_run_clock_counts_to_now(repo):
    """A builder works for an hour and writes no journal line until it ends. The page's clock
    stopped at the last line, so a run 25 minutes in read "1m in"; a running run counts to now,
    and a run that has ended still counts only its open passes."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import view
    rid = "drill-live-clock"
    d = run_dir(repo, rid)
    _write_journal(d, [{"ts": _iso(25 * 60), "kind": "phase", "phase": "opened", "beat": "slice", "ref": "#1"},
                       {"ts": _iso(24 * 60), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"}])
    from hb.runner import liveness
    liveness.record(d)
    saved = view.root
    view.root = lambda: d.parent
    try:
        v = view.run(rid) or {}
        wall = (v.get("cost") or {}).get("wall_min")
        check("live clock: a running run 25 minutes in reads 25 minutes, not the minute to its last line",
              v.get("state") == "running" and wall in (24, 25, 26), str({"state": v.get("state"), "wall_min": wall}))
        _write_journal(d, [{"ts": _iso(25 * 60), "kind": "phase", "phase": "opened"},
                           {"ts": _iso(24 * 60), "kind": "phase", "phase": "stopped", "why": "x"}])
        v = view.run(rid) or {}
        check("live clock: a stopped run counts only its open pass", (v.get("cost") or {}).get("wall_min") == 1,
              str(v.get("cost")))
    finally:
        view.root = saved


def _assistant(ts, block):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [block]}}


def drill_seat_stream_summary(repo):
    """A working seat says nothing to the journal for hours, so the page reads the tail of its
    output stream: the latest actions in plain words, newest first, with a cut first line, an
    unknown event, a half-written last line and a secret all tolerated and the secret hidden."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import live
    rid = "drill-seat-stream"
    d = run_dir(repo, rid)
    sd = d / "seats" / "build-1"
    sd.mkdir(parents=True)
    wt = "C:\\Users\\x\\Desktop\\product\\.claude\\worktrees\\run-slice-issue-1\\src\\services\\x.py"
    lines = [{"type": "user", "message": {"content": [{"type": "tool_result", "content": "y" * 80000}]}},
             {"type": "system", "subtype": "init", "tools": ["Bash"]},
             _assistant("2026-10-05T21:40:00Z", {"type": "tool_use", "name": "Bash", "input": {
                 "command": "python -m pytest tests/test_y.py -q", "description": "Run the unit tests"}}),
             _assistant("2026-10-05T21:41:00Z", {"type": "tool_use", "name": "Edit", "input": {"file_path": wt}}),
             {"type": "mystery", "shape": {"never": "seen"}},
             _assistant("2026-10-05T21:42:00Z", {"type": "text", "text": "Set ANTHROPIC_API_KEY=sk-ant-abc123456789xyz and moved on."}),
             _assistant("2026-10-05T21:43:00Z", {"type": "thinking", "thinking": ""}),
             _assistant("2026-10-05T21:44:00Z", {"type": "tool_use", "name": "Read", "input": {"file_path": "/c/r/.claude/worktrees/run-a/docs/a.md"}}),
             {"type": "system", "subtype": "task_summary", "detail": "Checking the nav tests"}]
    text = "\n".join(json.dumps(x) for x in lines) + "\nnot json at all\n" + '{"type":"assistant","message":{"content":[{"type":"tool_u'
    (sd / "stdout.txt").write_text(text, encoding="utf-8")
    rows = live.doing(sd / "stdout.txt")
    words = [r["text"] for r in rows]
    check("stream: the latest five actions, newest first, in plain words",
          words == ["Now: Checking the nav tests", "Reading docs/a.md", "Says: Set ANTHROPIC_API_KEY=[hidden] and moved on.",
                    "Editing src/services/x.py", "Running tests: Run the unit tests"], str(words))
    check("stream: no secret reaches the page", "sk-ant" not in json.dumps(rows), str(rows))
    check("stream: an unchanged file is not read again", live.doing(sd / "stdout.txt") is rows)
    check("stream: a missing stream reads as nothing", live.doing(sd / "absent.txt") == [])
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    base = g("rev-parse", "HEAD")[1].strip()
    (repo / "docs" / "b.md").write_text("b\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "The schedule starts empty")
    (d / "evidence").mkdir(exist_ok=True)
    (d / "evidence" / "progress.md").write_text("# progress\n\n## done\n- R-1\n\n## in progress\n- R-2 dashboard screens\n\n## next\n- R-3\n",
                                                encoding="utf-8")
    evs = [{"ts": _iso(30 * 60), "kind": "phase", "phase": "opened"},
           {"ts": _iso(30 * 60), "kind": "phase", "phase": "worktree", "worktree": str(repo), "base_sha": base},
           {"ts": _iso(29 * 60), "kind": "seat", "seat": "review", "attempt": 1, "state": "started"},
           {"ts": _iso(20 * 60), "kind": "seat", "seat": "review", "attempt": 1, "state": "ok", "minutes": 9},
           {"ts": _iso(19 * 60), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"}]
    panels = live.seats(d, evs)
    p = panels[0] if len(panels) == 1 else {}
    check("stream: one panel for the one seat still working, with its clock",
          p.get("role") == "Builder" and 18 * 60 <= p.get("running_s", 0) <= 20 * 60 and p.get("quiet_s", 99) < 60, str(panels)[:400])
    check("stream: a builder shows its commits since base and its in-progress lines",
          p.get("commits") == {"count": 1, "latest": ["The schedule starts empty"]} and p.get("in_progress") == ["R-2 dashboard screens"],
          str({k: p.get(k) for k in ("commits", "in_progress")}))


def drill_run_activity_feed(repo):
    """The run page showed where a run is, not that it moves. The journal reads as an activity
    feed in plain words, newest first: seats with minutes and cost, checks with pass and red
    counts, questions, answers and phases; an event with nothing to say is left out."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import live
    gates = [{"name": "ruff", "true_exit": 0}, {"name": "tsc", "true_exit": 0}, {"name": "backend-tests", "true_exit": 1}]
    evs = [{"ts": "2026-10-05T21:00:00Z", "kind": "phase", "phase": "opened"},
           {"ts": "2026-10-05T21:00:05Z", "kind": "phase", "phase": "contract", "goal": "the schedule starts empty"},
           {"ts": "2026-10-05T21:00:06Z", "kind": "seat", "seat": "build", "attempt": 1, "state": "started"},
           {"ts": "2026-10-05T21:40:00Z", "kind": "seat", "seat": "build", "attempt": 1, "state": "ok", "minutes": 39.9,
            "usage": {"cost_usd": 12.5}},
           {"ts": "2026-10-05T21:45:00Z", "kind": "op", "key": "gates-build@abc", "state": "complete", "result": gates},
           {"ts": "2026-10-05T21:46:00Z", "kind": "machine-slot", "state": "taken"},
           {"ts": "2026-10-05T21:47:00Z", "kind": "question", "seat": "verify.2", "text": "Is blue right?"},
           {"ts": "2026-10-05T21:48:00Z", "kind": "stop", "id": "A-1", "text": "A migration is needed"},
           {"ts": "2026-10-05T21:50:00Z", "kind": "control", "verb": "answer", "text": "A-1: yes, add it"},
           {"ts": "2026-10-05T21:51:00Z", "kind": "phase", "phase": "opened"},
           {"ts": "2026-10-05T21:52:00Z", "kind": "op", "key": "pr", "state": "complete", "result": {"url": "https://x/pr/9"}}]
    rows = live.activity(evs)
    words = [r["text"] for r in rows]
    check("feed: the journal in plain words, newest first", words == [
        "Opened the PR https://x/pr/9", "Run started again", "Your answer arrived: A-1: yes, add it",
        "Waiting on you: A migration is needed", "Tester 2 decided a question for you: Is blue right?",
        "Checks after build: 2 passed, 1 red (backend-tests)", "Builder finished after 39.9 min, $12.50",
        "Builder started", "Contract read: the schedule starts empty", "Run started"], str(words))
    check("feed: each line keeps its time", rows[0]["at"] == "2026-10-05T21:52:00Z" and rows[-1]["at"] == "2026-10-05T21:00:00Z")
    check("feed: an event of a shape it does not know is left out, not raised",
          live.activity([{"kind": "seat", "state": "ok", "usage": "odd", "minutes": None}, {"kind": None}]) is not None)


def drill_gate_timeout_is_unmeasured_and_kills_its_tree(repo):
    """A full suite hung under memory pressure; its 60-minute timeout raised out of the gate and
    killed the whole run, and its workers held 13 GB after their parent was killed. A gate that
    times out has an invalid verdict of the environment's (timeout), is run once more, and is
    then UNMEASURED by name; the timed-out call takes its whole process tree."""
    import subprocess as sp
    from hb.runner import hbcall
    from hb.gates import gating
    from hb.record.journal import Journal
    real_hb = hbcall.hb

    def hung(args, cwd, timeout=1800, env=None):
        raise sp.TimeoutExpired(args, timeout)
    hbcall.hb = hung
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        j = Journal("drill-gate-timeout", c={"state_dir": str(repo / ".context" / "harness")})
        rows = gating.measure(j, str(repo), "drill", "base", [], {"branches": {"landing": "dev"}, "state_dir": str(repo / ".context" / "harness")}, "gates-r1",
                              only=[{"name": "slow", "cmd": ["python", "-c", "1"]}])
        crashed = None
    except Exception as ex:  # the defect: the timeout escaped
        rows, crashed = [], repr(ex)
    finally:
        hbcall.hb = real_hb
        os.chdir(here)
    row = (rows or [{}])[0]
    check("gate timeout: the run does not crash", crashed is None, str(crashed))
    v = row.get("verdict") or {}
    check("gate timeout: executed false, class environment, signature timeout, rerun once",
          v.get("valid") is False and v.get("cause_class") == "environment" and v.get("signature") == "timeout", str(row))
    check("gate timeout: the gate is UNMEASURED with why", row.get("true_exit") is None
          and str(row.get("unmeasured")).startswith("timeout (environment): rerun once"), str(row))
    marker = repo.parent / "grandchild.pid"
    parent = sp.Popen([sys.executable, "-c", "import subprocess,sys,time;"
                       f"p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                       f"open(r'{marker}','w').write(str(p.pid));time.sleep(60)"])
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    hbcall.kill_tree(parent.pid)
    parent.wait(timeout=10)
    gpid = int(marker.read_text().strip())
    alive = sp.run(["tasklist", "/FI", f"PID eq {gpid}"], capture_output=True, text=True).stdout if os.name == "nt" else ""
    check("gate timeout: the timed-out call's grandchildren are gone too", str(gpid) not in alive, alive[-200:])


def drill_each_gate_shows_its_own_seconds(repo):
    """The page showed a round's checks as one op, each about 10 ms. A row now carries its
    own seconds; a journal from before is read from its artifacts' start stamps; with neither,
    nothing is printed. The page and `run watch` say the same numbers."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import timeline
    from hb.record import watch
    now = [{"name": "ruff", "true_exit": 0, "seconds": 2.5}, {"name": "tsc", "true_exit": 1, "seconds": 95.0}]
    old = [{"name": "ruff", "true_exit": 0, "artifact": "C:\\ev\\gates\\gate-20261012T185228Z.txt"},
           {"name": "tsc", "true_exit": 0, "artifact": "/ev/gates/gate-20261012T185231Z.txt"},
           {"name": "eslint", "true_exit": 0}]
    check("gate clock: a row's own seconds are read as they are", timeline.gate_seconds(now) == [2.5, 95.0])
    check("gate clock: an older journal is read from its artifacts, and a row with neither has none",
          timeline.gate_seconds(old, "2026-10-12T19:02:38Z") == [3.0, None, None], timeline.gate_seconds(old, "2026-10-12T19:02:38Z"))
    check("gate clock: the last stamped row runs to the round's end",
          timeline.gate_seconds(old[:2], "2026-10-12T18:53:31Z") == [3.0, 60.0])
    steps = timeline.collect(repo, [{"ts": "2026-10-12T18:52:28Z", "kind": "op", "key": "gates-r1@a", "state": "started"},
                                    {"ts": "2026-10-12T18:54:10Z", "kind": "op", "key": "gates-r1@a", "state": "complete", "result": now + old[2:]}])
    rows = steps[0]["detail"]["checks"]
    check("gate clock: the page's rows carry seconds only where they are known",
          rows[0].get("seconds") == 2.5 and rows[1].get("seconds") == 95.0 and "seconds" not in rows[2], rows)
    html = (ENGINE / "hb" / "record" / "page.html").read_text(encoding="utf-8")
    check("gate clock: the page prints a Took column and nothing for an unknown one", "<th>Took</th>" in html
          and 'r.seconds==null?""' in html)
    line = watch._line({"ts": "2026-10-12T18:54:10Z", "kind": "op", "key": "gates-r1@a", "state": "complete", "result": now + old[2:]})
    check("gate clock: run watch prints each gate's seconds, its red, and no number it does not have",
          line == "18:54 gates-r1: ruff 2.5s, tsc 95.0s red, eslint", line)


def drill_run_timeline_steps(repo):
    """The page's timeline was a strip of station names with no detail. Each seat and each
    round of checks is now a step with its station, clock, cost and what it produced, read
    from the journal and the seat's report; a seat cut short by a restart says so, testers
    working at once sit in parallel lanes, and a changed report is read again."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import timeline
    d = run_dir(repo, "drill-timeline")
    for label, rep in (("review-1", {"goal_verdict": {"met": False, "misses": ["F1: the total reads $0"]}, "codex": {"read": True, "adopted": 2, "refuted": 1,
                                               "claims": [{"claim": "a.py:3: the total is never set", "verdict": "refuted", "finding": None,
                                                           "reason": "the base did this before the diff"}]},
                                     "findings": [{"id": "F1", "severity": "P1", "title": "The total reads $0", "status": "open"}]}),
                       ("fix-2", {"fixes": [{"finding": "F1", "change": "the total reads Not set up"}], "left_open": []})):
        (d / "seats" / label).mkdir(parents=True, exist_ok=True)
        (d / "seats" / label / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    gates = [{"name": "ruff", "true_exit": 0}, {"name": "backend-tests", "true_exit": 1}, {"name": "tsc", "true_exit": None}]
    t = lambda m: f"2026-10-05T10:{m:02d}:00Z"
    evs = [{"ts": t(0), "kind": "phase", "phase": "opened"},
           {"ts": t(1), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"},
           {"ts": t(20), "kind": "seat", "seat": "build", "attempt": 1, "state": "ok", "minutes": 19, "usage": {"cost_usd": 9.5, "output_tokens": 1200}},
           {"ts": t(20), "kind": "phase", "phase": "build"},
           {"ts": t(21), "kind": "op", "key": "gates-build@a", "state": "started"},
           {"ts": t(24), "kind": "op", "key": "gates-build@a", "state": "complete", "result": gates},
           {"ts": t(25), "kind": "seat", "seat": "fix", "attempt": 1, "state": "started"},
           {"ts": t(30), "kind": "phase", "phase": "opened"},
           {"ts": t(31), "kind": "seat", "seat": "codex", "attempt": 1, "state": "started"},
           {"ts": t(33), "kind": "seat", "seat": "codex", "attempt": 1, "state": "ok", "minutes": 2},
           {"ts": t(33), "kind": "seat", "seat": "review", "attempt": 1, "state": "started"},
           {"ts": t(33), "kind": "seat", "seat": "verify.1", "attempt": 1, "state": "started"},
           {"ts": t(33), "kind": "seat", "seat": "verify.2", "attempt": 1, "state": "started"},
           {"ts": t(36), "kind": "question", "seat": "review", "attempt": 1, "text": "Is $0 ever right?"},
           {"ts": t(40), "kind": "seat", "seat": "review", "attempt": 1, "state": "ok", "minutes": 7, "usage": {"cost_usd": 3}},
           {"ts": t(41), "kind": "seat", "seat": "fix", "attempt": 2, "state": "started"},
           {"ts": t(45), "kind": "seat", "seat": "fix", "attempt": 2, "state": "ok", "minutes": 4},
           {"ts": t(46), "kind": "op", "key": "gates-suite@b", "state": "started"},
           {"ts": t(48), "kind": "op", "key": "gates-suite@b", "state": "complete", "result": [{"name": "backend-tests", "true_exit": 0}]}]
    steps = timeline.collect(d, evs, "slice", running=True)
    by = {s["id"]: s for s in steps}
    f1 = (by.get("review-1", {}).get("detail", {}).get("findings") or [{}])[0]
    check("timeline: a finding reads the state its review gave it, never a guess from a later line",
          f1.get("status") == "open" and not f1.get("closed_by"), str(f1))
    g = by.get("checks-4", {})
    check("timeline: a round of checks names its red and its unmeasured checks",
          g.get("summary") == "1 passed, 1 red, 1 measured nothing" and g.get("detail", {}).get("red") == ["backend-tests"]
          and g.get("station") == 2 and g.get("state") == "red", str(g)[:300])
    check("timeline: a seat the run's restart cut short says so and ends at the restart",
          by.get("fix-1", {}).get("state") == "cut" and by["fix-1"]["end"] == t(30), str(by.get("fix-1"))[:300])
    r = by.get("review-1", {})
    check("timeline: the review step carries its findings, its verdict and the question it raised",
          str(r.get("summary")).startswith("1 finding, goal not met yet") and r["detail"]["findings"][0]["title"] == "The total reads $0"
          and r.get("questions") == ["Is $0 ever right?"] and r.get("usd") == 3 and r.get("station") == 3, str(r)[:400])
    check("timeline: the second reader reads what the reviewer kept of it",
          by.get("codex-1", {}).get("summary") == "2 points kept by the reviewer, 1 set aside", str(by.get("codex-1"))[:300])
    check("timeline: the second reader's step lists each claim with its verdict and the reviewer's reason",
          [k.get("reason") for k in by.get("codex-1", {}).get("detail", {}).get("claims") or []] == ["the base did this before the diff"],
          str(by.get("codex-1"))[:300])
    check("timeline: a fix step lists what it changed, at the Fix station",
          by.get("fix-2", {}).get("detail", {}).get("fixes") == [{"finding": "F1", "change": "the total reads Not set up"}]
          and by["fix-2"]["station"] == 4, str(by.get("fix-2"))[:300])
    check("timeline: testers working beside the reviewer sit in their own lanes and are still working",
          len({by[k]["lane"] for k in ("review-1", "verify.1-1", "verify.2-1")}) == 3
          and by["verify.1-1"]["state"] == "running" and by["verify.1-1"]["end"] is None, str([(s["id"], s["lane"]) for s in steps]))
    ended = {s["id"]: s for s in timeline.collect(d, evs, "slice", running=False)}
    check("timeline: on a run that ended, a seat that never reported is cut, not working",
          ended["verify.1-1"]["state"] == "cut", str(ended["verify.1-1"])[:200])
    time.sleep(0.02)
    (d / "seats" / "fix-2" / "report.json").write_text(json.dumps({"fixes": [], "left_open": ["F1: needs a ruling"]}), encoding="utf-8")
    again = {s["id"]: s for s in timeline.collect(d, evs, "slice", running=True)}
    check("timeline: a report that changed is read again", again["fix-2"]["summary"] == "0 fixes, 1 left open", str(again["fix-2"])[:200])


def drill_nonwaivable_answered(repo):
    """A nonwaivable the owner answered by id reaches the fixer; stopping again would
    re-ask a question already ruled, and the resume would re-raise it forever."""
    rid = "drill-nonwaivable-answered"
    (run_dir(repo, rid) / "control.txt").write_text("answer: F1: scope the query by tenant\n", encoding="utf-8")
    finding = {"id": "F1", "axis": "correctness", "severity": "P0", "class": "nonwaivable",
               "rule": "tenant isolation", "confidence": 9, "title": "query without tenant scope", "summary": "query without tenant scope",
               "quote": {"file": "src/x.py", "line": 1, "text": "x = 1"}, "absence": None,
               "recurrence": "n/a", "status": "open", "fixed_at": None}
    fixed = dict(finding, status="fixed", fixed_at="x.py:1 @ tip")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    "review.1": {"mode": "ok", "report": {"findings": [finding]}},
                                    "fix": {"mode": "ok", **TOUCH, "report": {"fixes": [
                                        {"finding": "F1", "red_first": "test_x.py", "change": "tenant scope", "pinned_by": "test_x.py"}]}},
                                    "review.2": {"mode": "ok", "report": {"scope": "the fix", "findings": [fixed]}}},
                             extra=("--resume", rid))
    evs = journal(repo, rid)
    check("nonwaivable-answered: not stopped", not any("nonwaivable" in s for s in js.get("stops", [])), raw[-400:])
    check("nonwaivable-answered: fixer ran", any(s["state"] == "ok" for s in seats(evs, "fix")))
    fix_brief = run_dir(repo, rid) / "seats" / "fix-1" / "brief.md"
    text = fix_brief.read_text(encoding="utf-8") if fix_brief.exists() else ""
    check("nonwaivable-answered: the fixer reads the answer beside its finding id",
          "Named by the conductor: fix first" in text.split("Answers from the owner")[0]
          and "scope the query by tenant" in text.split("Answers from the owner")[0], text[:800])
    check("nonwaivable-answered: shipped", js.get("verdict") == "mergeable", raw[-400:])


def drill_nonwaivable_other_id(repo):
    """An answer naming another finding leaves this nonwaivable stopped, with its own id."""
    rid = "drill-nonwaivable-other-id"
    (run_dir(repo, rid) / "control.txt").write_text("answer: F2: scope the query by tenant\n", encoding="utf-8")
    finding = {"id": "F1", "axis": "correctness", "severity": "P0", "class": "nonwaivable",
               "rule": "tenant isolation", "confidence": 9, "title": "query without tenant scope", "summary": "query without tenant scope",
               "quote": {"file": "src/x.py", "line": 1, "text": "x = 1"}, "absence": None,
               "recurrence": "n/a", "status": "open", "fixed_at": None}
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    "review": {"mode": "ok", "report": {"findings": [finding]}}}, extra=("--resume", rid))
    check("nonwaivable-other-id: stopped on [F1]", any(s.startswith("[F1] nonwaivable") for s in js.get("stops", [])), raw[-400:])
    check("nonwaivable-other-id: no fixer", not seats(journal(repo, rid), "fix"))


def drill_gate_answered(repo):
    """A red gate is a fix item, never a stop: the fix pass sits on it, and
    a gate still red after that pass ships red, named, with no G- stop for anyone to answer."""
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    c["gates"] = [{"name": "redgate", "cmd": ["python", "-c", "import sys; print('red'); sys.exit(1)"]}, {"name": "suite", "cmd": ["python", "-c", "print('suite ok')"], "heavy": True}]
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("commit", "-qam", "a gate that is red"); g("push", "-q", "origin", "dev")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    # a valid product red stands under a cause; the fix does not turn it green
                                    "fix": {"mode": "ok", **TOUCH, "report": {"causes": [
                                        {"id": "C1", "invariant": "the gate is green", "boundary": {"module": "x", "decider": "x.py:1"},
                                         "rows": ["redgate"], "competing_deciders": [], "folded": [],
                                         "pin": {"path": "x.py", "failed_before": "red"}, "change": "tried"}], "left_open": []}}})
    evs = journal(repo, js.get("run_id", ""))
    check("red gate: never a stop", not js.get("stops") and js.get("verdict") != "stopped", raw[-400:])
    check("red gate: the fix pass sat on it", any(s_["state"] == "ok" for s_ in seats(evs, "fix")), str([(x["seat"], x["state"]) for x in seats(evs)]))
    check("red gate: still red after the pass, the run ships red", js.get("verdict") == "run-complete", raw[-300:])
    check("red gate: the loop ended on what it could not move",
          any(e.get("kind") == "loop" and e.get("result") in ("ended", "moved-nothing") for e in evs), str([e for e in evs if e.get("kind") == "loop"])[-300:])


def drill_every_gate_unmeasured_stops_without_a_fixer(repo):
    """A gate round where no gate printed a TRUE_EXIT line means the runner lost its tools
    (its parent shell was killed): the run stops with an id'd line naming --resume, and no
    fix seat is ever handed a gate that measured nothing as a red to turn green."""
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    # run-gate refuses this shape before running it, so it prints no TRUE_EXIT line
    c["gates"] = [{"name": f"blind{i}", "cmd": ["bash", "-c", "true; echo done"]} for i in (1, 2)] + [{"name": "suite", "cmd": ["python", "-c", "print('suite ok')"], "heavy": True}]
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("commit", "-qam", "gates that measure nothing"); g("push", "-q", "origin", "dev")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}})
    rid = js.get("run_id", "")
    evs = journal(repo, rid)
    stops = " ".join(js.get("stops") or [])
    check("lost tools: no fix seat is started on gates that measured nothing", not seats(evs, "fix"), raw[-400:])
    check("lost tools: the run stops with an id'd line naming the resume",
          js.get("verdict") == "stopped" and stops.startswith("[S-") and "lost its tools" in stops
          and f"--resume {rid}" in stops, raw[-400:])
    from hb.gates import gating
    mixed = [{"name": "red", "true_exit": 1}, {"name": "blind", "true_exit": None, "unmeasured": "no TRUE_EXIT line"}]
    check("lost tools: a fixer is handed only the gates that measured red",
          [x["name"] for x in getattr(gating, "gates_to_fix", gating.gates_red)(mixed)] == ["red"])


def drill_empty_tip_stops_before_measuring(repo):
    """A gate round whose tree tip cannot be read (git no longer runs for the runner) is a
    rig fault: it raises before any gate runs, so no journal key is ever written with an
    empty tip and nothing downstream reads the round as reds."""
    from hb.gates import gating
    from hb.record.journal import Journal
    lost = repo.parent / "not-a-tree"
    lost.mkdir()
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j = Journal("drill-empty-tip")
        try:
            gating.measure(j, lost, "x", "0" * 40, [], {"branches": {"landing": "dev"}, "state_dir": str(repo / ".context" / "harness")}, "gates-build",
                           only=[{"name": "ok", "cmd": ["python", "-c", "print('ok')"]}])
            raised = None
        except Exception as ex:
            raised = ex
        keys = [e.get("key") for e in j.events() if e.get("kind") == "op"]
    finally:
        os.chdir(here)
    check("empty tip: the round raises a rig fault", raised is not None and raised.__class__.__name__ == "RigFault",
          repr(raised))
    check("empty tip: no gate key is journaled with an empty tip", "gates-build@" not in keys, str(keys))


def drill_detached_run_outlives_its_launcher(repo):
    """`--detach` starts the run as its own process apart from the caller's shell, so a
    killed conductor shell cannot take its tools: the launch returns the run id, pid and
    out file at once, the run ends on its own, and the conductor's Claude variables are gone."""
    t0 = time.time()
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}}, extra=("--detach",),
                             env_extra={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "conductor"})
    rid, outp = js.get("run_id", ""), js.get("out") or ""
    check("detach: the launch returns the run id, pid and out file",
          rc == 0 and js.get("verdict") == "detached" and rid and js.get("pid") and outp, raw[-400:])
    final = {}
    while rid and time.time() - t0 < 300 and not final.get("verdict"):
        time.sleep(1)
        final = last_json(Path(outp).read_text(encoding="utf-8", errors="replace")) if Path(outp).exists() else {}
    check("detach: the detached run ends on its own under the same run id",
          final.get("verdict") == "mergeable" and final.get("run_id") == rid, str(final)[:300])
    stop_pid(js.get("page_started"))
    from hb.runner import detach
    env = detach.child_env({"CLAUDECODE": "1", "CLAUDE_PID": "7", "PATH": "p"})
    check("detach: the conductor's Claude variables are stripped", env == {"PATH": "p"}, str(env))


def drill_detach_starts_the_page_when_it_is_down(repo):
    """The page server died and nothing started it again, so the link the owner had was
    dead. A detached launch starts the page when its port does not answer and prints the
    run's address; `hb page --detach` on a port that answers starts nothing."""
    import urllib.request
    port = json.loads((repo / ".claude" / "harness-config.json").read_text(encoding="utf-8"))["page"]["port"]
    pid = None
    try:
        rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}}, extra=("--detach",))
        rid, pid = js.get("run_id", ""), js.get("page_started")
        check("page: a detached launch prints the page's one address, the home, not a link to one run",
              rc == 0 and js.get("page") == f"http://127.0.0.1:{port}/", raw[-400:])
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=10) as r:
                served = r.status == 200 and "live" in json.loads(r.read())
        except (OSError, ValueError) as ex:
            served = False
            raw = repr(ex)
        check("page: the launch started the page server and it answers", bool(pid) and served, f"pid {pid}: {raw[-300:]}")
        rc2, o2, e2 = sh([sys.executable, HB, "page", "--detach", "--port", str(port)], cwd=str(repo))
        again = last_json(o2)
        check("page: --detach on a port that answers starts nothing",
              rc2 == 0 and again.get("verdict") == "running" and again.get("started") is None, (o2 + e2)[-300:])
        final, t0 = {}, time.time()
        outp = js.get("out") or ""
        while rid and time.time() - t0 < 300 and not final.get("verdict"):
            time.sleep(1)
            final = last_json(Path(outp).read_text(encoding="utf-8", errors="replace")) if Path(outp).exists() else {}
    finally:
        stop_pid(pid)


WORKFLOW = """name: CI
on:
  pull_request:
  push:
    branches: [dev]
jobs:
  unit:
    name: Unit
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Load shared CI env
        run: grep -E '^[A-Za-z_]+=' ci/test.env >> "$GITHUB_ENV"
      - name: Layer gate
        run: |
          # pure AST, no database
          python tools/gates/layers.py
      - name: Pick the tests
        run: |
          python tools/tests/pick.py > "$RUNNER_TEMP/selection.txt"
"""


NPM_WORKFLOW = """name: Web
on: [pull_request]
jobs:
  web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - name: Style lint
        run: npm run lint:style
      - name: Unit tests
        run: npm run test:unit
"""


def drill_ci_parity(repo):
    """The runner's green is CI's green: a tools/ script the product's workflow runs is a gate
    here, read from the workflow. A red one refuses the PR and names the gate; once the
    script passes the PR opens, and its body lists every CI-parity gate with its TRUE_EXIT,
    a step only CI's runner can run named UNMEASURED. An `npm run` step is in the one
    inventory too, a lint unless it runs tests, so a style lint is measured where CI runs it."""
    sys.path.insert(0, str(HERE.parent / "engine"))
    from hb.gates import ciparity
    with tempfile.TemporaryDirectory(prefix="hb-npm-") as tmp:
        wf = Path(tmp) / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "web.yml").write_text(NPM_WORKFLOW, encoding="utf-8")
        got = {g_["name"]: g_ for g_ in ciparity.derive(tmp, script_prefixes=["npm run"])["gates"]}
    style = got.get("ci: lint:style") or {}
    check("ci-parity: an npm run step is a gate in the one inventory, with its working directory",
          style.get("cmd") == ["npm", "run", "lint:style"] and style.get("cwd") == "web", str(got))
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    c["ci"] = {"script_prefixes": ["tools/"]}
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    (repo / "ci").mkdir()
    (repo / "ci" / "test.env").write_text("# shared\nDRILL_CI_ENV=loaded\n", encoding="utf-8")
    (repo / "tools" / "gates").mkdir(parents=True)
    gate = repo / "tools" / "gates" / "layers.py"
    gate.write_text("import os, sys\nprint('layers', os.environ.get('DRILL_CI_ENV'))\n"
                    "sys.exit(0 if os.environ.get('DRILL_CI_ENV') == 'loaded' and os.path.exists('ok.flag') else 1)\n",
                    encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "ci"); g("push", "-q", "origin", "dev")
    env = {"FAKE_SCRIPT": str(repo / ".context" / "fake-script.json"), "HB_GH_STUB": str(FIXTURE / "gh_stub.py")}
    (repo / ".context").mkdir(exist_ok=True)
    (repo / ".context" / "fake-script.json").write_text(json.dumps({"build": {"mode": "ok", **TOUCH}}), encoding="utf-8")
    rc, o, e = sh([sys.executable, HB, "run", "hotfix", "the layers are crossed", "--no-venue", "--rate-limit-wait-min", "0"],
                  cwd=str(repo), env=env)
    js = last_json(o)
    evs = journal(repo, js.get("run_id", ""))
    check("ci-parity: a red CI script refuses the PR", js.get("verdict") == "stopped" and not js.get("pr")
          and not any(str(x.get("key", "")).startswith("pr") for x in evs if x.get("kind") == "op"), (o + e)[-500:])
    check("ci-parity: the stop names the gate", any("ci: tools/gates/layers.py" in s for s in js.get("stops") or []),
          str(js.get("stops")))
    (repo / "ok.flag").write_text("x", encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "the layers hold"); g("push", "-q", "origin", "dev")
    rc, o, e = sh([sys.executable, HB, "run", "hotfix", "the layers hold now", "--no-venue", "--rate-limit-wait-min", "0"],
                  cwd=str(repo), env=env)
    js = last_json(o)
    check("ci-parity: once the script passes the PR opens", js.get("verdict") == "mergeable" and js.get("pr") == "https://example.invalid/pr/1",
          (o + e)[-500:])
    body = run_dir(repo, js.get("run_id", "")) / "evidence" / "record.md"
    text = body.read_text(encoding="utf-8") if body.exists() else ""
    parity = text.split("## CI parity")[-1].split("## ")[0] if "## CI parity" in text else ""
    check("ci-parity: the record lists the gate with its TRUE_EXIT", "ci: tools/gates/layers.py: TRUE_EXIT=0" in parity, text[-900:])
    check("ci-parity: a step only CI can run is named UNMEASURED", "ci: Pick the tests: UNMEASURED" in parity and "$RUNNER_TEMP" in parity,
          parity)


def drill_replay_stale_base(repo):
    """A worktree left at the old fixed base path (a crash, a fixer's own replay) made
    every later replay unmeasured, so old reds counted against the slice."""
    from hb.runner import hbcall
    from hb.gates import gating
    sha = sh(["git", "-C", str(repo), "rev-parse", "HEAD"])[1].strip()
    wt = repo.parent / "wt"
    sh(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt), sha])
    sh(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(repo.parent / "wt-base"), sha])
    seen = {}
    real_db, real_hb = gating.test_db, hbcall.hb
    gating.test_db = lambda tmp, verb, slug: {}
    def fake_hb(args, cwd, timeout=1800, env=None):
        seen["cwd"] = str(cwd)
        return 1, {}, "TRUE_EXIT=1\nRED_NODEID=tests/test_a.py::test_x\n"
    hbcall.hb = fake_hb
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        j = SimpleNamespace(op=lambda key, fn: fn(), fresh_key=lambda key, landed: key, run_id="drill",
                            append=lambda *a, **k: None)
        rep = gating.replay_at_base(j, str(wt), sha, {"name": "backend-tests", "red_ids": ["tests/test_a.py::test_x"]}, "drill")
    finally:
        gating.test_db, hbcall.hb = real_db, real_hb
        os.chdir(here)
    check("replay-stale-base: measured past a stale copy", rep.get("measured") is True, str(rep))
    check("replay-stale-base: the red is found at base", rep.get("red_at_base") == ["tests/test_a.py::test_x"], str(rep))
    check("replay-stale-base: its own copy is removed after", not Path(seen.get("cwd", "x")).exists(), seen.get("cwd", ""))


def drill_replay_unmeasured_retried(repo):
    """A replay at base that measured nothing was cached under its key, so every later ask
    in the run got the same blank and old reds counted against the change. An unmeasured
    replay is run again; a measured one is never repeated."""
    from hb.runner import hbcall
    from hb.gates import gating
    from hb.record.journal import Journal
    sha = sh(["git", "-C", str(repo), "rev-parse", "HEAD"])[1].strip()
    wt = repo.parent / "wt"
    sh(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt), sha])
    answers = ["run-gate printed nothing", "TRUE_EXIT=1\nRED_NODEID=tests/test_a.py::test_x\n"]
    calls = []
    real_db, real_hb = gating.test_db, hbcall.hb
    gating.test_db = lambda tmp, verb, slug: {}

    def fake_hb(args, cwd, timeout=1800, env=None):
        calls.append(str(cwd))
        return 1, {}, answers[min(len(calls), len(answers)) - 1]
    hbcall.hb = fake_hb
    gate = {"name": "backend-tests", "red_ids": ["tests/test_a.py::test_x"]}
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        j = Journal("drill-replay-unmeasured", c={"state_dir": str(repo / ".context" / "harness")})
        first = gating.replay_at_base(j, str(wt), sha, gate, "drill")
        second = gating.replay_at_base(j, str(wt), sha, gate, "drill")
        third = gating.replay_at_base(j, str(wt), sha, gate, "drill")
    finally:
        gating.test_db, hbcall.hb = real_db, real_hb
        os.chdir(here)
    check("replay-unmeasured: the first replay measured nothing", first.get("measured") is False, str(first))
    check("replay-unmeasured: the next ask runs it again and measures", second.get("measured") is True
          and second.get("red_at_base") == ["tests/test_a.py::test_x"], str(second))
    check("replay-unmeasured: a measured replay is not run a third time", len(calls) == 2 and third == second, f"{len(calls)} runs")


def drill_wave_slice_never_began(repo):
    """Five slices raced one git config lock and one stopped before its worktree existed;
    the wave cached that stop for good. A slice that never began is read again from its
    log when it was relaunched into the wave, and a finished slice is never rerun."""
    rid = "drill-wave-began"
    rd = run_dir(repo, rid)
    stop = {"issue": "7", "verdict": "crashed", "branch": None, "stops": []}
    done = {"issue": "8", "verdict": "mergeable", "branch": "slice/issue-8", "stops": []}
    ruling = {"issue": "9", "verdict": "stopped", "branch": "slice/issue-9", "stops": ["migration: x"]}
    nl = chr(10)
    (rd / "journal.jsonl").write_text("".join(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "run_id": rid, "key": k, "state": "complete", "result": r,
                    "kind": "op"}) + nl for k, r in (("slice-7", stop), ("slice-8", done), ("slice-9", ruling))), encoding="utf-8")
    (rd / "slice-7.out").write_text("log" + nl + json.dumps({"verdict": "mergeable", "branch": "slice/issue-7",
                                                              "tip": "abc", "run_id": "r7"}) + nl, encoding="utf-8")
    sys.path.insert(0, str(ENGINE))
    from hb.record.journal import Journal
    try:
        from hb.beats import wave as W
    except ImportError:
        print("  SKIP  wave slice never began: this cut ships no wave door")
        return
    import os as _os
    here = _os.getcwd()
    _os.chdir(str(repo))
    spawned = []
    real = W.subprocess
    W.subprocess = SimpleNamespace(STDOUT=real.STDOUT, Popen=lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(RuntimeError("spawned")))
    try:
        j = Journal(rid)
        a = SimpleNamespace(seat_timeout_min=1, rate_limit_wait_min=1, shadow=False, runtime=None, no_venue=True)
        res = W._spawn_slices(j, [{"number": 7}, {"number": 8}, {"number": 9}], "drill", a, rd)
    finally:
        W.subprocess = real
        _os.chdir(here)
    by = {r["issue"]: r for r in res}
    check("wave-slice-never-began: nothing is spawned", not spawned, str(spawned))
    check("wave-slice-never-began: a slice stopped for a ruling stays parked", by.get("9", {}).get("verdict") == "stopped", str(by.get("9")))
    check("wave-slice-never-began: the relaunched slice is read from its log",
          by.get("7", {}).get("verdict") == "mergeable" and by["7"].get("branch") == "slice/issue-7", str(by.get("7")))
    check("wave-slice-never-began: a finished slice keeps its record", by.get("8", {}).get("branch") == "slice/issue-8")
    check("wave-slice-never-began: the lock race is retried in the worktree add",
          'racing = "lock" in e or "commondir" in e' in (ENGINE / "hb" / "rig" / "gitops.py").read_text(encoding="utf-8"))


VENUE_STUB = """import json, sys
from pathlib import Path
plan_path = Path(__file__).with_name("venue-stub.json")
plan = json.loads(plan_path.read_text(encoding="utf-8"))
verb = sys.argv[1]
steps = plan.get(verb) or [{"print": "{}"}]
step = steps.pop(0) if len(steps) > 1 else steps[0]
plan[verb] = steps
plan.setdefault("calls", []).append(sys.argv[1:])
plan_path.write_text(json.dumps(plan), encoding="utf-8")
print(step.get("print", ""))
sys.exit(step.get("exit", 0))
"""


def _venue_stub(repo, capabilities, **answers):
    """A venue block whose every command is one stub script, answering each verb from `answers`
    in turn (the last answer repeats); the verbs it is given and the capabilities it declares are
    the only ones the config names."""
    stub = repo / ".context" / "venue_stub.py"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(VENUE_STUB, encoding="utf-8")
    plan = {"capabilities": [{"print": json.dumps(capabilities)}], **answers}
    (stub.parent / "venue-stub.json").write_text(json.dumps(plan), encoding="utf-8")
    line = f"{sys.executable.replace(chr(92), '/')} {stub.as_posix()}"
    venue = {"capabilities": f"{line} capabilities", "up": f"{line} up {{name}}", "down": f"{line} down {{name}}",
             "list": f"{line} list"}
    placeholders = {"database_lease": "{slug}", "database_release": "{slug}", "purge": "{name}"}
    for verb in answers:
        venue[verb] = f"{line} {verb} {placeholders.get(verb, '')}".rstrip()
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    c["venue"] = venue
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")


def _venue_stub_calls(repo, verb):
    plan = json.loads((repo / ".context" / "venue-stub.json").read_text(encoding="utf-8"))
    return [c for c in plan.get("calls") or [] if c[0] == verb]


def drill_lease_busy_retried(repo):
    """Two wave slices stopped with the suite unmeasured: a sibling was cloning the same
    template, then the database was restarting. The lease is the venue's `database_lease`; a
    refusal a product signature names busy is tried again, any other refusal is returned at
    once, and a venue that does not offer `database` is asked nothing."""
    from hb.gates import gating
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    c["gates"] = {"commands": c["gates"], "signatures": [
        {"pattern": "being accessed by other users|starting up", "cause": "database-busy", "recovery": "lease"}]}
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    real_sleep = gating.time.sleep
    gating.time.sleep = lambda s: None
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        _venue_stub(repo, ["database"], database_lease=[
            {"print": json.dumps({"why": "source database is being accessed by other users"}), "exit": 1},
            {"print": json.dumps({"why": "the database system is starting up"}), "exit": 1},
            {"print": json.dumps({"url": "postgresql://x/y"})}], database_release=[{"print": "{}"}])
        got = gating.lease_with_retry(str(repo), "drill")
        busy_calls = _venue_stub_calls(repo, "database_lease")
        _venue_stub(repo, ["database"], database_lease=[{"print": json.dumps({"why": "the migrations failed"}), "exit": 1}],
                    database_release=[{"print": "{}"}])
        other = gating.lease_with_retry(str(repo), "drill")
        other_calls = _venue_stub_calls(repo, "database_lease")
        _venue_stub(repo, [])
        absent = gating.lease_with_retry(str(repo), "drill")
    finally:
        gating.time.sleep = real_sleep
        os.chdir(here)
    check("lease-busy: a busy template is waited out through venue.database_lease",
          got.get("url") == "postgresql://x/y" and busy_calls == [["database_lease", "drill"]] * 3, str((got, busy_calls)))
    check("lease-busy: another refusal is not retried", len(other_calls) == 1 and not other.get("url"), str((other, other_calls)))
    check("lease-busy: a venue without database is not offered one", not absent.get("url")
          and "not offered: database" in json.dumps(absent), str(absent))


# The suite holds its slot until the other run has provably arrived, so the drill reads the
# queue and never the machine's speed: with one slot it ends once a run journals that it is
# waiting, with two once both suites have started. A minute with no such sign ends it anyway
# and the checks below fail on what they see.
SLEEPY_SUITE = """import glob, os, sys, time
logs, slots, runs = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.makedirs(logs, exist_ok=True)
me = str(os.getpid())
open(os.path.join(logs, me + ".start"), "w").write(os.getcwd())


def others(suffix):
    return {os.path.basename(f).split(".")[0] for f in glob.glob(os.path.join(logs, "*" + suffix))} - {me}


def other_arrived():
    if slots > 1:
        return bool(others(".start"))
    for path in glob.glob(os.path.join(runs, "*", "journal.jsonl")):
        with open(path, encoding="utf-8", errors="replace") as f:
            if '"state": "waiting"' in f.read():
                return True
    return False


# a polling fuse, never a measurement: the drill reads only the markers
for _ in range(600):
    if other_arrived():
        break
    time.sleep(0.1)
# did another suite run while this one ran: it started and has not ended
running = others(".start") - others(".end")
open(os.path.join(logs, me + ".saw"), "w").write("other-running" if running else "alone")
open(os.path.join(logs, me + ".end"), "w").write(os.getcwd())
print("suite ok")
"""


def _two_heavy_runs(repo, free_gb):
    """Two hotfix runs at once on one machine, each with a heavy suite measured at 1 GB that
    sleeps and logs its start and end, on a host reading of `free_gb`. Returns ({worktree:
    [start, end]}, [journal of each run], outputs)."""
    log = repo.parent / "suite-times"
    suite = repo.parent / "sleepy_suite.py"
    suite.write_text(SLEEPY_SUITE, encoding="utf-8")
    conf = repo / ".claude" / "harness-config.json"
    c = json.loads(conf.read_text(encoding="utf-8"))
    c["gates"] = [{"name": "trivial", "cmd": ["python", "-c", "print('gate ok')"]},
                  {"name": "suite", "cmd": [sys.executable, str(suite), str(log), str(2 if free_gb >= 2 else 1),
                                                 str(repo / ".context" / "harness" / "runs")], "heavy": True}]
    c["machine"] = {"poll_s": 0.2, "settle_s": 0.2, "force_after_s": 1}
    conf.write_text(json.dumps(c, indent=2), encoding="utf-8")
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("commit", "-qam", "a heavy suite"); g("push", "-q", "origin", "dev")
    sp = repo / ".context" / "fake-script.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"build": {"mode": "ok", **TOUCH}}), encoding="utf-8")
    env = dict(os.environ, FAKE_SCRIPT=str(sp), HB_GH_STUB=str(FIXTURE / "gh_stub.py"), HB_MEMORY_FREE_GB=str(free_gb))
    procs = [subprocess.Popen([sys.executable, HB, "run", "hotfix", ref, "--shadow", "--no-venue", "--rate-limit-wait-min", "0"],
                              cwd=str(repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace")
             for ref in ("the first heavy run", "the second heavy run")]
    outs = [p.communicate(timeout=600)[0] for p in procs]
    # {pid: what each suite saw while it ran, "alone" or "other-running"}, from its own marker
    saw = {f.stem: f.read_text(encoding="utf-8") for f in sorted(log.glob("*.saw"))} if log.is_dir() else {}
    return saw, [journal(repo, last_json(o).get("run_id", "")) for o in outs], outs


def drill_heavy_suites_queue_by_room(repo):
    """Five backend suites at once on one machine went red for the machine's reasons. Under
    the line, two runs' suites never overlap: the first starts on the empty host after the
    forced wait, the second waits while it runs and journals whom it waits for, and admission
    is the only door (no configured slot count)."""
    saw, journals, outs = _two_heavy_runs(repo, 1.5)
    check("room for one: both suites ran", len(saw) == 2, str(saw) + "".join(o[-300:] for o in outs))
    check("room for one: neither suite saw the other running", len(saw) == 2 and set(saw.values()) == {"alone"}, str(saw))
    waits = [e for evs in journals for e in evs if e.get("kind") == "admission" and e.get("state") == "waiting"]
    runs = [evs[0].get("run_id") for evs in journals if evs]
    check("room for one: the waiting run journals whom it waits for, with its reason",
          any(w.get("why") and any(r in " ".join(w.get("held_by") or []) for r in runs) for w in waits),
          str(waits)[:400])
    check("room for one: no machine slot is taken for room",
          not any(e.get("kind") in ("machine-slot", "machine-wait") and e.get("pool") == "heavy" for evs in journals for e in evs))


def drill_heavy_suites_overlap_with_room(repo):
    """Above the line, two runs' suites run at once."""
    saw, journals, outs = _two_heavy_runs(repo, 64)
    check("room for two: both suites ran", len(saw) == 2, str(saw) + "".join(o[-300:] for o in outs))
    check("room for two: a suite saw the other running beside it", "other-running" in saw.values(), str(saw))


def drill_template_slot_stale_taken_over(repo):
    """A run that crashed holding the template slot must not hold every later clone forever: a
    slot whose pid is dead, or whose heartbeat stopped, is taken over; a live one is waited for."""
    from hb.venue import machine
    import types
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    # a fake clock: the slot reads the time and sleeps through it, so an hour-old heartbeat and a
    # wait past its fuse are numbers the drill sets, never the host's seconds
    clock = {"now": 1_000_000.0}
    fake = types.SimpleNamespace(time=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s))
    saved_time, machine.time = machine.time, fake
    _t = fake
    c = {"state_dir": ".context/harness", "machine": {"poll_s": 0.1, "stale_after_s": 60}}
    here = os.getcwd()
    os.chdir(str(repo))
    notes = []
    note = lambda kind, **f: notes.append(dict(f, kind=kind))
    took_dead = took_hung = waited_live = False
    try:
        slot0 = repo / ".context" / "harness" / "machine" / "template.0.json"
        slot0.parent.mkdir(parents=True, exist_ok=True)
        slot0.write_text(json.dumps({"holder": "crashed-run", "pid": dead.pid, "token": "x", "beat": _t.time()}), encoding="utf-8")
        with machine.slot("template", "drill-dead", c=c, note=note, timeout_s=5):
            took_dead = json.loads(slot0.read_text(encoding="utf-8")).get("holder") == "drill-dead"
        slot0.write_text(json.dumps({"holder": "hung-run", "pid": os.getpid(), "token": "y", "beat": _t.time() - 3600}), encoding="utf-8")
        with machine.slot("template", "drill-hung", c=c, note=note, timeout_s=5):
            took_hung = True
        slot0.write_text(json.dumps({"holder": "live-run", "pid": os.getpid(), "token": "z", "beat": _t.time()}), encoding="utf-8")
        try:
            with machine.slot("template", "drill-live", c=c, note=note, timeout_s=0.5):
                pass
        except TimeoutError:
            waited_live = True
    finally:
        machine.time = saved_time
        os.chdir(here)
    takeovers = [n for n in notes if n["kind"] == "machine-takeover"]
    check("template stale: a slot held by a dead pid is taken over", took_dead and bool(takeovers)
          and takeovers[0].get("stale_holder") == "crashed-run" and "not running" in takeovers[0].get("why", ""), str(notes)[:400])
    check("template stale: a slot whose heartbeat stopped is taken over", took_hung and len(takeovers) == 2
          and "heartbeat" in takeovers[1].get("why", ""), str(takeovers)[:400])
    check("template stale: a live holder is waited for, and the wait names it", waited_live
          and any(n["kind"] == "machine-wait" and n.get("waiting_for") == ["live-run"] for n in notes), str(notes)[-300:])


def drill_template_clone_takes_a_slot(repo):
    """Two runs cloning one template at once were refused by Postgres; every lease takes the
    template slot, so a second lease waits while one is cloning."""
    from hb.gates import gating
    from hb.venue import machine
    import threading
    import time as _t
    here = os.getcwd()
    os.chdir(str(repo))
    inside, peak, lock = [0], [0], threading.Lock()
    real_db = gating.test_db

    def slow_lease(wt, verb, slug):
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        _t.sleep(0.8)  # room for a second clone to come in, if the slot let it
        with lock:
            inside[0] -= 1
        return {"url": f"postgresql://x/{slug}"}
    gating.test_db = slow_lease
    try:
        threads = [threading.Thread(target=gating.lease_with_retry, args=("wt", f"t{i}")) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        gating.test_db = real_db
        os.chdir(here)
    check("template slot: two clones never inside at once", peak[0] == 1, str(peak))


def drill_seat_report_in_the_windows_code_page_is_read(repo):
    """A shard seat wrote valid JSON in the Windows code page (a middle dot, byte 0xb7) and
    the strict UTF-8 read raised UnicodeDecodeError twice, so a finished drive counted as a
    crash and its row went UNMEASURED. A report is read whatever its codec; the JSON judges it."""
    from hb.rig.common import report_text
    from hb.runner import seat
    d = repo.parent / "codec"
    d.mkdir(exist_ok=True)
    body = {"schema": "verify-report/v2", "items": [{"item": "T-01", "state": "WORKS", "observed": "11:00 · on the floor"}]}
    text = json.dumps(body, ensure_ascii=False)
    (d / "cp1252.json").write_bytes(text.encode("cp1252"))
    (d / "utf16.json").write_bytes(text.encode("utf-16"))
    (d / "bom.json").write_bytes(text.encode("utf-8-sig"))
    (d / "utf8.json").write_bytes(text.encode("utf-8"))
    for name in ("cp1252", "utf16", "bom", "utf8"):
        got = json.loads(report_text(d / f"{name}.json"))
        check(f"codec: a report written as {name} is read and its words survive", got["items"][0]["observed"] == "11:00 · on the floor", str(got)[:200])
    (d / "broken.json").write_bytes(b"{\xff\xfe\xfd not json")
    try:
        json.loads(report_text(d / "broken.json"))
        decoded = "parsed"
    except json.JSONDecodeError:
        decoded = "json-refused"
    except UnicodeDecodeError:
        decoded = "decode-raised"
    check("codec: bytes no codec can read never raise a decode error; the JSON parse refuses them", decoded == "json-refused", decoded)
    check("codec: the seat reads its report through the same reader", "report_text(out_path)" in Path(seat.__file__).read_text(encoding="utf-8"))


def drill_seat_never_waits_on_the_clock(repo):
    """A verifier slept three hours in a python loop for a timed state to arrive on the wall clock
    while the other shards were done. The seat's shell refuses a command that waits; the brief
    says to build the state at the venue's now."""
    import subprocess
    from hb.runner import adapters as A
    from hb.runner import seatguard
    guard = Path(seatguard.__file__)

    def hook(command, tool="Bash"):
        p = subprocess.run([sys.executable, str(guard)], input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}),
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stderr
    loop = ("python - <<'EOF'\nimport time, datetime as dt\ntarget = dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=53)\n"
            "while dt.datetime.now(dt.timezone.utc) < target: time.sleep(30)\nEOF\ncurl -s http://localhost:53220/version")
    rc, err = hook(loop)
    check("clock: a sleep loop waiting for the wall clock is refused with the way out", rc == 2 and "venue's now" in err, err[-300:])
    check("clock: one long sleep is refused", hook("sleep 300 && curl -s http://localhost:1/x")[0] == 2)
    check("clock: a python sleep of a minute or more is refused", hook("python -c \"import time; time.sleep(120)\"")[0] == 2)
    check("clock: a short single sleep for a server to come up passes", hook("sleep 2 && curl -s http://localhost:1/health")[0] == 0)
    check("clock: a command with no sleep passes", hook("npx vitest run src/a.test.tsx")[0] == 0)
    check("clock: another tool is not the hook's business", hook("sleep 999", tool="Read")[0] == 0)
    path = A.seat_settings(repo.parent / "seat-settings" / "verify-1")
    st = json.loads(path.read_text(encoding="utf-8"))
    pre = (st.get("hooks") or {}).get("PreToolUse") or []
    check("clock: the seat's settings run every Bash call through the guard",
          pre and "Bash" in pre[0].get("matcher", "").split("|") and "seatguard.py" in pre[0]["hooks"][0]["command"], str(st)[:300])
    from hb.runner import briefs as B
    real_role = B.role_text
    B.role_text = lambda role: "role"
    try:
        brief_text = B.render("verify", {}, {"cwd": ".", "evidence_dir": ".", "output_path": "r.json"})
    finally:
        B.role_text = real_role
    check("clock: every brief says never to wait on the clock", B.NO_CLOCK_WAIT in brief_text)


def drill_a_stubbed_venue_takes_the_venue_grant(repo):
    """A stubbed venue skipped its grant, so no drill saw a run deadlock against its own venue.
    A stubbed venue takes the grant the real one takes, with nothing to measure, and holds it
    until it goes down; a run's own venue never blocks its seats."""
    import os
    from hb.venue import admission
    from hb.venue import standin
    real_hold, real_hb = admission.hold, standin.hbcall.hb
    held = []
    admission.hold = lambda kind, holder, c=None, note=None, sample=None, give_up=None: held.append(kind) or (lambda: held.append("released"))
    standin.hbcall.hb = lambda args, cwd, timeout=1800, env=None: (0, {"venue": "v-9-1"}, "")
    j = SimpleNamespace(append=lambda *a, **k: None, op=lambda key, do, check=None: do(), fresh_key=lambda key, landed: key, run_id="r")
    before = os.environ.get("HB_VENUE_STUB")
    try:
        os.environ["HB_VENUE_STUB"] = "1"
        standin.venue_up(j, "9", repo)
        stubbed = list(held)
        os.environ.pop("HB_VENUE_STUB")
        standin.venue_up(j, "9", repo)
        standin._release_venue("v-9-1")
    finally:
        admission.hold, standin.hbcall.hb = real_hold, real_hb
        if before is None:
            os.environ.pop("HB_VENUE_STUB", None)
        else:
            os.environ["HB_VENUE_STUB"] = before
    check("venue grant: a stubbed venue takes the venue grant", stubbed == ["venue"], str(stubbed))
    check("venue grant: a real venue holds a venue grant until it goes down", held == ["venue", "venue", "released"], str(held))


def drill_a_new_red_beside_an_old_one_stays_red(repo):
    """Only the replay at base proves a red pre-existing, test by test: a test red at base is
    the base's; a new red in the same untouched file, green at base, stays the change's red."""
    from hb.rig import gitops
    from hb.gates import gating
    real_replay, real_changed = gating.replay_at_base, gitops.changed_paths
    gating.replay_at_base = lambda j, wt, base, g, slug: {"measured": True, "red_at_base": ["t/test_a.py::x"], "artifact": "a"}
    gitops.changed_paths = lambda wt, base, to="HEAD": ["t/test_b.py"]
    logged = []
    j = SimpleNamespace(append=lambda *a, **k: logged.append((a, k)))
    try:
        g1 = [{"name": "backend-tests", "true_exit": 2, "red_ids": ["t/test_a.py::x", "t/test_a.py::y"]}]
        g1, pre = gating.settle_heavy(j, "wt", "base", "drill", g1, [])
        g2 = [{"name": "backend-tests", "true_exit": 2, "red_ids": ["t/test_a.py::x", "t/test_b.py::z"]}]
        g2, pre2 = gating.settle_heavy(j, "wt", "base", "drill", g2, [])
    finally:
        gating.replay_at_base, gitops.changed_paths = real_replay, real_changed
    check("replay: a new red in a file the branch never touched stays red; only the test red at base is the base's",
          not pre and g1[0]["red_ids"] == ["t/test_a.py::y"] and g1[0].get("red_at_base") == ["t/test_a.py::x"], str((pre, g1)))
    check("replay: a red in a file the branch changed stays red", not pre2 and g2[0]["red_ids"] == ["t/test_b.py::z"], str(g2))


def drill_seat_long_commands(repo):
    """Four seats in one run exited with no report: each ran the suite in the background
    and the CLI cut it 10 minutes after the seat's last reply. A Claude seat now starts
    with both ceilings raised."""
    from hb.runner import adapters as A
    probe = ("import os,json;print(json.dumps({k: os.environ.get(k) for k in "
             "['CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS','BASH_MAX_TIMEOUT_MS']}))")
    real = A.runtimes
    A.runtimes = lambda: {"claude": {"cmd": ["python", "-c", probe], "brief": "stdin",
                                     "browser": "none", "usage": "none"}}
    try:
        out_dir = repo.parent / "run" / "seats" / "fix-1"
        brief = repo.parent / "brief.md"
        brief.write_text("x", encoding="utf-8")
        res = A.launch("claude", "fix", str(brief), str(repo), str(out_dir), timeout_s=60, effort="medium")
    finally:
        A.runtimes = real
    text = Path(res.get("stdout") or out_dir / "stdout.txt").read_text(encoding="utf-8")
    seen = json.loads(text.strip().splitlines()[-1])
    check("seat-long-commands: background wait raised", seen.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS") == str(3600000), text[-200:])
    check("seat-long-commands: foreground ceiling raised", seen.get("BASH_MAX_TIMEOUT_MS") == str(3600000), text[-200:])
    from hb.runner import briefs as B
    real_role = B.role_text
    B.role_text = lambda role: "role"
    try:
        brief_text = B.render("fix", {}, {"cwd": ".", "evidence_dir": ".", "output_path": "r.json"})
    finally:
        B.role_text = real_role
    check("seat-long-commands: every brief says foreground only", B.FOREGROUND_ONLY in brief_text, brief_text[-300:])


def drill_a_seat_starts_only_its_roles_tools(repo):
    """Tester seats started the owner's personal tool servers and plugins, one of them signed in
    to a live payments account, inside unattended runs. Every Claude seat now starts strict: only
    the servers in its own file (the venue's ui driver for a tester when the caller hands it one,
    none for a fixer), and its settings switch off every plugin the owner's own sessions enable."""
    from hb.runner import adapters as A
    from hb.runner import briefs as B
    probe = "import sys,json;print(json.dumps(sys.argv[1:]))"
    real_rt, real_pp = A.runtimes, A.personal_plugins
    A.runtimes = lambda: {"claude": {"cmd": ["python", "-c", probe, "--allowedTools", "Bash"], "brief": "stdin",
                                     "browser": "mcp", "usage": "none"}}
    A.personal_plugins = lambda: ["payments@market", "accounts@official"]
    driver = {"command": "node", "args": ["driver.js", "--storage-state", "s.json"], "env": {}}
    seen = {}
    try:
        for label, role, tools in (("fix", "fix", None), ("verify", "verify", {"ui": driver}), ("bare", "verify", None)):
            out_dir = repo.parent / "run" / "seats" / f"{label}-strict"
            brief = repo.parent / f"brief-{label}.md"
            brief.write_text("x", encoding="utf-8")
            res = A.launch("claude", role, str(brief), str(repo), str(out_dir), timeout_s=60, effort="medium", tools=tools)
            text = Path(res.get("stdout") or out_dir / "stdout.txt").read_text(encoding="utf-8")
            seen[label] = (json.loads(text.strip().splitlines()[-1]), out_dir)
    finally:
        A.runtimes, A.personal_plugins = real_rt, real_pp
    for label, (argv, out_dir) in seen.items():
        check(f"strict: a {label} seat starts with --strict-mcp-config and its own server file",
              "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1].endswith("mcp.json"), str(argv))
        settings = json.loads((out_dir / "settings.json").read_text(encoding="utf-8"))
        check(f"strict: a {label} seat's settings switch off every personal plugin",
              settings.get("enabledPlugins") == {"payments@market": False, "accounts@official": False}, str(settings)[:300])
    servers = {label: json.loads((out_dir / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
               for label, (_argv, out_dir) in seen.items()}
    check("strict: a fix seat declares no tool server", servers["fix"] == {}, str(servers["fix"]))
    check("strict: a tester starts the spec it was handed, by its own command, and nothing else",
          servers["verify"] == {"ui": {"command": "node", "args": ["driver.js", "--storage-state", "s.json"]}}
          and "mcp__ui" in seen["verify"][0][seen["verify"][0].index("--allowedTools") + 1], str(servers["verify"]))
    check("strict: a tester handed no spec starts no browser tool", servers["bare"] == {}, str(servers["bare"]))
    check("strict: a seat's admission kind names what it loads, so peaks from a heavier seat never price it",
          A.seat_kind("verify") == "seat:verify:strict-ui" and A.seat_kind("fix") == "seat:fix:strict-none",
          str([A.seat_kind("verify"), A.seat_kind("fix")]))
    real_role = B.role_text
    B.role_text = lambda role: "role"
    try:
        bare = B.render("verify", {}, {"cwd": ".", "evidence_dir": ".", "output_path": "r.json"})
        handed = B.render("verify", {}, {"cwd": ".", "evidence_dir": ".", "output_path": "r.json", "tools": {"ui": driver}})
    finally:
        B.role_text = real_role
    check("strict: a tester's brief says `not offered: ui` when no spec was handed, and names the tool when one was",
          "not offered: ui" in bare and "not offered: ui" not in handed and "Browser tool: `ui`" in handed, bare[-400:])


def drill_fix_round(repo):
    finding = {"id": "F1", "axis": "goal", "severity": "P1", "class": "broken", "rule": None,
               "confidence": 8, "title": "widget still shows twice", "summary": "widget still shows twice",
               "quote": {"file": "src/x.py", "line": 1, "text": "x = 1"}, "absence": None,
               "recurrence": "n/a", "status": "open", "fixed_at": None}
    fixed = dict(finding, status="fixed", fixed_at="x.py:1 @ tip")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    "review.1": {"mode": "ok", "report": {"findings": [finding]}},
                                    "fix": {"mode": "ok", **TOUCH, "report": {"fixes": [
                                        {"finding": "F1", "red_first": "test_x.py", "change": "dedupe", "pinned_by": "test_x.py"}]}},
                                    "review.2": {"mode": "ok", "report": {"scope": "the fix", "findings": [fixed]}}})
    evs = journal(repo, js.get("run_id", ""))
    check("fix-round: fixer ran once", len([s for s in seats(evs, "fix") if s["state"] == "ok"]) == 1)
    check("fix-round: the read after the fix ran", len([s for s in seats(evs, "review") if s["state"] == "ok"]) == 2)
    check("fix-round: fix-round journaled", any(e.get("kind") == "fix-round" and e.get("stage") == "r1" for e in evs))
    check("fix-round: shipped", js.get("verdict") == "mergeable", raw[-400:])


def drill_resume(repo):
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}})
    rid = js.get("run_id", "")
    before = len([e for e in journal(repo, rid) if e.get("kind") == "op" and e.get("state") == "complete"])
    rc2, js2, raw2 = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}}, extra=("--resume", rid))
    evs = journal(repo, rid)
    check("resume: same run id", js2.get("run_id") == rid)
    check("resume: contract op not repeated", len([e for e in evs if e.get("key") == "contract" and e.get("state") == "started"]) == 1)
    check("resume: worktree op not repeated", len([e for e in evs if e.get("key") == "worktree" and e.get("state") == "started"]) == 1)


def drill_fix_report_without_a_cause_is_refused(repo):
    """A fix round is the builder relaunched, so its report is a build note: a fix-report shape
    is refused and journaled on the seat with the field named, and the red ships red (A16)."""
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    c["gates"] = [{"name": "redgate", "cmd": ["python", "-c", "import sys; print('red'); sys.exit(1)"]}, {"name": "suite", "cmd": ["python", "-c", "print('suite ok')"], "heavy": True}]
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("commit", "-qam", "a gate that is red"); g("push", "-q", "origin", "dev")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH},
                                    "fix.1": {"mode": "ok", **TOUCH, "report": {"schema": "fix-report/v1", "fixes": [
                                        {"finding": "redgate", "red_first": "x", "change": "y", "pinned_by": "z"}], "left_open": []}},
                                    "fix.2": {"mode": "ok", **TOUCH, "report": {"causes": [], "left_open": [{"finding": "redgate", "why": "later"}]}}})
    evs = journal(repo, js.get("run_id", ""))
    fixes = [e for e in seats(evs, "fix") if e.get("state") not in ("started",)]
    check("fix-report: the v1 shape is refused and journaled on the seat",
          fixes and fixes[0].get("state") == "invalid-report" and any("fix-report/v1" in p for p in fixes[0].get("problems") or []),
          str(fixes[:1])[:400])
    check("fix-report: a fix round is the builder relaunched, so its report is a build note (A16)",
          len(fixes) >= 2 and fixes[1].get("state") == "ok", str(fixes[1:2])[:400])
    check("fix-report: nothing was submitted as a fix; the red ships red", js.get("verdict") == "run-complete" and not js.get("stops"), raw[-300:])


def drill_fixer_cannot_prove_a_red_preexisting(repo):
    """Only the runner's own replay at base proves a red pre-existing. A fixer's `left_open`
    row claiming it, with or without evidence, is a word: the gate stays red, no docket line
    is journaled, the PR ships red and never stops."""
    cfgp = repo / ".claude" / "harness-config.json"
    c = json.loads(cfgp.read_text(encoding="utf-8"))
    c["gates"] = [{"name": "redgate", "cmd": ["python", "-c", "import sys; print('old red'); sys.exit(1)"]}, {"name": "suite", "cmd": ["python", "-c", "print('suite ok')"], "heavy": True}]
    cfgp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    # The worktree is cut from origin/dev and refuses tracked edits, so the red gate
    # lands as a commit on dev and is pushed, the way a real config change would.
    g = lambda *a: sh(["git", "-C", str(repo), "-c", "user.email=d@d", "-c", "user.name=d", *a])
    g("commit", "-qam", "a gate that was red before the branch"); g("push", "-q", "origin", "dev")
    proven = {"build": {"mode": "ok", **TOUCH},
              "fix": {"mode": "ok", **TOUCH, "report": {"causes": [], "left_open": [
                  {"finding": "redgate", "why": "red at the base sha too", "verdict": "refuted",
                   "evidence": "replayed at base in a second worktree: TRUE_EXIT=1"}]}}}

    rc, js, raw = run_hotfix(repo, proven)
    evs = journal(repo, js.get("run_id", ""))
    check("preexisting: the fixer's claim does not clear the red; the PR ships red", js.get("verdict") == "run-complete", raw[-400:])
    check("preexisting: no docket line from a fixer's word", not any(e.get("kind") == "preexisting-red" for e in evs))
    check("preexisting: gate artifact named", any(g.get("artifact") for e in evs if str(e.get("key", "")).startswith("gates-r1@") and e.get("state") == "complete" for g in e["result"]))
    rep = Path(js.get("record", "")).read_text(encoding="utf-8") if js.get("record") else ""
    check("preexisting: the record names the red, not as pre-existing", "redgate" in rep and "pre-existing red" not in rep)
    asserted = {"build": {"mode": "ok", **TOUCH},
                "fix": {"mode": "ok", **TOUCH, "report": {"causes": [], "left_open": [
                    {"finding": "redgate", "why": "probably old"}]}}}
    rc, js, raw = run_hotfix(repo, asserted, ref="the second widget")
    check("preexisting: an assertion without evidence stays red and ships red, never a stop",
          js.get("verdict") == "run-complete" and not js.get("stops"), raw[-400:])


def drill_question(repo):
    """The soft channel: a `question` row never stops the run and reaches the report
    under Yours to rule."""
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH, "report": {
        "needs_authority": [{"kind": "question", "text": "wrap at 32 or 40?", "evidence": "x.py:1"}]}}})
    check("question: shipped", js.get("verdict") == "mergeable", raw[-400:])
    evs = journal(repo, js.get("run_id", ""))
    check("question: gates ran", any(str(e.get("key", "")).startswith("gates-r1@") for e in evs))
    rep = Path(js.get("record", "")).read_text(encoding="utf-8") if js.get("record") else ""
    check("question: under Yours to rule", "## Yours to rule" in rep and "wrap at 32 or 40?" in rep)


FAST_POLL = {"HB_POLL_S": "1", "HB_STALL_MIN": "0.05"}


def drill_stall(repo):
    """A seat that goes quiet is cut on the stall window, not the fuse, and relaunched."""
    rc, js, raw = run_hotfix(repo, {"build.1": {"mode": "timeout", "sleep": 25}, "build.2": {"mode": "ok", **TOUCH}},
                             env_extra=FAST_POLL)
    evs = journal(repo, js.get("run_id", ""))
    states = [s["state"] for s in seats(evs, "build")]
    check("stall: cut then relaunched", states == ["started", "stalled", "started", "ok"], str(states))
    check("stall: the cut names the window", any("no commit" in str(s.get("why")) for s in seats(evs, "build")))
    check("stall: shipped", js.get("verdict") == "mergeable", raw[-400:])


def drill_cut(repo):
    """The conductor's `cut` kills the running seat; it is relaunched and not counted."""
    rid = "drill-cut"
    (run_dir(repo, rid) / "control.txt").write_text("cut\n", encoding="utf-8")
    rc, js, raw = run_hotfix(repo, {"build.1": {"mode": "timeout", "sleep": 25}, "build.2": {"mode": "ok", **TOUCH}},
                             extra=("--resume", rid), env_extra={"HB_POLL_S": "1"})
    evs = journal(repo, rid)
    states = [s["state"] for s in seats(evs, "build")]
    check("cut: the seat was cut and relaunched", states == ["started", "cut", "started", "ok"], str(states))
    check("cut: journaled as a respawn after cut", any(e.get("kind") == "respawn" and e.get("after") == "cut" for e in evs))
    check("cut: the line was consumed", "cut" not in (run_dir(repo, rid) / "control.txt").read_text(encoding="utf-8"))
    check("cut: shipped", js.get("verdict") == "mergeable", raw[-400:])


def drill_pause(repo):
    """`pause` stops at the next boundary, resumable; the next --resume runs on."""
    rid = "drill-pause"
    (run_dir(repo, rid) / "control.txt").write_text("pause\n", encoding="utf-8")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}}, extra=("--resume", rid))
    check("pause: paused before the first seat", js.get("verdict") == "paused" and rc == 3, raw[-300:])
    evs = journal(repo, rid)
    check("pause: journaled", any(e.get("kind") == "control" and e.get("verb") == "pause" for e in evs))
    check("pause: no seat ran", not seats(evs))
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}}, extra=("--resume", rid))
    check("pause: resumed and shipped", js.get("verdict") == "mergeable", raw[-300:])


def drill_answer(repo):
    """An `answer:` line rides into every later brief and the PR body says which seat read it first."""
    rid = "drill-answer"
    (run_dir(repo, rid) / "control.txt").write_text("answer: use the blue one\n", encoding="utf-8")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}}, extra=("--resume", rid))
    check("answer: shipped", js.get("verdict") == "mergeable", raw[-300:])
    evs = journal(repo, rid)
    ctl = [e for e in evs if e.get("kind") == "control" and e.get("verb") == "answer"]
    check("answer: journaled once, reached the build seat", len(ctl) == 1 and ctl[0].get("reached") == "build", str(ctl))
    brief = run_dir(repo, rid) / "seats" / "review-1" / "brief.md"
    check("answer: in the review brief", brief.exists() and "use the blue one" in brief.read_text(encoding="utf-8"))
    body = run_dir(repo, rid) / "evidence" / "record.md"
    check("answer: named in the record", body.exists() and "reached the build seat" in body.read_text(encoding="utf-8"))


def drill_surfaced(repo):
    """A question the builder kept going on is a journal event at the boundary, not only a PR line."""
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH, "report": {"needs_authority": [
        {"kind": "question", "text": "which colour", "evidence": "x.py:1"}]}}})
    evs = journal(repo, js.get("run_id", ""))
    q = [e for e in evs if e.get("kind") == "question"]
    check("surfaced: one question event from build", len(q) == 1 and q[0].get("seat") == "build" and q[0].get("text") == "which colour", str(q))
    check("surfaced: the run did not wait", js.get("verdict") == "mergeable")


def drill_watch(repo):
    """`hb run watch` prints one line per boundary past a cursor and names the final phase."""
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH}})
    rid = js.get("run_id", "")
    rc, o, e = sh([sys.executable, HB, "run", "watch", rid, "--after", "0", "--max-min", "0.05"], cwd=str(repo))
    w = last_json(o)
    check("watch: final phase named", w.get("final") == "mergeable", (o + e)[-300:])
    check("watch: cursor at the end", w.get("cursor") == len(journal(repo, rid)), str(w))
    check("watch: one line per seat end and phase", "seat build 1 ok" in o and "phase mergeable" in o, o[-400:])
    rc, o, e = sh([sys.executable, HB, "run", "watch", rid, "--after", str(w.get("cursor")), "--max-min", "0.05"], cwd=str(repo))
    check("watch: nothing new past the cursor", last_json(o).get("lines") == 0, o[-200:])


def drill_failed_op_retried(repo):
    """A completed op whose result did not land (no url, no venue) is retried on resume."""
    rid = "drill-fresh"
    rd = run_dir(repo, rid)
    (rd / "journal.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "run_id": rid, "key": "pr", "state": "started", "facts": {}, "kind": "op"}) + "\n" +
        json.dumps({"ts": "2026-01-01T00:00:01Z", "run_id": rid, "key": "pr", "state": "complete",
                    "result": {"url": None, "error": "label"}, "kind": "op"}) + "\n", encoding="utf-8")
    sys.path.insert(0, str(ENGINE))
    from hb.record.journal import Journal
    import os as _os
    here = _os.getcwd()
    _os.chdir(str(repo))
    try:
        j = Journal(rid)
        check("fresh key: a refused pr op is retried under pr.retry2",
              j.fresh_key("pr", lambda p: bool((p or {}).get("url"))) == "pr.retry2")
        j.append("op", key="pr.retry2", state="complete", result={"url": "https://x/1"})
        check("fresh key: a landed pr op is kept", j.fresh_key("pr", lambda p: bool((p or {}).get("url"))) == "pr.retry2")
        j.append("op", key="venue-up", state="complete", result={"venue": "v-1", "record": {"record": str(rd / "v.json")}})
        (rd / "v.json").write_text(json.dumps({"state": "down"}), encoding="utf-8")
        from hb.venue import standin
        check("fresh key: a venue that is down now is retried",
              j.fresh_key("venue-up", lambda p: standin.venue_landed(p)) == "venue-up.retry2")
    finally:
        _os.chdir(here)


def drill_token_count_is_not_a_rate_limit(repo):
    """A seat that returned clean with its report is ok even when its stream carries a 429 token count."""
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH, "report": {"changelog": "cache_read_input_tokens 429 reset in"},
                                              "stderr": "usage: 429 tokens"}})
    evs = journal(repo, js.get("run_id", ""))
    states = [s["state"] for s in seats(evs, "build")]
    check("token count: build seat ok, not rate-limited", states == ["started", "ok"], str(states))


def drill_pr_body_refreshed(repo):
    """A PR that already exists for the branch is edited, not re-created, on a later pass."""
    from hb.beats import landing
    calls = []
    real_gh = landing.gh
    def fake_gh(args, timeout=45):
        calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return 0, '[{"url": "https://x/pr/9"}]', ""
        return 0, "", ""
    landing.gh = fake_gh
    import os as _os
    here = _os.getcwd(); _os.chdir(str(repo))
    try:
        from hb.record.journal import Journal
        j = Journal("drill-pr-edit")
        _heavy_measured(j, repo)
        res = landing.open_pr(j, repo, "hotfix/x", {"goal": "g", "issue": "#1"}, {"branches": {"landing": "dev"}}, False,
                     "body", beat="hotfix")
    finally:
        landing.gh = real_gh; _os.chdir(here)
    check("pr edit: the existing PR is returned", res.get("url") == "https://x/pr/9", str(res))
    check("pr edit: edited, never created", any(c[:2] == ["pr", "edit"] for c in calls) and not any(c[:2] == ["pr", "create"] for c in calls), str(calls))


def drill_owed_and_films(repo):
    """A question an answer names is answered, not owed; every drive's films are listed once per item."""
    from hb.runner import seating
    from hb.record import prbody
    from hb.record import record
    from hb.record import firstscreen
    qs = [{"text": "SI-17's ten seconds cannot hold", "evidence": "x"}, {"text": "is blue right", "evidence": "y"}]
    ans = [{"text": "SI-17 re-ruled: the ten is withdrawn", "reached": "review"}]
    owed, answered = firstscreen.owed_questions(qs, ans)
    check("owed: the SI-17 question is answered", [q["text"] for q in answered] == ["SI-17's ten seconds cannot hold"])
    check("owed: the other stays owed", [q["text"] for q in owed] == ["is blue right"])
    ev = repo / "ev"
    for n, items in ((1, ("A-1", "A-2")), (2, ("A-3",))):
        (ev / "drives" / f"verify-{n}" / "video").mkdir(parents=True)
        for i in items:
            (ev / "drives" / f"verify-{n}" / "video" / f"{i}.webm").write_bytes(b"x")
    (ev / "video").mkdir(); (ev / "video" / "A-3.webm").write_bytes(b"y")
    vids = record._all_videos(ev, [{"item": "A-3", "video": str(ev / "video" / "A-3.webm")}])
    check("films: latest receipt first, earlier drives' films kept, one per item",
          [v[0] for v in vids] == ["A-3", "A-1", "A-2"] and vids[0][1].endswith("A-3.webm"), str(vids))
    (ev / "video" / "keep.webm").write_bytes(b"z")
    seating.keep_drive_evidence(ev, 7)
    check("films: a drive's films are copied under drives/verify-7", (ev / "drives" / "verify-7" / "video" / "keep.webm").exists())


def drill_closeout_files_nothing(repo):
    """A run files no issue for a red row the merge carried (owner ruling): the row is
    named in the report, and gh is never asked to create anything."""
    sys.path.insert(0, str(ENGINE))
    from hb.beats import closeout
    real = closeout.gh
    calls = []

    def fake(args, timeout=45):
        calls.append(list(args))
        return 0, "", ""
    closeout.gh = fake
    try:
        v = {"id": "r1", "items": [{"item": "A-01", "state": "BROKEN", "substate": "", "text": "the title reads Sales", "seen": "no title"},
                                   {"item": "A-02", "state": "UNMEASURED", "substate": "", "text": "the bell rings"},
                                   {"item": "A-03", "state": "UNMEASURED", "substate": "by-test", "text": "pinned by a test"}]}
        reds = closeout._reds_carried(v)
    finally:
        closeout.gh = real
    check("closeout: every red row the merge carried is named, a test-pinned one is not",
          reds == ["A-01 is broken: the title reads Sales", "A-02 was not measured: the bell rings"], str(reds))
    check("closeout: no issue is filed and no label is made", not calls, str(calls))


def drill_page_task_is_built_not_registered(repo):
    """The page died with the chat that started it. `hb page --install` registers a per-user
    task (at logon and from now, every minute, one instance, hidden, the product's pinned
    entry from the product root); the drill reads the commands, it never registers one."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import pagehost
    port = free_port()
    xml = pagehost.task_xml(repo, repo / ".claude" / "harness" / "bin" / "hb.py", port, "C:/py/pythonw.exe", "HOST\\me",
                            start="2026-10-05T12:00:00")
    check("page task: logon and now, every minute, one instance at a time, hidden, no time limit",
          all(x in xml for x in ("<LogonTrigger>", "<TimeTrigger>", "<StartBoundary>2026-10-05T12:00:00</StartBoundary>",
                                 "<Interval>PT1M</Interval>", "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
                                 "<Hidden>true</Hidden>", "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
                                 "<RestartOnFailure>", "<UserId>HOST\\me</UserId>")), xml[:600])
    check("page task: runs the pinned entry's page verb on the port, from the product root",
          f"page --port {port}</Arguments>" in xml and "bin" in xml and f"<WorkingDirectory>{repo}</WorkingDirectory>" in xml
          and "<Command>C:/py/pythonw.exe</Command>" in xml, xml[-500:])
    name = pagehost.task_name(port)
    check("page task: install creates then runs it by name; uninstall deletes it",
          pagehost.install_steps(name, "t.xml") == [["schtasks", "/create", "/tn", name, "/xml", "t.xml", "/f"], ["schtasks", "/run", "/tn", name]]
          and pagehost.uninstall_steps(name) == [["schtasks", "/delete", "/tn", name, "/f"]], name)
    calls, real_call = [], pagehost.call
    pagehost.call = lambda argv, timeout=60, **kw: (calls.append(list(argv)) or (0, "", ""))
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        r = pagehost.install(port)
        u = pagehost.uninstall(port)
    finally:
        pagehost.call = real_call
        os.chdir(here)
    if os.name == "nt":
        check("page task: --install writes the task file and asks schtasks to create and run it, nothing else",
              [c[:2] for c in calls if c[0] == "schtasks"] == [["schtasks", "/create"], ["schtasks", "/run"], ["schtasks", "/delete"]]
              and r.get("task") == name and Path(r.get("xml") or "x").read_text(encoding="utf-16").count("<Exec>") == 1
              and u.get("verdict") == "uninstalled", str({"r": r, "u": u, "calls": calls})[:500])
    else:
        check("page task: off Windows, install and uninstall say so and run nothing",
              r.get("verdict") == "not-windows" and u.get("verdict") == "not-windows" and not calls, str(r))


def drill_page_serves_without_a_console(repo):
    """The task starts the page under pythonw, which has no stdout: the engine died on its
    first line reconfiguring a console it did not have. It serves, and writes its start line
    to the page log instead."""
    port = free_port()
    wrapper = (f"import sys, runpy; sys.stdout = sys.stderr = None; sys.path.insert(0, {str(HERE)!r}); "
               f"sys.argv = [{HB!r}, 'page', '--port', '{port}']; runpy.run_path({HB!r}, run_name='__main__')")
    p = subprocess.Popen([sys.executable, "-c", wrapper], cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t0, up = time.time(), False
        while time.time() - t0 < 20 and p.poll() is None and not up:
            time.sleep(0.3)
            up = bool(sh([sys.executable, "-c", f"import socket; socket.create_connection(('127.0.0.1', {port}), 1)"])[0] == 0)
        log = repo / ".context" / "harness" / "page.out"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        check("page: with no console it serves and logs its start line to page.out",
              up and f"127.0.0.1:{port}" in text, f"exit {p.poll()}, log {text[-200:]!r}")
    finally:
        if p.poll() is None:
            p.kill()


def drill_engine_children_open_no_window(repo):
    """Under the page's pythonw every git child with no flags opened a console window that
    flashed on the owner's screen each refresh. Once the engine is imported, a child with
    no flags is created with no window, and a caller's own flags are kept."""
    if os.name != "nt":
        check("engine children: no window (Windows only, skipped)", True)
        return
    probe = (f"import sys, subprocess; sys.path.insert(0, {str(HERE)!r}); import hb" + "\n" +
             "seen = []" + "\n" + "real = subprocess._winapi.CreateProcess" + "\n" +
             "def spy(*a): seen.append(a[5]); return real(*a)" + "\n" +
             "subprocess._winapi.CreateProcess = spy" + "\n" +
             "subprocess.run(['git', '--version'], capture_output=True)" + "\n" +
             "subprocess.run(['git', '--version'], capture_output=True, "
             "creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)" + "\n" + "print(seen)")
    rc, so, se = sh([sys.executable, "-c", probe])
    try:
        plain, named = eval(so.strip().splitlines()[-1])
    except Exception:
        plain, named = None, None
    check("engine children: a child with no flags opens no window, a caller's flags are kept",
          plain is not None and plain & subprocess.CREATE_NO_WINDOW and named == subprocess.CREATE_NEW_PROCESS_GROUP,
          f"rc {rc} out {so[-200:]!r} err {se[-300:]!r}")


def drill_page_leaves_the_callers_job(repo):
    """A page started from a chat lived in the chat's job and died with it. The spawn asks to
    break away from the job, and starts inside it when the job forbids that, never failing;
    ensure_running goes through it when no task owns the page."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import pagehost
    from hb.record import page
    seen, real_popen, real_call = [], pagehost.popen, pagehost.call

    class Dead:
        pid = 4242

        def poll(self):
            return 1

    def fake_popen(cmd, **kw):
        seen.append(kw.get("creationflags", 0) if os.name == "nt" else kw.get("start_new_session"))
        if os.name == "nt" and kw.get("creationflags", 0) & pagehost.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return Dead()
    pagehost.popen = fake_popen
    pagehost.call = lambda argv, timeout=60, **kw: (1, "", "no task")
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        r = pagehost.ensure_running(free_port())
    finally:
        pagehost.popen, pagehost.call = real_popen, real_call
        os.chdir(here)
    if os.name == "nt":
        import subprocess as sp
        check("page spawn: asks to leave the job, then starts inside it when refused, with no window",
              len(seen) == 2 and seen[0] & pagehost.CREATE_BREAKAWAY_FROM_JOB and not seen[1] & pagehost.CREATE_BREAKAWAY_FROM_JOB
              and all(f & sp.CREATE_NO_WINDOW and f & sp.CREATE_NEW_PROCESS_GROUP for f in seen) and r.get("started") == 4242,
              str({"flags": [hex(f) for f in seen], "r": r}))
    else:
        check("page spawn: a session of its own", seen == [True] and r.get("started") == 4242, str(seen))


def drill_home_shows_every_live_run(repo):
    """The owner's one address: the home lists every live run in full (a working run with
    its seats, a run stopped on a question with the question) and then the finished ones with
    their PR, and a slice inside a wave is not listed apart from its wave."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import view
    from hb.record import page
    root = repo / ".context" / "harness" / "runs"
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    working, asking, done, inwave = (f"{base}-slice-1", f"{base}-hotfix-2", f"{base}-hotfix-3", f"{base}-slice-4")
    _write_journal(run_dir(repo, working), [{"ts": _iso(600), "kind": "phase", "phase": "opened", "beat": "slice", "ref": "#1"},
                                            {"ts": _iso(300), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"}])
    (root / working / "seats" / "build-1").mkdir(parents=True)
    (root / working / "seats" / "build-1" / "stdout.txt").write_text("", encoding="utf-8")
    _write_journal(run_dir(repo, asking), [{"ts": _iso(900), "kind": "phase", "phase": "opened", "beat": "hotfix", "ref": "make it blue"},
                                           {"ts": _iso(800), "kind": "stop", "id": "Q-1", "stop_kind": "question", "text": "Which blue?"},
                                           {"ts": _iso(800), "kind": "phase", "phase": "stopped", "why": "a question"}])
    _write_journal(run_dir(repo, done), [{"ts": _iso(5000), "kind": "phase", "phase": "opened", "beat": "hotfix", "ref": "fix it"},
                                         {"ts": _iso(4000), "kind": "phase", "phase": "mergeable", "pr": "https://github.com/o/r/pull/9"}])
    _write_journal(run_dir(repo, inwave), [{"ts": _iso(600), "kind": "phase", "phase": "opened", "beat": "slice", "ref": "#4", "wave": "w1"},
                                           {"ts": _iso(300), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"}])
    left = f"{base}-hotfix-5"
    _write_journal(run_dir(repo, left), [{"ts": _iso(3 * 86400), "kind": "phase", "phase": "opened", "beat": "hotfix", "ref": "left"}])
    from hb.runner import liveness
    for rid in (working, inwave):
        # a live run is one whose recorded process is there: this drill's own
        liveness.record(run_dir(repo, rid))
    saved = view.root
    view.root = lambda: root
    try:
        h = view.home()
        live = {v["id"]: v for v in h.get("live") or []}
        check("home: every live run is listed in full, the working one and the one asking",
              set(live) == {working, asking} and [s["role"] for s in live[working]["seats"]] == ["Builder"]
              and live[working]["seats"][0].get("since") and [s["id"] for s in live[asking]["stops"]] == ["Q-1"]
              and h.get("needs") == 1 and all("steps" not in v for v in live.values()), str(h)[:600])
        fin = {r["id"]: r for r in h.get("finished") or []}
        check("home: a finished run, and one gone quiet for days, are listed after them; a wave's slice is not listed apart",
              set(fin) == {done, left} and fin[done]["pr"] == "https://github.com/o/r/pull/9" and inwave not in live, str(h.get("finished"))[:300])
        from http.server import ThreadingHTTPServer
        import threading
        import urllib.request
        port = free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), page.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=20) as r:
                html = r.read().decode("utf-8")
        finally:
            server.shutdown()
        check("home: the page's root draws the home from the dashboard and the event stream",
              '"/api/dashboard"' in html and '"/api/events"' in html and "function tabsHtml" in html, html[:200])
    finally:
        view.root = saved


def drill_the_gates_are_the_configs_and_cis(repo):
    """HS-07: the gates a tip runs come from two sources only, the product config (its
    commands, and the gates the boxes run) and CI's own workflows (ciparity); a gate only the
    boxes run, with no local command, reads UNMEASURED by name when the boxes are down."""
    from hb.gates import gating
    from hb.record.journal import Journal
    c = {"branches": {"landing": "dev"}, "gates": {"commands": [{"name": "lint", "cmd": ["python", "-c", "print('lint ok')"]}],
                   "remote": {"gates": ["backend-tests"]}}, "ci": {"script_prefixes": ["tools/"]}}
    gates = gating.default_gates(["web/z.tsx"], c)
    names = {g["name"]: g for g in gates}
    check("config gates: the config's command and the boxes' gate, nothing else",
          sorted(names) == ["backend-tests", "lint"] and names["backend-tests"].get("remote") and names["backend-tests"].get("heavy"),
          str(gates))
    check("config gates: a product that names no gate runs none of its own", gating.default_gates(["src/x.py"], {}) == [])
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "on:\n  pull_request:\njobs:\n  lint:\n    steps:\n      - name: Words\n        run: python tools/lint/words.py\n",
        encoding="utf-8")
    listed = [g["name"] for g in gating.round_list(repo, [], c)]
    check("config gates: a round adds CI's own scripts, read from its workflows", "ci: tools/lint/words.py" in listed, str(listed))
    saved = gating.remote.available
    gating.remote.available = lambda c: (False, "no self-hosted runner is online")
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j = Journal("drill-config-gates")
        j.phase("opened", beat="slice", ref="#1")
        rows = gating.measure(j, repo, "drill", "base", [], c, "gates-boxes-down", heavy=True)
    finally:
        gating.remote.available = saved
        os.chdir(here)
    row = next((r for r in rows if r["name"] == "backend-tests"), {})
    check("config gates: a boxes-only gate with no local command is UNMEASURED by name when the boxes are down",
          row.get("true_exit") is None and "names no local command" in str(row.get("unmeasured")), str(rows))


def _page_modules(repo):
    sys.path.insert(0, str(ENGINE))
    from hb.record import dashboard
    from hb.runner import liveness
    from hb.record import page
    from hb.record import prune
    from hb.record import view
    from hb.record import watcher
    return dashboard, liveness, page, prune, view, watcher


def _finished_run(repo, rid, minutes_ago=60):
    _write_journal(run_dir(repo, rid), [{"ts": _iso(minutes_ago * 60), "kind": "phase", "phase": "opened", "beat": "hotfix", "ref": rid},
                                        {"ts": _iso(minutes_ago * 60 - 60), "kind": "phase", "phase": "mergeable", "pr": "https://x/pull/1"}])


def _append(d, event):
    with open(d / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def drill_watcher_reads_only_a_changed_journal(repo):
    """The home re-read every run folder's journal on every request: 119 runs, hundreds of
    milliseconds warm. The watcher reads each run once, then again only when its journal's
    time or size changed, and the home it holds is served in milliseconds."""
    dashboard, liveness, page, prune, view, watcher = _page_modules(repo)
    root = repo / ".context" / "harness" / "runs"
    ids = [f"20261001-00000{i}-hotfix-w{i}" for i in range(3)]
    for rid in ids:
        _finished_run(repo, rid)
    saved, saved_blocks = view.root, dashboard.blocks
    view.root, dashboard.blocks = (lambda: root), (lambda: {"repo": "drill/drill"})
    try:
        w = watcher.Watcher()
        w.scan()
        check("watcher: the first look reads every run once", sorted(w.reads) == sorted(ids), str(w.reads))
        w.scan()
        check("watcher: a second look with nothing changed reads no journal", w.reads == [], str(w.reads))
        _append(root / ids[1], {"ts": _iso(10), "kind": "question", "text": "a late note"})
        events = w.scan()
        check("watcher: an appended journal is the only one read again, and the home is told",
              w.reads == [ids[1]] and ("home", None) in events, str((w.reads, [e[0] for e in events])))
        doc = w.served()
        check("watcher: the held home is served from what it holds, reading no journal, with every finished run",
              w.reads == [ids[1]] and {r["id"] for r in doc.get("finished") or []} == set(ids), f"{w.reads} {doc.get('finished')}")
    finally:
        view.root, dashboard.blocks = saved, saved_blocks


def drill_request_never_waits_on_a_slow_reader(repo):
    """Three `gh issue list` calls with 45 s timeouts ran inside the home's request, and a
    stale cache blocked it for minutes. A reader runs in its own thread: a request gets the
    value held last, with its age, at once, or `reading` before the first read lands."""
    dashboard, liveness, page, prune, view, watcher = _page_modules(repo)
    import threading
    gate, calls = threading.Event(), []

    def slow():
        calls.append(len(calls))
        gate.wait(20)
        return {"n": len(calls)}
    key = f"drill-slow-{id(gate)}"
    first = dashboard._cached(key, 0, slow)
    # the reader is blocked on the gate: an answer now is one that did not wait for it
    check("slow reader: nothing held yet answers `reading` while the read is blocked", first.get("reading") is True
          and not gate.is_set(), str(first))
    gate.set()
    deadline = time.time() + 10
    while key not in dashboard._CACHE and time.time() < deadline:
        time.sleep(0.02)
    gate.clear()
    second = dashboard._cached(key, 0, slow)
    check("slow reader: a stale value is served with its age while the read again is blocked",
          second.get("n") == 1 and second.get("age_s") is not None and not gate.is_set(), str(second))
    third = dashboard._cached(key, 0, slow)
    check("slow reader: one read at a time per block", len(calls) == 2 and third.get("n") == 1, f"{len(calls)} reads, {third}")
    gate.set()
    saved_gh, saved_repo = dashboard.gh, dashboard._repo
    held = threading.Event()
    dashboard.gh = lambda *a, **k: (held.wait(20), (1, "", ""))[1]
    dashboard._repo = lambda: "drill/drill"
    with dashboard._LOCK:
        for k in ("queue", "venues", "health"):
            dashboard._CACHE.pop(k, None)
    try:
        b = dashboard.blocks()
        check("slow reader: the home's blocks answer while gh is blocked", b["queue"].get("reading")
              and not held.is_set(), str(b["queue"]))
    finally:
        held.set()
        dashboard.gh, dashboard._repo = saved_gh, saved_repo


def drill_events_push_a_run_on_journal_append(repo):
    """The page asked for the whole home every four seconds. It opens one event stream: the
    home as it stands first, then `run` with that run's card when its journal moves."""
    dashboard, liveness, page, prune, view, watcher = _page_modules(repo)
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    root = repo / ".context" / "harness" / "runs"
    rid = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-hotfix-push"
    d = run_dir(repo, rid)
    _write_journal(d, [{"ts": _iso(300), "kind": "phase", "phase": "opened", "beat": "hotfix", "ref": "push"},
                       {"ts": _iso(200), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"}])
    liveness.record(d)
    saved, saved_blocks, saved_current = view.root, dashboard.blocks, watcher.CURRENT
    view.root, dashboard.blocks = (lambda: root), (lambda: {"repo": "drill/drill"})
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), page.Handler)
    try:
        w = watcher.Watcher()
        watcher.CURRENT = w
        w.step()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()

        def next_event():
            kind, data = None, None
            while True:
                line = resp.fp.readline().decode("utf-8")
                if not line:
                    return kind, data
                line = line.rstrip("\n")
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "" and kind:
                    return kind, data
        kind, home = next_event()
        check("events: the stream opens with the home as it stands, the running run in it",
              resp.status == 200 and kind == "home" and [v["id"] for v in home.get("live") or []] == [rid], str((resp.status, kind, str(home)[:200])))
        _append(d, {"ts": _iso(1), "kind": "seat", "seat": "build", "attempt": 1, "state": "ok", "minutes": 3})
        w.step()
        kind, data = next_event()
        check("events: a journal append pushes `run` with that run's card",
              kind == "run" and data.get("id") == rid and (data.get("card") or {}).get("state") == "running", str((kind, str(data)[:200])))
        conn.close()
    finally:
        server.shutdown()
        view.root, dashboard.blocks, watcher.CURRENT = saved, saved_blocks, saved_current


def drill_a_dead_pid_reads_finished(repo):
    """The home listed runs whose process died hours before, guessed live from journal
    silence. A run records its pid when it opens: a gone pid has ended, a live one is running
    however quiet, and a pid handed to another process does not read as the run."""
    dashboard, liveness, page, prune, view, watcher = _page_modules(repo)
    rid = "drill-dead-pid"
    d = run_dir(repo, rid)
    _write_journal(d, [{"ts": _iso(3 * 3600), "kind": "phase", "phase": "opened", "beat": "hotfix", "ref": "x"},
                       {"ts": _iso(3 * 3600), "kind": "seat", "seat": "build", "attempt": 1, "state": "started"}])
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    liveness.record(d, p.pid)
    saved = view.root
    view.root = lambda: d.parent
    try:
        v = view.run(rid) or {}
        check("pid: a run whose process is gone reads ended, with no seats, and the home does not list it as running",
              v.get("state") == "ended" and v.get("seats") == [] and not view.placed([view.row_of(rid)]), str({k: v.get(k) for k in ("state", "seats")}))
        liveness.record(d)
        v = view.run(rid) or {}
        check("pid: a live process three hours quiet is running", v.get("state") == "running", str(v.get("state")))
        rec = liveness.read(d)
        if rec.get("started") is not None:
            rec["started"] -= 3600
            (d / liveness.FILE).write_text(json.dumps(rec), encoding="utf-8")
            check("pid: a pid now held by a process that started at another time reads ended", (view.run(rid) or {}).get("state") == "ended")
        _append(d, {"ts": _iso(1), "kind": "phase", "phase": "mergeable"})
        check("pid: a run's last final phase stands whatever its process", (view.run(rid) or {}).get("state") == "mergeable")
    finally:
        view.root = saved


def drill_prune_keeps_the_record_and_skips_a_live_run(repo):
    """Eight gigabytes of films and streams under 119 runs. Prune empties a run older than
    14 days down to what the page reads, lists without deleting under --dry-run, and never
    touches a run whose process is still there; a gate transcript of that age goes too, and
    the closeout's prune keeps the demo."""
    dashboard, liveness, page, prune, view, watcher = _page_modules(repo)
    root = repo / ".context" / "harness" / "runs"
    kept = ["journal.jsonl", "phase.json", "control.txt", "seats/build-1/report.json", "evidence/closeout.json"]
    gone = ["seats/build-1/stdout.txt", "seats/build-1/brief.md", "evidence/after/a-desktop.png", "evidence/video/a.webm", "detached.out"]
    for rid in ("20260801-000000-hotfix-old", "20260801-000001-hotfix-old-live", "20261001-000000-hotfix-new"):
        d = run_dir(repo, rid)
        for rel in kept + gone:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("x" * 10, encoding="utf-8")
        if "new" not in rid:
            old = time.time() - 40 * 86400
            os.utime(d / "journal.jsonl", (old, old))
    liveness.record(root / "20260801-000001-hotfix-old-live")
    gate = root.parent / "evidence" / "1" / "gates" / "gate-old.txt"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text("TRUE_EXIT=0", encoding="utf-8")
    os.utime(gate, (old, old))
    rc, o, e = sh([sys.executable, HB, "page", "prune", "--dry-run"], cwd=str(repo))
    dry = last_json(o)
    still = all((root / "20260801-000000-hotfix-old" / rel).exists() for rel in gone) and gate.exists()
    check("prune: --dry-run lists the old run's files and the old gate transcript and deletes nothing",
          rc == 0 and [r["id"] for r in (dry.get("runs") or {}).get("runs") or []] == ["20260801-000000-hotfix-old"]
          and (dry.get("gates") or {}).get("files") == 1 and still, (o + e)[-400:])
    r = prune.prune(root)
    g = prune.prune_gates(root.parent / "evidence")
    check("prune: an old gate transcript goes", g["files"] == 1 and not gate.exists(), str(g))
    demo = root / "20261001-000000-hotfix-new" / "evidence" / "demo" / "a.webm"
    demo.parent.mkdir(parents=True, exist_ok=True)
    demo.write_text("x", encoding="utf-8")
    prune.prune_run(root / "20261001-000000-hotfix-new", keep_dirs=("demo",))
    check("prune: the closeout's prune keeps the demo and takes the films",
          demo.exists() and not (root / "20261001-000000-hotfix-new" / "evidence" / "video" / "a.webm").exists(), "")
    for rel in gone:
        (root / "20261001-000000-hotfix-new" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "20261001-000000-hotfix-new" / rel).write_text("x", encoding="utf-8")
    d = root / "20260801-000000-hotfix-old"
    check("prune: an old run keeps its journal, phase, control, seat reports and verdict files, and loses the rest",
          all((d / rel).exists() for rel in kept) and not any((d / rel).exists() for rel in gone) and not (d / "evidence" / "after").exists(),
          str(sorted(str(p.relative_to(d)) for p in d.rglob("*"))))
    live_d, new_d = root / "20260801-000001-hotfix-old-live", root / "20261001-000000-hotfix-new"
    check("prune: a run whose process is there is skipped, and a recent run is not touched",
          r["skipped_live"] == [live_d.name] and all((live_d / rel).exists() and (new_d / rel).exists() for rel in gone), str(r))


def drill_collapse_leaves_only_the_record(repo):
    """A merged run's issue evidence and its worktree's Claude transcripts outlived every
    reader. Once the demo is filmed, collapse takes both and the run keeps its journal,
    phase, closeout and the demo video the session has yet to send."""
    dashboard, liveness, page, prune, view, watcher = _page_modules(repo)
    root = repo / ".context" / "harness" / "runs"
    d = run_dir(repo, "20261002-000000-slice-77")
    wt = repo / "wt-77"
    (d / "phase.json").write_text(json.dumps({"issue": "#77", "worktree": str(wt)}), encoding="utf-8")
    for rel in ("journal.jsonl", "evidence/closeout.json", "evidence/demo/a.webm", "seats/build-1/stdout.txt"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x", encoding="utf-8")
    ev = root.parent / "evidence" / "77"
    (ev / "gates").mkdir(parents=True, exist_ok=True)
    (ev / "decisions.md").write_text("x", encoding="utf-8")
    (ev / "gates" / "gate.txt").write_text("x", encoding="utf-8")
    tx = repo / "transcripts-77"
    tx.mkdir()
    (tx / "s.jsonl").write_text("x", encoding="utf-8")
    prune._transcripts_dir = lambda w: tx if Path(w) == wt else repo / "elsewhere"
    r = prune.collapse(d, root.parent / "evidence")
    check("collapse: the issue's evidence and the worktree's transcripts go",
          not ev.exists() and not tx.exists() and r["bytes"] > 0, str(r))
    check("collapse: the run keeps its journal, phase, closeout and the demo still to be sent, and loses its streams",
          all((d / rel).exists() for rel in ("journal.jsonl", "phase.json", "evidence/closeout.json", "evidence/demo/a.webm"))
          and not (d / "seats" / "build-1" / "stdout.txt").exists(), "")

def _text_opens_without_encoding(root):
    """Every open(), read_text() and write_text() under `root` that reads or writes text with
    no encoding named, as file:line. A binary mode carries no codec and is not counted."""
    import ast
    found = []
    for f in sorted(Path(root).rglob("*.py")):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name not in ("open", "read_text", "write_text") or any(k.arg == "encoding" for k in n.keywords):
                continue
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "os":
                continue
            if name == "open":
                modes = [a.value for a in (n.args[1:2] if isinstance(fn, ast.Name) else n.args[0:1]) if isinstance(a, ast.Constant)]
                modes += [k.value.value for k in n.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
                if any("b" in str(m) for m in modes):
                    continue
            found.append(f"{f.relative_to(root)}:{n.lineno}")
    return found


def drill_engine_reads_and_writes_utf8(repo):
    """A Windows seat wrote its report in the system code page and a finished drive counted as
    a crash. Every file the engine opens as text names UTF-8, and every seat process starts
    with Python told to write UTF-8 whatever the host's code page."""
    bare = _text_opens_without_encoding(ENGINE / "hb")
    check("utf-8: no open, read_text or write_text in engine/hb leaves the codec to the host", not bare, str(bare[:8]))
    probe = repo.parent / "seat-env.txt"
    script = repo.parent / "seat_env.py"
    script.write_text("import os\nopen(%r, 'w', encoding='utf-8').write(os.environ.get('PYTHONUTF8', '') + '|' + "
                      "os.environ.get('PYTHONIOENCODING', ''))\n" % str(probe), encoding="utf-8")
    rc, js, raw = run_hotfix(repo, {"build": {"mode": "ok", **TOUCH, "exec": [str(script)]}})
    seen = probe.read_text(encoding="utf-8") if probe.exists() else "no probe written"
    check("utf-8: a seat process starts with PYTHONUTF8=1 and PYTHONIOENCODING=utf-8", seen == "1|utf-8", seen)


def drill_usage_limit_parks_the_run(repo):
    """Run 20261007-184837 met the plan's weekly limit: eight seats crashed and four were
    respawned into the same 429. A seat whose stream says the limit is spent parks the run
    at that seat with an L- stop and the reset; --resume re-enters at that seat."""
    from hb.runner import adapters
    seen = adapters.usage_limit('{"type":"result","api_error_status":429,"result":"You hit it, resets 6am (UTC)"}')
    check("usage limit: a 429 result line is read with its reset", (seen or {}).get("resets") == "6am (UTC)", str(seen))
    check("usage limit: a token count of 429 in prose is not a limit",
          adapters.usage_limit('usage: 429 tokens\n{"type":"result","usage":{"output_tokens":429}}') is None)
    script = {"build": {"mode": "ok", **TOUCH}, "review.1": {"mode": "usagelimit"}}
    rc, js, raw = run_hotfix(repo, script)
    rid = js.get("run_id", "")
    evs = journal(repo, rid)
    states = [s["state"] for s in seats(evs, "review")]
    check("usage limit: the run parks, rc 3", rc == 3 and js.get("verdict") == "paused-limit", raw[-600:])
    check("usage limit: the seat is rate-limited, not crashed, and never respawned",
          states == ["started", "rate-limited"] and not any(e.get("kind") in ("respawn", "wait") for e in evs), str(states))
    stops = js.get("stops") or []
    check("usage limit: the stop reads [L-<id>] usage limit: <the CLI's words>",
          len(stops) == 1 and stops[0].startswith("[L-") and "] usage limit: You've hit your weekly limit" in stops[0], str(stops))
    phase = json.loads((run_dir(repo, rid) / "phase.json").read_text(encoding="utf-8"))
    check("usage limit: the phase is paused-limit with the reset time",
          phase.get("phase") == "paused-limit" and phase.get("resets") == "Oct 11, 6am (America/Chicago)"
          and phase.get("resets_at", "").startswith("2026-10-11"), str(phase)[:300])
    sys.path.insert(0, str(ENGINE))
    from hb.record import view
    here = os.getcwd(); os.chdir(str(repo))
    try:
        v = view.run(rid, detail=False)
    finally:
        os.chdir(here)
    check("usage limit: the run card reads paused: usage limit, resets <time>",
          (v or {}).get("where") == "paused: usage limit, resets Oct 11, 6am (America/Chicago)" and not (v or {}).get("needs"),
          str({k: (v or {}).get(k) for k in ("state", "where", "needs")}))
    from hb.runner import usage
    from hb.record import watcher
    spent = usage.of(run_dir(repo, rid), evs)
    check("usage limit: the header's meters read the limit off the rate-limited seat, and the watcher holds the run as done",
          (spent.get("limit") or {}).get("resets") == 1791716400 and "paused-limit" in watcher.DONE, str(spent.get("limit")))
    rc2, js2, raw2 = run_hotfix(repo, script, extra=("--resume", rid))
    evs2 = journal(repo, rid)
    check("usage limit: --resume re-enters at the review seat, the build is kept",
          js2.get("verdict") == "mergeable" and [s["state"] for s in seats(evs2, "build")] == ["started", "ok"]
          and [s["state"] for s in seats(evs2, "review")][-1] == "ok", raw2[-400:])


def _record_fixture(repo, run_id):
    """A run folder with pictures, a gate line, a seat report, a film and a demo, over a repo
    whose HEAD deleted one test file the builder's note never named."""
    from hb.record.journal import Journal
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    j = Journal(run_id)
    ev = j.dir / "evidence"
    for rel in ("after/T-01-desktop.png", "after/T-01-phone.png", "before/T-01-desktop.png", "red/T-02.png", "after/HF-01-desktop.png"):
        (ev / rel).parent.mkdir(parents=True, exist_ok=True)
        (ev / rel).write_bytes(png)
    (ev / "gates").mkdir(parents=True, exist_ok=True)
    (ev / "gates" / "lint.txt").write_text("TRUE_EXIT=0\n", encoding="utf-8")
    (ev / "video").mkdir(parents=True, exist_ok=True)
    (ev / "video" / "T-01.webm").write_bytes(b"film")
    (ev / "demo").mkdir(parents=True, exist_ok=True)
    (ev / "demo" / "demo.webm").write_bytes(b"demo")
    (j.dir / "seats" / "build-1").mkdir(parents=True, exist_ok=True)
    (j.dir / "seats" / "build-1" / "report.json").write_text("{}", encoding="utf-8")
    g = lambda *args: sh(["git", "-C", str(repo), *args])
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_old_pin.py").write_text("def test_x(): pass\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "a pin")
    _, base, _ = g("rev-parse", "HEAD")
    (repo / "tests" / "test_old_pin.py").unlink()
    g("add", "-A"); g("commit", "-q", "-m", "the pin retired")
    items = [{"item": "T-01", "kind": "screen", "then": "the drawer opens"},
             {"item": "T-02", "kind": "api", "then": "the row is hidden"},
             {"item": "T-03", "kind": "api", "then": "no screen, so no after picture"},
             {"item": "HF-01", "then": "a sentence-born item with no kind: its picture on disk counts"}]
    receipts = [{"item": "T-01", "state": "WORKS", "identity": {"tip": "a" * 40, "venue": "sealed"}},
                {"item": "T-02", "state": "BROKEN", "observed": "the row stayed"}, {"item": "T-03", "state": "WORKS"},
                {"item": "HF-01", "state": "WORKS"}]
    return j, ev, base.strip(), items, receipts


def _branch_tip(repo, branch):
    rc, remote, _ = sh(["git", "-C", str(repo), "ls-remote", "origin", f"refs/heads/{branch}"])
    return remote.split()[0] if remote.strip() else ""


def drill_pr_links_the_published_record(repo):
    """The PR carried the whole record, GitHub refused a 65k body, and the trim that followed
    lost rows under a link to a force-pushed branch. The record goes up as one commit on
    evidence/<run>, parented on the previous publish and never forced, before the PR body is
    written; the body is bounded by construction and links record.md by that commit's sha; a
    retired test with no replacement shown failing is named in the record and the synopsis."""
    from hb.record import record
    from hb.record import prbody
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j, ev, base, items, receipts = _record_fixture(repo, "20261014-010101-hotfix-record")
        contract = {"goal": "g", "issue": "#1", "items": items}
        c = {"repo": "drill/drill", "branches": {"landing": "dev"}, "landing": {"status_context": "Walk"}}
        gates = [{"name": "lint", "true_exit": 0}]
        fs, published, retired = record.land(j, repo, c, contract, ev, base, "hotfix/x", receipts, gates, [], None,
                                                     {"decisions": [{"text": "kept the drawer"}]}, False, [])
        branch = "evidence/" + j.run_id
        sha_ = _branch_tip(repo, branch)
        check("record: one commit on evidence/<run>, pushed without force, its sha in the result",
              sha_ and published.get("commit") == sha_ and published.get("pushed"), str(published))
        rc, tree, _ = sh(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha_]) if sha_ else (1, "", "")
        names = sorted(tree.split())
        want = [f"evidence/{j.run_id}/{x}" for x in ("after/HF-01-desktop.png", "after/T-01-desktop.png", "after/T-01-phone.png",
                "before/T-01-desktop.png", "gates/lint.txt", "record.json", "record.md", "red/T-02.png", "seats/build-1/report.json")]
        check("record: the commit holds record.md, record.json, the pictures, the gate line and the seat report, no film, no demo",
              names == want, str(names))
        rc, md, _ = sh(["git", "-C", str(repo), "show", f"{sha_}:evidence/{j.run_id}/record.md"])
        check("record: record.md names every item with its identity, the retired test without a replacement, the gate line",
              "| the row is hidden | BROKEN |" in md and "tip aaaaaaaaa, venue sealed" in md
              and "`tests/test_old_pin.py`: NO REPLACEMENT shown failing on the same defect" in md and "lint: TRUE_EXIT=0" in md
              and 'src="after/T-01-desktop.png"' in md, md[:1200])
        link = f"https://github.com/drill/drill/blob/{sha_}/evidence/{j.run_id}/record.md"
        check("record: the link carries the commit sha, never the branch name",
              published.get("url") == link and f"blob/{branch}/" not in link, str(published.get("url")))
        body = prbody.render(contract, "b" * 40, j.run_id, gates, None, receipts, {}, ev, fs, published,
                             retired=retired, issues=["1"])
        check("pr body: the synopsis links the record at its sha, counts the rows, names the retired test, says the state",
              link in body and "3 WORKS, 1 BROKEN, 0 UNMEASURED, 0 DIVERGED" in body
              and "1 without a replacement shown failing: `tests/test_old_pin.py`" in body
              and body.splitlines()[2].startswith("**run-complete.** record published at " + sha_[:9])
              and "not mergeable: 1 item red" in body and "the row stayed" not in body and str(ev) not in body, body[:1500])
        many = [{"item": f"M-{n:03d}", "kind": "screen", "then": "a screen"} for n in range(1, 301)]
        rows = [{"item": it["item"], "state": "BROKEN", "observed": "o" * 3000} for it in many]
        big = prbody.render({"goal": "g", "issue": "#1", "items": many}, "b" * 40, j.run_id, gates, None, rows, {}, ev, fs,
                            published, issues=["1"])
        check("pr body: 300 red rows leave the body bounded; the rows are in the record, not the body",
              len(big) < 20000 and "300 broken" in big and "o" * 100 not in big, f"{len(big)} chars")
        (ev / "gates" / "suite.txt").write_text("TRUE_EXIT=1\n", encoding="utf-8")
        again = record.publish(j, repo, c, ev, {}, False)
        rc, parents, _ = sh(["git", "-C", str(repo), "rev-list", "--parents", "-n", "1", again.get("commit") or sha_])
        check("record: a second publish is a new commit with the first as parent; the branch is append-only",
              again.get("commit") != sha_ and parents.split()[1:] == [sha_] and _branch_tip(repo, branch) == again.get("commit"),
              f"{again} {parents}")
        refs = record.references(j.dir)
        check("record: record.json lists every path the commit holds, as the prune reads them",
              "evidence/record.md" in refs and "evidence/after/T-01-desktop.png" in refs and "seats/build-1/report.json" in refs
              and "evidence/gates/suite.txt" in refs and "evidence/video/T-01.webm" not in refs, str(sorted(refs))[:400])
    finally:
        os.chdir(here)


def drill_audit_disagreements_are_printed(repo):
    """The loop journaled the audit's disagreements with the checks and nobody printed them.
    The record and the PR's first screen both carry one block: each false green by case id
    with the check's line and the audit's words, the cases the audit drove, and the checked
    cases it never reached, read off the journal event. A run with no audit reads unmeasured."""
    from hb.record import record
    from hb.record import prbody
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j, ev, base, items, receipts = _record_fixture(repo, "20261024-010101-hotfix-audit")
        j.append("seat", seat="build", attempt=1, state="ok", minutes=90)
        j.append("audit-disagreements", branch="hotfix/x", false_greens=1, false_reds=0, cases=2, excluded=["T-03-a"],
                 disagreed={"false_greens": [{"case": "T-01-a", "rule": "T-01", "check": "WORKS",
                                              "audit": "the drawer stayed shut after the tap"}], "false_reds": []})
        contract = {"goal": "g", "issue": "#1", "items": items}
        c = {"repo": "drill/drill", "branches": {"landing": "dev"}, "landing": {"status_context": "Walk"}}
        gates = [{"name": "lint", "true_exit": 0}]
        al = prbody.journal_allowance(j)
        fs, published, retired = record.land(j, repo, c, contract, ev, base, "hotfix/x", receipts, gates, [], None,
                                                     {}, False, [], allowance=al)
        md = (ev / "record.md").read_text(encoding="utf-8")
        body = prbody.render(contract, "b" * 40, j.run_id, gates, None, receipts, {}, ev, fs, published,
                             retired=retired, allowance=al)
        for name, text in (("record", md), ("first screen", body)):
            block = text.split("## Audit disagreements", 1)[1].split("\n## ", 1)[0] if "## Audit disagreements" in text else ""
            check(f"{name}: the false green is named by case with the check's line and the audit's words",
                  "T-01-a" in block and "WORKS" in block and "the drawer stayed shut" in block, block or text[:600])
            check(f"{name}: the denominator is the cases the audit drove and the unreached case is named",
                  "drove: 2" in block and "T-03-a" in block, block)
            check(f"{name}: the seat hours stand beside the score", "1.5 h" in block, block)
        j2, ev2, base2, items2, receipts2 = _record_fixture(repo, "20261024-020202-hotfix-noaudit")
        fs2, published2, retired2 = record.land(j2, repo, c, contract, ev2, base2, "hotfix/y", receipts2, gates, [], None,
                                                         {}, False, [], allowance=prbody.journal_allowance(j2))
        md2 = (ev2 / "record.md").read_text(encoding="utf-8")
        body2 = prbody.render(contract, "b" * 40, j2.run_id, gates, None, receipts2, {}, ev2, fs2, published2,
                              allowance=prbody.journal_allowance(j2))
        check("record and first screen: no audit reads unmeasured, never zero",
              "no audit: unmeasured" in md2 and "no audit: unmeasured" in body2 and "False greens (check WORKS" not in md2,
              md2[-600:])
    finally:
        os.chdir(here)


def drill_finished_is_three_states(repo):
    """Both waves ended 'shipped' and one shipped with 87 UNMEASURED rows. Finished is three
    states: the record published (truthful, may be red), the run complete, the PR mergeable.
    Each is a journaled finish event the reducer folds, the title and first line say the
    state, and a red record is never written in a finished run's words."""
    from hb.record import record
    from hb.record import prbody
    from hb.record import reducer
    from hb.beats import landing
    from hb.contract import policy
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j, ev, base, items, receipts = _record_fixture(repo, "20261014-020202-hotfix-states")
        contract = {"goal": "g", "issue": "#1", "items": items}
        c = {"repo": "drill/drill", "branches": {"landing": "dev"}, "landing": {"status_context": "Walk"}}
        fs, published, _ = record.land(j, repo, c, contract, ev, base, "hotfix/x", receipts, [], [], None, {}, False, [])
        fin = reducer.finish_of(j.events())
        check("states: a red record publishes, the run completes, the PR is not mergeable, and the reducer folds the three",
              fs["state"] == "run-complete" and fin == {"record": "published", "run": "complete", "merge": "not-eligible",
                                                         "state": "not-mergeable", "why": ["1 item red"]}, str((fs, fin)))
        check("states: the title names the state, never a finished word",
              prbody.title("hotfix", "the drawer opens", fs["state"]) == "hotfix [run-complete]: the drawer opens"
              and "shipped" not in prbody.finish_line(fs, published), prbody.finish_line(fs, published))
        green = [dict(r, state="WORKS") for r in receipts]
        from hb.record import firstscreen
        fs2 = firstscreen.finish_state([], green, items, "published", gates=[{"name": "lint", "true_exit": 0}])
        check("states: every row and gate green on a published record is mergeable",
              fs2["state"] == "mergeable" and fs2["merge"] == "eligible" and fs2["why"] == []
              and prbody.title("slice", "x", fs2["state"]).startswith("slice [mergeable]: "), str(fs2))
        fs3 = firstscreen.finish_state([], green, items, "unpublished")
        check("states: a green run whose record did not publish is not mergeable, and says why",
              fs3["state"] == "run-complete" and fs3["why"] == ["record not published"], str(fs3))
        fs4 = firstscreen.finish_state(["[S-aaaa] a stop"], green, items, "published")
        check("states: a stopped run is stopped, whatever its rows", fs4["state"] == "stopped" and fs4["run"] == "stopped", str(fs4))
        calls = []
        real_gh = landing.gh
        landing.gh = lambda args, timeout=45: (calls.append(list(args)) or (0, "", ""))
        try:
            landing.post_walk(j, c, "c" * 40, fs, False)
            landing.post_walk(j, c, "d" * 40, fs2, False)
        finally:
            landing.gh = real_gh
        states = [next(x for x in call if x.startswith("state=")) for call in calls]
        check("states: the Walk status derives from the merge fact: failure naming why, success when mergeable",
              states == ["state=failure", "state=success"] and "description=1 item red" in calls[0], str(calls))
        landing.gh = lambda args, timeout=45: (calls.append(list(args)) or (0, "", ""))
        try:
            landing.post_walk(j, c, "c" * 40, fs2, False)
        finally:
            landing.gh = real_gh
        check("states: the same tip walked red and then green posts the green too, never the cached red",
              len(calls) == 3 and "state=success" in " ".join(calls[2]), str(calls[-1]))
        landing.gh = lambda args, timeout=45: (calls.append(list(args)) or (0, "", ""))
        try:
            landing.post_walk(j, dict(c, landing={**c["landing"], "status_via": "dispatch"}), "e" * 40, fs2, False)
        finally:
            landing.gh = real_gh
        check("states: with landing.status_via dispatch the verdict rides a repository dispatch the product's walk workflow posts as Actions",
              "dispatches" in calls[-1][1] and "client_payload[state]=success" in calls[-1] and "event_type=walk" in calls[-1], str(calls[-1]))
        v = reducer.finish_of([{"kind": "finish", "state": "record-published", "record": "unpublished"},
                               {"kind": "finish", "state": "run-stopped", "run": "stopped"}])
        check("states: a stopped run's finish folds to its last event", v["state"] == "run-stopped" and v["record"] == "unpublished", str(v))
    finally:
        os.chdir(here)


def drill_closeout_keeps_referenced_artifacts(repo):
    """The closeout pruned the pictures a PR still linked. A prune reads the record's
    reference list and keeps every path it names; what the record does not name still goes."""
    from hb.record import prune
    d = run_dir(repo, "20261014-030303-hotfix-ref")
    for rel in ("journal.jsonl", "evidence/record.md", "evidence/after/T-01-desktop.png", "evidence/gates/lint.txt",
                "evidence/video/T-01.webm", "evidence/demo/d.webm", "seats/build-1/report.json", "seats/build-1/stdout.txt",
                "evidence/demo-owed.json"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x", encoding="utf-8")
    (d / "evidence" / "record.json").write_text(json.dumps({"files": ["record.md", "after/T-01-desktop.png", "gates/lint.txt",
                                                                        "seats/build-1/report.json"]}), encoding="utf-8")
    r = prune.prune_run(d)
    kept = all((d / rel).exists() for rel in ("evidence/record.md", "evidence/record.json", "evidence/after/T-01-desktop.png",
                                               "evidence/gates/lint.txt", "seats/build-1/report.json", "journal.jsonl"))
    gone = not any((d / rel).exists() for rel in ("evidence/video/T-01.webm", "evidence/demo/d.webm", "seats/build-1/stdout.txt"))
    check("prune: every path the record lists stays; the film, the demo and the stream go", kept and gone and r["files"] == 3, str(r))
    (d / "evidence" / "record.json").unlink()
    r2 = prune.prune_run(d)
    check("prune: with no record the old rule stands; the pictures and the gate line go",
          not (d / "evidence" / "after" / "T-01-desktop.png").exists() and (d / "seats" / "build-1" / "report.json").exists(), str(r2))
    check("prune: the closeout's demo debt, written right before the prune, stays",
          (d / "evidence" / "demo-owed.json").exists(), str(r2))


def drill_demo_starts_after_the_merge(repo):
    """The owner typed /demo after every merge, and a merge nobody filmed went unseen. A
    closeout of every merged run writes demo-owed.json and starts the demo; the demo
    waits for the merge's deploy to read green before its seat sits, and films nothing on a
    red deploy."""
    from hb.beats import closeout
    from hb.beats import demo
    from hb.record.journal import Journal
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j = Journal("20261008-030303-hotfix-screen")
        j.op("contract", lambda: {"issue": "#5", "items": [{"item": "HF-01", "kind": "screen"}]})
        started = []
        res = closeout.owe_demo(j.run_id, {"kind": "hotfix", "issue": "#5"}, "https://x/pr/5", "abc123",
                                start=lambda rid: started.append(rid) or {"pid": 1})
        owed = json.loads(demo.owed_path(j.run_id).read_text(encoding="utf-8")) if demo.owed_path(j.run_id).exists() else {}
        check("demo: a merged run writes demo-owed.json and starts the demo",
              owed == {"run_id": j.run_id, "pr": "https://x/pr/5", "merge_sha": "abc123", "issue": "#5"}
              and started == [j.run_id] and res.get("owed"), str(owed) + str(res))
        res = closeout.owe_demo(j.run_id, {"kind": "hotfix", "issue": "#5"}, "p", "abc123", no_demo=True,
                                start=lambda rid: started.append(rid))
        check("demo: --no-demo writes what is owed and starts nothing", res.get("owed") and started == [j.run_id], str(res))
        out_dir = j.dir / "evidence" / "demo"
        asked = []

        def probe(sha):
            asked.append((sha, (out_dir / "brief.md").exists()))
            return (10, {"verdict": "running"}) if len(asked) < 3 else (0, {"verdict": "green"})
        rec = demo.launch_demo(j.run_id, probe=probe, poll_s=0, sleep=lambda s: None, runtime="fake",
                               provision=lambda slug: {"tenant_id": "t-5", "name": "demo-" + slug})
        brief = (out_dir / "brief.md").read_text(encoding="utf-8") if (out_dir / "brief.md").exists() else ""
        check("demo: demo-launch waits for the merge's deploy to read green before the seat sits",
              [a[0] for a in asked] == ["abc123"] * 3 and not any(a[1] for a in asked) and rec.get("verdict") == "filmed", str(asked) + str(rec))
        check("demo: the seat's brief is commands/demo.md plus the run's facts, its output under evidence/demo",
              "# /demo" in brief and j.run_id in brief and "record.md" in brief and (out_dir / "stdout.txt").exists()
              and json.loads((out_dir / "launch.json").read_text(encoding="utf-8")).get("verdict") == "filmed",
              brief[-300:] + " launch: " + ((out_dir / "launch.json").read_text(encoding="utf-8")[:400] if (out_dir / "launch.json").exists() else "no launch.json"))
        (out_dir / "brief.md").unlink()
        rec = demo.launch_demo(j.run_id, probe=lambda sha: (2, {"verdict": "deploy-failed"}), poll_s=0, sleep=lambda s: None,
                               runtime="fake")
        check("demo: a red deploy films nothing", rec.get("verdict") == "no-demo" and not (out_dir / "brief.md").exists(), str(rec))
    finally:
        os.chdir(here)



def drill_a_dev_venue_is_born_with_an_owner_and_an_expiry(repo):
    """Dev venues the harness made were never deleted, because nothing said whose they were. A
    venue's registry line carries its owner, tool, birth and expiry, stamped onto the line the
    provisioner wrote, and the demo launcher stamps its run on the venue its seat made."""
    from hb.venue import devshops
    from hb.beats import demo
    from hb.record.journal import Journal
    here = os.getcwd(); os.chdir(str(repo))
    try:
        path = devshops.registry_path()
        made = devshops.register(path, "walk-venue", "session", "walk", now="2026-01-01T00:00:00Z")
        check("dev venue: a venue with no provisioner line gets one line with its owner, tool, birth and a 7 day expiry",
              devshops.read(path) == [made] and made == {"name": "walk-venue", "owner": "session", "tool": "walk",
                                                        "born": "2026-01-01T00:00:00Z", "expires": "2026-01-08T00:00:00Z"}, str(devshops.read(path)))
        j = Journal("20261009-050505-hotfix-film")
        made_slugs = []

        def provisioner(slug):
            # the product's demo provisioner writes its line at birth, then answers with the tenant
            made_slugs.append(slug)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"tenant_id": "tenant-1", "name": "demo-" + slug, "wave": slug, "status": "provisioned",
                                    "created_at": "2026-01-02T10:00:00Z"}) + chr(10))
            return {"tenant_id": "tenant-1", "name": "demo-" + slug}
        demo.owed_path(j.run_id).parent.mkdir(parents=True, exist_ok=True)
        demo.owed_path(j.run_id).write_text(json.dumps({"run_id": j.run_id, "pr": "p", "merge_sha": "abc", "issue": "#7"}), encoding="utf-8")
        real_launch = demo.adapters.launch

        def dies(*a, **k):
            raise RuntimeError("the seat died")
        demo.adapters.launch = dies
        try:
            demo.launch_demo(j.run_id, probe=lambda sha: (0, {}), poll_s=0, sleep=lambda s: None, runtime="fake",
                             provision=provisioner)
            died = False
        except RuntimeError:
            died = True
        finally:
            demo.adapters.launch = real_launch
        lines = devshops.read(path)
        film = [e for e in lines if e.get("owner") == f"run:{j.run_id}"]
        check("dev venue: the launcher owns the venue at provision time, so a seat that dies after leaves one owned line, a 1 day expiry",
              died and len(film) == 1 and film[0].get("name") == f"demo-{made_slugs[0]}" and film[0].get("tool") == "demo"
              and film[0].get("born") == "2026-01-02T10:00:00Z" and film[0].get("expires") == "2026-01-03T10:00:00Z"
              and film[0].get("tenant_id") == "tenant-1" and film[0].get("status") == "provisioned" and len(lines) == 2, str(lines))
        check("dev venue: the closeout finds the venue the dead demo left", devshops.owned_by(lines, f"run:{j.run_id}") == film, str(lines))
        rec = demo.launch_demo(j.run_id, probe=lambda sha: (0, {}), poll_s=0, sleep=lambda s: None, runtime="fake",
                               provision=lambda slug: {"tenant_id": "tenant-2", "name": "demo-again"})
        brief = (Path(rec["out_dir"]) / "brief.md").read_text(encoding="utf-8")
        check("dev venue: the brief names the venue the launcher made, and the launch record names it",
              "`demo-again` (tenant tenant-2)" in brief and "--slug" not in brief.split("# This demo")[-1]
              and (rec.get("venue") or {}).get("name") == "demo-again", str(rec))
        rec = demo.launch_demo(j.run_id, probe=lambda sha: (0, {}), poll_s=0, sleep=lambda s: None, runtime="fake",
                               provision=lambda slug: {"error": "provision failed at: sign-up", "failed_step": "sign-up"})
        check("dev venue: a provision that made no tenant sits no seat and owns nothing",
              rec.get("verdict") == "no-demo" and rec["provision"]["failed_step"] == "sign-up"
              and len(devshops.read(path)) == 3, str(rec))
    finally:
        os.chdir(here)


def drill_a_closeout_purges_the_dev_venues_its_run_made(repo):
    """A closeout purges every dev venue the registry gives to its run, through the product's
    `venue.purge`; a line leaves the registry only when the product answered ok, and a refused
    venue is a left-behind row with the product's words, never dropped."""
    from hb.venue import devshops
    from hb.beats import closeout
    here = os.getcwd(); os.chdir(str(repo))
    try:
        path = devshops.registry_path()
        for name, owner in (("film-a", "run:r1"), ("film-b", "run:r1"), ("other", "run:r2"), ("film-a", "session")):
            devshops.register(path, name, owner, "demo")
        asked = []

        def purge(name):
            asked.append(name)
            return (True, '{"ok": true}') if name == "film-a" else (False, "refused: film-b carries no provenance mark")
        rows = closeout.purge_dev_venues("r1", purge=purge)
        left = [e["name"] for e in devshops.read(path)]
        check("closeout: each venue the run made is purged through the product and recorded",
              asked == ["film-a", "film-b"] and rows == [
                  {"what": "Dev venues this run made: film-a", "ok": True, "state": None},
                  {"what": "Dev venues this run made: film-b", "ok": False,
                   "state": "left behind: refused: film-b carries no provenance mark"}], str(rows))
        check("closeout: only the purged line leaves the registry; the refused one, another run's and a same-named venue of another owner stay",
              left == ["film-b", "other", "film-a"] and devshops.read(path)[-1]["owner"] == "session", str(devshops.read(path)))
        _venue_stub(repo, ["states"], purge=[{"print": json.dumps({"ok": True})}])
        ok, words = devshops.venue_purge("film-b")
        check("closeout: the door is the product's venue.purge, handed the venue's name",
              ok and _venue_stub_calls(repo, "purge") == [["purge", "film-b"]], words)
    finally:
        os.chdir(here)


def _closeout_persisted(run_id):
    """Run hb closeout on a merged run with every network read stubbed; returns closeout.json."""
    from hb.beats import closeout
    stubs = {"_merged": lambda pr: {"state": "MERGED", "headRefName": "hotfix/gone", "mergeCommit": {"oid": ""}},
             "_hb": lambda *a, **k: {}, "_live": lambda c, sha: (True, "served"),
             "_paid_runners": lambda branch: ([], None), "owe_demo": lambda *a, **k: {"owed": False},
             "success_check": lambda *a, **k: []}
    real = {k: getattr(closeout, k) for k in stubs}
    real_view, real_prune = closeout.view.run, (closeout.prune.prune_run, closeout.prune.collapse)
    for k, v in stubs.items():
        setattr(closeout, k, v)
    closeout.view.run = lambda rid: {"id": rid, "kind": "hotfix", "pr": "p", "items": []}
    closeout.prune.prune_run = closeout.prune.collapse = lambda *a, **k: {}
    try:
        closeout.cmd_closeout(SimpleNamespace(run_id=run_id, pr="p", no_demo=True))
    except SystemExit:
        pass
    finally:
        for k, v in real.items():
            setattr(closeout, k, v)
        closeout.view.run = real_view
        closeout.prune.prune_run, closeout.prune.collapse = real_prune
    return json.loads((closeout.view.root() / run_id / "evidence" / "closeout.json").read_text(encoding="utf-8"))


def drill_a_closeout_names_every_dev_venue_it_could_not_purge(repo):
    """A closeout that cannot purge its run's venue, because the product gives no `venue.purge`
    or the purge answers nonzero, persists a named left-behind row and keeps the registry line;
    one whose purge answers ok purges the venue the demo owned and drops its line."""
    from hb.venue import devshops
    from hb.beats import demo
    here = os.getcwd(); os.chdir(str(repo))
    try:
        path = devshops.registry_path()
        venue, _ = demo.provision_venue("rc", provision=lambda slug: {"tenant_id": "t-9", "name": "demo-" + slug})
        cases = [("purge not offered", None, "not offered: purge"),
                 ("purge nonzero", [{"print": "refused: no provenance mark", "exit": 3}], "refused: no provenance mark")]
        for label, answers, words in cases:
            _venue_stub(repo, ["states"], **({"purge": answers} if answers else {}))
            rep = _closeout_persisted("rc")
            row = [r for r in rep["cleaned"] if r["what"].startswith("Dev venues this run made")]
            check(f"closeout {label}: the persisted result names the venue left behind with why, and the line stays",
                  len(row) == 1 and not row[0]["ok"] and row[0]["state"].startswith("left behind: ") and words in row[0]["state"]
                  and venue["name"] in row[0]["what"] and not rep["clean"] and devshops.owned_by(devshops.read(path), "run:rc"),
                  str(row) + str(devshops.read(path)))
        _venue_stub(repo, ["states"], purge=[{"print": json.dumps({"ok": True})}])
        rep = _closeout_persisted("rc")
        row = [r for r in rep["cleaned"] if r["what"].startswith("Dev venues this run made")]
        check("closeout end to end: the venue the demo owned is purged through venue.purge and its line is gone",
              row == [{"what": f"Dev venues this run made: {venue['name']}", "ok": True, "state": "gone"}]
              and not devshops.owned_by(devshops.read(path), "run:rc"), str(row) + str(devshops.read(path)))
    finally:
        os.chdir(here)


def drill_the_sweep_purges_only_expired_dev_venues(repo):
    """Venues nobody closed out are purged by the nightly sweep past their expiry: an expired
    venue is purged, one not yet expired and one never stamped stay, and a dry run names what
    it would purge and deletes nothing."""
    from hb.venue import devshops
    here = os.getcwd(); os.chdir(str(repo))
    try:
        path = devshops.registry_path()
        devshops.register(path, "old-film", "run:r1", "demo", devshops.DEMO_TTL_DAYS, now="2026-01-01T00:00:00Z")
        devshops.register(path, "fresh-walk", "session", "walk", now="2026-01-05T00:00:00Z")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"name": "hand-made", "created_at": "2025-01-01T00:00:00Z"}) + chr(10))
            f.write(json.dumps({"name": "ownerless", "tool": "walk", "expires": "2025-01-02T00:00:00Z"}) + chr(10))
        asked = []
        dry = devshops.sweep(path, dry_run=True, purge=lambda n: asked.append(n) or (True, "ok"), now="2026-01-06T00:00:00Z")
        check("sweep: a dry run names the expired venue and purges and deletes nothing",
              [w["name"] for w in dry["would_purge"]] == ["old-film"] and not asked and len(devshops.read(path)) == 4, str(dry))
        rec = devshops.sweep(path, purge=lambda n: asked.append(n) or (True, "ok"), now="2026-01-06T00:00:00Z")
        check("sweep: only the expired owned venue is purged; the fresh, the unstamped and the ownerless past expiry stay",
              asked == ["old-film"] and rec["purged"] == ["old-film"] and rec["verdict"] == "purged"
              and [e["name"] for e in devshops.read(path)] == ["fresh-walk", "hand-made", "ownerless"], str(rec))
        rc, o, e = sh([sys.executable, HB, "venues", "sweep", "--expired", "--dry-run"], cwd=str(repo))
        check("sweep: hb venues sweep --expired --dry-run reads the registry and changes nothing",
              rc == 0 and last_json(o).get("verdict") == "dry-run" and "would_purge" in last_json(o)
              and [e["name"] for e in devshops.read(path)] == ["fresh-walk", "hand-made", "ownerless"], o + e)
    finally:
        os.chdir(here)


def drill_standing_stop_waits_on_you(repo):
    """A standing run whose last step raised a stop (a sync conflict, a red gate) shows the
    stop on its card and counts in "waiting on you", as a stopped run does; a stop an earlier
    step outlived is not shown, and a standing run with none is live with its PR, not halted."""
    sys.path.insert(0, str(ENGINE))
    from hb.record import view
    from hb.runner import liveness
    d = run_dir(repo, "20990101-000000-slice-standing")
    pr = "https://example.invalid/pr/5"
    base = [{"kind": "phase", "phase": "opened", "beat": "slice", "ref": "#5", "ts": _iso(300)},
            {"kind": "phase", "phase": "standing", "pr": pr, "ts": _iso(200)},
            {"kind": "next", "verb": "fix", "text": "x", "n": 1, "ts": _iso(190)},
            {"kind": "stop", "id": "G-aaaa", "stop_kind": "gate", "text": "an old red gate", "ts": _iso(180)},
            {"kind": "next-done", "verb": "fix", "n": 1, "result": "stopped", "ts": _iso(170)},
            {"kind": "phase", "phase": "standing", "pr": pr, "ts": _iso(160)},
            {"kind": "next", "verb": "sync", "text": "", "n": 2, "ts": _iso(150)}]
    saved = view.root
    view.root = lambda: d.parent
    try:
        liveness.record(d)
        _write_journal(d, base + [{"kind": "next-done", "verb": "sync", "n": 2, "result": "up to date", "ts": _iso(140)},
                                  {"kind": "phase", "phase": "standing", "pr": pr, "ts": _iso(130)}])
        v = view.run(d.name) or {}
        check("standing card: live with its PR, nothing owed, not halted",
              v.get("state") == "standing" and v.get("pr") == pr and not v.get("stops") and not v.get("halt")
              and "standing" in str(v.get("where")), str({k: v.get(k) for k in ("state", "pr", "stops", "halt", "where")}))
        _write_journal(d, base + [{"kind": "stop", "id": "S-bbbb", "stop_kind": "other", "text": "origin/dev does not merge clean", "ts": _iso(140)},
                                  {"kind": "next-done", "verb": "sync", "n": 2, "result": "conflict", "ts": _iso(130)},
                                  {"kind": "phase", "phase": "standing", "pr": pr, "ts": _iso(120)}])
        v = view.run(d.name) or {}
        check("standing card: the last step's stop is on the card, the outlived one is not",
              [s.get("id") for s in v.get("stops") or []] == ["S-bbbb"] and v.get("halt") is True, str(v.get("stops")))
        row = view.row_of(d.name)
        home = view.compose([row], {d.name: view.card(d.name)})
        check("standing card: placed on the home and counted in waiting on you",
              [x.get("id") for x in home.get("live") or []] == [d.name] and home.get("needs") == 1, str(home.get("needs")))
    finally:
        view.root = saved

TOY_RUNNER = """import os, runpy, sys
failed = passed = 0
with open(os.path.join(".context", "toy-runs.txt"), "a", encoding="utf-8") as f:
    f.write(os.environ.get("DATABASE_URL", "no database") + " " + " ".join(sys.argv[1:]) + "\\n")
for path in sys.argv[1:]:
    for name, fn in runpy.run_path(path).items():
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
            except AssertionError as ex:
                failed += 1
                print(f"FAILED {path}::{name} - {ex}")
print(f"{failed} failed, {passed} passed")
sys.exit(1 if failed else 0)
"""

TOY_TEST_DB = """import json, os, sys
verb, slug = sys.argv[1], sys.argv[sys.argv.index("--for") + 1]
with open(os.path.join(".context", "toy-leases.txt"), "a", encoding="utf-8") as f:
    f.write(f"{verb} {slug}\\n")
print(json.dumps({"url": f"toy://server/db_{slug}"} if verb == "lease" else {"dropped": [slug]}))
"""


def drill_pr_create_refused_is_a_named_stop(repo):
    """The PR did not open (`Body is too long`) and the run ended with no PR and nothing to
    resume. A refused create is a stop named by the branch with gh's words; a resume tries
    the create again; the body goes as rendered, never trimmed, on a create and a refresh."""
    from hb.beats import landing
    from hb.record.journal import Journal
    calls, refuse = [], [2]
    real_gh = landing.gh

    def fake_gh(args, timeout=45):
        calls.append(list(args))
        if args[:2] == ["pr", "create"]:
            if refuse[0]:
                refuse[0] -= 1
                return 1, "", "pull request create failed: GraphQL: Body is too long (maximum is 65536 characters)"
            return 0, "https://x/pr/5\n", ""
        return 0, "[]", ""
    landing.gh = fake_gh
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j = Journal("20261013-010101-wave-long")
        _heavy_measured(j, repo)
        c = {"repo": "drill/drill", "branches": {"landing": "dev"}, "landing": {"status_context": "Walk"}}
        text = "Head\n\n**run-complete.** record published at abc.\n\n## Record\n\n- the record: https://x/blob/abc/record.md\n\nIssue: #3\n"
        outs, stops_seen = [], []
        for _ in range(3):
            stops = []
            outs.append(landing.open_pr(j, repo, "wave/long", {"goal": "g", "issue": "#3"}, c, False,
                                        text, beat="wave", stops=stops))
            stops_seen.append(stops)
        creates = [x for x in calls if x[:2] == ["pr", "create"]]
        check("pr create: a refusal is a named stop with gh's words, never a silent null",
              len(stops_seen[0]) == 1 and stops_seen[0][0].startswith("[S-") and "Body is too long" in stops_seen[0][0]
              and outs[0].get("url") is None, str(stops_seen[0]))
        check("pr create: a resume tries the create again, under the same stop id while it fails",
              len(creates) == 3 and stops_seen[1] == stops_seen[0] and outs[2].get("url") == "https://x/pr/5" and stops_seen[2] == [],
              f"{len(creates)} creates, {stops_seen}")
        sent = (j.dir / "pr-body.md").read_bytes().decode("utf-8")
        check("pr body: the body is sent as rendered with the footer, no CR, never trimmed",
              sent == text + landing.FOOTER and "\r" not in sent, sent[:300])
        calls.clear()

        def open_gh(args, timeout=45):
            calls.append(list(args))
            return (0, '[{"url": "https://x/pr/5"}]', "") if args[:2] == ["pr", "list"] else (0, "", "")
        landing.gh = open_gh
        landing.open_pr(j, repo, "wave/long", {"goal": "g", "issue": "#3"}, c, False, text + "more\n", beat="wave",
                        state="mergeable")
        sent = (j.dir / "pr-body.md").read_bytes().decode("utf-8")
        edit = next((x for x in calls if x[:2] == ["pr", "edit"]), [])
        check("pr body: a refresh edits the body as rendered and the title with the state",
              sent == text + "more\n" + landing.FOOTER and "wave [mergeable]: Head" in edit, str(edit))
    finally:
        landing.gh = real_gh; os.chdir(here)


def drill_a_pr_on_a_tip_no_heavy_gate_measured_is_refused(repo):
    """HS-06: every instrument CI runs is measured at the tip the PR carries. A lander that
    reaches the PR with no heavy row at that tip (a late commit after the last measure, a gate
    list that named none) is refused by the runner, named as a stop, and gh is never asked."""
    from hb.beats import landing
    from hb.rig import gitops
    from hb.record.journal import Journal
    calls = []
    real_gh = landing.gh
    landing.gh = lambda args, timeout=45: (calls.append(list(args)) or (0, "[]", ""))
    here = os.getcwd(); os.chdir(str(repo))
    try:
        j = Journal("drill-pr-no-heavy")
        # a heavy row at an older tip measures nothing about this one
        j.append("pr-tip", tip="0" * 40, heavy=[{"name": "suite", "true_exit": 0, "unmeasured": None}], parity=[])
        stops = []
        res = landing.open_pr(j, repo, "hotfix/x", {"goal": "g", "issue": "#1"}, {"branches": {"landing": "dev"}}, False,
                              "body", beat="hotfix", stops=stops)
        refused = [e for e in j.events() if e.get("kind") == "pr-refused"]
    finally:
        landing.gh = real_gh; os.chdir(here)
    check("pr tip: no heavy row at the tip refuses the PR, named", res.get("refused") and refused
          and refused[0].get("tip") == gitops.head_of(repo), str(res))
    check("pr tip: the refusal is a stop with an id, and gh was never asked to create",
          len(stops) == 1 and stops[0].startswith("[S-") and not any(c[:2] == ["pr", "create"] for c in calls),
          str(stops) + str(calls))


def drill_pytest_sees_each_directory_once(_repo):
    """Files of one directory split apart on pytest's command line lose that
    directory's conftest fixtures under pytest 9; every batch and replay goes in path order."""
    from hb.gates import gating
    got = gating.in_path_order(["tests/evals/test_b.py", "tests/test_x.py", "tests/evals/test_a.py::t",
                                "tests\\evals\\test_c.py"])
    dirs = [g.replace("\\", "/").rsplit("/", 1)[0] for g in got]
    check("pytest order: a directory's files are contiguous on the command line",
          len([1 for a, b in zip(dirs, dirs[1:]) if a != b]) == len(set(dirs)) - 1, str(got))


def drill_a_run_with_no_change_is_never_mergeable(_repo):
    """A run whose branch carries no change (every item left out) has nothing to merge, so
    its finish state is run-complete, never mergeable (ruling 12)."""
    from hb.contract import policy
    from hb.record import firstscreen
    fs = firstscreen.finish_state([], [], [], "published", changed=False, loop_end="clean")
    check("finish: no change reads run-complete with the reason, never mergeable",
          fs["state"] == "run-complete" and any("nothing to merge" in w for w in fs["why"]), str(fs))
    fs = firstscreen.finish_state([], [], [], "published", changed=True, loop_end="clean")
    check("finish: a clean run with a change still reads mergeable", fs["state"] == "mergeable", str(fs))


def drill_a_pause_is_honored_at_a_phase(repo):
    """A pause written while no seat runs is taken at the next phase the run moves into."""
    from hb.record import journal
    from hb.record import control
    here = os.getcwd()
    os.chdir(repo)
    try:
        _pause_at_a_phase(journal, control)
    finally:
        os.chdir(here)


def _pause_at_a_phase(journal, control):
    j = journal.Journal(f"drill-pause-{os.getpid()}-{int(time.time() * 1000)}")
    d = j.dir
    (d / "control.txt").write_text("pause\n", encoding="utf-8")
    try:
        j.phase("gates")
        check("pause: a pause before a phase raises Paused", False, "phase moved on")
    except control.Paused:
        check("pause: a pause before a phase raises Paused", True)
    (d / "control.txt").write_text("pause\n", encoding="utf-8")
    j.phase("mergeable")
    check("pause: a final phase is still written", j.read_phase().get("phase") == "mergeable", str(j.read_phase()))

DRILLS = [drill_a_run_with_no_change_is_never_mergeable, drill_a_pause_is_honored_at_a_phase, drill_pytest_sees_each_directory_once, drill_standing_stop_waits_on_you, drill_watcher_reads_only_a_changed_journal, drill_request_never_waits_on_a_slow_reader, drill_events_push_a_run_on_journal_append,
          drill_a_dead_pid_reads_finished, drill_prune_keeps_the_record_and_skips_a_live_run, drill_collapse_leaves_only_the_record, drill_closeout_files_nothing, drill_page_serves_without_a_console, drill_page_task_is_built_not_registered, drill_page_leaves_the_callers_job,
          drill_home_shows_every_live_run, drill_engine_children_open_no_window, drill_the_gates_are_the_configs_and_cis, drill_live_run_clock_counts_to_now, drill_gate_timeout_is_unmeasured_and_kills_its_tree, drill_each_gate_shows_its_own_seconds, drill_run_timeline_steps, drill_seat_stream_summary, drill_run_activity_feed, drill_resume_keeps_build_and_review, drill_kept_seat_rule, drill_first_screen, drill_run_view, drill_run_page_plays_the_film_and_opens_pictures, drill_known_reds, drill_fix_report_without_a_cause_is_refused, drill_fixer_cannot_prove_a_red_preexisting, drill_question, drill_happy, drill_invalid, drill_ratelimit, drill_crash_twice, drill_authority,
          drill_nonwaivable, drill_nonwaivable_answered, drill_nonwaivable_other_id, drill_gate_answered, drill_every_gate_unmeasured_stops_without_a_fixer, drill_empty_tip_stops_before_measuring, drill_detached_run_outlives_its_launcher, drill_detach_starts_the_page_when_it_is_down, drill_ci_parity, drill_replay_stale_base, drill_replay_unmeasured_retried, drill_wave_slice_never_began, drill_lease_busy_retried, drill_heavy_suites_queue_by_room, drill_heavy_suites_overlap_with_room, drill_template_slot_stale_taken_over, drill_template_clone_takes_a_slot, drill_seat_report_in_the_windows_code_page_is_read, drill_seat_never_waits_on_the_clock, drill_a_stubbed_venue_takes_the_venue_grant, drill_a_new_red_beside_an_old_one_stays_red, drill_seat_long_commands, drill_a_seat_starts_only_its_roles_tools, drill_fix_round, drill_resume, drill_stall, drill_cut, drill_pause,
          drill_answer, drill_surfaced, drill_watch, drill_failed_op_retried, drill_token_count_is_not_a_rate_limit, drill_pr_body_refreshed, drill_owed_and_films, drill_engine_reads_and_writes_utf8, drill_usage_limit_parks_the_run, drill_pr_links_the_published_record, drill_audit_disagreements_are_printed, drill_finished_is_three_states, drill_closeout_keeps_referenced_artifacts,
          drill_demo_starts_after_the_merge, drill_a_dev_venue_is_born_with_an_owner_and_an_expiry, drill_a_closeout_purges_the_dev_venues_its_run_made, drill_a_closeout_names_every_dev_venue_it_could_not_purge, drill_the_sweep_purges_only_expired_dev_venues, drill_pr_create_refused_is_a_named_stop, drill_a_pr_on_a_tip_no_heavy_gate_measured_is_refused]


def _remote_round(repo, scenario, round_key="gates-full", timeout_min=45):
    """One heavy round whose two gates are remote, GitHub answered by the stub from `scenario`.
    Returns (rows, the journal, the scenario as the stub left it, the tip)."""
    from hb.gates import gating
    from hb.gates import remote
    from hb.record.journal import Journal
    tip = sh(["git", "-C", str(repo), "rev-parse", "HEAD"])[1].strip()
    sc = repo / ".context" / "remote-scenario.json"
    sc.parent.mkdir(parents=True, exist_ok=True)
    title = f"harness-gates drill-remote {tip}"
    runs = [{"id": rid, "display_title": title, "status": "queued", "html_url": f"https://example.invalid/{rid}"}
            for rid in scenario.pop("run_ids", [7])]
    provenance = scenario.pop("provenance", f"RUN_ID=drill-remote\nTIP={tip}\nHEAD={tip}\n")
    if scenario.get("artifact") is not None:
        scenario["artifact"].setdefault("provenance.txt", provenance)
    sc.write_text(json.dumps({"runs": runs, **scenario}), encoding="utf-8")
    c = {"repo": "drill/drill", "state_dir": str(repo / ".context" / "harness"), "branches": {"landing": "dev"},
         "gates": {"commands": [{"name": n, "cmd": ["python", "-c", "print('ok')"]} for n in ("backend-tests", "vitest")],
                   "remote": {"workflow": "harness-gates.yml", "gates": ["backend-tests", "vitest"], "timeout_min": timeout_min}}}
    before = {k: os.environ.get(k) for k in ("HB_GH_STUB", "HB_REMOTE_SCENARIO")}
    os.environ.update(HB_GH_STUB=str(FIXTURE / "remote_gh_stub.py"), HB_REMOTE_SCENARIO=str(sc))
    waits = remote.POLL_S, remote.ACK_S
    remote.POLL_S = remote.ACK_S = 0
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        j = Journal("drill-remote", c=c)
        rows = gating.measure(j, str(repo), "drill", "base", [], c, round_key, heavy=True)
    finally:
        os.chdir(here)
        remote.POLL_S, remote.ACK_S = waits
        for k, v in before.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return rows, j, json.loads(sc.read_text(encoding="utf-8")), tip


def _remote_unmeasured(label, rows, mode):
    """Every gate of the round reads UNMEASURED by the mode's name: never red, never green, never a fix item."""
    from hb.gates import gating
    from hb.gates import verdict
    check(f"{label}: both gates carry the line, with no exit code",
          [r.get("true_exit") for r in rows] == [None, None]
          and all(str(r.get("unmeasured")).startswith(f"remote gate: {mode}") for r in rows), [r.get("unmeasured") for r in rows])
    v = [verdict.classify(r) for r in rows]
    check(f"{label}: the verdict is UNMEASURED, environment, signature remote, no rerun",
          all(x["outcome"] == "UNMEASURED" and x["cause_class"] == "environment" and x["signature"] == "remote"
              and not x["recovery"] for x in v), v)
    check(f"{label}: named in the PR, handed to no fixer",
          gating.unmeasured_gate_names(rows) == ["backend-tests", "vitest"] and gating.gates_to_fix(rows) == [], rows)


def drill_remote_gates_read_the_artifact_of_their_own_run(repo):
    """The heavy gates run on the CI boxes: one dispatch per tip with the run id and the tip,
    the workflow run it made journaled, the artifact read whole for one TRUE_EXIT line per
    gate, and a second ask at the same tip dispatches nothing."""
    log = "FAILED tests/test_a.py::test_x - boom\n1 failed, 5 passed in 3s\nTRUE_EXIT=1\n"
    rows, j, sc, tip = _remote_round(repo, {"completes_after": 2,
                                            "artifact": {"backend-tests.txt": log, "vitest.txt": "ok\nTRUE_EXIT=0\n"}})
    dispatches = [a for a in sc["calls"] if any(x.endswith("/dispatches") for x in a)]
    check("remote green: one dispatch, carrying the run id and the exact tip",
          len(dispatches) == 1 and f"inputs[run_id]=drill-remote" in dispatches[0] and f"inputs[tip]={tip}" in dispatches[0], dispatches)
    evs = list(j.events())
    run_ev = [e for e in evs if e.get("kind") == "remote-run"]
    check("remote green: the journal records the workflow run the dispatch made",
          [e.get("workflow_run") for e in run_ev] == [7] and any(e.get("kind") == "remote-dispatch" and e.get("tip") == tip for e in evs), run_ev)
    check("remote green: each gate's TRUE_EXIT is read from its own file, red ids with it",
          [(r["name"], r.get("true_exit")) for r in rows] == [("backend-tests", 1), ("vitest", 0)]
          and rows[0].get("red_ids") == ["tests/test_a.py::test_x"]
          and all(r.get("artifact") and Path(r["artifact"]).exists() for r in rows), rows)
    check("remote green: the rows wait for the run to complete", sc.get("polls") == 2, sc.get("polls"))
    from hb.gates import gating
    check("remote green: a red gate is a fix item, a green one is WORKS",
          [g["name"] for g in gating.gates_to_fix(rows)] == ["backend-tests"] and rows[1]["verdict"]["outcome"] == "WORKS", rows)
    again, j2, sc2, _ = _remote_round(repo, {"completes_after": 1, "artifact": {"backend-tests.txt": log, "vitest.txt": "ok\nTRUE_EXIT=0\n"}},
                                      round_key="gates-suite")
    check("remote green: a second ask at the same tip reads the same run and dispatches nothing",
          not [a for a in sc2["calls"] if any(x.endswith("/dispatches") for x in a)]
          and [(r["name"], r.get("true_exit")) for r in again] == [("backend-tests", 1), ("vitest", 0)], sc2["calls"])


def drill_remote_pushes_the_tip_before_the_dispatch(repo):
    """The boxes check out the branch by name, so a branch the run never pushed reads
    'No ref found' and every heavy gate measures nothing: the tip is on origin before the dispatch."""
    sh(["git", "-C", str(repo), "checkout", "-q", "-b", "hotfix/drill-unpushed"])
    sh(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "unpushed tip"])
    rows, j, sc, tip = _remote_round(repo, {"completes_after": 1, "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}})
    on_origin = sh(["git", "-C", str(repo), "ls-remote", "origin", "refs/heads/hotfix/drill-unpushed"])[1].split()
    check("remote push: the tip is on origin when the dispatch reads the branch", on_origin[:1] == [tip], on_origin)
    kinds = [e.get("kind") if e.get("kind") != "op" else e.get("key", "").split("@")[0] for e in j.events()]
    check("remote push: the push is journaled before the dispatch",
          "remote-push" in kinds and kinds.index("remote-push") < kinds.index("remote-dispatch"), kinds)
    check("remote push: the gates are measured", [r.get("true_exit") for r in rows] == [0, 0], rows)


def drill_remote_lost_acknowledgement_is_unmeasured(repo):
    """The dispatch is sent and GitHub never says it landed: no retry, no second dispatch, every
    gate UNMEASURED by name. The same when the dispatch is acknowledged and no run ever appears."""
    rows, j, sc, _ = _remote_round(repo, {"dispatch_rc": 1, "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}})
    _remote_unmeasured("remote lost ack", rows, "lost acknowledgement")
    check("remote lost ack: the dispatch is sent once and never again",
          len([a for a in sc["calls"] if any(x.endswith("/dispatches") for x in a)]) == 1, sc["calls"])
    rows2, _, _, _ = _remote_round(repo, {"run_ids": [], "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}})
    check("remote lost ack: an acknowledged dispatch with no run behind it reads the same way",
          all(str(r.get("unmeasured")).startswith("remote gate: lost acknowledgement") for r in rows2), rows2)


def drill_remote_duplicate_dispatch_is_unmeasured(repo):
    """Two workflow runs name this run id and tip: neither is trusted, every gate UNMEASURED by name."""
    rows, j, sc, _ = _remote_round(repo, {"run_ids": [7, 8], "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}})
    _remote_unmeasured("remote duplicate", rows, "duplicate dispatch")
    check("remote duplicate: the line names both runs", "7, 8" in rows[0]["unmeasured"], rows[0]["unmeasured"])
    check("remote duplicate: no artifact is downloaded", not [a for a in sc["calls"] if a[:2] == ["run", "download"]], sc["calls"])


def drill_remote_partial_artifact_is_unmeasured(repo):
    """An artifact missing a gate's TRUE_EXIT line, or missing altogether, measures nothing: the
    gate that did have a line is not read as green beside it."""
    rows, _, _, _ = _remote_round(repo, {"artifact": {"backend-tests.txt": "all fine\nTRUE_EXIT=0\n", "vitest.txt": "cut off\n"}})
    _remote_unmeasured("remote partial", rows, "partial artifact")
    check("remote partial: the line names the gate without a result", "vitest" in rows[0]["unmeasured"], rows[0]["unmeasured"])
    rows2, _, _, _ = _remote_round(repo, {"artifact": None})
    _remote_unmeasured("remote no artifact", rows2, "partial artifact")


def drill_remote_foreign_tip_is_unmeasured(repo):
    """An artifact whose provenance names another tip or another run is somebody else's
    measurement: every gate UNMEASURED by name, whatever its lines say."""
    rows, _, _, tip = _remote_round(repo, {"provenance": "RUN_ID=drill-remote\nTIP=" + "f" * 40 + "\nHEAD=" + "f" * 40 + "\n",
                                           "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}})
    _remote_unmeasured("remote foreign tip", rows, "foreign tip")
    check("remote foreign tip: the line names both tips", tip in rows[0]["unmeasured"] and "f" * 40 in rows[0]["unmeasured"], rows[0]["unmeasured"])
    rows2, _, _, _ = _remote_round(repo, {"provenance": f"RUN_ID=someone-else\nTIP={tip}\nHEAD={tip}\n",
                                          "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}})
    _remote_unmeasured("remote foreign run", rows2, "foreign tip")


def drill_remote_timeout_is_unmeasured(repo):
    """A workflow run still going past `timeout_min` is UNMEASURED by name, never red, never green."""
    rows, _, sc, _ = _remote_round(repo, {"completes_after": 99, "artifact": {"backend-tests.txt": "TRUE_EXIT=0\n", "vitest.txt": "TRUE_EXIT=0\n"}},
                                   timeout_min=0)
    _remote_unmeasured("remote timeout", rows, "timeout")
    check("remote timeout: the line names the workflow run", "workflow run 7" in rows[0]["unmeasured"], rows[0]["unmeasured"])
    check("remote timeout: nothing is downloaded from a run that never ended",
          not [a for a in sc["calls"] if a[:2] == ["run", "download"]], sc["calls"])


def drill_remote_gates_run_locally_only_when_the_boxes_are_down(repo):
    """GitHub cannot list an online runner: the gates run their local command, the row says so,
    nothing is dispatched. The same when the list answers and no runner is online."""
    from hb.gates import remote
    rows, j, sc, _ = _remote_round(repo, {"down": True})
    check("remote down: the local command ran and the gate line says so",
          [r.get("true_exit") for r in rows] == [0, 0]
          and all(str(r.get("fallback")).startswith(remote.FALLBACK) for r in rows), rows)
    check("remote down: nothing was dispatched", not [a for a in sc["calls"] if any(x.endswith("/dispatches") for x in a)], sc["calls"])
    check("remote down: the journal names the fallback and why",
          any(e.get("kind") == "remote-fallback" and e.get("gates") == ["backend-tests", "vitest"] and "did not answer" in e.get("why", "")
              for e in j.events()), [e for e in j.events() if e.get("kind") == "remote-fallback"])
    rows2, _, sc2, _ = _remote_round(repo, {"online": 0}, round_key="gates-suite")
    check("remote down: no runner online reads the same way",
          [r.get("true_exit") for r in rows2] == [0, 0] and "no self-hosted runner is online" in str(rows2[0].get("fallback")), rows2)


DRILLS += [drill_remote_gates_read_the_artifact_of_their_own_run, drill_remote_pushes_the_tip_before_the_dispatch, drill_remote_lost_acknowledgement_is_unmeasured, drill_remote_duplicate_dispatch_is_unmeasured, drill_remote_partial_artifact_is_unmeasured, drill_remote_foreign_tip_is_unmeasured, drill_remote_timeout_is_unmeasured, drill_remote_gates_run_locally_only_when_the_boxes_are_down]


def main():
    only = sys.argv[1:] or None
    from hb.record import view as _view
    for d in DRILLS:
        # every section builds its own repo; the runs root is found once per process and must be found again
        _view.root.cache_clear()
        name = d.__name__.replace("drill_", "")
        if only and name not in only:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="hb-run-drill-"))
        try:
            repo = make_repo(tmp)
            d(repo)
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
