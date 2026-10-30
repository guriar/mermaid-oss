"""hb.run.adapters - one launcher per runtime.

The runner knows nothing about a model except what an adapter tells it: how to
launch the CLI with a brief, where its output lands, how to read its usage line,
and how to classify its exit. Adding a runtime is adding one entry here or in the
product config's `runtimes` block; no brief and no stage changes.

Exit classes: `ok` (a process that returned), `rate-limited` (wait for the reset,
never respawn at once), `timeout` (the budget cut it; its tree is killed), `context` (its
running context crossed the ceiling; it is relaunched on its progress file, A16),
`crashed` (a non-zero exit with no rate-limit sign), `missing` (the CLI is not on
this host). What the seat WROTE is judged by the caller, not here.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from hb.venue import admission
from hb.rig.common import cfg, run
from hb.gates.gates import _kill_tree

FAKE_SEAT = str(Path(__file__).parent / "fake_seat.py")

# The defaults a product config may override under `runtimes.<name>`. `{model}` and
# `{brief}` are filled at launch; a `-` argument means the brief rides stdin.
DEFAULT_RUNTIMES = {
    "claude": {
        # A seat is a trusted builder (ruling 1) and nothing may prompt mid-run: the
        # named tools are pre-approved, and --add-dir lets it write its report and
        # progress note under the run directory, the one place outside its tree.
        # stream-json (which needs --verbose) prints every turn as it happens, so the
        # seat's own output is its heartbeat; the last line still carries the usage.
        "cmd": ["claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode",
                "acceptEdits", "--allowedTools", "Bash,Read,Write,Edit,MultiEdit,Glob,Grep",
                "--add-dir", "{run_dir}", "--model", "{model}", "--effort", "{effort}"],
        "brief": "stdin",
        "browser": "mcp",
        "usage": "claude-json",
    },
    "codex": {
        "cmd": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "{sandbox}",
                "-c", "model={model}", "-c", "model_reasoning_effort={effort}", "-"],
        "brief": "stdin",
        # the only browser a seat may drive is the venue's driver, attached the mcp way; codex has no such door, so a verify seat refuses it
        "browser": "none",
        "usage": "codex-text",
    },
    "fake": {
        "cmd": ["python", FAKE_SEAT],
        "brief": "stdin",
        "browser": "none",
        "usage": "none",
    },
}

# A seat runs the full suite or a blast that outlasts the CLI's 10-minute limits: a
# background command was cut 10 minutes after the seat's last reply, the seat exited
# with no report, and the runner respawned it to start over. Both ceilings are raised.
CLAUDE_SEAT_ENV = {
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": str(60 * 60 * 1000),
    "BASH_MAX_TIMEOUT_MS": str(60 * 60 * 1000),
}

SEAT_UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

DEFAULT_MODELS = {"claude": "opus", "codex": "gpt-5.5", "fake": "fake"}
# Effort per seat, overridable under `seats.<role>.effort`. Thinking seats run
# high or above; a driving seat is mostly waiting on a browser.
DEFAULT_EFFORT = {"build": "high", "review": "xhigh", "fix": "medium", "verify": "medium",
                  "codex": "high", "demo": "medium"}


def seat_effort(role):
    """The effort level for a role: config `seats.<role>.effort`, else the default."""
    seats = cfg().get("seats") or {}
    spec = seats.get(role) if isinstance(seats.get(role), dict) else {}
    return spec.get("effort") or DEFAULT_EFFORT.get(role, "high")


# A codex seat runs on the owner's ChatGPT plan, so it carries nothing of the owner's own
# Codex: no MCP server, plugin, app, hook, skill catalog or subagent. Each rode every turn.
CODEX_FEATURES_OFF = ("plugins", "apps", "hooks", "computer_use", "browser_use", "image_generation",
                      "memories", "tool_suggest")
CODEX_CONFIG = ("agents.enabled=false", "skills.max_context_tokens=1",
                # `default` is the standard tier; the owner's own `priority` (Fast) spends the
                # plan's limits at 2.5x for gpt-5.5 (learn.chatgpt.com, Speed)
                "service_tier=default")


def codex_quiet_args(mcp_names):
    """The codex options that switch off everything a seat does not need."""
    args = []
    for feature in CODEX_FEATURES_OFF:
        args += ["--disable", feature]
    for setting in CODEX_CONFIG:
        args += ["-c", setting]
    for name in mcp_names:
        key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
        args += ["-c", f"mcp_servers.{key}.enabled=false"]
    return args


def codex_mcp_names(exe, cwd):
    """The MCP servers codex would start in `cwd`, from every config layer it reads. A config
    `mcp_servers={}` merges and removes none, so each is switched off by name."""
    try:
        p = subprocess.run([exe, "--disable", "plugins", "mcp", "list", "--json"], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        return [str(s["name"]) for s in json.loads(p.stdout or "[]") if isinstance(s, dict) and s.get("name")]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []


# Codex's words when the plan's window is spent: "try again at 1:32 AM", with a date when the
# reset falls on another day ("Oct 9th, 2026 1:32 AM"), in the host's local time.
USAGE_LIMIT = re.compile(r"hit your usage limit", re.I)
_TRY_AGAIN = re.compile(r"try again at (?:(?P<date>[A-Z][a-z]{2} \d{1,2})(?:st|nd|rd|th)?, (?P<year>\d{4}) )?"
                        r"(?P<time>\d{1,2}:\d{2} ?[AP]M)", re.I)


def usage_reset(text, now=None):
    """When a usage-limited plan is back, as a naive local datetime, or None when the text is
    not codex's usage limit or names no time. A bare time is today's; one already past means
    the plan is back now."""
    from datetime import datetime
    if not USAGE_LIMIT.search(text or ""):
        return None
    found = list(_TRY_AGAIN.finditer(text))
    if not found:
        return None
    m = found[-1]
    now = (now or datetime.now()).replace(microsecond=0)
    clock = datetime.strptime(m.group("time").replace(" ", "").upper(), "%I:%M%p")
    if m.group("date"):
        day = datetime.strptime(f"{m.group('date')} {m.group('year')}", "%b %d %Y")
        return day.replace(hour=clock.hour, minute=clock.minute)
    return max(now, now.replace(hour=clock.hour, minute=clock.minute, second=0))


def usage_limit(stream):
    """{text, resets, resets_at} when a Claude seat's stream-json says the plan's usage limit is
    spent (`api_error: usage_limit_reached`, or `api_error_status: 429`), else None. Only the
    stream's own fields are read: a token count of 429 in prose is not a limit."""
    found, resets_at, kind = None, None, None
    for line in (stream or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        info = o.get("rate_limit_info") if o.get("type") == "rate_limit_event" else None
        if isinstance(info, dict) and info.get("status") == "rejected" and info.get("resetsAt"):
            resets_at = info["resetsAt"]
            kind = info.get("rateLimitType") or kind
        if o.get("api_error") == "usage_limit_reached" or str(o.get("api_error_status") or "") == "429":
            text = o.get("result") if isinstance(o.get("result"), str) else None
            for block in ((o.get("message") or {}).get("content") or []) if not text else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
            found = re.sub(r"\s+", " ", str(text or "the plan's usage limit is reached")).strip()
    if not found:
        return None
    m = re.search(r"\bresets?\s+(?:at\s+|in\s+)?(.+)$", found, re.I)
    when = None
    if resets_at:
        from datetime import datetime, timezone
        try:
            when = datetime.fromtimestamp(float(resets_at), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            when = None
    try:
        epoch = int(float(resets_at)) if resets_at else None
    except (TypeError, ValueError):
        epoch = None
    return {"text": found, "resets": m.group(1).strip() if m else (when or "unknown"), "resets_at": when,
            "resets_epoch": epoch, "kind": kind}


RATE_LIMIT_SIGNS = re.compile(
    r"rate.?limit|usage.?limit|too many requests|\b429\b|overloaded|quota|"
    r"credit balance|reset(s)? (at|in)", re.I)


def runtimes():
    """The runtime table: defaults overlaid by the product config."""
    table = {k: dict(v) for k, v in DEFAULT_RUNTIMES.items()}
    for name, spec in (cfg().get("runtimes") or {}).items():
        if name.startswith("$") or not isinstance(spec, dict):
            continue  # a $comment is for the reader, not the table
        table.setdefault(name, {}).update(spec)
    return table


def seat_runtime(role, override=None):
    """(runtime name, model) for a role: the CLI flag, else config `seats.<role>`,
    else claude. The `seats` block is `{"build": {"runtime": "codex", "model": "gpt-5.5"}}`."""
    seats = cfg().get("seats") or {}
    spec = seats.get(role) if isinstance(seats.get(role), dict) else {}
    configured = spec.get("runtime") or "claude"
    name = override or configured
    # A configured model belongs to the configured runtime; an override takes the
    # override runtime's own default (measured: codex refused "opus").
    model = spec.get("model") if name == configured else None
    return name, model or DEFAULT_MODELS.get(name, "")


def probe(name, role=None):
    """Can this host run this runtime for this role? (ok, why)."""
    spec = runtimes().get(name)
    if not spec:
        return False, f"runtime `{name}` is not configured"
    exe = spec["cmd"][0]
    if exe == "python":
        return True, "pinned interpreter"
    if not shutil.which(exe) and not Path(exe).exists():
        return False, f"`{exe}` is not on PATH on this host"
    if role == "verify" and spec.get("browser") in (None, "none"):
        return False, f"runtime `{name}` names no browser tool; a verify seat needs one"
    return True, "ok"


# Seats that read or drive never edit the tree; the allowed tools say so, and the second
# reader's sandbox is read-only (Codex review 2026-09-21).
# the stall window per role, minutes: a seat silent this long is relaunched
STALL_MIN = {"build": 20, "fix": 20, "review": 10, "verify": 10, "codex": 10, "demo": 20}


def stall_seconds(role):
    return STALL_MIN.get(role, 10) * 60


READ_ONLY_ROLES = ("review", "verify", "discovery", "codex")
READ_TOOLS = "Bash,Read,Write,Glob,Grep"
CODEX_EFFORT_CEILING = ("minimal", "low", "medium", "high")


def _fill(cmd, model, brief_path, run_dir="", effort="high", role=""):
    sandbox = "read-only" if role in READ_ONLY_ROLES else "workspace-write"
    out = []
    for c in cmd:
        c = (c.replace("{model}", model).replace("{brief}", str(brief_path))
              .replace("{run_dir}", str(run_dir)).replace("{effort}", effort).replace("{sandbox}", sandbox))
        out.append(c)
    if role in READ_ONLY_ROLES and "--allowedTools" in out:
        out[out.index("--allowedTools") + 1] = READ_TOOLS
    return out


def _activity(cwd, run_dir, so, se):
    """A signature that changes whenever the seat is doing something: its output grew,
    the tree's HEAD moved, a modified file in the tree was written, or anything under
    the run directory (progress, captures, receipts) was written."""
    sig = []
    for p in (so, se):
        try:
            sig.append(p.stat().st_size)
        except OSError:
            sig.append(0)
    rc, o, _ = run(["git", "-C", str(cwd), "rev-parse", "HEAD"], timeout=30)
    sig.append(o.strip() if rc == 0 else "")
    rc, o, _ = run(["git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=normal"], timeout=60)
    newest = 0.0
    for line in (o or "").splitlines():
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        try:
            newest = max(newest, (Path(cwd) / rel).stat().st_mtime)
        except OSError:
            pass
    sig.append(newest)
    newest = 0.0
    for p in Path(run_dir).rglob("*"):
        if p == so or p == se:
            continue
        try:
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    sig.append(newest)
    return tuple(sig)


def _cut_requested(run_dir):
    from hb.record import control
    return control.cut_requested(run_dir)


def seat_settings(out_dir):
    """The settings a Claude seat starts with: its Bash and Agent calls pass through `seatguard`,
    which refuses a command that waits on the wall clock (a verifier slept three hours for a
    shift to start) and a helper that runs in the background, commits or spawns (A17).
    Written beside the seat's report; returns the path."""
    import sys
    guard = Path(__file__).resolve().parent / "seatguard.py"
    hook = f'"{Path(sys.executable).as_posix()}" "{guard.as_posix()}"'
    path = Path(out_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|Agent|Task",
                                                          "hooks": [{"type": "command", "command": hook, "timeout": 20}]}]},
                                "enabledPlugins": {name: False for name in personal_plugins() if not name.startswith("mermaid@")}}, indent=2),
                    encoding="utf-8")
    return path


