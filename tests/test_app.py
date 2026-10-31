import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse

from superaimail.app import Application
from superaimail.auth import Auth
from superaimail.engine import oauth
from superaimail.engine.core import Worker
from superaimail.engine.accounts import AccountError, preset


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.app = Application(cls.directory.name, port=0, password="test-workspace-password")
        cls.url = cls.app.start()
        cls.port = cls.app.server.server_port
        account, _ = cls.app.worker.accounts.save_account({"address": "reader@example.org", "imap_host": "imap.example.org", "secret": "fixture-secret", "enabled": False})
        cls.account = account
        cls.app.worker.store.add_message({"id": "fixture-message", "account_id": account["id"], "folder": "INBOX", "validity": "1", "uid": 1,
            "received": int(time.time()), "subject": "测试邮件", "from_name": "发件人", "from_addr": "sender@example.org",
            "text": "会议安排在周五下午。", "text_at": int(time.time()), "html": "", "html_inline": {}, "read": 0})

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.app.worker.store.db.close()
        cls.directory.cleanup()

    def setUp(self):
        self.cookie = ""
        self.csrf = ""

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        values = {"Cookie": self.cookie, "Origin": self.url}
        if body is not None:
            values.update({"Content-Type": "application/json", "X-Supermail-CSRF": self.csrf})
        values.update(headers or {})
        connection.request("POST" if body is not None else "GET", path, json.dumps(body).encode() if body is not None else None, values)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def login(self):
        code, headers, payload = self.request("/api/login", {"password": "test-workspace-password"})
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(payload)["csrf"]

    def test_private_api_rejects_anonymous_and_bad_host(self):
        self.assertEqual(self.request("/api/overview")[0], 401)
        self.assertEqual(self.request("/api/session", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("/v1/overview")[0], 404)

    def test_login_cookie_csrf_origin_and_logout(self):
        self.login()
        self.assertIn("HttpOnly", self.request("/api/login", {"password": "test-workspace-password"})[1]["Set-Cookie"])
        self.assertEqual(self.request("/api/action", {"action": "messages_read"}, {"X-Supermail-CSRF": ""})[0], 403)
        self.assertEqual(self.request("/api/action", {"action": "messages_read"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/logout", {})[0], 200)
        self.assertEqual(self.request("/api/overview")[0], 401)

    def test_index_and_detail_work_without_ai_and_do_not_reveal_secrets(self):
        self.login()
        code, headers, body = self.request("/api/overview")
        self.assertEqual(code, 200)
        self.assertNotIn(b"fixture-secret", body)
        self.assertFalse(json.loads(body)["ai"]["enabled"])
        code, _, body = self.request("/api/message?id=fixture-message")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["text"], "会议安排在周五下午。")
        code, _, body = self.request("/api/action", {"action": "ai_summarize", "id": "fixture-message"})
        self.assertEqual(code, 400)
        self.assertIn("AI", json.loads(body)["error"])

    def test_message_list_etag_is_reused_and_invalidated_after_edit(self):
        self.login()
        code, headers, body = self.request("/api/messages")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["messages"][0]["starred"], 0)
        etag = headers["ETag"]
        self.assertEqual(self.request("/api/messages", headers={"If-None-Match": etag})[0], 304)
        try:
            self.assertEqual(self.request("/api/action", {"action": "message_mark", "id": "fixture-message", "starred": True})[0], 200)
            code, changed_headers, body = self.request("/api/messages", headers={"If-None-Match": etag})
            self.assertEqual(code, 200)
            self.assertNotEqual(changed_headers["ETag"], etag)
            self.assertEqual(json.loads(body)["messages"][0]["starred"], 1)
        finally:
            self.app.worker.store.update_message("fixture-message", starred=0)

    def test_public_static_files_and_path_traversal(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/icon.svg")[1]["Content-Type"], "image/svg+xml")
        self.assertEqual(self.request("/../access.json")[0], 404)
        self.assertEqual(self.request("/access.json")[0], 404)

    def test_setup_cannot_overwrite_existing_access_password(self):
        self.assertEqual(self.request("/api/setup", {"password": "other-password"})[0], 400)
        self.login()

    def test_loopback_alias_origin_is_accepted_but_foreign_port_is_rejected(self):
        code, _, _ = self.request("/api/login", {"password": "test-workspace-password"}, {"Origin": "http://localhost:%d" % self.port})
        self.assertEqual(code, 200)
        code, _, _ = self.request("/api/login", {"password": "test-workspace-password"}, {"Origin": "http://localhost:1"})
        self.assertEqual(code, 403)


class AuthTests(unittest.TestCase):
    def test_unknown_mailbox_domain_requires_explicit_imap_host(self):
        self.assertEqual(preset('reader@example.org')['imap_host'], '')
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory)
            try:
                with self.assertRaisesRegex(AccountError, 'IMAP'):
                    worker.accounts.save_account({'address':'reader@example.org','secret':'fixture-only'})
                saved = worker.save_account({'address':'reader@example.org','imap_host':'imap.example.org','secret':'fixture-only','enabled':False})
                self.assertFalse(saved['enabled'])
                self.assertFalse(worker.links)
            finally:
                worker.store.db.close()

    def test_initial_setup_persistence_and_session_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Auth(directory)
            self.assertFalse(auth.configured)
            auth.set_password("my-password-123", initial=True)
            token = auth.login("my-password-123", "127.0.0.1")
            self.assertTrue(Auth(directory).valid(token))
            self.assertFalse(auth.valid(token + "bad"))
            auth.set_password("a-new-password-123")
            self.assertFalse(auth.valid(token))
            with self.assertRaises(ValueError):
                auth.login("my-password-123", "127.0.0.1")


class LocalAIHandler(BaseHTTPRequestHandler):
    received = None

    def log_message(self, *_):
        pass

    def do_POST(self):
        type(self).received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"choices": [{"message": {"content": json.dumps({"summary": "周五开会", "points": ["会议安排"], "todos": ["参加会议"], "needs_reply": False})}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AIIntegrationTests(unittest.TestCase):
    def test_real_compatible_http_call_and_disabled_mode(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                worker = Worker(directory, log=lambda _: None)
                self.assertEqual(worker.accounts.ai()["url"], "")
                worker.accounts.save_ai({"enabled": True, "url": "http://127.0.0.1:%d/v1" % server.server_port, "model": "local-model"})
                summary = worker.ai.summarize({"received": int(time.time()), "subject": "会议", "text": "周五开会"})
                self.assertEqual(summary["summary"], "周五开会")
                self.assertEqual(LocalAIHandler.received["model"], "local-model")
                self.assertIn("不可信", LocalAIHandler.received["messages"][0]["content"])
                self.assertNotIn("reasoning_effort", LocalAIHandler.received)
                worker.accounts.save_ai({"enabled": False})
                with self.assertRaisesRegex(ValueError, "已关闭"):
                    worker.ai.summarize({"received": int(time.time())})
                worker.store.db.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class OAuthTests(unittest.TestCase):
    def test_device_flow_pending_completion_and_refresh_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory, log=lambda _: None)
            client_id = "11111111-2222-3333-4444-555555555555"
            replies = [
                (200, {"device_code": "fixture-device", "user_code": "ABCD-EFGH", "expires_in": 900, "interval": 5}),
                (400, {"error": "authorization_pending"}),
                (200, {"access_token": "first-access", "refresh_token": "first-refresh", "expires_in": 3600}),
                (200, {"access_token": "next-access", "refresh_token": "rotated-refresh", "expires_in": 3600}),
            ]
            with patch.object(worker.oauth, "_post", side_effect=replies) as request:
                flow = worker.oauth.begin(client_id)
                self.assertNotIn("device_code", flow)
                self.assertFalse(worker.oauth.poll(flow["ticket"])["ready"])
                worker.oauth.flows[flow["ticket"]]["next"] = 0
                self.assertTrue(worker.oauth.poll(flow["ticket"])["ready"])
                tokens = worker.oauth.credentials(flow["ticket"])
                tokens["expires"] = 0
                account, _ = worker.accounts.save_account({"address": "reader@outlook.com", "auth_type": "microsoft", "oauth": tokens, "enabled": False})
                prepared = worker.oauth.prepare(account)
                self.assertEqual(prepared["_access_token"], "next-access")
                self.assertEqual(worker.accounts.account(account["id"])["oauth"]["refresh_token"], "rotated-refresh")
                self.assertNotIn("next-access", json.dumps(worker.accounts.public()))
                self.assertEqual(request.call_args.args[2]["grant_type"], "refresh_token")
            worker.store.db.close()

    def test_xoauth2_uses_rfc_control_separator_without_password_login(self):
        class Client:
            def authenticate(self, mechanism, callback):
                self.mechanism = mechanism
                self.payload = callback(b"")
        client = Client()
        oauth.imap_login(client, {"address": "reader@example.org", "auth_type": "microsoft", "_access_token": "fixture-token"})
        self.assertEqual(client.mechanism, "XOAUTH2")
        self.assertEqual(client.payload, b"user=reader@example.org\x01auth=Bearer fixture-token\x01\x01")


if __name__ == "__main__":
    unittest.main()
