"""Mailboxes, outgoing proxies, purpose rules and AI settings, kept in one 0600 JSON file.

Secrets (authorization codes, proxy passwords, the AI key) are stored here or in files beside it and are never
returned by `public()`. Server names for common providers come from PRESETS, keyed by the address's domain, and
can always be overridden per mailbox.
"""
import json
import os
import re
import secrets
import threading
import time

LIMIT_ACCOUNTS, LIMIT_PROXIES, LIMIT_RULES, LIMIT_GROUPS = 30, 10, 100, 30
_ID = re.compile(r"^[a-z0-9]{10,16}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ADDRESS = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")

# domain -> (IMAP host, IMAP port, SMTP host, SMTP port, SMTP security, note)
PRESETS = {
    "qq.com": ("imap.qq.com", 993, "smtp.qq.com", 465, "ssl", "在 QQ 邮箱设置里开启 IMAP/SMTP 并生成授权码"),
    "foxmail.com": ("imap.qq.com", 993, "smtp.qq.com", 465, "ssl", "在 QQ 邮箱设置里开启 IMAP/SMTP 并生成授权码"),
    "163.com": ("imap.163.com", 993, "smtp.163.com", 465, "ssl", "在网页邮箱设置里开启 IMAP/SMTP 并生成授权码"),
    "126.com": ("imap.126.com", 993, "smtp.126.com", 465, "ssl", "在网页邮箱设置里开启 IMAP/SMTP 并生成授权码"),
    "yeah.net": ("imap.yeah.net", 993, "smtp.yeah.net", 465, "ssl", "在网页邮箱设置里开启 IMAP/SMTP 并生成授权码"),
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465, "ssl", "需要两步验证后生成的应用专用密码；中国大陆需选择出口代理"),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465, "ssl", "需要应用专用密码；中国大陆需选择出口代理"),
    "outlook.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587, "starttls", "微软账号可能已停用密码登录 IMAP"),
    "hotmail.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587, "starttls", "微软账号可能已停用密码登录 IMAP"),
    "live.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587, "starttls", "使用 Microsoft OAuth 登录"),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587, "starttls", "使用 Apple 账号的 App 专用密码"),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587, "starttls", "使用 Apple 账号的 App 专用密码"),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465, "ssl", "使用 Yahoo 生成的应用密码"),
    "aol.com": ("imap.aol.com", 993, "smtp.aol.com", 465, "ssl", "使用 AOL 应用密码"),
    "fastmail.com": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465, "ssl", "使用 Fastmail 应用密码，需套餐支持 IMAP"),
    "sina.com": ("imap.sina.com", 993, "smtp.sina.com", 465, "ssl", "先在网页邮箱开启 IMAP/SMTP"),
    "sohu.com": ("imap.sohu.com", 993, "smtp.sohu.com", 465, "ssl", "先在网页邮箱开启 IMAP/SMTP"),
}

NOTIFY = ("notify", "silent")
# mode: "each" sorts every new mail within a couple of minutes; "daily" and "every3" do it at `night` once a day or every three days.
AI_MODES = ("each", "daily", "every3")
AI_DEFAULTS = {"enabled": False, "mode": "each", "url": "", "model": "", "proxy_id": "", "daily_limit": 300, "night": "02:30"}
# Groups the nightly sorting chooses from; the owner can rename, add and remove them.
DEFAULT_GROUPS = [
    ("工作与项目", "项目沟通、工作安排、进度反馈、材料收集与评审"),
    ("学习与课程", "课程、培训、学校与学习资料的通知"),
    ("会议与活动", "会议安排、日程确认、活动报名与邀请"),
    ("账单与结算", "账单、发票、结算单、付款、续费、订单"),
    ("账号与安全", "验证码、登录提醒、密码、账户安全"),
    ("服务与通知", "云服务、域名、软件服务、物流与一般服务通知"),
    ("订阅与推广", "新闻简报、营销、广告、社区推送"),
    ("其他", "不属于以上任何一组"),
]


class AccountError(ValueError):
    pass


def preset(address):
    domain = address.rsplit("@", 1)[-1].lower()
    found = PRESETS.get(domain)
    if found:
        return {"imap_host": found[0], "imap_port": found[1], "smtp_host": found[2], "smtp_port": found[3], "smtp_security": found[4], "note": found[5]}
    return {"imap_host": "", "imap_port": 993, "smtp_host": "", "smtp_port": 465, "smtp_security": "ssl", "note": "请按服务商提供的说明填写 IMAP 和 SMTP 服务器"}


def _text(value, limit):
    return "".join(c for c in str(value or "") if ord(c) >= 32)[:limit].strip()


