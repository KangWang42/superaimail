"""Sending mail, only when the owner asks: a reply, a new message, or a receipt / reminder for a collection task.

`preview` renders every message and returns a short-lived token; nothing leaves the server until `send` is called
with that token. Recipients are always shown in the preview. Nothing in the worker sends mail on its own.
"""
import email.message
import email.utils
import re
import secrets
import threading
import time

from . import net
from . import oauth

PREVIEW_SECONDS, BATCH_LIMIT = 900, 60
_ADDRESS = re.compile(r"^[^@\s<>,;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


class SendError(ValueError):
    pass


def addresses(value):
    items = value if isinstance(value, list) else re.split(r"[,，;；\s]+", str(value or ""))
    found = []
    for item in items:
        item = str(item).strip().lower()
        if not item:
            continue
        if not _ADDRESS.fullmatch(item):
            raise SendError("收件地址格式不对：" + item[:60])
        if item not in found:
            found.append(item)
    return found


def render(template, values):
    return re.sub(r"\{(姓名|主题|文件|任务)\}", lambda m: values.get(m.group(1), ""), template or "")


class Outbox:
    def __init__(self, worker):
        self.worker = worker
        self.lock = threading.Lock()
        self.previews = {}

    def preview(self, account, messages):
        """messages: [{to: [..], cc: [..], subject, body, in_reply_to, references}]. Returns the token and what will be sent."""
        if not account.get("smtp_host"):
            raise SendError("这个邮箱还没有填写发信服务器")
        if not messages:
            raise SendError("没有要发送的邮件")
        if len(messages) > BATCH_LIMIT:
            raise SendError("一次最多发送 %d 封" % BATCH_LIMIT)
        clean = []
        for item in messages:
            to = addresses(item.get("to"))
            if not to:
                raise SendError("请填写收件人")
            subject = str(item.get("subject") or "").strip()[:200]
            body = str(item.get("body") or "").strip()
            if not subject or not body:
                raise SendError("主题和正文都不能为空")
            clean.append({"to": to, "cc": addresses(item.get("cc") or []), "subject": subject, "body": body[:20000],
                          "in_reply_to": str(item.get("in_reply_to") or "")[:300], "references": str(item.get("references") or "")[:900]})
        token = secrets.token_urlsafe(16)
        with self.lock:
            now = time.time()
            self.previews = {k: v for k, v in self.previews.items() if v["expires"] > now}
            self.previews[token] = {"expires": now + PREVIEW_SECONDS, "account_id": account["id"], "messages": clean}
        return {"token": token, "from": account["address"], "messages": clean}

    def send(self, token):
        with self.lock:
            prepared = self.previews.pop(str(token or ""), None)
        if not prepared or prepared["expires"] < time.time():
            raise SendError("预览已过期，请重新预览后再发送")
        account = self.worker.accounts.account(prepared["account_id"])
        if not account:
            raise SendError("发信邮箱已不存在")
        account = self.worker.oauth.prepare(account)
        proxy = self.worker.accounts.proxy(account.get("proxy_id"))
        kind = net.SMTPStartTLS if account.get("smtp_security") == "starttls" else net.SMTPS
        sent, failed, copy_errors = [], [], []
        try:
            client = kind(account["smtp_host"], account["smtp_port"], proxy=proxy)
        except OSError as error:
            raise SendError("连不上发信服务器：" + (str(error) if isinstance(error, net.NetError) else type(error).__name__)) from None
        try:
            try:
                oauth.smtp_login(client, account)
            except Exception:
                raise SendError("发信服务器登录失败：请检查授权码，或确认邮箱已开启 SMTP") from None
            for item in prepared["messages"]:
                mail = email.message.EmailMessage()
                name = account.get("sender_name") or ""
                mail["From"] = email.utils.formataddr((name, account["address"])) if name else account["address"]
                mail["To"] = ", ".join(item["to"])
                if item["cc"]:
                    mail["Cc"] = ", ".join(item["cc"])
                mail["Subject"] = item["subject"]
                mail["Date"] = email.utils.formatdate(localtime=True)
                mail["Message-ID"] = email.utils.make_msgid(domain=account["address"].split("@")[1])
                if item["in_reply_to"]:
                    mail["In-Reply-To"] = item["in_reply_to"]
                    mail["References"] = (item["references"] + " " + item["in_reply_to"]).strip()
                mail.set_content(item["body"])
                try:
                    client.send_message(mail)
                    sent.append(", ".join(item["to"]))
                    try:
                        self.worker.record_sent(account, item, str(mail["Message-ID"]))
                    except Exception:
                        copy_errors.append(", ".join(item["to"]))
                except Exception as error:
                    failed.append({"to": ", ".join(item["to"]), "error": type(error).__name__})
        finally:
            try:
                client.quit()
            except Exception:
                pass
        self.worker.log("sent %d message(s) from %s, %d failed" % (len(sent), account["id"], len(failed)))
        return {"sent": sent, "failed": failed, "copy_errors": copy_errors}
