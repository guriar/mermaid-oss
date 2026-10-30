"""hello-web: one page and one API, on the standard library alone.

GET /                 the page (web/index.html)
GET /api/greeting     {"greeting": "<text>"}; the text comes from the venue's state file when one exists
GET /version          {"sha": "<git head>"} so a harness can read what this server serves

    python app/server.py --port 8765 [--state-dir .venue/state]
"""
import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GREETING = "Hello, world"


def head_sha():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def greeting(state_dir: Path):
    """The greeting the API answers: the state file's text when a state is built, else the default."""
    p = state_dir / "greeting.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else DEFAULT_GREETING


def make_handler(state_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/greeting":
                return self._send(200, {"greeting": greeting(state_dir)})
            if self.path == "/version":
                return self._send(200, {"sha": head_sha()})
            return self._send(404, {"error": "not found"})

    return Handler


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--state-dir", default=str(ROOT / ".venue" / "state"))
    args = a.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Path(args.state_dir)))
    print(json.dumps({"origin": f"http://127.0.0.1:{args.port}", "sha": head_sha()}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