# A17: the builder alone may spawn helpers (a browser driver, a check runner), foreground only
HELPER_ROLES = ("build",)


def role_tools(role):
    """The tool names a seat of `role` takes, and nothing else (resource ruling 7): a tester
    drives the venue's `ui`, and so do the builder's browser helpers (Q6); every other seat
    takes none. The spec of each comes from the caller as `launch(tools=)`."""
    return ("ui",) if role in ("verify", "discovery") + HELPER_ROLES else ()


def seat_kind(role):
    """The admission kind of a seat: its role and what it loads, so a peak measured while a seat
    loaded more than it does now never prices it (resource ruling 9)."""
    return f"seat:{role}:strict-{'+'.join(role_tools(role)) or 'none'}"


def personal_plugins():
    """The plugins the owner's own sessions enable: a seat starts none of them, since each is
    a process per seat and some reach live accounts (resource ruling 7)."""
    try:
        user = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted((user.get("enabledPlugins") or {}).keys())


# the runtime's window is a model limit, not a plan: a builder past this running context wrote
# an invalid report at 959k, and a successor on its progress file finished the tail
CONTEXT_CEILING = 600000


def context_ceiling():
    """The running context at which a build seat is cut and relaunched on its own progress
    file: config `seats.context_ceiling`, else 600k tokens."""
    from hb.rig.common import cfg_or_empty
    return int((cfg_or_empty().get("seats") or {}).get("context_ceiling") or CONTEXT_CEILING)


