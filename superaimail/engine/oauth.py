"""Microsoft device authorization and XOAUTH2 tokens, using public-client registration."""
import json
import re
import secrets
import threading
import time
import urllib.parse

from . import net

SCOPES = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send"


class OAuthError(ValueError):
    pass


class Microsoft:
    def __init__(self, accounts):
        self.accounts = accounts
        self.lock = threading.RLock()
        self.flows = {}

    def _post(self, tenant, endpoint, fields):
        status, raw = net.https_request(
            "https://login.microsoftonline.com/%s/oauth2/v2.0/%s" % (tenant, endpoint),
            urllib.parse.urlencode(fields).encode(), {"Content-Type": "application/x-www-form-urlencoded"}, timeout=25)
        try:
            data = json.loads(raw)
        except ValueError:
            raise OAuthError("微软登录服务返回了无法识别的响应") from None
        return status, data

    def begin(self, client_id, tenant="common"):
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", str(client_id or "")):
            raise OAuthError("请填写你在 Microsoft Entra 注册的应用 Client ID")
        if not re.fullmatch(r"common|consumers|organizations|[a-fA-F0-9-]{36}", str(tenant)):
            raise OAuthError("Tenant 应为 common、consumers、organizations 或租户 ID")
        status, data = self._post(tenant, "devicecode", {"client_id": client_id, "scope": SCOPES})
        if status != 200:
            raise OAuthError("无法开始微软登录，请核对 Client ID、账户类型和公共客户端开关")
        ticket = secrets.token_urlsafe(24)
        with self.lock:
            self.flows = {k: v for k, v in self.flows.items() if v["expires"] > time.time()}
            self.flows[ticket] = {"client_id": client_id, "tenant": tenant, "device_code": data["device_code"],
                                  "expires": time.time() + min(int(data["expires_in"]), 1800),
                                  "interval": max(5, int(data.get("interval", 5))), "next": 0}
        return {"ticket": ticket, "user_code": data["user_code"], "url": "https://microsoft.com/devicelogin",
                "interval": self.flows[ticket]["interval"], "expires_in": int(data["expires_in"])}

    def poll(self, ticket):
        with self.lock:
            flow = self.flows.get(str(ticket))
            if not flow or flow["expires"] <= time.time():
                raise OAuthError("登录已过期，请重新开始")
            if flow.get("tokens"):
                return {"ready": True}
            if flow["next"] > time.time():
                return {"ready": False, "interval": flow["interval"]}
            flow["next"] = time.time() + flow["interval"]
            status, data = self._post(flow["tenant"], "token", {"client_id": flow["client_id"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": flow["device_code"]})
            if status == 200:
                if not data.get("refresh_token") or not data.get("access_token"):
                    raise OAuthError("微软未返回离线登录凭据，请检查 offline_access 权限")
                flow["tokens"] = {"client_id": flow["client_id"], "tenant": flow["tenant"],
                                  "refresh_token": data["refresh_token"], "access_token": data["access_token"],
                                  "expires": time.time() + int(data["expires_in"])}
                return {"ready": True}
            code = data.get("error")
            if code in ("authorization_pending", "slow_down"):
                if code == "slow_down":
                    flow["interval"] += 5
                return {"ready": False, "interval": flow["interval"]}
            raise OAuthError("微软登录未完成或被拒绝，请重新授权")

    def credentials(self, ticket):
        with self.lock:
            flow = self.flows.get(str(ticket))
            if not flow or flow["expires"] <= time.time() or not flow.get("tokens"):
                raise OAuthError("请先完成微软登录")
            return dict(flow["tokens"])

    def prepare(self, account):
        if account.get("auth_type") != "microsoft":
            return account
        with self.lock:
            current = self.accounts.account(account.get("id")) or account
            tokens = dict(current.get("oauth") or {})
            if tokens.get("expires", 0) <= time.time() + 120:
                status, data = self._post(tokens.get("tenant", "common"), "token", {
                    "client_id": tokens.get("client_id", ""), "grant_type": "refresh_token",
                    "refresh_token": tokens.get("refresh_token", ""), "scope": SCOPES})
                if status != 200 or not data.get("access_token"):
                    raise OAuthError("微软授权已失效，请在邮箱设置中重新登录")
                tokens.update(access_token=data["access_token"], expires=time.time() + int(data["expires_in"]))
                if data.get("refresh_token"):
                    tokens["refresh_token"] = data["refresh_token"]
                with self.accounts.lock:
                    state = self.accounts.load()
                    for item in state["accounts"]:
                        if item["id"] == account.get("id"):
                            item["oauth"] = tokens
                    self.accounts.save(state)
            return dict(account, _access_token=tokens["access_token"])


def imap_login(client, account):
    if account.get("auth_type") == "microsoft":
        payload = ("user=%s\x01auth=Bearer %s\x01\x01" % (account["address"], account["_access_token"])).encode()
        client.authenticate("XOAUTH2", lambda challenge: payload if not challenge else b"")
    else:
        client.login(account["address"], account["secret"])


def smtp_login(client, account):
    if account.get("auth_type") == "microsoft":
        payload = "user=%s\x01auth=Bearer %s\x01\x01" % (account["address"], account["_access_token"])
        client.ehlo_or_helo_if_needed()
        client.auth("XOAUTH2", lambda challenge=None: payload if challenge is None else "")
    else:
        client.login(account["address"], account["secret"])
