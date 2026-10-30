"""hb.run.pagehost - the run page kept up on this machine, apart from any chat.

    hb page --install | --uninstall

The page server is one process on one port. Started from a chat it lives inside that
chat's job object, which refuses breakaway and kills it when the chat's process ends, so
it died with no line in its log. On Windows a per-user scheduled task owns it instead:
started at logon and at install, tried again every minute, one instance at a time. The
product's entry reads the pin once at start, so a new pin restarts the server.
"""
import os
import socket
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from hb.rig.common import HB_ENTRY, canon, cfg, run, state_dir

TASK_PREFIX = "Harness run page"
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# the seam a drill replaces, so no drill registers or starts a real task
call = run
popen = subprocess.Popen


def task_name(port):
    return f"{TASK_PREFIX} {port}"


SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_MESSAGING_SOCKET", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_PID",
               # a run restarted from the page takes the product's current pin, not the version this server runs
               "HB_PINNED_ENTRY")


def page_port(port=None):
    return int(port or (cfg().get("page") or {}).get("port") or 4950)


def answering(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_running(port=None):
    """The page server on this port, started apart from the caller when nothing answers:
    by its scheduled task when one is installed, else as a process that leaves the caller's
    job when it may. It runs the product's pinned entry, so it serves the current pin.
    Returns {"page": url, "started": pid, "task" or None}."""
    port = page_port(port)
    url = f"http://127.0.0.1:{port}/"
    if answering(port):
        return {"page": url, "started": None}
    if task_installed(port) and run_task(port):
        started, alive = "task", lambda: True
    else:
        cmd = [sys.executable, str(entry_path()), "page", "--port", str(port)]
        env = {k: val for k, val in os.environ.items() if k not in SESSION_ENV}
        with open(state_dir() / "page.out", "ab") as fh:
            p = spawn(cmd, cwd=str(canon()), env=env, stdout=fh, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, close_fds=True)
        started, alive = p.pid, lambda: p.poll() is None
    deadline = time.time() + 15
    while time.time() < deadline and not answering(port) and alive():
        time.sleep(0.2)
    return {"page": url, "started": started, "up": answering(port)}


def entry_path():
    """The entry this engine runs from: the frozen version a run pinned, else the tree's own hb.py."""
    return Path(os.environ.get("HB_PINNED_ENTRY") or HB_ENTRY)


def windowless_python(exe=None):
    """pythonw beside the interpreter, so a task started at logon opens no console window."""
    exe = Path(exe or sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return quiet if quiet.exists() else exe


def task_xml(root, entry, port, python, user, start=None):
    """The task: at logon and from now, every minute, one instance; a running server makes
    the minute's start a no-op, a dead one is started again within the minute."""
    start = start or datetime.now().replace(microsecond=0).isoformat()
    every = "<Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
    u, py, args, wd = (escape(str(x)) for x in (user, python, f'"{entry}" page --port {port}', root))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>The harness run page at http://127.0.0.1:{port}/, kept up by hb page --install.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{u}</UserId>{every}</LogonTrigger>
    <TimeTrigger><Enabled>true</Enabled><StartBoundary>{start}</StartBoundary>{every}</TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{u}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{py}</Command><Arguments>{args}</Arguments><WorkingDirectory>{wd}</WorkingDirectory></Exec></Actions>
</Task>
"""


def install_steps(name, xml_path):
    return [["schtasks", "/create", "/tn", name, "/xml", str(xml_path), "/f"], ["schtasks", "/run", "/tn", name]]


def uninstall_steps(name):
    return [["schtasks", "/delete", "/tn", name, "/f"]]


def task_installed(port):
    return os.name == "nt" and call(["schtasks", "/query", "/tn", task_name(port)], timeout=30)[0] == 0


def run_task(port):
    return call(["schtasks", "/run", "/tn", task_name(port)], timeout=30)[0] == 0


def spawn(cmd, **kw):
    """A process apart from the caller. On Windows it asks to leave the caller's job, and
    starts inside it when the job forbids leaving."""
    if os.name != "nt":
        return popen(cmd, start_new_session=True, **kw)
    base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        return popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except PermissionError:
        return popen(cmd, creationflags=base, **kw)


def listener_pid(port):
    """The pid listening on the loopback port, or None."""
    if os.name == "nt":
        rc, o, _ = call(["netstat", "-ano", "-p", "TCP"], timeout=30)
        for line in o.splitlines() if rc == 0 else []:
            m = re.match(rf"\s*TCP\s+127\.0\.0\.1:{port}\s+\S+\s+LISTENING\s+(\d+)", line)
            if m:
                return int(m.group(1))
        return None
    rc, o, _ = call(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=30)
    first = o.split()[0] if rc == 0 and o.split() else ""
    return int(first) if first.isdigit() else None


def stop(port):
    """Stop the python serving the port. Returns its pid, or None when nothing was stopped."""
    pid = listener_pid(port)
    if not pid:
        return None
    if os.name == "nt":
        rc, image, _ = call(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=30)
        if "python" not in image.lower():
            return None
        call(["taskkill", "/F", "/PID", str(pid)], timeout=30)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return None
    deadline = time.time() + 10
    while time.time() < deadline and answering(port):
        time.sleep(0.2)
    return pid


def restart(port=None):
    """After a pin change: the old server stops, and the task or a fresh process starts the
    new pin's. Nothing answering means nothing to restart."""
    port = page_port(port)
    if not answering(port):
        return {"restarted": False, "why": "nothing answered on the port"}
    stopped = stop(port)
    if not stopped:
        return {"restarted": False, "why": "the port is held by something that is not a python page server"}
    return {"restarted": True, "stopped": stopped, **ensure_running(port)}


def install(port):
    if os.name != "nt":
        return {"verdict": "not-windows", "why": "a scheduled task is a Windows thing; run `hb page` under your own "
                "service manager (launchd, systemd --user) to keep it up"}
    name = task_name(port)
    xml_path = state_dir() / "page-task.xml"
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    xml_path.write_text(task_xml(canon(), entry_path(), port, windowless_python(), user), encoding="utf-16")
    create, start = install_steps(name, xml_path)
    rc, o, e = call(create, timeout=60)
    if rc != 0:
        return {"verdict": "not-installed", "task": name, "why": (o + e)[-300:]}
    # whatever a chat started holds the port; the task's own process takes it over
    stopped = stop(port)
    call(start, timeout=60)
    deadline = time.time() + 20
    while time.time() < deadline and not answering(port):
        time.sleep(0.3)
    return {"verdict": "installed" if answering(port) else "installed-not-up", "task": name,
            "page": f"http://127.0.0.1:{port}/", "replaced_pid": stopped, "xml": str(xml_path)}


def uninstall(port):
    if os.name != "nt":
        return {"verdict": "not-windows", "why": "no scheduled task on this system"}
    name = task_name(port)
    (step,) = uninstall_steps(name)
    rc, o, e = call(step, timeout=60)
    stopped = stop(port)
    return {"verdict": "uninstalled" if rc == 0 else "not-installed", "task": name, "stopped": stopped,
            **({} if rc == 0 else {"why": (o + e)[-300:]})}
