"""HTTP application for desktop, embedded Android and shared self-hosting."""
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path
import threading
import urllib.parse

from . import __version__
from .api import Handler as MailHandler
from .auth import Auth, SESSION_SECONDS
from .engine.core import Worker

WEB = Path(__file__).parent / "web"
MAX_BODY = 2 * 1024 * 1024


class Application:
    def __init__(self, data, host="127.0.0.1", port=8787, public_url="", password=None):
        self.data = Path(data).expanduser().resolve()
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.auth = Auth(self.data)
        if password and not self.auth.configured:
            self.auth.set_password(password)
        if host not in ("127.0.0.1", "::1", "localhost") and not self.auth.configured:
            raise ValueError("对外监听前请先运行 superaimail init 设置访问密码")
        parsed = urllib.parse.urlsplit(public_url)
        if public_url and (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
                           or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("公共访问地址应为完整的 http:// 或 https:// 来源地址，不含路径和账户信息")
        self.public_url = public_url.rstrip("/")
        self.secure = self.public_url.startswith("https://")
        self.worker = Worker(str(self.data))
        handler = type("AppHandler", (Handler,), {"app": self, "worker": self.worker})
        self.server = ThreadingHTTPServer((host, port), handler)
        self.server.daemon_threads = True
        self.url = self.public_url or "http://%s:%d" % ("127.0.0.1" if host in ("0.0.0.0", "localhost") else host, self.server.server_port)
        self.allowed_hosts = {urllib.parse.urlsplit(self.url).netloc}
        if not self.public_url:
            self.allowed_hosts.add("localhost:%d" % self.server.server_port)
        self.thread = None

    def start(self):
        self.worker.start()
        self.thread = threading.Thread(target=self.server.serve_forever, name="superaimail-http", daemon=True)
        self.thread.start()
        return self.url

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
        self.worker.stop()


class Handler(MailHandler):
    server_version = "SuperAIMail"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            # Browsers close idle connections and cancel requests when navigating away.
            self.close_connection = True

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        super().end_headers()

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["supermail_session"].value if "supermail_session" in cookie else ""
        except Exception:
            return ""

    def _authorized(self):
        return self.app.auth.valid(self._token())

    def _host_ok(self):
        if self.headers.get("Host", "") not in self.app.allowed_hosts:
            self.close_connection = True
            self._json(403, {"error": "访问地址不匹配，请使用配置的公共地址"})
            return False
        return True

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if self.app.public_url:
            return origin == self.app.url
        parsed = urllib.parse.urlsplit(origin)
        return parsed.scheme == "http" and parsed.netloc in self.app.allowed_hosts and not parsed.path and not parsed.query and not parsed.fragment

    def _json(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
        cache = self.command == "GET" and self.path.startswith(("/api/", "/v1/")) and code == 200
        if cache and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-cache" if cache else "no-store")
        self.send_header("Vary", "Cookie")
        if cache:
            self.send_header("ETag", etag)
        if getattr(self, "session_cookie", None) is not None:
            self.send_header("Set-Cookie", self.session_cookie)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, token=""):
        self.session_cookie = "supermail_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d%s" % (
            token, SESSION_SECONDS if token else 0, "; Secure" if self.app.secure else "")

    def do_GET(self):
        if not self._host_ok():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/session":
            valid = self._authorized()
            return self._json(200, {"authenticated": valid, "setup": not self.app.auth.configured,
                                    "csrf": self.app.auth.csrf(self._token()) if valid else "", "version": __version__})
        if path.startswith("/api/"):
            # Preserve the API implementation while exposing one public origin.
            original = self.path
            self.path = "/v1/" + self.path[len("/api/"):]
            try:
                return super().do_GET()
            finally:
                self.path = original
        if path.startswith("/v1/"):
            return self._json(404, {"error": "接口不存在"})
        name = "index.html" if path == "/" else path.lstrip("/")
        allowed = {p.name for p in WEB.iterdir() if p.is_file()}
        if name not in allowed:
            return self._json(404, {"error": "页面不存在"})
        body = (WEB / name).read_bytes()
        self.send_response(200)
        kind = {".svg": "image/svg+xml", ".webmanifest": "application/manifest+json", ".js": "text/javascript",
                ".css": "text/css", ".html": "text/html"}.get(Path(name).suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            kind = "text/javascript"
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.close_connection = True
        if not self._host_ok():
            return
        if not self._origin_ok():
            return self._json(403, {"error": "请求来源不匹配"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get_content_type() != "application/json":
                return self._json(400, {"error": "请发送有效的 JSON 请求"})
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/api/login", "/api/setup"):
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请求格式无效")
                if path == "/api/setup":
                    if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                        return self._json(403, {"error": "首次设置请在本机完成"})
                    self.app.auth.set_password(payload.get("password"), initial=True)
                token = self.app.auth.login(payload.get("password"), self.client_address[0])
                self._cookie(token)
                return self._json(200, {"authenticated": True, "csrf": self.app.auth.csrf(token)})
            if not self._authorized():
                return self._json(401, {"error": "请先登录"})
            if not hmac.compare_digest(self.headers.get("X-Supermail-CSRF", ""), self.app.auth.csrf(self._token())):
                return self._json(403, {"error": "会话校验失败，请刷新页面"})
            if path == "/api/logout":
                self.rfile.read(length)
                self.app.auth.revoke_all()
                self._cookie()
                return self._json(200, {"ok": True})
            if path == "/api/action":
                self.path = "/v1/action"
                return super().do_POST()
            return self._json(404, {"error": "接口不存在"})
        except (ValueError, UnicodeError):
            return self._json(400, {"error": "请求格式或密码不正确，请检查后重试"})
