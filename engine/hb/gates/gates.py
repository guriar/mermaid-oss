"""hb.gates - gate running, and the size measure the composed review reads."""

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from hb.rig.common import _canon_python, cfg, cfg_or_empty, die, is_test_path, out, run, state_dir


# The wrapper's bound sits above the longest gate a product runs, with headroom for a slower
# host: a bound below the wall converts green runs into 124s.
_GATE_TIMEOUT_DEFAULT_MIN = 90
# the environment variable the runner names a gate by when it calls run-gate
GATE_NAME_ENV = "HB_GATE"


def gates_block(c=None):
    """The `gates` block of the product config, or {} when it is absent or a bare list."""
    block = (c if c is not None else cfg_or_empty()).get("gates")
    return block if isinstance(block, dict) else {}


# ── preflight ─────────────────────────────────────────────────────────────


def _last_json_line(text):
    for line in reversed((text or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def preflight(args, env, gate_name, cwd=None):
    """(argv, env, notes) after the product's `gates.preflight` command, or the gate as given
    when none is configured. The command reads `{"argv", "env", "gate"}` on stdin and prints one
    JSON line: `{"argv", "env", "notes"?}` adjusted, or `{"refuse": why}`. Every product fact a
    gate needs (which database, which marker, which run it refuses) is the product's to say."""
    command = gates_block().get("preflight")
    if not command:
        return args, env, []
    if cwd is None:
        # the preflight is a product command written from the repo root; a gate with a cwd of its
        # own (a dashboard folder) must not move it
        rc_top, top, _e = run(["git", "rev-parse", "--show-toplevel"])
        cwd = top.strip() if rc_top == 0 and top.strip() else None
    request = json.dumps({"argv": args, "env": env, "gate": gate_name})
    rc, o, e = run(shlex.split(command, posix=(os.name != "nt")), timeout=300, input=request,
                   cwd=str(cwd) if cwd else None)
    answer = _last_json_line(o)
    if rc != 0 or not isinstance(answer, dict):
        out({"error": f"run-gate: gates.preflight answered no JSON line (exit {rc}): {(o + e)[-300:]}",
             "verdict": "refused-preflight-silent", "gate_cmd": " ".join(args)}, 2)
    if answer.get("refuse"):
        out({"error": f"run-gate: the product's preflight refused this gate: {answer['refuse']}",
             "verdict": "refused-by-preflight", "gate_cmd": " ".join(args)}, 2)
    new_args = answer.get("argv")
    if not isinstance(new_args, list) or not new_args:
        out({"error": "run-gate: gates.preflight answered without an argv list",
             "verdict": "refused-preflight-silent", "gate_cmd": " ".join(args)}, 2)
    merged_env = dict(env, **{str(k): str(v) for k, v in (answer.get("env") or {}).items()})
    return [str(x) for x in new_args], merged_env, [str(n) for n in answer.get("notes") or []]


def _kill_tree(pid):
    """Kill the process AND its children.

    On Windows only `taskkill /T` reaches the tree; a bare terminate() leaves
    pytest-xdist workers holding the port and the database. MSYS_NO_PATHCONV stops
    Git Bash rewriting `/PID` into a path.
    """
    if os.name == "nt":
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, env=env)
    else:
        import signal
        try:
            group = os.getpgid(pid)
            if group == os.getpgid(0):
                # the child shares our group: killing the group would kill us and whatever runs us
                os.kill(pid, signal.SIGKILL)
            else:
                os.killpg(group, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _is_silent_ok(args):
    """Is this a tool whose silence means success (config `gates.silent_ok_tools`)? Exit 0
    with an empty capture is its pass shape; every other tool keeps the empty capture
    refusal. Checks the first token and, for an interpreter invocation, the `-m` module."""
    tools = [str(t).lower() for t in gates_block().get("silent_ok_tools") or []]
    names = []
    if args:
        names.append(Path(args[0]).stem.lower())
    for i, tok in enumerate(args):
        if tok == "-m" and i + 1 < len(args):
            names.append(str(args[i + 1]).split(".")[0].lower())
            break
    return any(n == t or n.startswith(t) for n in names for t in tools)


_TRUE_EXIT_LINE = re.compile(r"^TRUE_EXIT=(\d+)\s*$", re.M)


def read_true_exit(text):
    """The result an artifact or a run-gate's output holds: its LAST top-level `TRUE_EXIT=N`
    line, or None when it holds none (a proof gate prints indented arm lines too, and a
    partial artifact holds none: UNMEASURED, never green)."""
    found = _TRUE_EXIT_LINE.findall(str(text or ""))
    return int(found[-1]) if found else None


def cmd_run_gate(a):
    """The sanctioned gate runner: it runs the gate command DIRECTLY (no shell, no pipeline,
    no redirect), captures combined output to the evidence dir, prints the tail plus a
    TRUE_EXIT line, and exits with the command's OWN code. Agents quote the TRUE_EXIT line
    verbatim."""
    # run-gate never SILENTLY rewrites the command: only the leading `--` is stripped (a
    # gate's own inner `--` belongs to it), and what was TYPED is compared with what will
    # run; any difference is warned in the artifact and on stderr. The receipt is GATE_CMD.
    args = list(a.gateargs or [])
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        die("run-gate requires a command after -- (e.g. hb run-gate --issue N -- pytest -q ...)")
    # `bash -c "pytest ...; echo done"` exits with the ECHO's code, so a red
    # suite reports TRUE_EXIT=0. The wrapper cannot see through the shell string, so
    # the shape is refused at the door rather than measured wrong.
    if len(args) >= 3 and Path(args[0]).name.split(".")[0] in ("bash", "sh") \
            and args[1] == "-c":
        _stmts = [x.strip() for x in re.split(r"[;&|]+", args[2]) if x.strip()]
        if _stmts and _stmts[-1].split()[0:1] == ["echo"]:
            out({"error": "run-gate: a trailing echo launders the exit code; put the "
                          "gate command last",
                 "verdict": "refused-trailing-echo",
                 "gate_cmd": " ".join(args),
                 "final_statement": _stmts[-1]}, 3)
    gate_warnings = []
    if "--" in sys.argv:
        typed = sys.argv[sys.argv.index("--") + 1:]
        if typed != args:
            import collections
            dropped = list((collections.Counter(typed) - collections.Counter(args)).elements())
            gate_warnings.append(
                "RUN_GATE_WARNING: the command that RAN differs from what was typed after "
                f"`--`: dropped/moved tokens: {dropped or '(reordered)'} (typed: "
                f"{' '.join(typed)}). A flag meant for the gate was likely eaten by "
                "run-gate's own parser; quote the GATE_CMD line, never the invocation.")
    # one gates dir per slice, under the slice's own evidence tree, so its artifacts
    # travel with the rest of its evidence
    outdir = state_dir() / "evidence" / (a.issue or "na").lstrip("#") / "gates"
    outdir.mkdir(parents=True, exist_ok=True)
    # UTC, like every other timestamp the harness writes; local time goes backwards at a DST change
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    outfile = outdir / f"gate-{stamp}.txt"
    child_env = dict(os.environ)
    # A gate that runs python inside bash -c never reaches the argv pin below, and PATH's
    # python may hold none of the project's pins; the venv leads PATH for every gate.
    venv_python, venv_why = _canon_python()
    if "venv" in venv_why:
        child_env["PATH"] = str(Path(venv_python).parent) + os.pathsep + child_env.get("PATH", "")
    # The gate's clock is UTC, because CI's is. MEASURED on this host: a local gate
    # ran on Central time while the same suite ran on Linux under UTC, so a
    # clock-sensitive test passed here and reddened there, and the artifact said
    # nothing about which day the child thought it was. --tz machine keeps the host
    # clock for the rare gate that means to measure it; either way the artifact
    # names the value, because an unnamed clock is an unmeasured one.
    tz_choice = (getattr(a, "tz", None) or "UTC").strip()
    if tz_choice.lower() == "machine":
        tz_line = "TZ=machine (the host clock, --tz machine; no override was set)"
    else:
        child_env["TZ"] = tz_choice
        tz_line = f"TZ={tz_choice}"
    # the product's preflight adjusts or refuses the gate before it runs; its notes are
    # written into the artifact, because a receipt that does not name what it ran on is
    # unmeasured on that axis
    gate_name = os.environ.get(GATE_NAME_ENV) or Path(args[0]).stem
    args, child_env, preflight_notes = preflight(args, child_env, gate_name)
    # Windows: CreateProcess resolves bare names to .exe only (`npx` -> 127 while `npx.cmd`
    # works); resolve via PATH. A bare `python` is pinned to the product's interpreter, and
    # the artifact names which one ran.
    interp_note = None
    if Path(args[0]).stem.lower() in ("python", "python3", "py"):
        args[0], interp_note = _canon_python()
    else:
        args[0] = shutil.which(args[0]) or args[0]
    timed_out = False
    _tmo = (a.timeout_min or _GATE_TIMEOUT_DEFAULT_MIN)
    try:
        # Popen + wait, not subprocess.run, because a timeout must be able
        # to reach the CHILDREN. `subprocess.run` kills only the process it started;
        # pytest -n spawns workers that survive it, keep the port and the database
        # busy, and wedge the next gate on this host.
        # off Windows the gate gets its own process group, so killing its tree at a timeout
        # takes its workers and nothing above it: in the parent's group, killpg took the
        # drill runner and a hosted CI runner with it
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             env=child_env, start_new_session=(os.name != "nt"))
        try:
            blob = p.communicate(timeout=_tmo * 60)[0] or ""
            code = p.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(p.pid)
            try:
                blob = p.communicate(timeout=30)[0] or ""
            except Exception:      # noqa: BLE001 - the capture is best-effort here
                blob = ""
            code = 124
            blob += f"\nrun-gate: TIMEOUT after {_tmo}min; process tree killed"
    except FileNotFoundError:
        code, blob = 127, f"run-gate: command not found: {args[0]}"
    # An EMPTY capture is a named refusal, never a pass: a reader thread that died on the
    # console encoding returns an empty blob, and the exit code alone would then read as a
    # green gate. A 0 over an empty capture is the failure mode, so `code` is not trusted
    # here. 127 (command not found) keeps its own blob.
    silent_ok = False
    if not blob.strip() and code == 0 and _is_silent_ok(args):
        silent_ok = True
        blob = ("run-gate: silent success - this tool reports nothing when it passes "
                "(gates.silent_ok_tools), and it exited 0. The empty capture is the PASS "
                "shape here, not the dead-reader shape.")
    elif not blob.strip():
        blob = (f"run-gate: EMPTY CAPTURE: the command exited {code} but produced no "
                "output on either stream. That is unmeasured, not green: a "
                "dead reader thread, a swallowed encoding error and a genuinely silent "
                "command are indistinguishable from here, so the gate refuses rather "
                "than laundering the exit code into a verdict. Re-run with the output "
                "visible; if the command really is silent, capture something it prints.")
        code = 2
    # The TRUE_EXIT line and every warning go INTO the artifact, not only to the terminal:
    # an exit code that exists nowhere on disk is a claim about a measurement, not one.
    warn_blob = ("".join(w + "\n" for w in gate_warnings))
    if warn_blob:
        sys.stderr.write(warn_blob)
    head_line = ""
    if silent_ok:
        head_line += "silent success\n"
    if interp_note:
        head_line += f"INTERPRETER={args[0]} ({interp_note})\n"
    head_line += tz_line + "\n"
    head_line += "".join(note + "\n" for note in preflight_notes)
    # ruff prints `warning: Invalid \`# noqa\` directive` and still exits 0. A suppression
    # it cannot parse suppresses nothing while reading as if it does, so the gate reddens.
    if code == 0 and re.search(r"warning: Invalid .?# noqa", blob):
        code = 2
        head_line += "RUFF_INVALID_NOQA=true (a malformed suppression reddens the gate)\n"
    outfile.write_text(head_line + blob + "\n" + warn_blob
                       + f"GATE_CMD={' '.join(args)}\n"
                       + (f"TIMEOUT={str(timed_out).lower()}\n" if timed_out else "")
                       + ("SILENT=true\n" if silent_ok else "")
                       + f"TRUE_EXIT={code}\n",
                       encoding="utf-8")
    # every red test id, one line each, so the runner reads the reds from here and proves a
    # pre-existing one itself at the base commit (gating.settle_heavy, gating.known_reds)
    red_now = sorted({m.replace("\\", "/") for m in _NODEID_RE.findall(blob)})
    reds_line = "".join(f"RED_NODEID={n}\n" for n in red_now)
    if reds_line:
        with open(outfile, "a", encoding="utf-8") as fh:
            fh.write(reds_line)
    tail = "\n".join(blob.splitlines()[-40:])
    sys.stdout.write(tail + f"\nGATE_OUTPUT={outfile}\n{reds_line}"
                     + ("TIMEOUT=true\n" if timed_out else "")
                     + ("SILENT=true\n" if silent_ok else "")
                     + f"TRUE_EXIT={code}\n")
    sys.exit(code)


# The node-id shapes pytest prints for a red test. Both the `FAILED x::y - msg`
# short-summary form and the bare `ERROR x::y` collection form.
_NODEID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s.*)?$", re.M)


