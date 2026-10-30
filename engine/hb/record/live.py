"""hb.run.live - what a run is doing right now, for the run page.

The journal says when a seat starts and when it ends; between those two lines a seat can
work for hours. This reads the seats working now from the files they write as they go:
the tail of each seat's output stream, a builder's commits and its progress file. It also
turns the journal into the run's activity in plain words, newest first. It only reads,
and a file it cannot read or parse leaves a line out rather than failing the page.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from hb.runner import usage

TAIL_BYTES = 64 * 1024
# one tool result can outweigh the first tail; widen until enough actions show, then stop
MAX_TAIL_BYTES = 1024 * 1024
DOING_LINES = 5
COMMITS_TTL_S = 15
_TAIL_CACHE = {}
_COMMITS_CACHE = {}

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|password|passwd|authorization)[\w-]*)(\s*[:=]\s*)"
                r"([\"']?)(bearer\s+)?[^\s\"']+"), r"\1\2\3[hidden]"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer [hidden]"),
    (re.compile(r"(\w+://[^:/\s@]+:)[^@\s]+@"), r"\1[hidden]@"),
    (re.compile(r"\b(?:sk-[\w-]{8,}|gh[pousr]_\w{20,}|github_pat_\w+|xox[abps]-[\w-]+|AKIA[0-9A-Z]{16}"
                r"|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]+)"), "[hidden]"),
]
_LONG_RUN = re.compile(r"[A-Za-z0-9_+=-]{32,}")


def redact(text):
    """Anything shaped like a secret is replaced: named keys and tokens, URL passwords, and
    long runs mixing upper, lower and digits (a git sha is lowercase and stays)."""
    s = str(text or "")
    for pattern, repl in _SECRET_PATTERNS:
        s = pattern.sub(repl, s)
    return _LONG_RUN.sub(lambda m: "[hidden]" if (re.search(r"[A-Z]", m.group()) and re.search(r"[a-z]", m.group())
                                                  and re.search(r"\d", m.group())) else m.group(), s)


def _short(text, limit=140):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _path(p):
    """A file as the owner knows it: relative to the run's worktree, else its last parts."""
    s = str(p or "").replace("\\", "/")
    m = re.search(r"/\.claude/worktrees/[^/]+/(.+)$", s)
    if m:
        return m.group(1)
    m = re.search(r"/runs/[^/]+/(.+)$", s)
    if m:
        return "run folder/" + m.group(1)
    parts = [x for x in s.split("/") if x]
    return "/".join(parts[-3:]) if len(parts) > 3 else s


TEST_WORDS = re.compile(r"\b(pytest|vitest|jest|playwright|npm (?:run )?test|run-gate)\b")


