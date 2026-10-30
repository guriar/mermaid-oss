"""hb.run.page - the run page, served on this machine.

    hb page [--port 4950] [--detach | --install | --uninstall]
    hb page prune [--days 14] [--dry-run]

One small server over the read model (hb.run.view): the home, which shows every live run
and what finished lately, one run's page, the pictures a run left, and the two things the
owner can do from the page. The home is kept ready by hb.run.watcher and pushed to open
pages as server-sent events at /api/events; no request reads a journal it has read before
or waits on GitHub or the venue. An answer is written to the run's control file exactly as
the chat would write it and the run is started again the way it was first started; a hold
writes `pause`. It listens on loopback only and serves no file outside a run's own folder.
`--detach` starts the server when nothing answers on the port; `--install` hands it to a
scheduled task so it outlives every chat (hb.run.pagehost).
"""
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from hb.rig.common import canon, cfg, out, state_dir
from hb.record import control
from hb.record import pagehost
from hb.record import prune
from hb.record import view
from hb.record import watcher

PAGE = Path(__file__).with_name("page.html")


def page_html():
    """The page, with the journal's one list of run states put in where it reads them."""
    from hb.record.journal import STATE_LABELS
    return PAGE.read_bytes().replace(b"/*RUN_STATES*/{}", json.dumps(STATE_LABELS).encode("utf-8"))
CHUNK = 4 * 1024 * 1024
# a seat signs in on its own; the chat's own session variables must not reach it



def answer(run_id, stop_id, text):
    """Write the owner's answer beside its stop id and start the run again as it was
    started. A run from before launch cards has no recorded flags: the answer is written
    and the chat starts it again."""
    v = view.run(run_id)
    if not v:
        return {"ok": False, "why": "no such run"}
    text = " ".join(str(text or "").split())
    if not text:
        return {"ok": False, "why": "the answer is empty"}
    known = {s["id"] for s in v["stops"]}
    if stop_id not in known:
        return {"ok": False, "why": f"{stop_id} is not an open stop on this run"}
    d = view.root() / run_id
    with open(control.path(d), "a", encoding="utf-8") as f:
        f.write(f"answer: {stop_id}: {text}\n")
    if len(known) > 1:
        return {"ok": True, "resumed": False, "why": "recorded; the run starts again when every stop is answered"}
    if not v.get("launch"):
        return {"ok": True, "resumed": False, "why": "recorded; this run predates launch cards, start it again from the chat"}
    cmd = [sys.executable, str(pagehost.entry_path()), "run", v["kind"], str(v["ref"]), "--resume", run_id,
           *v["launch"].get("flags", [])]
    env = {k: val for k, val in os.environ.items() if k not in pagehost.SESSION_ENV}
    log = state_dir() / "reviews" / f"run-{run_id}.resume.out"
    log.parent.mkdir(parents=True, exist_ok=True)
    # a hidden console, as detach starts a run: no console at all would flash one per git child
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(log, "ab") as fh:
        subprocess.Popen(cmd, cwd=str(canon()), env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=os.name != "nt")
    return {"ok": True, "resumed": True}


def hold(run_id):
    d = view.root() / run_id
    if not (d / "journal.jsonl").exists():
        return {"ok": False, "why": "no such run"}
    with open(control.path(d), "a", encoding="utf-8") as f:
        f.write("pause\n")
    return {"ok": True}