_FUNC_HEAD = {
    ".py": re.compile(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)", re.M),
    ".ts": re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"
                      r"|^(?:export\s+)?(?:const|let)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>", re.M),
}
_FUNC_HEAD[".tsx"] = _FUNC_HEAD[".ts"]
_FUNC_HEAD[".js"] = _FUNC_HEAD[".ts"]


def _brace_end(lines, start):
    """Index one past the line where the block opened at or after `start` closes,
    counting braces outside strings and line comments. A head with no brace on its
    first three lines (a one-line arrow) is one line."""
    depth, opened = 0, False
    for i in range(start, len(lines)):
        ln = lines[i]
        j, in_str, quote = 0, False, ""
        while j < len(ln):
            ch = ln[j]
            if in_str:
                if ch == "\\":
                    j += 1
                elif ch == quote:
                    in_str = False
            elif ch in ("'", '"', "`"):
                in_str, quote = True, ch
            elif ch == "/" and ln[j + 1:j + 2] == "/":
                break
            elif ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return i + 1
            j += 1
        if not opened and i - start >= 2:
            return start + 1
    return len(lines)


def _all_functions(text, suffix):
    """{name: lines} for every head the size measure recognizes."""
    pat = _FUNC_HEAD.get(suffix)
    if not pat:
        return {}
    lines = text.splitlines()
    sizes = {}
    for i, ln in enumerate(lines):
        m = pat.match(ln.lstrip()) if suffix == ".py" else pat.match(ln)
        if not m or (suffix == ".py" and len(ln) - len(ln.lstrip()) > 4):
            continue
        name = next(g for g in m.groups() if g)
        if suffix == ".py":
            indent = len(ln) - len(ln.lstrip())
            end = len(lines)
            for j in range(i + 1, len(lines)):
                m2 = pat.match(lines[j].lstrip())
                if m2 and len(lines[j]) - len(lines[j].lstrip()) <= indent:
                    end = j
                    break
        else:
            end = _brace_end(lines, i)
        span = end - i
        while span > 1 and not lines[i + span - 1].strip():
            span -= 1
        sizes[name] = max(sizes.get(name, 0), span)
    return sizes