def launch(name, role, brief_path, cwd, out_dir, model="", timeout_s=3600, env=None, effort="",
           stall_s=None, context_ceiling=None, tools=None):
    """Run the seat's CLI. Returns a dict the journal can store: rc, exit_class,
    seconds, stdout and stderr paths, usage (or None), and the command that ran.

    The loop cuts a seat for four reasons only: a STALL (no activity for `stall_s`),
    a `cut` line in the run's control file, its running context past `context_ceiling`
    (A16), or the fuse `timeout_s`. `tools` maps a tool name to the spec a seat of a role in
    `role_tools` starts (adapter.ui_driver); without it the seat starts no browser tool."""
    spec = runtimes()[name]
    model = model or DEFAULT_MODELS.get(name, "")
    # out_dir is <run_dir>/seats/<seat-n>; the report and the evidence dir both live
    # under run_dir.
    run_dir = Path(out_dir).parent.parent
    effort = effort or seat_effort(role)
    if name == "codex" and effort not in CODEX_EFFORT_CEILING:
        effort = "high"
    cmd = _fill(list(spec["cmd"]), model, brief_path, run_dir, effort, role)
    if tools and role_tools(role) and spec.get("browser") == "mcp" and "--allowedTools" in cmd:
        # the tool specs come from the venue (`venue.ui.driver`), each started by its own command
        mcp = Path(out_dir) / "mcp.json"
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        servers = {n: {"command": s["command"], "args": list(s.get("args") or []),
                       **({"env": s["env"]} if s.get("env") else {})} for n, s in tools.items() if s}
        mcp.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        i = cmd.index("--allowedTools")
        cmd[i + 1] = cmd[i + 1] + "".join(f",mcp__{n}" for n in servers)
    if name == "claude" and "--allowedTools" in cmd and role in HELPER_ROLES:
        i = cmd.index("--allowedTools")
        cmd[i + 1] = cmd[i + 1] + ",Agent"
    if name == "claude" and "--allowedTools" in cmd:
        # a seat starts only the tool servers its role declares, never the owner's own: the
        # strict flag drops every server not in this file (resource ruling 7)
        mcp = Path(out_dir) / "mcp.json"
        if not mcp.exists():
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        i = cmd.index("--allowedTools")
        cmd[i + 2:i + 2] = ["--strict-mcp-config", "--mcp-config", str(mcp)]
        # the seat's shell runs through the guard: a command that waits on the clock is refused
        cmd[cmd.index("--allowedTools"):cmd.index("--allowedTools")] = ["--settings", str(seat_settings(out_dir))]
    exe = cmd[0]
    if exe == "python":
        import sys
        cmd[0] = sys.executable
    else:
        cmd[0] = shutil.which(exe) or exe
    if name == "codex" and "exec" in cmd:
        at = cmd.index("exec") + 1
        cmd[at:at] = codex_quiet_args(codex_mcp_names(cmd[0], cwd))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    so, se = out_dir / "stdout.txt", out_dir / "stderr.txt"
    child_env = dict(os.environ)
    child_env.update(env or {})
    # a seat knows it is a seat: the venue refuses to be brought up by one (one venue per run)
    child_env["HB_SEAT_ROLE"] = role
    child_env.setdefault("TZ", "UTC")
    # a seat's Python writes UTF-8 whatever the host's code page (a cp1252 report read as a crash)
    child_env.update(SEAT_UTF8_ENV)
    if name == "claude":
        for key, value in CLAUDE_SEAT_ENV.items():
            child_env.setdefault(key, value)
    brief_text = Path(brief_path).read_text(encoding="utf-8")
    if stall_s is None:
        stall_s = stall_seconds(role)
    if os.environ.get("HB_STALL_MIN"):
        stall_s = float(os.environ["HB_STALL_MIN"]) * 60
    poll_s = float(os.environ.get("HB_POLL_S") or 20)
    t0 = time.time()
    with open(so, "w", encoding="utf-8") as fo, open(se, "w", encoding="utf-8") as fe:
        try:
            # a seat gets its own process group off Windows, so a stall kill takes its tree and nothing above it
            p = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=fo,
                                 stderr=fe, env=child_env, text=True, encoding="utf-8",
                                 start_new_session=(os.name != "nt"))
            admission.watch(p.pid)
        except FileNotFoundError as ex:
            return {"rc": None, "exit_class": "missing", "seconds": 0.0, "cmd": cmd,
                    "stdout": str(so), "stderr": str(se), "usage": None, "why": str(ex)}
        import threading

        def feed():
            try:
                if spec.get("brief") == "stdin":
                    p.stdin.write(brief_text)
                p.stdin.close()
            except (OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()
        last_sig, last_change = None, time.time()
        exit_class, why = None, None
        context_now, context_at = 0, 0
        while True:
            try:
                p.wait(timeout=poll_s)
                rc = p.returncode
                exit_class = "ok" if rc == 0 else "crashed"
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            sig = _activity(cwd, run_dir, so, se)
            if sig != last_sig:
                last_sig, last_change = sig, now
            if context_ceiling:
                held, context_at = running_context(so, context_at)
                context_now = held if held is not None else context_now
            if _cut_requested(run_dir):
                exit_class, why = "cut", "the conductor cut this seat"
            elif context_ceiling and context_now >= context_ceiling:
                exit_class, why = "context", f"its context reached {context_now} tokens, past the ceiling {context_ceiling}"
            elif now - last_change > stall_s:
                exit_class, why = "stalled", f"no commit, edit, progress line or output for {int(stall_s // 60)} min"
            elif now - t0 > timeout_s:
                exit_class, why = "timeout", f"the fuse: {int(timeout_s // 60)} min on one seat"
            if exit_class:
                _kill_tree(p.pid)
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                rc = p.returncode
                break
    seconds = round(time.time() - t0, 1)
    err_tail = _tail(se)
    out_tail = _tail(so)
    # A seat that returned 0 and wrote its report is ok whatever its stream says: the
    # stream carries token counts, and one read "429" as a rate limit and threw away a
    # finished fixer (run 20260922-010153). Signs are read on a crash, or on a clean exit
    # that left no report, and then from stderr and the non-JSON lines only.
    prose = err_tail + "\n".join(l for l in out_tail.splitlines() if not l.lstrip().startswith("{"))
    # codex's report is its last message on stdout; its stderr echoes the prompt, whose diff
    # can carry the words "usage limit" in code it reads
    report_written = (out_dir / "report.json").exists() or (spec.get("usage") == "codex-text" and bool(out_tail.strip()))
    if (exit_class == "crashed" or (exit_class == "ok" and not report_written)) and RATE_LIMIT_SIGNS.search(prose):
        exit_class = "rate-limited"
    # the plan's limit is spent: a respawn only meets it again, so the run parks (run 20261007-184837)
    limit = usage_limit(_tail(so, 200000)) if not (exit_class == "ok" and report_written) else None
    if limit:
        exit_class = "rate-limited"
    resets = usage_reset(err_tail + out_tail) if exit_class == "rate-limited" else None
    return {"rc": rc, "exit_class": exit_class, "seconds": seconds, "cmd": cmd, "why": why,
            "stdout": str(so), "stderr": str(se), "context_tokens": context_now or None,
            "usage": read_usage(spec.get("usage"), so, se),
            "reset_hint": _reset_hint(err_tail + out_tail) if exit_class == "rate-limited" else None,
            "usage_resets_at": resets.isoformat() if resets else None, "usage_limit": limit}


def _tail(path, n=4000):
    try:
        t = Path(path).read_text(encoding="utf-8", errors="replace")
        return t[-n:]
    except OSError:
        return ""


def _reset_hint(text):
    m = re.search(r"reset(?:s)? (?:at|in) ([^\n.]{1,40})", text, re.I)
    return m.group(1).strip() if m else None


def context_of(usage):
    """What one turn held in context: the fresh input plus everything read from or written to
    the cache. The result line's `input_tokens` is only the uncached part (622 on a 151-minute
    build), which is why nobody could see how full a seat ran."""
    u = usage or {}
    return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)


