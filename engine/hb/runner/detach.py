"""hb.run.detach - a run started apart from the shell that asked for it.

A run takes hours, and a conductor's background shell is killed at its own limit; a runner
that outlived its shell could no longer start git or a gate. `--detach` starts the same
command as its own process, sharing no console or session with the caller, and returns the
run id at once with the page's one address, whose home shows every live run; the page
server is started too when nothing answers on its port. The conductor watches it with
`hb run watch` as before.
"""
import os
import subprocess
import sys

from hb.rig.common import out
from hb.runner import liveness
from hb.record.journal import new_run_id, runs_dir

# the conductor's own Claude session: a seat that inherits these signs in as the conductor
CONDUCTOR_ENV = ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_MESSAGING_SOCKET", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                 "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_PID")


def child_env(environ):
    return {k: v for k, v in environ.items() if k not in CONDUCTOR_ENV}


def launch_detached(a):
    """Start this same `hb run` without --detach, naming its run id up front with --resume
    (a fresh id opens a fresh journal), and print the id, the pid and the out file."""
    named = a.resume or (a.repair if a.kind == "wave" else None)
    run_id = named or new_run_id(a.kind, a.ref)
    argv = [x for x in sys.argv[1:] if x != "--detach"] + ([] if named else ["--resume", run_id])
    run_dir = runs_dir() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "detached.out"
    if os.name == "nt":
        # a hidden console of its own: DETACHED_PROCESS would give every git and gate it
        # starts a console window of its own
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        kw = {"start_new_session": True}
    with open(out_path, "ab") as f:
        p = subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0]), *argv], cwd=os.getcwd(),
                             env=child_env(os.environ), stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT, close_fds=True, **kw)
    # the run records its own pid when it opens; until then the page asks after this one
    if not liveness.alive(run_dir):
        liveness.record(run_dir, p.pid)
    from hb.record.pagehost import ensure_running
    try:
        page = ensure_running()
    except Exception as ex:  # the run is already going; a page that will not start must not hide that
        page = {"page": None, "started": None, "why": f"{ex.__class__.__name__}: {ex}"}
    out({"verdict": "detached", "run_id": run_id, "pid": p.pid, "out": str(out_path),
         "watch": f"hb run watch {run_id}", "page": page.get("page"),
         "page_started": page.get("started"), **({"page_why": page["why"]} if page.get("why") else {})}, 0)
