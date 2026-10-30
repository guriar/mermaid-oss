"""The API's two answers, on the standard library test runner: python -m unittest -v"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.state))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()

    def get(self, path):
        with urllib.request.urlopen(self.origin + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_default_greeting(self):
        self.assertEqual(self.get("/api/greeting"), {"greeting": "Hello, world"})

    def test_state_file_changes_the_greeting(self):
        (self.state / "greeting.txt").write_text("", encoding="utf-8")
        self.assertEqual(self.get("/api/greeting"), {"greeting": ""})

    def test_version_names_a_sha(self):
        self.assertIn("sha", self.get("/version"))


if __name__ == "__main__":
    unittest.main()
