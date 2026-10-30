"""hb.run.hbcall - one hb verb calling another.

A door reaches the ledger, the stand-in and the gate runner as subprocesses of the same
entry file and reads the JSON each prints last.
"""

import json
import subprocess
import sys

from hb.venue import admission
from hb.rig.common import HB_ENTRY


def last_json(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _run(argv, cwd, timeout, env):
    """(rc, stdout, stderr) of argv, its pid watched by the grant this thread holds; past
    `timeout` the whole tree goes and TimeoutExpired is raised."""
    import os
    p = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", env={**os.environ, **(env or {})})
    admission.watch(p.pid)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # the whole tree goes: a timed-out suite's workers held 13 GB after their parent was killed
        kill_tree(p.pid)
        p.communicate()
        raise
    return p.returncode, out or "", err or ""


def hb(args, cwd, timeout=1800, env=None):
    """Call another hb verb as a subprocess and read its JSON."""
    rc, out, err = _run([sys.executable, HB_ENTRY, *args], cwd, timeout, env)
    return rc, last_json(out), out + err


def call(argv, cwd, timeout, kind, holder, c=None, note=None):
    """A product command under its own admission grant: (rc, stdout, stderr), 124 past
    `timeout` with its whole tree killed, 127 when the command is not there."""
    with admission.take(kind, holder, c, note) as grant:
        try:
            return _run(argv, cwd, timeout, grant.get("env"))
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
        except FileNotFoundError as ex:
            return 127, "", str(ex)


def kill_tree(pid):
    """Kill a process and every process under it."""
    import os
    import signal
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        # ponytail: only the direct child off Windows; its group is ours, and killing it would kill the runner
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
