"""Single-user password authentication and revocable signed browser sessions."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

SESSION_SECONDS = 30 * 24 * 3600


def write_private(path, text):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


class Auth:
    def __init__(self, data):
        self.path = Path(data) / "access.json"
        self.lock = threading.Lock()
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {"signing": secrets.token_hex(32), "generation": 0}
            write_private(self.path, json.dumps(self.state))
        self.failures = {}

    @property
    def configured(self):
        return bool(self.state.get("password"))

    def set_password(self, password, initial=False):
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ValueError("访问密码需为 10 到 256 个字符")
        with self.lock:
            if initial and self.configured:
                raise ValueError("已经设置访问密码，请直接登录")
            salt = secrets.token_hex(16)
            hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
            self.state.update(salt=salt, password=hashed, generation=self.state["generation"] + 1)
            write_private(self.path, json.dumps(self.state))

    def login(self, password, address):
        now = time.time()
        with self.lock:
            self.failures = {key: item for key, item in self.failures.items() if now - item[0] < 300}
            attempt = self.failures.get(address, (now, 0))
            if attempt[1] >= 8:
                raise ValueError("尝试次数较多，请 5 分钟后再试")
        if not self.configured:
            raise ValueError("请先设置访问密码")
        candidate = hashlib.pbkdf2_hmac("sha256", str(password or "").encode()[:1024], bytes.fromhex(self.state["salt"]), 600_000).hex()
        if not hmac.compare_digest(candidate, self.state["password"]):
            with self.lock:
                self.failures[address] = (attempt[0], attempt[1] + 1)
            raise ValueError("访问密码不正确")
        with self.lock:
            self.failures.pop(address, None)
        return self.issue()

    def sign(self, value):
        return hmac.new(bytes.fromhex(self.state["signing"]), value.encode(), hashlib.sha256).hexdigest()

    def issue(self):
        payload = "%d:%d:%s" % (int(time.time()) + SESSION_SECONDS, self.state["generation"], secrets.token_hex(16))
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + self.sign(payload)

    def valid(self, token):
        if not self.configured or not token or len(token) > 300:
            return False
        try:
            value, signature = token.split(".")
            payload = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
            expiry, generation, _ = payload.split(":")
            return (int(expiry) > time.time() and int(generation) == self.state["generation"]
                    and hmac.compare_digest(self.sign(payload), signature))
        except (ValueError, UnicodeError):
            return False

    def csrf(self, token):
        return self.sign("csrf:" + token)

    def revoke_all(self):
        with self.lock:
            self.state["generation"] += 1
            write_private(self.path, json.dumps(self.state))
