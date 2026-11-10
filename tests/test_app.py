import contextlib
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("account_app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

FAKE_CLI = r'''#!/usr/bin/env python3
import json,os,sys,pathlib
p=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
if sys.argv[1:3] == ['auth','status']:
    signed=(p/'.signed-in').exists()
    print(json.dumps({'loggedIn':signed,'authMethod':'claude.ai' if signed else 'none',
      'email':'test@example.com' if signed else None,'subscriptionType':'pro',
      'accessToken':'MUST_NOT_LEAK','refreshToken':'MUST_NOT_LEAK'}))
    sys.exit(0 if signed else 1)
if sys.argv[1:3] == ['auth','login']:
    print('Open: https://claude.ai/oauth/authorize?state=test-only',flush=True)
    print('Paste code here if prompted:',flush=True)
    code=sys.stdin.readline().strip()
    if code == 'valid-test-code':
        (p/'.signed-in').write_text('ok')
        print('Login successful',flush=True)
        sys.exit(0)
    print('Invalid code',flush=True)
    sys.exit(1)
sys.exit(2)
'''


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex_environment = patch.dict("os.environ", {"CODEX_HOME": str(self.home / ".codex")})
        self.codex_environment.start()
        self.cli = self.root / "claude"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o700)
        self.manager = app.Manager(self.root / "data", self.home, str(self.cli), auto_import=False)

    def tearDown(self):
        self.manager.close()
        self.codex_environment.stop()
        self.temp.cleanup()

    def account(self, name="工作号", signed=False):
        ident = self.manager.create(name)
        item = self.manager.account(ident)
        if signed:
            (self.manager.profile(item) / ".signed-in").write_text("ok")
        return item

    def wait_job(self, job):
        limit = time.monotonic() + 5
        while job.status == "running" and time.monotonic() < limit:
            time.sleep(0.02)
        self.assertNotEqual(job.status, "running")

    def test_share_transcript_and_activate_same_session_without_copying_credentials(self):
        one = self.account("工作", True)
        two = self.account("个人", True)
        project = self.manager.shared / "project"
        project.mkdir()
        (project / "same-session.jsonl").write_text('{"sessionId":"same-session"}\n')
        with patch.object(app, "claude_processes", return_value=[]):
            self.manager.activate(one["id"])
            first = (self.manager.data / "current/projects/project/same-session.jsonl").read_text()
            self.manager.activate(two["id"])
            self.assertEqual((self.manager.data / "current/projects/project/same-session.jsonl").read_text(), first)
        self.assertEqual(self.manager.active_id(), two["id"])
        self.assertTrue(all(a["historyShared"] for a in self.manager.state()["accounts"]))

    def test_running_claude_blocks_switch_and_preserves_pointer(self):
        one = self.account("原账号", True)
        two = self.account("新账号", True)
        with patch.object(app, "claude_processes", return_value=[]):
            self.manager.activate(one["id"])
        with patch.object(app, "claude_processes", return_value=[{"pid": 999}]):
            with self.assertRaisesRegex(app.AppError, "Claude 进程"):
                self.manager.activate(two["id"])
        self.assertEqual(self.manager.active_id(), one["id"])

    def test_unsigned_and_invalid_account_cannot_activate(self):
        item = self.account()
        with patch.object(app, "claude_processes", return_value=[]):
            with self.assertRaisesRegex(app.AppError, "尚未登录"):
                self.manager.activate(item["id"])
        for ident in ["../../.claude", "f" * 16, None, []]:
            with self.assertRaises(app.AppError):
                self.manager.account(ident)
        self.assertFalse((self.manager.data / "current").exists())

    def test_login_pty_and_status_redact_secrets_and_never_auto_activate(self):
        item = self.account()
        public = self.manager.start_login(item["id"])
        job = self.manager.job(public["id"])
        limit = time.monotonic() + 5
        while not job.public()["links"] and time.monotonic() < limit:
            time.sleep(0.02)
        self.assertIn("https://claude.ai/oauth/authorize?state=test-only", job.public()["links"])
        job.send("valid-test-code")
        self.wait_job(job)
        self.assertEqual(job.status, "completed")
        self.assertTrue(self.manager.account(item["id"])["auth"]["loggedIn"])
        self.assertIsNone(self.manager.active_id())
        output = json.dumps(self.manager.state()) + self.manager.registry_path.read_text()
        self.assertNotIn("MUST_NOT_LEAK", output)
        self.assertNotIn("valid-test-code", output)
        self.assertNotIn("oauth/authorize", self.manager.registry_path.read_text())

    def test_cancel_login_and_prevent_duplicate_live_jobs(self):
        item = self.account()
        job = self.manager.job(self.manager.start_login(item["id"])["id"])
        with self.assertRaisesRegex(app.AppError, "已有登录"):
            self.manager.start_login(item["id"])
        job.cancel()
        self.wait_job(job)
        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(job.process.poll())

    def test_import_native_does_not_change_existing_files(self):
        self.manager.close()
        native = self.home / ".claude"
        (native / "projects").mkdir(parents=True)
        cred = native / ".credentials.json"
        cred.write_text('{"secret":"never-return-this"}')
        config = self.home / ".claude.json"
        config.write_text('{"oauthAccount":{"emailAddress":"local@example.com"}}')
        (native / ".signed-in").write_text("ok")
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.manager = app.Manager(self.root / "native-data", self.home, str(self.cli))
        after = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        item = self.manager.registry["accounts"][0]
        self.assertEqual(self.manager.profile(item), native)
        self.assertEqual((self.manager.data / "current").resolve(), native.resolve())
        self.assertNotIn("never-return-this", json.dumps(self.manager.state()))
        with self.assertRaisesRegex(app.AppError, "原终端登录"):
            self.manager.start_login(item["id"])
        # Native atomic replacement stays visible through the fixed directory.
        replacement = native / ".new-credential"
        replacement.write_text("rotated")
        os.replace(replacement, cred)
        self.assertEqual((self.manager.data / "current/.credentials.json").read_text(), "rotated")

    def test_protect_unrelated_current_directory_and_reject_duplicate_names(self):
        item = self.account(signed=True)
        current = self.manager.data / "current"
        current.mkdir()
        sentinel = current / "keep.txt"
        sentinel.write_text("keep")
        with patch.object(app, "claude_processes", return_value=[]):
            with self.assertRaisesRegex(app.AppError, "保护现有文件"):
                self.manager.activate(item["id"])
        self.assertEqual(sentinel.read_text(), "keep")
        with self.assertRaisesRegex(app.AppError, "名称已经存在"):
            self.manager.create("工作号")
        self.assertEqual(self.manager.registry_path.stat().st_mode & 0o777, 0o600)

    def test_shared_history_location_survives_restart(self):
        item = self.account()
        original_shared = self.manager.shared
        self.manager.close()
        (self.home / ".claude/projects").mkdir(parents=True)
        self.manager = app.Manager(self.root / "data", self.home, str(self.cli), auto_import=False)
        self.assertEqual(self.manager.shared, original_shared)
        self.assertEqual((self.manager.profile(item) / "projects").resolve(), original_shared.resolve())

    def test_http_requires_session_csrf_and_exact_origin(self):
        server = app.Server(("127.0.0.1", 0), self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        try:
            conn.request("GET", "/api/state")
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.request("GET", "/")
            response = conn.getresponse()
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            html = response.read().decode()
            token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
            headers = {"Cookie": cookie, "X-CSRF-Token": token, "Content-Type": "application/json"}
            conn.request("POST", "/api/accounts", json.dumps({"name": "from-web"}), headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            headers["Origin"] = "https://evil.example"
            conn.request("POST", "/api/accounts", '{"name":"blocked"}', headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.request("GET", "/", headers={"Host": "evil.example"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            self.assertEqual(len(self.manager.registry["accounts"]), 1)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

    def test_lan_form_login_protects_data_and_keeps_origin_checks_without_browser_prompt(self):
        with self.assertRaisesRegex(app.AppError, "访问口令"):
            app.Server(("0.0.0.0", 0), self.manager)
        server = app.Server(("127.0.0.1", 0), self.manager,
                            password="test-only-password", allowed_hosts=["192.168.1.20"])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        host = f"192.168.1.20:{server.server_port}"
        def request(method, path, body=None, headers=None):
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Host": host, "Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            return response, response.read()
        try:
            response, html = request("GET", "/")
            self.assertEqual(response.status, 200)
            self.assertIn(b'panel-login-form', html)
            self.assertNotIn(b'account-list', html)
            self.assertIsNone(response.getheader("WWW-Authenticate"))
            token = re.search(r'name="csrf-token" content="([^"]+)"', html.decode()).group(1)
            login_headers = {"X-CSRF-Token": token, "Origin": f"http://{host}"}
            for path in ["/app.js", "/api/state", "/api/usage", "/source.zip"]:
                response, _ = request("GET", path)
                self.assertEqual(response.status, 401)
                self.assertIsNone(response.getheader("WWW-Authenticate"))
            for path in ["/style.css", "/favicon.svg", "/login.js"]:
                response, _ = request("GET", path)
                self.assertEqual(response.status, 200)
            response, _ = request("POST", "/api/auth/login", {"password": "test-only-password"}, {**login_headers, "Origin": "http://evil.example"})
            self.assertEqual(response.status, 403)
            response, _ = request("POST", "/api/auth/login", {"password": "test-only-password"})
            self.assertEqual(response.status, 403)
            response, _ = request("POST", "/api/auth/login", {"password": "wrong"}, login_headers)
            self.assertEqual(response.status, 401)
            self.assertIsNone(response.getheader("Set-Cookie"))
            response, _ = request("POST", "/api/auth/login", {"password": "test-only-password"}, login_headers)
            self.assertEqual(response.status, 200)
            cookie = response.getheader("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Max-Age=604800", cookie)
            headers = {**login_headers, "Cookie": cookie.split(";", 1)[0]}
            response, payload = request("GET", "/api/state", headers=headers)
            self.assertEqual(response.status, 200)
            self.assertNotIn(b"test-only-password", payload)
            response, html = request("GET", "/", headers=headers)
            self.assertEqual(response.status, 200)
            self.assertIn(b'account-list', html)
            self.assertIn(b'id="logout"', html)
            response, _ = request("POST", "/api/accounts", {"name": "blocked"}, {**headers, "Origin": "http://evil.example"})
            self.assertEqual(response.status, 403)
            response, _ = request("POST", "/api/auth/login", {"password": "test-only-password"}, login_headers)
            second_cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            self.assertNotEqual(second_cookie, headers["Cookie"])
            response, _ = request("POST", "/api/auth/logout", {}, headers)
            self.assertEqual(response.status, 200)
            self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
            response, _ = request("GET", "/api/state", headers=headers)
            self.assertEqual(response.status, 401)
            response, _ = request("GET", "/api/state", headers={**login_headers, "Cookie": second_cookie})
            self.assertEqual(response.status, 200)
            with server.auth_lock:
                server.auth_sessions[second_cookie.split("=", 1)[1]] = time.time() - 1
            response, _ = request("GET", "/api/state", headers={**login_headers, "Cookie": second_cookie})
            self.assertEqual(response.status, 401)
            for _ in range(5):
                response, _ = request("POST", "/api/auth/login", {"password": "wrong"}, login_headers)
                self.assertEqual(response.status, 401)
            response, _ = request("POST", "/api/auth/login", {"password": "wrong"}, login_headers)
            self.assertEqual(response.status, 429)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
