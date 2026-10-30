"""The venue adapter for hello-web: what mermaid's `venue` config block runs.

Every verb prints one JSON line last. The harness never knows how the venue is built; this
file does. It starts the app's server on a free port, remembers it under .venue/, builds the
two states the contract names by writing the state file the server reads, and answers the
checks by asking the API.

    python tools/venue.py capabilities
    python tools/venue.py up --name <venue> --worktree <path>
    python tools/venue.py down --name <venue> [--keep]
    python tools/venue.py list
    python tools/venue.py state build|check|undo <state> --name <venue>
    python tools/venue.py version --name <venue>
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENUES = ROOT / ".venue"
STATES = {
    "greeting-default": {"says": "the API answers the default greeting", "text": None},
    "greeting-empty": {"says": "the API answers an empty greeting, the page shows its empty face", "text": ""},
    "greeting-long": {"says": "the API answers a greeting too long for one line", "text": "Hello, " + "world " * 30},
}


def say(obj, code=0):
    print(json.dumps(obj), flush=True)
    sys.exit(code)


def record_path(name):
    VENUES.mkdir(exist_ok=True)
    return VENUES / f"{name}.json"


def read(name):
    p = record_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(origin, path):
    with urllib.request.urlopen(f"{origin}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd_capabilities(_a):
    say(["ui", "states"])


def cmd_up(a):
    rec = read(a.name)
    if rec and alive(rec.get("pid")):
        say({"origin": rec["origin"], "served_sha": rec.get("served_sha"), "facts": {"port": rec["port"], "reused": True}})
    worktree = Path(a.worktree or ROOT).resolve()
    port = free_port()
    state_dir = VENUES / a.name / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = open(VENUES / f"{a.name}.log", "ab")
    p = subprocess.Popen([sys.executable, str(worktree / "app" / "server.py"), "--port", str(port), "--state-dir", str(state_dir)],
                         cwd=str(worktree), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    origin = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            sha = get(origin, "/version").get("sha")
            break
        except OSError:
            time.sleep(0.2)
    else:
        say({"error": f"the server did not answer on {origin}"}, 1)
    rec = {"name": a.name, "pid": p.pid, "port": port, "origin": origin, "served_sha": sha, "worktree": str(worktree),
           "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    record_path(a.name).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    say({"origin": origin, "served_sha": sha, "facts": {"port": port}})


def alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_down(a):
    rec = read(a.name)
    if not rec:
        say({"down": a.name, "was": "absent"})
    pid = rec.get("pid")
    if alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    if not a.keep:
        state = VENUES / a.name / "state" / "greeting.txt"
        if state.exists():
            state.unlink()
    record_path(a.name).unlink(missing_ok=True)
    say({"down": a.name, "kept": bool(a.keep)})


def cmd_list(_a):
    rows = []
    for p in sorted(VENUES.glob("*.json")) if VENUES.exists() else []:
        rec = json.loads(p.read_text(encoding="utf-8"))
        rows.append({"name": rec["name"], "origin": rec["origin"], "started": rec.get("started"), "alive": alive(rec.get("pid"))})
    say(rows)


def cmd_state(a):
    rec = read(a.name)
    if not rec:
        say({"error": f"no venue {a.name}"}, 2)
    spec = STATES.get(a.state)
    if spec is None:
        say({"error": f"unknown state {a.state}", "known": sorted(STATES)}, 2)
    f = VENUES / a.name / "state" / "greeting.txt"
    if a.verb == "build":
        if spec["text"] is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(spec["text"], encoding="utf-8")
        say({"built": a.state})
    if a.verb == "undo":
        f.unlink(missing_ok=True)
        say({"undone": a.state})
    want = spec["text"] if spec["text"] is not None else "Hello, world"
    got = get(rec["origin"], "/api/greeting").get("greeting")
    holds = got == want.strip()
    say({"state": a.state, "holds": holds, "got": got[:60]}, 0 if holds else 1)


def cmd_version(a):
    rec = read(a.name)
    if not rec:
        say({"error": f"no venue {a.name}"}, 2)
    say(get(rec["origin"], "/version"))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capabilities").set_defaults(fn=cmd_capabilities)
    s = sub.add_parser("up"); s.add_argument("--name", required=True); s.add_argument("--worktree"); s.add_argument("options", nargs="*"); s.set_defaults(fn=cmd_up)
    s = sub.add_parser("down"); s.add_argument("--name", required=True); s.add_argument("--keep", action="store_true"); s.set_defaults(fn=cmd_down)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    s = sub.add_parser("state"); s.add_argument("verb", choices=["build", "check", "undo"]); s.add_argument("state"); s.add_argument("--name", required=True); s.set_defaults(fn=cmd_state)
    s = sub.add_parser("version"); s.add_argument("--name", required=True); s.set_defaults(fn=cmd_version)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