def _own_turn(line):
    """The seat's own assistant turn on one stream line, or None. A helper's turns carry a
    `parent_tool_use_id` and hold a context of their own, so they never fill the seat's (A17)."""
    if '"assistant"' not in line or '"usage"' not in line:
        return None
    try:
        o = json.loads(line)
    except ValueError:
        return None
    if not isinstance(o, dict) or o.get("type") != "assistant" or o.get("parent_tool_use_id"):
        return None
    return o


def running_context(stdout_path, at=0):
    """(the context the seat's latest own turn held, or None when no new turn, the offset read
    to), reading the stream from `at` so a long seat's file is read once (A16)."""
    try:
        with open(stdout_path, "rb") as f:
            f.seek(at)
            chunk = f.read()
    except OSError:
        return None, at
    end = chunk.rfind(b"\n") + 1
    held = None
    for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
        o = _own_turn(line)
        if o:
            held = context_of((o.get("message") or {}).get("usage"))
    return held, at + end


def _claude_usage(stdout_path, tail):
    """The result line's totals, with the cache fields, and the peak context of any one turn
    read from the whole stream (`type: assistant` messages carry their own usage)."""
    import json
    totals = None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = o.get("usage") or {}
            if u or "total_cost_usd" in o:
                totals = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                          "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                          "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
                          "cost_usd": o.get("total_cost_usd")}
                break
    if totals is None:
        return None
    peak, turns = 0, 0
    try:
        with open(stdout_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                o = _own_turn(line)
                if not o:
                    continue
                turns += 1
                peak = max(peak, context_of((o.get("message") or {}).get("usage")))
    except OSError:
        pass
    totals["peak_context_tokens"] = peak or None
    totals["turns"] = turns or None
    return totals


def read_usage(kind, stdout_path, stderr_path):
    """The CLI's own usage line, or None when it prints none. Never estimated."""
    text = _tail(stdout_path, 200000)
    if kind == "claude-json":
        return _claude_usage(stdout_path, text)
    if kind == "codex-text":
        m = re.search(r"tokens used\s*\n?\s*([\d,]+)", text + _tail(stderr_path, 20000), re.I)
        if m:
            return {"total_tokens": int(m.group(1).replace(",", ""))}
        return None
    return None
