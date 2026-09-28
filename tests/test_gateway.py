"""Exercise the real Caddy gateway against an HTTP stub, without cloud/API calls."""

import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        result = json.dumps({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "host": self.headers.get("Host"),
            "origin": self.headers.get("Origin"),
            "session": self.headers.get("Mcp-Session-Id"),
            "body": body.decode(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "stub-session")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    do_POST = do_GET
    do_DELETE = do_GET

    def log_message(self, *args):
        pass


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("CADDY_BIN") or shutil.which("caddy")
        if not binary:
            raise RuntimeError("Install Caddy or set CADDY_BIN to run gateway tests")
        cls.tmp = tempfile.TemporaryDirectory(prefix="chronicle-test-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.upstream.server_close)
        cls.addClassCleanup(cls.upstream.shutdown)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        root = Path(cls.tmp.name)
        config = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text()
        # Only the listener changes; auth, routing, Origin and proxy rules are real.
        config = config.replace("{$MCP_DOMAIN} {", f"http://127.0.0.1:{cls.port} {{")
        (root / "Caddyfile").write_text(config)
        env = dict(os.environ, MCP_DOMAIN="chronicle.specite.dev",
                   MCP_BEARER_TOKEN="a" * 64, ACME_EMAIL="test@example.com",
                   MCP_UPSTREAM=f"127.0.0.1:{cls.upstream.server_port}",
                   XDG_DATA_HOME=str(root / "data"), XDG_CONFIG_HOME=str(root / "config"))
        cls.log = (root / "caddy.log").open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            [binary, "run", "--config", str(root / "Caddyfile"), "--adapter", "caddyfile"],
            env=env, stdout=cls.log, stderr=cls.log,
        )
        cls.addClassCleanup(cls.stop_caddy)
        for _ in range(100):
            if cls.process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        cls.log.seek(0)
        raise RuntimeError(cls.log.read())

    @classmethod
    def stop_caddy(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)

    def request(self, method="GET", path="/mcp/", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def auth(self, **extra):
        return {"Authorization": "Bearer " + "a" * 64, **extra}

    def test_missing_and_invalid_auth_on_all_methods(self):
        for method in ("GET", "POST", "DELETE"):
            for value in (None, "Bearer wrong", "Bearer", "Basic " + "a" * 64):
                with self.subTest(method=method, value=value):
                    headers = {} if value is None else {"Authorization": value}
                    status, response_headers, _ = self.request(method, headers=headers)
                    self.assertEqual(status, 401)
                    self.assertEqual(response_headers["Www-Authenticate"], 'Bearer realm="chronicle"')

    def test_query_string_is_not_auth(self):
        self.assertEqual(self.request(path="/mcp/?token=" + "a" * 64)[0], 401)

    def test_methods_session_and_body_pass_through(self):
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method=method):
                status, headers, body = self.request(
                    method, headers=self.auth(**{"Mcp-Session-Id": "session-123"}), body="{}")
                self.assertEqual(status, 200)
                result = json.loads(body)
                self.assertEqual(result["method"], method)
                self.assertEqual(result["body"], "{}")
                self.assertEqual(result["session"], "session-123")
                self.assertIsNone(result["authorization"])
                self.assertEqual(result["host"], "localhost:8000")
                self.assertEqual(headers["Mcp-Session-Id"], "stub-session")

    def test_invalid_origins(self):
        for origin in ("https://evil.example", "null", "http://chronicle.specite.dev",
                       "https://chronicle.specite.dev.evil.example"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers=self.auth(Origin=origin))[0], 403)

    def test_allowed_origin(self):
        status, _, body = self.request(headers=self.auth(Origin="https://chronicle.specite.dev"))
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["origin"])

    def test_non_mcp_endpoints_are_not_exposed(self):
        for path in ("/health", "/", "/mcp-evil", "/config/"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, headers=self.auth())[0], 404)

    def test_both_mcp_paths_are_proxied(self):
        for path in ("/mcp", "/mcp/"):
            status, _, body = self.request(path=path, headers=self.auth())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["path"], "/mcp")