def _tool_words(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        cmd = str(inp.get("command") or "").strip().splitlines()[0:1]
        cmd = cmd[0] if cmd else ""
        what = inp.get("description") or cmd
        return ("Running tests: " if TEST_WORDS.search(str(inp.get("command") or "")) else "Running: ") + str(what)
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "Editing " + _path(inp.get("file_path") or inp.get("notebook_path"))
    if name == "Write":
        return "Writing " + _path(inp.get("file_path"))
    if name == "Read":
        return "Reading " + _path(inp.get("file_path"))
    if name == "Grep":
        return "Searching for " + str(inp.get("pattern") or "")
    if name == "Glob":
        return "Looking for files " + str(inp.get("pattern") or "")
    if name in ("Task", "Agent"):
        return "Asking a helper: " + str(inp.get("description") or "")
    if name == "TodoWrite":
        return "Updating its checklist"
    return "Using " + str(name)


def _event_lines(e):
    """Plain lines from one stream event, oldest first; an unknown shape gives none."""
    if not isinstance(e, dict):
        return []
    at = e.get("timestamp")
    t = e.get("type")
    if t == "assistant":
        out = []
        for b in ((e.get("message") or {}).get("content") or []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                out.append((at, _tool_words(b.get("name"), b.get("input"))))
            elif b.get("type") == "text" and str(b.get("text") or "").strip():
                out.append((at, "Says: " + str(b.get("text")).strip()))
        return out
    if t == "system" and e.get("subtype") == "task_summary" and e.get("detail"):
        return [(at, "Now: " + str(e["detail"]))]
    if t == "result":
        return [(at, "Finished: " + str(e.get("subtype") or "done"))]
    return []


def _read_tail(path, size, nbytes):
    with open(path, "rb") as f:
        start = max(0, size - nbytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # a tail that starts mid-file starts mid-line
    return lines[1:] if start > 0 else lines


def doing(path, limit=DOING_LINES):
    """The seat's latest actions, newest first, read from the tail of its output stream.
    Kept while the file's size and time are unchanged."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = str(path)
    hit = _TAIL_CACHE.get(key)
    if hit and hit[0] == (st.st_size, st.st_mtime_ns):
        return hit[1]
    lines, nbytes = [], TAIL_BYTES
    try:
        while True:
            found = []
            for raw in _read_tail(path, st.st_size, nbytes):
                raw = raw.strip()
                if not raw.startswith("{"):
                    continue
                try:
                    found.extend(_event_lines(json.loads(raw)))
                except (ValueError, TypeError, AttributeError):
                    continue
            lines = found
            if len(lines) >= limit or nbytes >= st.st_size or nbytes >= MAX_TAIL_BYTES:
                break
            nbytes *= 4
    except OSError:
        return []
    rows = [{"at": at, "text": _short(redact(text))} for at, text in reversed(lines[-limit:])]
    _TAIL_CACHE[key] = ((st.st_size, st.st_mtime_ns), rows)
    return rows


def _git_lines(wt, *args):
    try:
        p = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return [x for x in p.stdout.splitlines() if x.strip()] if p.returncode == 0 else None


def commits(wt, base):
    """Commits on the run's branch since its base: the count and the newest three subjects.
    Asked of git at most every few seconds per worktree."""
    if not wt or not base or not Path(wt).is_dir():
        return None
    key = (str(wt), str(base))
    hit = _COMMITS_CACHE.get(key)
    if hit and time.time() - hit[0] < COMMITS_TTL_S:
        return hit[1]
    subjects = _git_lines(wt, "log", "--format=%s", f"{base}..HEAD")
    value = None if subjects is None else {"count": len(subjects), "latest": [_short(redact(s), 110) for s in subjects[:3]]}
    _COMMITS_CACHE[key] = (time.time(), value)
    return value


def in_progress(progress_path):
    """The lines under the progress file's `in progress` heading."""
    try:
        text = Path(progress_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip().lower() == "in progress"
            continue
        if inside and line.strip():
            out.append(_short(redact(line.strip().lstrip("-* ").strip()), 160))
    return out[:5]


def _epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def running(evs):
    """The seats started and not yet ended, in start order: (label, attempt, started ts)."""
    open_ = {}
    for e in evs:
        if e.get("kind") == "phase" and e.get("phase") == "opened":
            open_ = {}  # a run that starts again has no seat left from before
            continue
        if e.get("kind") != "seat":
            continue
        key = (str(e.get("seat")), e.get("attempt"))
        if e.get("state") == "started":
            open_[key] = e.get("ts")
        else:
            open_.pop(key, None)
    return [(label, attempt, ts) for (label, attempt), ts in open_.items()]


def _workspace(evs):
    for e in reversed(evs):
        if e.get("kind") == "phase" and e.get("phase") in ("worktree", "wave-branch") and (e.get("worktree") or e.get("base_sha")):
            return e.get("worktree"), e.get("base_sha")
    return None, None


def seats(run_dir, evs, now=None):
    """One panel per working seat: who, how long, how long quiet, and what it is doing."""
    now = now or time.time()
    wt, base = _workspace(evs)
    panels = []
    for label, attempt, ts in running(evs):
        role = usage.label_role(label)
        sd = Path(run_dir) / "seats" / f"{label}-{attempt}"
        started = _epoch(ts) or now
        try:
            wrote = (sd / "stdout.txt").stat().st_mtime
        except OSError:
            wrote = started
        panel = {"seat": label, "role": usage.role_name(label, ""),
                 "attempt": attempt, "since": ts, "running_s": max(0, round(now - started)), "quiet_s": max(0, round(now - max(wrote, started))),
                 "doing": doing(sd / "stdout.txt")}
        if role in ("build", "fix"):
            panel["commits"] = commits(wt, base)
            panel["in_progress"] = in_progress(Path(run_dir) / "evidence" / "progress.md")
        panels.append(panel)
    return panels


def _gb(n):
    return "a host to itself" if n is None else f"{n / (1 << 30):.1f} GB"


def _who(e):
    return usage.role_name(e.get("seat"))


def _gate_words(stage, result):
    rows = [r for r in result or [] if isinstance(r, dict)]
    if not rows:
        return None
    red = [r.get("name") for r in rows if r.get("true_exit") not in (0, None)]
    blank = [r.get("name") for r in rows if r.get("true_exit") is None]
    words = f"Checks after {stage}: {len(rows) - len(red) - len(blank)} passed"
    if red:
        words += f", {len(red)} red ({', '.join(str(x) for x in red[:3])})"
    if blank:
        words += f", {len(blank)} measured nothing"
    return words


def _phase_words(e, opened_before):
    # imported here, not at the top: journal reaches this module through the view, and a module-level
    # import of the journal from here is a cycle the full drill order on Linux walks into
    from hb.record.journal import PHASE_WORDS
    ph = e.get("phase")
    if ph == "opened":
        return "Run started again" if opened_before else "Run started"
    if ph == "contract":
        return "Contract read: " + str(e.get("goal") or e.get("slug") or "")
    if ph == "gates":
        return f"Checks finished, {e.get('red') or 0} red"
    if ph == "review":
        return f"Review read the change: {e.get('findings') or 0} findings, {e.get('fix') or 0} to fix"
    if ph == "drive":
        if e.get("skipped"):
            return "Drive skipped"
        return f"Drive: {e.get('works') or 0} work, {e.get('broken') or 0} broken, {e.get('unmeasured') or 0} not measured"
    if ph == "stopped":
        return "Stopped: " + str(e.get("why") or "")
    if ph == "wave-branch":
        return "Wave branch ready: " + str(e.get("branch") or "")
    return PHASE_WORDS.get(ph)


def _op_words(e):
    key, res = str(e.get("key") or ""), e.get("result")
    stage = key.split("@")[0]
    if stage.startswith("gates-"):
        return _gate_words(stage[len("gates-"):], res)
    if stage.startswith("push-") and isinstance(res, dict):
        return "Pushed the branch" if res.get("rc") == 0 else f"Push failed (exit {res.get('rc')})"
    if stage.startswith("slice-") and isinstance(res, dict) and res.get("verdict"):
        from hb.record.journal import PHASE_WORDS
        return f"Slice #{res.get('issue')}: " + (PHASE_WORDS.get(res["verdict"]) or f"ended {res['verdict']}")
    if key == "pr" and isinstance(res, dict):
        return "Opened the PR " + str(res.get("url")) if res.get("url") else "The PR did not open: " + str(res.get("error"))
    return None


def _tokens(e):
    n = (e.get("usage") or {}).get("total_tokens") if isinstance(e.get("usage"), dict) else None
    return f", {n:,} tokens" if isinstance(n, int) else ""


def _seat_words(e):
    if e.get("state") == "started":
        return f"{_who(e)} started" + (f", attempt {e.get('attempt')}" if (e.get("attempt") or 1) > 1 else "")
    if e.get("state") == "waiting-usage" and e.get("words"):
        return str(e["words"]) + _tokens(e)
    cost = (e.get("usage") or {}).get("cost_usd")
    if e.get("state") == "context":
        return f"{_who(e)} ran out of room after {e.get('minutes') or 0} min; the next one takes over from its notes"
    how = "finished" if e.get("state") == "ok" else f"ended: {e.get('state')}"
    words = f"{_who(e)} {how} after {e.get('minutes') or 0} min" + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else "") \
        + _tokens(e)
    if e.get("problems"):
        words += ": " + str((e.get("problems") or [""])[0])
    return words


def _line(e, opened_before):
    k = e.get("kind")
    if k == "phase":
        return _phase_words(e, opened_before)
    if k == "seat":
        return _seat_words(e)
    if k == "op" and e.get("state") == "complete":
        return _op_words(e)
    if k == "question":
        return f"{_who(e)} decided a question for you: {e.get('text')}"
    if k == "stop":
        return "Waiting on you: " + str(e.get("text") or "")
    if k == "control":
        if e.get("verb") == "answer":
            return "Your answer arrived: " + str(e.get("text") or "")
        return f"You asked to {e.get('verb')}"
    if k == "build":
        return f"Build {'done' if e.get('result') == 'ok' else e.get('result')}"
    if k == "fix-round":
        return f"Fix round, {e.get('stage')}: {e.get('result')}"
    if k == "respawn":
        return f"{_who(e)} started again after {e.get('after')}"
    if k == "wait":
        return f"Waiting {e.get('minutes')} min: {e.get('reason')}"
    if k == "machine-wait":
        return "Waiting for a free test machine"
    if k == "admission" and e.get("state") == "waiting":
        return f"Waiting for room: {e.get('workload')} needs {_gb(e.get('cost'))}, oldest wait {e.get('oldest_wait_s')}s"
    if k == "admission" and e.get("state") == "granted" and e.get("waited_s"):
        return f"Room found for {e.get('workload')} after {e.get('waited_s')}s"
    if k == "preexisting-red":
        return f"{e.get('gate')} is red without this change too"
    if k == "venue" and e.get("state") == "failed":
        return "The stand-in did not come up: " + str(e.get("why") or "")
    if k == "slice" and e.get("state") == "started":
        return f"Slice #{e.get('issue')} started"
    if k == "compose-merge":
        return f"Slice #{e.get('issue')} merged into the wave"
    if k == "compose-conflict":
        return f"Slice #{e.get('issue')} conflicts with the wave: " + ", ".join(str(x) for x in (e.get("hit") or [])[:2])
    if k == "compose-park":
        return f"Slice #{e.get('issue')} left out: {e.get('why')}"
    if k == "repair":
        return f"Repair round {e.get('round')}: {e.get('result')}, {e.get('broken') or 0} broken"
    if k == "proven-by-test":
        return "Proven by test: " + ", ".join(str(x) for x in e.get("items") or [])
    if k == "second-read":
        return _second_read_words(e)
    return None


def _second_read_words(e):
    kept = len(e.get("adopted") or [])
    return {"scheduled": f"{e.get('words')}; it reads then and posts what it finds to the PR",
            "gated": f"The reviewer checked the second reader's late list: kept {kept}",
            "read": f"Second reader read after the PR: kept {kept}, "
                    + ("posted to the PR" if e.get("commented") else "the PR comment failed"),
            "fallback": f"Codex could not read ({e.get('why')}); a Claude reader took the same brief",
            "missed": f"Second reader did not read: {e.get('why')}"}.get(e.get("state"))


def second_reader(evs):
    """The second reader in one plain line for the run page, with what its reads cost."""
    last = next((e for e in reversed(evs)
                 if (e.get("kind") == "second-read" and e.get("state") != "gated")
                 or (e.get("kind") == "seat" and e.get("seat") == "codex" and e.get("state") != "started")), None)
    if not last:
        return None
    spent = sum(((e.get("usage") or {}).get("total_tokens") or 0) for e in evs
                if e.get("kind") == "seat" and e.get("seat") == "codex" and isinstance(e.get("usage"), dict))
    cost = f" ({spent:,} tokens)" if spent else ""
    if last.get("kind") == "second-read":
        return _second_read_words(last) + cost
    if last.get("state") == "ok":
        return "Codex read the whole change" + cost
    if last.get("state") == "waiting-usage":
        return str(last.get("words")) + cost
    return f"None this run ({last.get('state')})" + cost


def activity(evs, limit=40):
    """The journal as plain lines, newest first; events with nothing to say are left out."""
    rows, opened = [], 0
    for e in evs:
        try:
            text = _line(e, opened)
        except (TypeError, ValueError, AttributeError):
            text = None
        if e.get("kind") == "phase" and e.get("phase") == "opened":
            opened += 1
        if text:
            rows.append({"at": e.get("ts"), "text": _short(redact(text), 200)})
    return rows[::-1][:limit]