def _port(value, default):
    try:
        port = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        raise AccountError("端口需为数字") from None
    if not 1 <= port <= 65535:
        raise AccountError("端口超出范围")
    return port


def _secret(value, existing, required):
    if value is None or value == "":
        if existing:
            return existing
        if required:
            raise AccountError("请填写授权码或密码")
        return ""
    if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise AccountError("授权码或密码格式不对")
    return value


def _flag(value, default):
    if value is None:
        return default
    if type(value) is not bool:
        raise AccountError("开关取值无效")
    return value


class Accounts:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    # ------------------------------------------------------------ storage

    def load(self):
        with self.lock:
            try:
                with open(self.path, encoding="utf-8") as stream:
                    value = json.load(stream)
            except FileNotFoundError:
                value = {}
            for key in ("accounts", "proxies", "rules"):
                value.setdefault(key, [])
            value["ai"] = dict(AI_DEFAULTS, **value.get("ai", {}))
            if "groups" not in value:
                value["groups"] = [{"id": "g%02d" % i, "name": name, "description": text} for i, (name, text) in enumerate(DEFAULT_GROUPS)]
            value.setdefault("version", 1)
            return value

    def save(self, state):
        with self.lock:
            tmp = self.path + ".tmp"
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    def account(self, account_id):
        return next((dict(a) for a in self.load()["accounts"] if a["id"] == account_id), None)

    def accounts(self):
        return [dict(a) for a in self.load()["accounts"]]

    def proxy(self, proxy_id):
        if not proxy_id:
            return None
        return next((dict(p) for p in self.load()["proxies"] if p["id"] == proxy_id), None)

    def rules(self):
        return [dict(r) for r in self.load()["rules"]]

    def ai(self):
        return dict(self.load()["ai"])

    def groups(self):
        return [dict(g) for g in self.load()["groups"]]

    # ------------------------------------------------------------ public view

    def public(self):
        state = self.load()
        return {
            "accounts": [{k: a.get(k) for k in ("id", "address", "label", "purpose", "sender_name", "imap_host", "imap_port", "smtp_host", "smtp_port",
                                                 "smtp_security", "proxy_id", "enabled", "notify", "created", "auth_type")}
                         | {"has_smtp": bool(a.get("smtp_host"))} for a in state["accounts"]],
            "proxies": [{"id": p["id"], "name": p["name"], "host": p["host"], "port": p["port"], "username": p.get("username", ""),
                         "has_password": bool(p.get("password"))} for p in state["proxies"]],
            "rules": state["rules"],
            "groups": state["groups"],
            "ai": {k: state["ai"].get(k) for k in ("enabled", "mode", "url", "model", "proxy_id", "daily_limit", "night")},
            "presets": {domain: preset("x@" + domain) for domain in PRESETS},
            "limits": {"accounts": LIMIT_ACCOUNTS, "proxies": LIMIT_PROXIES, "rules": LIMIT_RULES},
        }

    # ------------------------------------------------------------ editing

    def build_account(self, payload, existing=None):
        """Validate an account edit. Returns the full record (with secret) but does not store it."""
        address = _text(payload.get("address", existing and existing["address"]), 260).lower()
        if not _ADDRESS.fullmatch(address):
            raise AccountError("邮箱地址格式不对")
        defaults = preset(address)
        same_address = existing and existing["address"] == address
        imap_host = _text(payload.get("imap_host"), 255).lower() or (existing["imap_host"] if same_address else defaults["imap_host"])
        if not imap_host:
            raise AccountError("请按邮箱服务商说明填写 IMAP 服务器")
        smtp_host = _text(payload.get("smtp_host"), 255).lower() if "smtp_host" in payload else (existing.get("smtp_host", "") if same_address else defaults["smtp_host"])
        for host in (imap_host, smtp_host):
            if host and not _HOST.fullmatch(host):
                raise AccountError("服务器地址格式不对")
        security = payload.get("smtp_security") or (existing.get("smtp_security") if same_address else defaults["smtp_security"]) or "ssl"
        if security not in ("ssl", "starttls"):
            raise AccountError("发信加密方式无效")
        proxy_id = str(payload.get("proxy_id", existing.get("proxy_id", "") if existing else "") or "")
        if proxy_id and not self.proxy(proxy_id):
            raise AccountError("所选出口代理不存在")
        notify = payload.get("notify", existing.get("notify", "notify") if existing else "notify")
        if notify not in NOTIFY:
            raise AccountError("通知方式无效")
        record = dict(existing or {})
        auth_type = payload.get("auth_type", record.get("auth_type", "password"))
        if auth_type not in ("password", "microsoft"):
            raise AccountError("登录方式无效")
        oauth = payload.get("oauth") or (record.get("oauth") if same_address else None)
        if auth_type == "microsoft" and not oauth:
            raise AccountError("请先完成微软登录")
        record.update(
            address=address, label=_text(payload.get("label", record.get("label", "")), 30), purpose=_text(payload.get("purpose", record.get("purpose", "")), 60),
            sender_name=_text(payload.get("sender_name", record.get("sender_name", "")), 40),
            imap_host=imap_host, imap_port=_port(payload.get("imap_port"), existing["imap_port"] if same_address else defaults["imap_port"]),
            smtp_host=smtp_host, smtp_port=_port(payload.get("smtp_port"), existing.get("smtp_port", 465) if same_address else defaults["smtp_port"]),
            smtp_security=security, proxy_id=proxy_id, notify=notify,
            secret=_secret(payload.get("secret"), existing and existing.get("secret"), auth_type == "password"),
            auth_type=auth_type,
            enabled=_flag(payload.get("enabled"), record.get("enabled", True)),
        )
        if auth_type == "microsoft":
            record["oauth"] = oauth
            record["secret"] = ""
        else:
            record.pop("oauth", None)
        record.pop("ai_auto", None)
        record.setdefault("created", int(time.time()))
        return record

    def save_account(self, payload):
        with self.lock:
            state = self.load()
            identifier = str(payload.get("id") or "")
            existing = next((a for a in state["accounts"] if a["id"] == identifier), None)
            if identifier and existing is None:
                raise AccountError("这个邮箱已不存在，请刷新后重试")
            record = self.build_account(payload, existing)
            if any(a["address"] == record["address"] and a is not existing for a in state["accounts"]):
                raise AccountError("已经添加过这个邮箱")
            if existing is None:
                if len(state["accounts"]) >= LIMIT_ACCOUNTS:
                    raise AccountError("邮箱数量已达上限")
                record["id"] = secrets.token_hex(6)
                state["accounts"].append(record)
            else:
                state["accounts"][state["accounts"].index(existing)] = record
            self.save(state)
            return record, existing

    def delete_account(self, account_id):
        with self.lock:
            state = self.load()
            before = len(state["accounts"])
            state["accounts"] = [a for a in state["accounts"] if a["id"] != account_id]
            if len(state["accounts"]) == before:
                raise AccountError("这个邮箱已不存在，请刷新后重试")
            self.save(state)

    def save_proxy(self, payload):
        with self.lock:
            state = self.load()
            identifier = str(payload.get("id") or "")
            existing = next((p for p in state["proxies"] if p["id"] == identifier), None)
            if identifier and existing is None:
                raise AccountError("这个代理已不存在，请刷新后重试")
            host = _text(payload.get("host"), 255)
            if not _HOST.fullmatch(host):
                raise AccountError("代理地址格式不对")
            record = dict(existing or {}, name=_text(payload.get("name"), 30) or host, host=host, port=_port(payload.get("port"), 1080),
                          username=_text(payload.get("username"), 100))
            record["password"] = _secret(payload.get("password"), existing and existing.get("password"), False)
            if existing is None:
                if len(state["proxies"]) >= LIMIT_PROXIES:
                    raise AccountError("代理数量已达上限")
                record["id"] = secrets.token_hex(6)
                state["proxies"].append(record)
            else:
                state["proxies"][state["proxies"].index(existing)] = record
            self.save(state)
            return record

    def delete_proxy(self, proxy_id):
        with self.lock:
            state = self.load()
            if any(a.get("proxy_id") == proxy_id for a in state["accounts"]) or state["ai"].get("proxy_id") == proxy_id:
                raise AccountError("还有邮箱或 AI 设置在使用这个代理")
            state["proxies"] = [p for p in state["proxies"] if p["id"] != proxy_id]
            self.save(state)

    def save_rule(self, payload):
        with self.lock:
            state = self.load()
            identifier = str(payload.get("id") or "")
            existing = next((r for r in state["rules"] if r["id"] == identifier), None)
            if identifier and existing is None:
                raise AccountError("这条规则已不存在，请刷新后重试")
            label = _text(payload.get("label"), 20)
            if not label:
                raise AccountError("请填写标签名称")
            attach = payload.get("has_attach")
            if attach not in (None, True, False):
                raise AccountError("附件条件无效")
            notify = payload.get("notify") or "notify"
            if notify not in NOTIFY:
                raise AccountError("通知方式无效")
            account_id = str(payload.get("account_id") or "")
            if account_id and not any(a["id"] == account_id for a in state["accounts"]):
                raise AccountError("所选邮箱不存在")
            record = {"id": existing["id"] if existing else secrets.token_hex(6), "label": label, "account_id": account_id,
                      "from": _text(payload.get("from"), 200), "subject": _text(payload.get("subject"), 200), "has_attach": attach,
                      "notify": notify, "enabled": _flag(payload.get("enabled"), True)}
            if not (record["from"] or record["subject"] or record["account_id"] or record["has_attach"] is not None):
                raise AccountError("至少填写一个条件")
            if existing is None:
                if len(state["rules"]) >= LIMIT_RULES:
                    raise AccountError("规则数量已达上限")
                state["rules"].append(record)
            else:
                state["rules"][state["rules"].index(existing)] = record
            self.save(state)
            return record

    def delete_rule(self, rule_id):
        with self.lock:
            state = self.load()
            state["rules"] = [r for r in state["rules"] if r["id"] != rule_id]
            self.save(state)

    def save_ai(self, payload):
        with self.lock:
            state = self.load()
            url = _text(payload.get("url"), 300).rstrip("/") or state["ai"]["url"]
            enabled = _flag(payload.get("enabled"), state["ai"].get("enabled", False))
            from .net import valid_ai_url
            if (enabled or url) and not valid_ai_url(url):
                raise AccountError("AI 地址需为 HTTPS；本机模型可使用 http://localhost 或 http://127.0.0.1")
            if enabled and not _text(payload.get("model"), 80) and not state["ai"]["model"]:
                raise AccountError("请填写模型名称")
            proxy_id = str(payload.get("proxy_id", state["ai"].get("proxy_id", "")) or "")
            if proxy_id and not self.proxy(proxy_id):
                raise AccountError("所选出口代理不存在")
            try:
                limit = int(payload.get("daily_limit", state["ai"].get("daily_limit", 300)))
            except (TypeError, ValueError):
                raise AccountError("每日上限需为数字") from None
            if not 0 <= limit <= 5000:
                raise AccountError("每日上限应在 0 到 5000 之间")
            night = _text(payload.get("night", state["ai"].get("night", "02:30")), 5)
            if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", night):
                raise AccountError("夜间整理时间格式应为 HH:MM")
            mode = payload.get("mode", state["ai"].get("mode", "each"))
            if mode not in AI_MODES:
                raise AccountError("AI 处理方式无效")
            state["ai"] = {"enabled": _flag(payload.get("enabled"), state["ai"].get("enabled", False)), "mode": mode, "url": url,
                           "model": _text(payload.get("model"), 80) or state["ai"]["model"], "proxy_id": proxy_id, "daily_limit": limit, "night": night}
            self.save(state)

    def save_group(self, payload):
        with self.lock:
            state = self.load()
            identifier = str(payload.get("id") or "")
            existing = next((g for g in state["groups"] if g["id"] == identifier), None)
            if identifier and existing is None:
                raise AccountError("这个分组已不存在，请刷新后重试")
            name = _text(payload.get("name"), 16)
            if not name:
                raise AccountError("请填写分组名称")
            if any(g["name"] == name and g is not existing for g in state["groups"]):
                raise AccountError("已有同名分组")
            attention = str(payload.get("attention", (existing or {}).get("attention", "")) or "")
            if attention not in ("", "act", "read", "fyi", "low"):
                raise AccountError("分组默认关注度无效")
            record = {"id": existing["id"] if existing else "g" + secrets.token_hex(4), "name": name, "description": _text(payload.get("description"), 120)}
            if attention:                                  # used when the model has not judged a mail of this group
                record["attention"] = attention
            if existing is None:
                if len(state["groups"]) >= LIMIT_GROUPS:
                    raise AccountError("分组数量已达上限")
                state["groups"].append(record)
            else:
                state["groups"][state["groups"].index(existing)] = record
            self.save(state)
            return record, existing

    def delete_group(self, group_id):
        with self.lock:
            state = self.load()
            found = next((g for g in state["groups"] if g["id"] == group_id), None)
            if not found:
                raise AccountError("这个分组已不存在，请刷新后重试")
            state["groups"].remove(found)
            self.save(state)
            return found


def match_rules(rules, account_id, from_text, subject, has_attach):
    """Labels from every matching rule, and whether any of them silences the notice."""
    labels, silent = [], False
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        if rule.get("account_id") and rule["account_id"] != account_id:
            continue
        if rule.get("has_attach") is not None and rule["has_attach"] != has_attach:
            continue
        if rule.get("from") and not any(w and w.lower() in from_text.lower() for w in re.split(r"[,，;；\s]+", rule["from"])):
            continue
        if rule.get("subject") and not any(w and w.lower() in subject.lower() for w in re.split(r"[,，;；\s]+", rule["subject"])):
            continue
        if rule["label"] not in labels:
            labels.append(rule["label"])
        silent = silent or rule.get("notify") == "silent"
    return labels, silent
