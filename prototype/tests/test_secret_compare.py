"""Regression tests for bootstrap secret comparison.

Covers the defect found during the DSH bridge verification (Issue #2):
`secrets.compare_digest(str, str)` raises TypeError on non-ASCII operands, and
that exception used to abort the request thread, so the client saw a dropped
connection instead of an HTTP 403.

Run from prototype/:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as crew


class BootstrapSecretTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        crew.DB = Path(self.tmp.name) / "crew.sqlite3"
        crew.BOOTSTRAP_SECRET = "strong-test-secret"
        crew.setup()
        self.srv = crew.ThreadingHTTPServer(("127.0.0.1", 0), crew.CrewHandler)
        self.host = "http://127.0.0.1:" + str(self.srv.server_port)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def post(self, path, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = request.Request(
            self.host + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(r, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_non_ascii_secret_returns_403_instead_of_dropping_the_connection(self):
        # Before the fix this raised TypeError inside the handler: the socket was
        # closed without a response and the client saw RemoteDisconnected.
        status, body = self.post("/api/bootstrap", {"secret": "玖玖的乱码口令", "name": "x"})
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_secret_with_utf8_bom_prefix_returns_403(self):
        # A UTF-8 BOM left on the secret file is the realistic way to hit it.
        status, body = self.post("/api/bootstrap", {"secret": "\ufeffstrong-test-secret", "name": "x"})
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_ascii_secret_still_bootstraps(self):
        status, body = self.post("/api/bootstrap", {"secret": "strong-test-secret", "name": "玖玖"})
        self.assertEqual(status, 201)
        self.assertIn("token", body)

    def test_missing_secret_is_403(self):
        status, body = self.post("/api/bootstrap", {"name": "x"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