def _evidence_file(run_id, rel):
    """A file under the run's own evidence folder, or None: nothing outside it is served."""
    base = (view.root() / run_id / "evidence").resolve()
    p = (base / rel).resolve()
    return p if p.is_file() and base in p.parents else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, p, ctype):
        """A file whole, or the one byte range the browser asked for: a film is seekable only
        when its server answers Range with 206."""
        size = p.stat().st_size
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if not m or not (m.group(1) or m.group(2)):
            return self._send(200, p.read_bytes(), ctype)
        if m.group(1):
            # an open-ended ask gets one chunk, so a long film is never read whole into memory
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else start + CHUNK - 1, size - 1)
        else:
            start, end = max(size - int(m.group(2)), 0), size - 1
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        with open(p, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        parts = [x for x in path.split("/") if x]
        if not parts:
            return self._send(200, page_html(), "text/html; charset=utf-8")
        if parts == ["api", "dashboard"]:
            if watcher.CURRENT:
                return self._send(200, watcher.CURRENT.served())
            from hb.record import dashboard
            return self._send(200, dashboard.home())
        if parts == ["api", "events"]:
            if not watcher.CURRENT:
                return self._send(503, {"why": "no watcher on this server"})
            return watcher.stream(self, watcher.CURRENT)
        if parts[:2] == ["api", "run"] and len(parts) == 3:
            v = view.run(parts[2])
            return self._send(200 if v else 404, v or {"why": "no such run"})
        if parts[0] == "file" and len(parts) >= 3:
            p = _evidence_file(parts[1], "/".join(parts[2:]))
            if p:
                ctype = mimetypes.guess_type(p.name)[0] or ("video/webm" if p.suffix == ".webm" else "application/octet-stream")
                return self._send_file(p, ctype)
        return self._send(404, {"why": "not found"})

    def do_POST(self):
        parts = [x for x in unquote(self.path).split("/") if x]
        # a page on another origin cannot read this server, and must not be able to write to it
        origin = self.headers.get("Origin") or ""
        if origin and origin.split("//")[-1] != self.headers.get("Host"):
            return self._send(403, {"why": "cross-origin"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"why": "not JSON"})
        if parts[:2] == ["api", "run"] and len(parts) == 4 and parts[3] == "answer":
            return self._send(200, answer(parts[2], str(body.get("id") or ""), body.get("text")))
        if parts[:2] == ["api", "run"] and len(parts) == 4 and parts[3] == "hold":
            return self._send(200, hold(parts[2]))
        return self._send(404, {"why": "not found"})


def cmd_page(a):
    if a.verb == "prune":
        out(prune.nightly(view.root(), view.root().parent / "evidence", days=a.days, dry_run=a.dry_run), 0)
    if a.install or a.uninstall:
        r = (pagehost.install if a.install else pagehost.uninstall)(pagehost.page_port(a.port))
        out(r, 0 if r["verdict"] in ("installed", "uninstalled", "not-windows") else 1)
    if sys.stdout is None:
        # pythonw, as the scheduled task starts it, has no console: what it says goes to the log
        sys.stdout = sys.stderr = open(state_dir() / "page.out", "a", encoding="utf-8", buffering=1)
    if a.detach:
        r = pagehost.ensure_running(a.port)
        out({"verdict": "running" if r.get("up", True) else "not-up", **r}, 0 if r.get("up", True) else 1)
    port = pagehost.page_port(a.port)
    # the first reading of every journal takes seconds; it is done before the port opens
    watcher.Watcher().start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(json.dumps({"page": f"http://127.0.0.1:{port}/", "ok": True}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        out({"verdict": "stopped"}, 0)


def register(sub):
    s = sub.add_parser("page", help="the run page: every run and what needs you, at http://127.0.0.1:4950/")
    s.add_argument("verb", nargs="?", choices=["prune"], help="prune: old runs lose their pictures and keep their record; old gate transcripts go")
    s.add_argument("--port", type=int)
    s.add_argument("--days", type=int, default=prune.DAYS, help="prune: runs whose journal has not moved for this many days")
    s.add_argument("--dry-run", action="store_true", help="prune: list what would go and delete nothing")
    s.add_argument("--detach", action="store_true", help="start the server as its own process unless the port already answers")
    s.add_argument("--install", action="store_true", help="Windows: a scheduled task keeps the page up at logon and restarts it within a minute")
    s.add_argument("--uninstall", action="store_true", help="remove that task and stop the page")
