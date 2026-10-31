"""Authenticated mail API and explicit send previews."""
import hmac
import json
import os
import ssl
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



from .engine import accounts as accounts_module
from .engine import ai as ai_module
from .engine import collect
from .engine import core
from .engine import smtp_send
from .engine.oauth import OAuthError

BODY_LIMIT = 2 * 1024 * 1024
KNOWN_ERRORS = (core.WorkerError, accounts_module.AccountError, collect.TaskError, ai_module.AIError, smtp_send.SendError, OAuthError)


def content_disposition(name):
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "download"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(name, safe=""))


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value):
    return None if value in (None, "") else value in ("1", "true", "yes")


class Handler(BaseHTTPRequestHandler):
    worker = None
    token = ""
    server_version = "mail-worker"
    sys_version = ""
    # The panel keeps its connections open between requests, which spares a TLS handshake on each; an idle one is
    # closed after a minute. Every reply carries a length or closes the connection.
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):
        pass                                                     # requests carry message ids and names; keep them out of the journal

    # ------------------------------------------------------------ plumbing

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        return supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:].encode(), self.token.encode())

    def _page(self, page):
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, error):
        if isinstance(error, KNOWN_ERRORS):
            return self._json(400, {"error": str(error)})
        self.worker.log("request failed: " + "".join(traceback.format_exception_only(type(error), error)).strip()[:300])
        return self._json(500, {"error": "邮件后台处理失败，请稍后重试"})

    def _stream(self, ctype, filename, produce, length=None):
        """Send headers on the first byte, so an error found before any data still becomes a JSON reply."""
        started = {"value": False}

        def write(data):
            if not started["value"]:
                started["value"] = True
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", content_disposition(filename))
                self.send_header("Cache-Control", "no-store")
                if length is not None:
                    self.send_header("Content-Length", str(length))
                self.send_header("Connection", "close")
                self.end_headers()
            if data:
                self.wfile.write(data)
        try:
            produce(write)
            write(b"")
        except Exception as error:
            if started["value"]:
                self.close_connection = True
                self.worker.log("stream aborted: %s" % type(error).__name__)
                return
            self._fail(error)

    # ------------------------------------------------------------ requests

    def do_GET(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        worker = self.worker
        try:
            if url.path == "/v1/health":
                return self._json(200, worker.health())
            if url.path == "/v1/overview":
                return self._json(200, worker.overview())
            if url.path == "/v1/messages":
                items = worker.store.messages(account_id=query.get("account", ""), label=query.get("label", ""), query=query.get("q", ""),
                                              attach=_flag(query.get("attach")), folder=query.get("folder", ""), before=_int(query.get("before")),
                                              since=_int(query.get("since")), until=_int(query.get("until")), starred=_flag(query.get("starred")),
                                              unread=_flag(query.get("unread")), group=query.get("group", ""), task=query.get("task", ""),
                                              limit=_int(query.get("limit"), 50), view=query.get("view", "inbox"),
                                              sender=query.get("sender", ""), before_id=query.get("before_id", ""), level=query.get("level", ""))
                return self._json(200, {"messages": items, "more": len(items) >= _int(query.get("limit"), 50)})
            if url.path == "/v1/senders":
                return self._json(200, {"senders": worker.store.senders(query.get("group", ""), query.get("account", ""))})
            if url.path == "/v1/message":
                return self._json(200, worker.message_view(query.get("id", "")))
            if url.path == "/v1/bodies":
                return self._json(200, {"messages": worker.stored_views([i for i in query.get("ids", "").split(",") if i])})
            if url.path == "/v1/html":
                page = worker.reading_page(query.get("id", ""), query.get("mode", "original"), query.get("theme", "light"), query.get("images") == "1")
                return self._page(page)
            if url.path == "/v1/inline":
                kind, produce = worker.inline_image(query.get("id", ""), query.get("part", ""))
                return self._stream(kind, "", produce)
            if url.path == "/v1/attachment_preview":
                return self._json(200, worker.attachment_preview(query.get("id", ""), query.get("part", "")))
            if url.path == "/v1/attachment":
                message, item = worker.attachment(query.get("id", ""), query.get("part", ""))
                return self._stream(item.get("type") if query.get("inline") == "1" and item.get("type", "").startswith("image/") else "application/octet-stream",
                                    item["name"], lambda write: worker.stream_attachment(message, item, write))
            if url.path == "/v1/events":
                return self._json(200, {"events": worker.store.events(_int(query.get("after")), 200), "last": worker.store.last_event()})
            if url.path == "/v1/notice_text":
                return self._json(200, worker.notice_text(query.get("id", "")))
            if url.path == "/v1/task":
                return self._json(200, worker.task_view(query.get("id", "")))
            if url.path == "/v1/sync":
                return self._json(200, worker.sync_manifest())
            if url.path == "/v1/file":
                row = worker.file_path(query.get("id", ""))

                def produce(write):
                    with open(row["path"], "rb") as source:
                        for block in iter(lambda: source.read(256 * 1024), b""):
                            write(block)
                return self._stream("application/octet-stream", row["name"], produce, os.path.getsize(row["path"]))
            if url.path == "/v1/task_zip":
                task_id = query.get("id", "")
                task = worker.task(task_id) if task_id else None
                if task_id and not task:
                    raise core.WorkerError("这个任务已不存在")
                name = (task["config"]["name"] if task else "邮件归档") + ".zip"

                class Sink:
                    def __init__(self, write):
                        self.write_out, self.count = write, 0

                    def write(self, data):
                        self.write_out(bytes(data))
                        self.count += len(data)
                        return len(data)

                    def flush(self):
                        pass
                return self._stream("application/zip", name, lambda write: worker.write_zip(task_id, Sink(write), latest_only=query.get("latest") == "1"))
            return self._json(404, {"error": "not found"})
        except Exception as error:
            return self._fail(error)

    def do_POST(self):
        # A body left unread would be taken for the next request on this connection.
        if not self._authorized():
            self.close_connection = True
            return self._json(401, {"error": "unauthorized"})
        if urllib.parse.urlsplit(self.path).path != "/v1/action":
            self.close_connection = True
            return self._json(404, {"error": "not found"})
        length = _int(self.headers.get("Content-Length"), -1)
        if not 0 <= length <= BODY_LIMIT:
            self.close_connection = True
            return self._json(413, {"error": "请求过大"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise core.WorkerError("请求格式无效")
            return self._json(200, actions(self.worker, payload))
        except (ValueError, UnicodeDecodeError) as error:
            if isinstance(error, KNOWN_ERRORS):
                return self._fail(error)
            return self._json(400, {"error": "请求格式无效"})
        except Exception as error:
            return self._fail(error)


def actions(worker, payload):
    """Every change the panel can ask for."""
    action = payload.get("action")
    accounts = worker.accounts
    if action == "oauth_begin":
        return worker.oauth.begin(str(payload.get("client_id") or ""), str(payload.get("tenant") or "common"))
    if action == "oauth_poll":
        return worker.oauth.poll(payload.get("ticket"))
    if action in ("account_save", "account_test"):
        payload = dict(payload)
        payload.pop("oauth", None)
        if payload.get("oauth_ticket"):
            payload["oauth"] = worker.oauth.credentials(payload["oauth_ticket"])
            payload["auth_type"] = "microsoft"
    if action == "account_test":
        return worker.test_account(payload)
    if action == "account_save":
        record = worker.save_account(payload)
        return {"id": record["id"], "overview": worker.overview()}
    if action == "account_delete":
        worker.delete_account(str(payload.get("id") or ""))
        return {"overview": worker.overview()}
    if action == "account_backfill":
        return worker.backfill(str(payload.get("id") or ""), payload.get("days"))
    if action == "proxy_save":
        accounts.save_proxy(payload)
        return {"overview": worker.overview()}
    if action == "proxy_delete":
        accounts.delete_proxy(str(payload.get("id") or ""))
        return {"overview": worker.overview()}
    if action == "rule_save":
        accounts.save_rule(payload)
        return {"overview": worker.overview()}
    if action == "rule_delete":
        accounts.delete_rule(str(payload.get("id") or ""))
        return {"overview": worker.overview()}
    if action == "group_save":
        worker.save_group(payload)
        return {"overview": worker.overview()}
    if action == "group_delete":
        worker.delete_group(str(payload.get("id") or ""))
        return {"overview": worker.overview()}
    if action == "message_group":
        worker.set_message_group(str(payload.get("id") or ""), str(payload.get("group") or ""))
        return {"ok": True}
    if action == "ai_night_run":
        return worker.start_night()
    if action == "ai_settings_save":
        accounts.save_ai(payload)
        worker.ai.set_key(payload.get("key"))
        return {"overview": worker.overview()}
    if action == "archive_sender":
        return {"ok": True, "count": worker.store.archive_sender(str(payload.get("account_id") or ""), str(payload.get("sender") or ""), str(payload.get("group") or ""))}
    if action == "mail_preferences":
        return worker.save_mail_preferences(payload)
    if action == "sender_rule":
        return worker.sender_rule(payload)
    if action == "messages_mark":
        return worker.mark_messages(payload)
    if action == "message_mark":
        message_id = str(payload.get("id") or "")
        if not worker.store.message(message_id):
            raise core.WorkerError("这封邮件已不在索引中")
        changes = {k: 1 if payload[k] else 0 for k in ("read", "starred", "archived", "deleted") if k in payload}
        worker.store.update_message(message_id, **changes)
        return {"ok": True}
    if action in ("messages_read", "messages_archive"):
        # The same filters as GET /v1/messages, so "全部已读" covers exactly what the list shows, on every page.
        text = lambda key: str(payload.get(key) or "")[:200]
        flag = lambda key: None if payload.get(key) in (None, "") else payload.get(key) is True or str(payload.get(key)) in ("1", "true", "yes")
        filters = dict(account_id=text("account") or text("account_id"), label=text("label"), query=text("q"), attach=flag("attach"),
                       folder=text("folder"), since=_int(payload.get("since")), until=_int(payload.get("until")), starred=flag("starred"),
                       group=text("group"), task=text("task"), sender=text("sender"), level=text("level"))
        if action == "messages_archive":
            return {"ok": True, "count": worker.store.archive_matching(**filters)}
        return {"ok": True, "count": worker.store.read_matching(view=text("view") or "inbox", **filters)}
    if action == "attention_rule":
        return worker.attention_rule(payload)
    if action == "attention_rule_delete":
        return worker.remove_attention_rule(payload.get("key"))
    if action == "task_save":
        return worker.save_task(payload)
    if action == "task_delete":
        worker.delete_task(str(payload.get("id") or ""))
        return {"ok": True}
    if action == "task_pause":
        worker.set_task_enabled(str(payload.get("id") or ""), payload.get("enabled") is True)
        return worker.task_view(str(payload.get("id") or ""))
    if action == "task_end":
        return worker.end_task(str(payload.get("id") or ""), payload.get("ended", True) is not False)
    if action == "task_rescan":
        return worker.rescan(str(payload.get("id") or ""))
    if action == "task_retry_files":
        return worker.retry_files(str(payload.get("id") or ""))
    if action == "assign":
        return worker.assign(str(payload.get("submission_id") or ""), payload.get("person"), payload.get("remember", True) is not False)
    if action == "ignore":
        return worker.ignore(str(payload.get("submission_id") or ""), payload.get("ignored", True) is not False)
    if action == "ai_summarize":
        message = worker.message_view(str(payload.get("id") or ""), mark_read=False)
        result = worker.ai.summarize(message)
        worker.store.update_message(message["id"], ai_summary=result["summary"])
        return result
    if action == "ai_reply":
        message = worker.message_view(str(payload.get("id") or ""), mark_read=False)
        return {"draft": worker.ai.draft_reply(message, str(payload.get("instruction") or "")[:300])}
    if action == "ai_translate":
        message = worker.message_view(str(payload.get("id") or ""), mark_read=False)
        return {"text": worker.ai.translate(message, str(payload.get("target") or "中文")[:20])}
    if action == "ai_task_draft":
        return {"draft": worker.ai.task_from_notice(str(payload.get("text") or ""), accounts.accounts())}
    if action == "ai_search":
        labels = sorted(set(worker.store.label_counts()) | {r["label"] for r in accounts.rules()})
        return {"filters": worker.ai.search(str(payload.get("query") or ""), accounts.accounts(), labels)}
    if action == "send_preview":
        return send_preview(worker, payload)
    if action == "send_confirm":
        return worker.outbox.send(payload.get("token"))
    raise core.WorkerError("不支持此操作")


def send_preview(worker, payload):
    """kind: reply (id, body), compose (account_id, to, subject, body), receipt (task_id, submission_ids, subject, body),
    remind (task_id, names, subject, body). Templates may use {姓名} {主题} {文件} {任务}."""
    kind = payload.get("kind")
    if kind == "reply":
        message = worker.message_view(str(payload.get("id") or ""), mark_read=False)
        account = worker.accounts.account(message["account_id"])
        subject = message["subject"] if message["subject"].lower().startswith("re:") else "Re: " + message["subject"]
        return worker.outbox.preview(account, [{"to": payload.get("to") or [message["from_addr"]], "cc": payload.get("cc") or [],
                                                "subject": str(payload.get("subject") or subject), "body": payload.get("body"),
                                                "in_reply_to": message.get("message_id") or ""}])
    if kind == "replies":
        return worker.outbox.preview(*batch_replies(worker, payload))
    if kind == "compose":
        account = worker.accounts.account(str(payload.get("account_id") or ""))
        if not account:
            raise core.WorkerError("请选择发信邮箱")
        return worker.outbox.preview(account, [{"to": payload.get("to"), "cc": payload.get("cc") or [], "subject": payload.get("subject"), "body": payload.get("body")}])
    if kind in ("receipt", "remind"):
        view = worker.task_view(str(payload.get("task_id") or ""))
        account = worker.accounts.account(view["config"]["account_id"])
        template_subject, template_body = str(payload.get("subject") or ""), str(payload.get("body") or "")
        messages = []
        if kind == "receipt":
            wanted = set(payload.get("submission_ids") or [])
            for person in view["people"]:
                for sub in person["submissions"]:
                    if sub["id"] in wanted and sub["from_addr"]:
                        values = {"姓名": person["name"], "主题": sub["subject"], "文件": "、".join(f["name"] for f in sub["files"]), "任务": view["config"]["name"]}
                        # A receipt answers the submitted mail itself, so it lands in the sender's thread.
                        original = worker.store.row("SELECT message_id FROM messages WHERE id=?", (sub["message_id"],)) or {}
                        messages.append({"to": [sub["from_addr"]], "subject": smtp_send.render(template_subject, values),
                                         "body": smtp_send.render(template_body, values), "in_reply_to": original.get("message_id") or ""})
        else:
            wanted = set(payload.get("names") or [])
            for person in view["people"]:
                if person["name"] in wanted and person["status"] == "missing" and person["email"]:
                    values = {"姓名": person["name"], "主题": "", "文件": "", "任务": view["config"]["name"]}
                    messages.append({"to": [person["email"]], "subject": smtp_send.render(template_subject, values),
                                     "body": smtp_send.render(template_body, values)})
        if not messages:
            raise core.WorkerError("所选的人没有可用的邮箱地址")
        return worker.outbox.preview(account, messages)
    raise core.WorkerError("不支持的发送类型")


def batch_replies(worker, payload):
    """(account, messages): one reply per chosen mail, to its sender and in its thread. {姓名} is the submitter a
    collection task recorded for that mail, otherwise the sender's display name."""
    ids = [str(i) for i in payload.get("ids") or []][:smtp_send.BATCH_LIMIT + 1]
    rows = [worker.store.message(i) for i in ids]
    rows = [r for r in rows if r and r.get("from_addr") and r.get("folder") != "SENT"]
    if not rows:
        raise core.WorkerError("所选邮件没有可回复的发件人")
    if len({r["account_id"] for r in rows}) > 1:
        raise core.WorkerError("所选邮件来自不同邮箱，请按邮箱分开回复")
    account = worker.accounts.account(rows[0]["account_id"])
    people = {row["message_id"]: (row["person"], json.loads(row["config"] or "{}").get("name") or "") for row in worker.store.rows(
        "SELECT s.message_id, s.person, t.config FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE s.ignored=0 AND s.person<>'' "
        "AND s.message_id IN (%s)" % ",".join("?" * len(rows)), [r["id"] for r in rows])}
    messages = []
    for r in rows:
        person, task = people.get(r["id"], ("", ""))
        values = {"姓名": person or r.get("from_name") or r["from_addr"].split("@")[0], "主题": r.get("subject") or "",
                  "文件": "、".join(a["name"] for a in r.get("attachments") or [] if not a.get("inline")), "任务": task}
        messages.append({"to": [r["from_addr"]], "subject": smtp_send.render(str(payload.get("subject") or "Re: {主题}"), values),
                         "body": smtp_send.render(str(payload.get("body") or ""), values), "in_reply_to": r.get("message_id") or ""})
    return account, messages