def size_hold(base, head="HEAD", cwd=None):
    """The largest file and the largest function a diff adds or grows, before and after,
    and every one it pushed past the limit. A tripwire for the owner, printed on the
    PR; never a target a builder is told. Limits live in config `review_gate.size_hold`,
    and the paths it ignores (generated migrations, lockfiles) in its `exclude_globs`."""
    c = cfg()
    hold_cfg = (c.get("review_gate") or {}).get("size_hold") or {}
    file_max = int(hold_cfg.get("file_lines", 600))
    func_max = int(hold_cfg.get("function_lines", 120))
    grow = float(hold_cfg.get("grow_ratio", 1.1))  # an already-over file holds past this
    exclude_globs = [str(g) for g in hold_cfg.get("exclude_globs") or []]
    kw = {"cwd": str(cwd)} if cwd else {}
    rc, o, e = run(["git", "diff", "--numstat", f"{base}..{head}"], timeout=120, **kw)
    if rc != 0:
        return {"error": f"git diff failed: {e[:200]}", "verdict": "unmeasured", "hold": False, "reasons": []}
    files = []
    for ln in o.splitlines():
        parts = ln.split("\t")
        if len(parts) != 3 or parts[0] == "-":
            continue
        files.append(parts[2])
    largest_file, largest_func = None, None
    grown_files, grown_funcs = [], []
    for path in files:
        if is_test_path(path) or any(fnmatch.fnmatch(path, g) for g in exclude_globs):
            continue
        suffix = Path(path).suffix
        if suffix not in (".py", ".ts", ".tsx", ".js"):
            continue
        _, after_txt, _ = run(["git", "show", f"{head}:{path}"], timeout=60, **kw)
        _, before_txt, _ = run(["git", "show", f"{base}:{path}"], timeout=60, **kw)
        head_lines = " ".join(after_txt.splitlines()[:8]).lower()
        if "generated" in head_lines or "do not edit" in head_lines:
            continue  # a generated file's size is the generator's, not the wave's
        after_n = len(after_txt.splitlines())
        before_n = len(before_txt.splitlines()) if before_txt else 0
        if after_n > before_n:
            grown_files.append({"path": path, "before": before_n, "after": after_n})
            if largest_file is None or after_n > largest_file["after"]:
                largest_file = grown_files[-1]
        after_fns = _all_functions(after_txt, suffix)
        before_fns = _all_functions(before_txt, suffix) if before_txt else {}
        for name, span in after_fns.items():
            was = before_fns.get(name, 0)
            if span <= was:
                continue  # unchanged or shrunk: not this wave's doing
            grown_funcs.append({"path": path, "name": name, "lines": span, "before": was})
            if largest_func is None or span > largest_func["lines"]:
                largest_func = grown_funcs[-1]
    # The hold is for what THIS wave did: a new file or function over the limit, one
    # the wave pushed over the limit, or one already over that the wave grew by a
    # tenth or more. The repo carries 10k-line modules from before the rule; a wave
    # that adds forty lines to one is not the wave that made it a monolith, and a
    # hold on every such touch would hold every wave.
    def _grew_past(after, before, limit):
        return after > limit and (before <= limit or after >= before * grow)
    # Every offender is named, not only the largest: the fallout wave's 1,465-line new
    # service sat beside an 8,700-line module it grew by 184 lines, and "the largest
    # file" alone would have named the wrong one.
    reasons = []
    for f in sorted(grown_files, key=lambda x: -x["after"]):
        if _grew_past(f["after"], f["before"], file_max):
            reasons.append(f"{f['path']} grows from {f['before']} to {f['after']} lines "
                           f"(limit {file_max}); a module that size owns more than one concern")
    for f in sorted(grown_funcs, key=lambda x: -x["lines"]):
        if _grew_past(f["lines"], f["before"], func_max):
            reasons.append(f"{f['name']} in {f['path']} grows from {f['before']} to "
                           f"{f['lines']} lines (limit {func_max})")
    return {"base": base, "head": head, "files_measured": len(files),
            "largest_file": largest_file, "largest_function": largest_func,
            "limits": {"file_lines": file_max, "function_lines": func_max, "grow_ratio": grow},
            "hold": bool(reasons), "reasons": reasons}


