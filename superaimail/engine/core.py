"""The mail worker: mailboxes are watched by `imap_link.Link`, every message goes through `ingest`, and the views
the panel asks for are built here. All state lives under one data directory so the worker can move between servers.

Data directory:
    accounts.json   mailboxes, proxies, rules, AI settings (0600, holds secrets)
    ai.key          AI API key (0600)
    mail.db         index, tasks, submissions, events
    files/<task>/   collected attachments (0600)
"""
import hashlib
import json
import os
import queue
from datetime import datetime
import re
import shutil
import threading
import time
import urllib.parse
import zipfile
from contextlib import contextmanager

from . import accounts as accounts_module
from . import ai as ai_module
from . import collect
from . import content_preview
from . import imap_link
from . import mail_html
from . import mime
from . import smtp_send
from . import oauth
from . import store as store_module
from .store import Store, message_key

from .. import __version__ as VERSION
DOWNLOAD_LIMIT = 200 * 1024 * 1024
BODIES_MAX = 30                                                  # messages per stored_views request
_STORED_TYPES = {"zip", "rar", "7z", "gz", "xlsx", "docx", "pptx", "pdf", "jpg", "jpeg", "png", "mp4", "et"}


class WorkerError(ValueError):
    pass


class Worker:
    def __init__(self, data_dir, log=None):
        self.data = data_dir
        os.makedirs(os.path.join(data_dir, "files"), mode=0o700, exist_ok=True)
        self.accounts = accounts_module.Accounts(os.path.join(data_dir, "accounts.json"))
        self.oauth = oauth.Microsoft(self.accounts)
        self.store = Store(os.path.join(data_dir, "mail.db"))
        self.ai = ai_module.AI(self, os.path.join(data_dir, "ai.key"))
        self.outbox = smtp_send.Outbox(self)
        self.links = {}
        self.lock = threading.RLock()
        self.night_running = threading.Event()
        self.ai_lock = threading.Lock()                         # one AI pass at a time: per-mail batches and the scheduled pass
        self.live_queue = queue.Queue(maxsize=2000)
        self.started = int(time.time())
        self.stopping = threading.Event()
        self._log = log or (lambda text: print(time.strftime("%Y-%m-%dT%H:%M:%S ") + text, flush=True))
        self.last_purge = 0
        self.threads = []

    def log(self, text):
        self._log(text)

    # ------------------------------------------------------------ lifecycle

    def start(self):
        self.refresh_links()
        for name, target in (("scheduler", self._scheduler), ("ai-live", self._ai_live), ("html-backlog", self._html_backlog)):
            thread = threading.Thread(target=target, daemon=True, name=name)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stopping.set()
        try:
            self.live_queue.put_nowait(None)
        except queue.Full:
            pass
        with self.lock:
            for link in self.links.values():
                link.stop()
        for thread in self.threads:
            thread.join(timeout=5)

    def refresh_links(self):
        with self.lock:
            wanted = {a["id"] for a in self.accounts.accounts() if a.get("enabled", True)}
            for account_id, link in list(self.links.items()):
                if account_id not in wanted or not link.is_alive():
                    link.stop()
                    del self.links[account_id]
            for account_id in wanted - set(self.links):
                link = imap_link.Link(self, account_id)
                self.links[account_id] = link
                link.start()

    def _scheduler(self):
        while not self.stopping.wait(20):
            try:
                self.refresh_links()
                self._reminders()
                self.mail_maintenance()
                if self.night_due():
                    self.start_night()
                if time.time() - self.last_purge > 86400:
                    self.store.purge()
                    self.last_purge = time.time()
            except Exception as error:                       # keep the scheduler alive; the log shows what went wrong
                self.log("scheduler: %s: %s" % (type(error).__name__, error))

    # ------------------------------------------------------------ receiving

    def ingest(self, client, account, folder, validity, uids, live):
        """Index new messages, apply rules, file collection submissions and announce what is new.
        Already indexed messages are still offered to collection tasks (for backfills after a task is created)."""
        keys = {uid: message_key(account["id"], folder, validity, uid) for uid in uids}
        fresh = [uid for uid in uids if not self.store.has_message(keys[uid])]
        rules = self.accounts.rules()
        added = []
        for item in imap_link.fetch_messages(client, fresh):
            key = keys.get(item["uid"]) or message_key(account["id"], folder, validity, item["uid"])
            real_files = [a for a in item["attachments"] if not a.get("inline")]
            labels, silent = accounts_module.match_rules(rules, account["id"], item["from_name"] + " " + item["from_addr"], item["subject"], bool(real_files))
            code = mime.verification_code(item["subject"]) or mime.verification_code(item["text"][:1500])
            if code and "验证码" not in labels:
                labels.append("验证码")
            if folder != "INBOX" and "垃圾邮件" not in labels:
                labels.append("垃圾邮件")
            record = {"id": key, "account_id": account["id"], "folder": folder, "validity": validity, "uid": item["uid"],
                      "received": item["received"], "sent": item["sent"], "from_name": item["from_name"], "from_addr": item["from_addr"],
                      "to_addrs": item["to_addrs"], "subject": item["subject"], "size": item["size"], "message_id": item["message_id"],
                      "has_attach": 1 if real_files else 0, "attachments": item["attachments"], "body_part": item["body_part"],
                      "unsubscribe": item.get("unsubscribe", ""),
                      "snippet": re.sub(r"\s+", " ", item["text"])[:300], "code": code, "labels": labels,
                      "link_attach": 1 if mime.has_link_attachment(item["text"]) else 0, "text": item["text"], "text_at": int(time.time()),
                      "text_cut": 1 if item["text_cut"] else 0, "read": 0 if live else 1,
                      "html": item.get("html", ""), "html_inline": item.get("html_inline") or {}}
            rule = self.store.meta("sender_rules", {}).get(account["id"] + ":" + item["from_addr"].lower())
            if rule and rule in [g["name"] for g in self.accounts.groups()]:
                record.update(group_name=rule, group_manual=1)
            self.store.add_message(record)
            added.append((record, silent))
        collected = set()
        tasks = [t for t in self.tasks() if t["enabled"] and not t["config"].get("ended") and t["config"]["account_id"] == account["id"]]
        if tasks:
            for uid in uids:
                message = self.store.message(keys[uid])
                if not message:
                    continue
                for task in tasks:
                    if collect.matches(task["config"], message) and not self.store.row(
                            "SELECT 1 AS x FROM submissions WHERE task_id=? AND message_id=?", (task["id"], message["id"])):
                        self._collect(client, account, task, message, live)
                        collected.add(message["id"])
        if live and added:
            settings = self.accounts.ai()
            if settings.get("enabled") and settings.get("mode", "each") == "each":
                for record, _ in added:
                    if record["folder"] == "INBOX":
                        try:
                            self.live_queue.put_nowait(record["id"])
                        except queue.Full:
                            break                               # the scheduled pass picks up whatever is left
        if live and account.get("notify", "notify") == "notify":
            announce = [record for record, silent in added if not silent and record["id"] not in collected and record["folder"] == "INBOX"]
            if announce:
                self._announce(account, validity, announce)

    def _announce(self, account, validity, records):
        mailbox = "\n收件邮箱 " + account["address"] + ("（%s）" % account["label"] if account.get("label") else "")
        if len(records) == 1:
            record = records[0]
            title = "新邮件 · " + (record["from_name"] or record["from_addr"] or "未知发件人")[:18]
            message = record["subject"] + ("\n验证码 " + record["code"] if record["code"] else "") + mailbox
        else:
            code = next((r["code"] for r in reversed(records) if r["code"]), "")
            title = "%d 封新邮件 · %s" % (len(records), account.get("label") or account["address"].split("@")[0])
            message = "\n".join("%s：%s" % ((r["from_name"] or r["from_addr"])[:14], r["subject"]) for r in reversed(records[-8:])) + \
                ("\n验证码 " + code if code else "") + mailbox
        notice_id = "mail-" + hashlib.sha1(("%s|%s|%d" % (account["address"], validity, max(r["uid"] for r in records))).encode()).hexdigest()[:24]
        self.store.add_notice(notice_id, account["address"], [r["id"] for r in records])
        self.store.add_event("notice", {"channels": ["app"], "kind": "mail", "account_id": account["id"],
                                        "notice": {"id": notice_id, "at": time.time(), "title": title, "message": message[:600], "source": "mail",
                                                   "severity": "info", "origin": "mail:" + account["address"]}})

    # ------------------------------------------------------------ collection

    def tasks(self):
        found = []
        for row in self.store.rows("SELECT * FROM tasks ORDER BY created DESC"):
            row["config"], row["state"] = json.loads(row["config"]), json.loads(row["state"] or "{}")
            row["enabled"] = bool(row["enabled"])
            found.append(row)
        return found

    def task(self, task_id):
        return next((t for t in self.tasks() if t["id"] == task_id), None)

    def _submissions(self, task_id):
        return self.store.rows("SELECT * FROM submissions WHERE task_id=?", (task_id,))

    def _files(self, task_id):
        return self.store.rows("SELECT * FROM files WHERE task_id=?", (task_id,))

    def _collect(self, client, account, task, message, live, suggestion=None):
        """File clear AI matches directly; preserve uncertain matches for review."""
        config = task["config"]
        if suggestion is None:
            person, reason, flags = collect.identify(config, message)
            suggest = ""
        else:
            suggest = suggestion[0]
            person, reason, flags = "", "AI 判断可能属于此任务" + ("：" + suggestion[1] if suggestion[1] else ""), []
            if len(suggestion) > 2 and suggestion[2] is True:
                person, identity_reason, flags = collect.identify(config, message)
                if person and suggest and person != suggest:
                    person, identity_reason = "", "提交人判断不一致"
                if person:
                    reason = "AI 自动收录：" + suggestion[1]
                    flags.append("AI 自动收录")
                elif identity_reason:
                    reason += "；" + identity_reason
        files = [a for a in message["attachments"] if not a.get("inline")]
        flags += collect.submission_flags(config, message, person, files)
        submission_id = collect.new_id("s")
        self.store.execute("INSERT INTO submissions (id, task_id, message_id, person, pending, ignored, manual, reason, flags, received, subject, "
                           "from_name, from_addr, suggest) VALUES (?,?,?,?,?,0,0,?,?,?,?,?,?,?)",
                           (submission_id, task["id"], message["id"], person, 0 if person else 1, reason, json.dumps(flags, ensure_ascii=False),
                            message["received"], message["subject"], message["from_name"], message["from_addr"], suggest))
        for item in files:
            self._save_file(client, task["id"], submission_id, message, item)
        if live:
            view = self.task_view(task["id"])
            counts = view["counts"]
            progress = "（%d/%d）" % (counts["submitted"], counts["expected"]) if counts["expected"] else "（已收 %d 份）" % counts["submitted"]
            title = "%s · %s" % (config["name"], person + "已提交" + progress if person else "有一封需要确认")
            text = message["subject"] + ("\n" + reason if reason else "") + ("\n" + "、".join(flags) if flags else "") + "\n收件邮箱 " + account["address"]
            notice_id = "col-" + submission_id
            self.store.add_notice(notice_id, account["address"], [message["id"]])
            self.store.add_event("notice", {"channels": ["app"], "kind": "collect", "task_id": task["id"], "account_id": account["id"],
                                            "notice": {"id": notice_id, "at": time.time(), "title": title[:150], "message": text[:600],
                                                       "source": "mail", "severity": "info", "origin": "mail:" + account["address"]}})

    def _save_file(self, client, task_id, submission_id, message, item):
        file_id = collect.new_id("f")
        folder = os.path.join(self.data, "files", task_id)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        target = os.path.join(folder, file_id)
        digest, error, size = hashlib.sha256(), "", 0
        if item["size"] > collect.FILE_LIMIT * 1.4:
            error = "附件超过 50 MB，请在邮箱网页下载"
        else:
            try:
                with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
                    def write(data):
                        digest.update(data)
                        stream.write(data)
                    size = imap_link.stream_part(client, message["uid"], item["part"], item["encoding"], write, collect.FILE_LIMIT)
            except imap_link.LinkError as failure:
                error = str(failure)
            except OSError as failure:
                error = imap_link.describe(failure)
            if error:
                try:
                    os.remove(target)
                except OSError:
                    pass
        self.store.execute("INSERT INTO files (id, task_id, submission_id, name, size, sha256, path, part, error) VALUES (?,?,?,?,?,?,?,?,?)",
                           (file_id, task_id, submission_id, item["name"], size, "" if error else digest.hexdigest(),
                            "" if error else target, item["part"], error))

    def retry_files(self, task_id):
        """Download again the files of a task that failed earlier, over a separate connection."""
        broken = self.store.rows("SELECT f.*, s.message_id FROM files f JOIN submissions s ON s.id=f.submission_id WHERE f.task_id=? AND f.error!=''", (task_id,))
        repaired = 0
        for row in broken:
            message = self.store.message(row["message_id"])
            if not message:
                continue
            item = next((a for a in message["attachments"] if a["part"] == row["part"]), None)
            if not item:
                continue
            account = self.accounts.account(message["account_id"])
            with self.session(account, message) as client:
                self.store.execute("DELETE FROM files WHERE id=?", (row["id"],))
                self._save_file(client, task_id, row["submission_id"], message, item)
                repaired += 1
        return {"retried": repaired}

    def save_task(self, payload):
        account_ids = {a["id"] for a in self.accounts.accounts()}
        task_id = str(payload.get("id") or "")
        existing = self.task(task_id) if task_id else None
        if task_id and not existing:
            raise WorkerError("这个任务已不存在，请刷新后重试")
        config = collect.build_config(payload, existing and existing["config"], account_ids)
        if existing is None:
            if len(self.tasks()) >= collect.TASK_LIMIT:
                raise WorkerError("任务数量已达上限")
            task_id = collect.new_id("t")
            self.store.execute("INSERT INTO tasks (id, created, enabled, config, state) VALUES (?,?,1,?,'{}')",
                               (task_id, int(time.time()), json.dumps(config, ensure_ascii=False)))
        else:
            state = existing["state"]
            if config.get("deadline") != existing["config"].get("deadline") or config.get("remind_hours") != existing["config"].get("remind_hours"):
                state = {}
            self.store.execute("UPDATE tasks SET config=?, state=? WHERE id=?", (json.dumps(config, ensure_ascii=False), json.dumps(state), task_id))
            self._reidentify(task_id)
        old = existing["config"] if existing else {}
        if not existing or any(old.get(k) != config.get(k) for k in ("start", "keywords", "account_id")):
            self.rescan(task_id)
        return self.task_view(task_id)

    def rescan(self, task_id):
        task = self.task(task_id)
        if not task:
            raise WorkerError("这个任务已不存在，请刷新后重试")
        link = self.links.get(task["config"]["account_id"])
        if link:
            link.queue_backfill("", task["config"]["start"])
        return {"queued": bool(link)}

    def set_task_enabled(self, task_id, enabled):
        self.store.execute("UPDATE tasks SET enabled=? WHERE id=?", (1 if enabled else 0, task_id))
        if enabled:
            self.rescan(task_id)

    def end_task(self, task_id, ended):
        """Ending stops collecting and reminders; the files and records stay. Reopening resumes and catches up."""
        task = self.task(task_id)
        if not task:
            raise WorkerError("这个任务已不存在，请刷新后重试")
        task["config"]["ended"] = int(time.time()) if ended else 0
        self.store.execute("UPDATE tasks SET config=?, enabled=? WHERE id=?", (json.dumps(task["config"], ensure_ascii=False), 0 if ended else 1, task_id))
        if not ended:
            self.rescan(task_id)
        return self.task_view(task_id)

    def collect_suggested(self, client, account, task_id, items):
        """Run AI collection decisions from the mailbox's own session."""
        task = self.task(task_id)
        if not task or not task["enabled"] or task["config"].get("ended"):
            return
        for message_id, suggestion in items:
            message = self.store.message(message_id)
            if not message or self.store.row("SELECT 1 AS x FROM submissions WHERE task_id=? AND message_id=?", (task_id, message_id)):
                continue
            if message['account_id'] != task['config']['account_id'] or message['received'] < task['config']['start']:
                continue
            if self.store.row("SELECT 1 AS x FROM submissions WHERE message_id=? AND task_id!=? AND ignored=0 AND pending=0", (message_id, task_id)):
                continue
            validity = imap_link.select(client, message["folder"])
            if validity == message["validity"]:
                self._collect(client, account, task, message, False, suggestion)

    def delete_task(self, task_id):
        with self.store.lock:
            self.store.execute("DELETE FROM files WHERE task_id=?", (task_id,))
            self.store.execute("DELETE FROM submissions WHERE task_id=?", (task_id,))
            self.store.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        shutil.rmtree(os.path.join(self.data, "files", task_id), ignore_errors=True)

    def assign(self, submission_id, person, remember=True):
        sub = self.store.row("SELECT * FROM submissions WHERE id=?", (submission_id,))
        if not sub:
            raise WorkerError("这份提交已不存在，请刷新后重试")
        person = "".join(c for c in str(person or "") if ord(c) >= 32).strip()[:20]
        if not person:
            raise WorkerError("请填写提交人姓名")
        task = self.task(sub["task_id"])
        message = self.store.message(sub["message_id"]) or {"received": sub["received"], "subject": sub["subject"], "attachments": [], "link_attach": False}
        files = [{"name": f["name"]} for f in self._files(sub["task_id"]) if f["submission_id"] == submission_id]
        flags = collect.submission_flags(task["config"], message, person, files)
        self.store.execute("UPDATE submissions SET person=?, pending=0, manual=1, reason='', flags=? WHERE id=?",
                           (person, json.dumps(flags, ensure_ascii=False), submission_id))
        if remember and sub["from_addr"]:
            config = task["config"]
            address = sub["from_addr"].lower()
            owned = {a["address"].lower() for a in self.accounts.accounts()}
            # A forwarding mailbox can carry many people's files; correct the item without learning its sender.
            if collect.is_forwarded(message) or address in owned:
                changed = config["senders"].pop(address, None) is not None
            else:
                changed = config["senders"].get(address) != person
                config["senders"][address] = person
            if changed:
                self.store.execute("UPDATE tasks SET config=? WHERE id=?", (json.dumps(config, ensure_ascii=False), task["id"]))
                self._reidentify(task["id"])
        return self.task_view(task["id"])

    def ignore(self, submission_id, ignored):
        sub = self.store.row("SELECT task_id FROM submissions WHERE id=?", (submission_id,))
        if not sub:
            raise WorkerError("这份提交已不存在，请刷新后重试")
        self.store.execute("UPDATE submissions SET ignored=? WHERE id=?", (1 if ignored else 0, submission_id))
        return self.task_view(sub["task_id"])

    def _reidentify(self, task_id):
        task = self.task(task_id)
        files = self._files(task_id)
        for sub in self._submissions(task_id):
            if sub["manual"]:
                continue
            message = self.store.message(sub["message_id"])
            if not message:
                continue
            person, reason, flags = collect.identify(task["config"], message)
            if not person and sub["person"] and not sub["pending"]:
                continue        # a task edit can file waiting mail, never send filed mail (e.g. an AI match) back to 待确认
            flags += collect.submission_flags(task["config"], message, person, [f for f in files if f["submission_id"] == sub["id"]])
            self.store.execute("UPDATE submissions SET person=?, pending=?, reason=?, flags=? WHERE id=?",
                               (person, 0 if person else 1, reason, json.dumps(flags, ensure_ascii=False), sub["id"]))

    def task_view(self, task_id):
        task = self.task(task_id)
        if not task:
            raise WorkerError("这个任务已不存在，请刷新后重试")
        view = collect.overview(task, self._submissions(task_id), self._files(task_id))
        account = self.accounts.account(task["config"]["account_id"])
        view["account"] = {"id": account["id"], "address": account["address"], "label": account.get("label", "")} if account else None
        view["folder"] = (mime.safe_name(account["address"], 120) + "/" + mime.safe_name(task["config"]["name"], 60)) if account else ""
        link = self.links.get(task["config"]["account_id"])
        view["scanning"] = bool(link and (link.backfill or not link.jobs.empty()))
        return view

    def _reminders(self):
        now = time.time()
        for task in self.tasks():
            for kind in collect.due_reminders(task, now):
                view = self.task_view(task["id"])
                text = collect.summary_text(view, kind == "final")
                title = ("%s · 已截止" if kind == "final" else "%s · 截止前提醒") % task["config"]["name"]
                notice_id = "col-%s-%s" % (kind, task["id"])
                self.store.add_event("notice", {"channels": ["app"], "kind": "collect_summary", "task_id": task["id"],
                                                "notice": {"id": notice_id, "at": time.time(), "title": title, "message": text[:600],
                                                           "source": "mail", "severity": "warning", "origin": "mail:collect"}})
                task["state"][kind] = int(now)
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (json.dumps(task["state"]), task["id"]))

    # ------------------------------------------------------------ files for the computer

    def sync_manifest(self):
        entries = []
        for task in self.tasks():
            account = self.accounts.account(task["config"]["account_id"])
            if not account:
                continue
            for entry in collect.layout(task, account["address"], self._submissions(task["id"]), self._files(task["id"])):
                entries.append(dict(entry, task_id=task["id"]))
        version = hashlib.sha256(json.dumps([[e["path"], e["sha256"]] for e in entries], ensure_ascii=False).encode()).hexdigest()[:20]
        return {"version": version, "files": entries}

    def file_path(self, file_id):
        row = self.store.row("SELECT * FROM files WHERE id=? AND error=''", (file_id,))
        if not row or not row["path"] or not os.path.exists(row["path"]):
            raise WorkerError("文件不存在")
        return row

    def write_zip(self, task_id, stream, latest_only=False):
        """A ZIP of the task's folder (or of every task when task_id is empty), written as it is read."""
        manifest = self.sync_manifest()["files"]
        chosen = [e for e in manifest if not task_id or e["task_id"] == task_id]
        if latest_only:
            chosen = [e for e in chosen if collect.HISTORY not in e["path"].split("/")]
        with zipfile.ZipFile(stream, "w", allowZip64=True) as archive:
            for entry in chosen:
                row = self.file_path(entry["file_id"])
                kind = zipfile.ZIP_STORED if mime.extension(entry["path"]) in _STORED_TYPES else zipfile.ZIP_DEFLATED
                info = zipfile.ZipInfo(entry["path"], date_time=time.localtime(entry["received"])[:6])
                info.compress_type = kind
                info.external_attr = 0o644 << 16
                with open(row["path"], "rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, 256 * 1024)

    # ------------------------------------------------------------ reading on request

    @contextmanager
    def session(self, account, message=None):
        """A separate, short-lived connection, so a large download never holds up new-mail checks."""
        if not account:
            raise WorkerError("这封邮件所在的邮箱已移除")
        client = imap_link.connect(self.oauth.prepare(account), self.accounts.proxy(account.get("proxy_id")))
        try:
            if message:
                validity = imap_link.select(client, message["folder"])
                if validity != message["validity"]:
                    raise WorkerError("邮箱文件夹已重建，这封邮件需要重新读取")
            yield client
        finally:
            imap_link.close(client)

    def message_view(self, message_id, mark_read=True):
        message = self.store.message(message_id)
        if not message:
            raise WorkerError("这封邮件已不在索引中")
        if message.get("text") is None:
            account = self.accounts.account(message["account_id"])
            try:
                with self.session(account, message) as client:
                    fetched = imap_link.fetch_messages(client, [message["uid"]])
                if fetched:
                    message["text"], message["text_cut"] = fetched[0]["text"], fetched[0]["text_cut"]
                    message["has_html"] = bool(fetched[0].get("html"))
                    self.store.update_message(message_id, text=message["text"], text_at=int(time.time()), text_cut=1 if message["text_cut"] else 0,
                                              html=fetched[0].get("html", ""), html_inline=fetched[0].get("html_inline") or {})
            except Exception as error:
                message["text"], message["text_error"] = "", imap_link.describe(error) if not isinstance(error, WorkerError) else str(error)
        elif message.get("has_html") is None:
            # Mail indexed before HTML was kept: read it now, once, so its action links show on this first opening.
            stored = self._fill_reading(message)
            message["has_html"] = None if stored is None else bool(stored)
        if mark_read and self.mail_preferences()["read_on_open"] and not message["read"]:
            self.store.update_message(message_id, read=1)
            message["read"] = True
        return self._reading(message)

    def stored_views(self, ids):
        """Messages as message_view shows them, for those whose text is already stored; nothing is fetched from the
        mail server and nothing is marked read. Clients use it to have the newest mail ready before it is opened."""
        out = []
        for message_id in list(dict.fromkeys(ids))[:BODIES_MAX]:
            message = self.store.message(message_id)
            if message and message.get("text") is not None:
                out.append(self._reading(message))
        return out

    def reading_page(self, message_id, mode="original", theme="light", images=False):
        """The page a client shows for one mail (mail_html.page). Mail indexed before HTML was kept has it read now,
        once, from the mail server (read-only); its text is refreshed from the same reading."""
        message = self.store.message(message_id)
        if not message:
            raise WorkerError("这封邮件已不在索引中")
        stored, _ = self.store.html(message_id)
        if stored is None:
            stored = self._fill_reading(message)
        inline = lambda part: "inline?" + urllib.parse.urlencode({"id": message_id, "part": part})
        return mail_html.page(stored or "", message.get("text") or "", mode if mode in ("original", "simple") else "original",
                              theme, images, inline)

    def _fill_reading(self, message):
        try:
            with self.session(self.accounts.account(message["account_id"]), message) as client:
                reading = imap_link.fetch_reading(client, message["uid"])
        except Exception:
            return None                                   # not stored: tried again on the next opening
        stored = self._keep_reading(message["id"], reading)
        if reading:
            message["text"] = reading["text"]
        return stored

    def _keep_reading(self, message_id, reading):
        """Store one reading of a message indexed earlier: its HTML (empty when gone or none) and the text taken from
        the same reading. The text's retention clock (text_at) is left as it was."""
        if reading is None:
            self.store.update_message(message_id, html="", html_inline={})
            return ""
        text = reading["text"]
        self.store.update_message(message_id, html=reading["html"], html_inline=reading["html_inline"], text=text,
                                  text_cut=1 if reading["text_cut"] else 0, snippet=re.sub(r"\s+", " ", text)[:300])
        return reading["html"]

    def _html_backlog(self):
        """Mail indexed before the reading view kept HTML gets it in the background, newest first, so opening a mail
        never waits for the mail server. When nothing is left it looks again every ten minutes."""
        resting = {}
        while not self.stopping.wait(30):
            try:
                done = self.fill_html_backlog(resting)
            except Exception as error:
                self.log("补取邮件排版出错：%s" % error)
                done = 0
            if not done:
                self.stopping.wait(570)

    def fill_html_backlog(self, resting=None, limit=25):
        """Read the HTML of up to `limit` messages that lack it, one mail-server session per folder. An account that
        fails rests for ten minutes; a folder rebuilt since indexing (UIDVALIDITY changed) is marked as having none."""
        resting = {} if resting is None else resting
        rows = self.store.rows("SELECT id, account_id, folder, validity, uid FROM messages WHERE html IS NULL AND text_at IS NOT NULL "
                               "AND deleted=0 ORDER BY received DESC LIMIT 400")
        groups = {}
        for row in rows:
            if resting.get(row["account_id"], 0) <= time.time():
                groups.setdefault((row["account_id"], row["folder"], row["validity"]), []).append(row)
        done = 0
        for (account_id, folder, validity), items in groups.items():
            if done >= limit or self.stopping.is_set():
                break
            account = self.accounts.account(account_id)
            if not account or not account.get("enabled", True):
                resting[account_id] = time.time() + 3600
                continue
            try:
                with self.session(account, {"folder": folder, "validity": validity}) as client:
                    for row in items[:limit - done]:
                        if self.stopping.is_set():
                            break
                        self._keep_reading(row["id"], imap_link.fetch_reading(client, row["uid"]))
                        done += 1
            except WorkerError as error:
                if "重建" in str(error):
                    for row in items:
                        self.store.update_message(row["id"], html="", html_inline={})
                else:
                    resting[account_id] = time.time() + 600
            except Exception:
                resting[account_id] = time.time() + 600
        return done

    def inline_image(self, message_id, part):
        """(content type, producer) of an inline image the mail's HTML shows."""
        message = self.store.message(message_id)
        if not message:
            raise WorkerError("这封邮件已不在索引中")
        _, parts = self.store.html(message_id)
        item = parts.get(part)
        if not item or not item["type"].startswith("image/"):
            raise WorkerError("没有这张图片")
        account = self.accounts.account(message["account_id"])

        def produce(write):
            with self.session(account, message) as client:
                imap_link.stream_part(client, message["uid"], part, item["encoding"], write, DOWNLOAD_LIMIT)
        return item["type"], produce

    def _reading(self, message):
        message_id = message["id"]
        message["action_links"] = content_preview.action_links(message.get("text") or "", message.get("unsubscribe") or "")
        if message.get("has_html"):
            stored, _ = self.store.html(message_id)
            known = {link["url"] for link in message["action_links"]}
            message["action_links"] = [dict(link, kind="action") for link in mail_html.links(stored or "") if link["url"] not in known] + message["action_links"]
        message.pop("validity", None)
        account = self.accounts.account(message["account_id"])
        message["account"] = {"id": account["id"], "address": account["address"], "label": account.get("label", "")} if account else None
        message["submissions"] = self.store.rows("SELECT s.id, s.task_id, s.person, s.pending, t.config FROM submissions s JOIN tasks t ON t.id=s.task_id "
                                                 "WHERE s.message_id=? AND s.ignored=0", (message_id,))
        for sub in message["submissions"]:
            sub["task_name"] = json.loads(sub.pop("config"))["name"]
        return message

    def attachment(self, message_id, part):
        message = self.store.message(message_id)
        if not message:
            raise WorkerError("这封邮件已不在索引中")
        item = next((a for a in message["attachments"] if a["part"] == part), None)
        if not item:
            raise WorkerError("没有这个附件")
        return message, item

    def stream_attachment(self, message, item, write):
        account = self.accounts.account(message["account_id"])
        with self.session(account, message) as client:
            imap_link.stream_part(client, message["uid"], item["part"], item["encoding"], write, DOWNLOAD_LIMIT)

    def attachment_preview(self, message_id, part):
        message, item = self.attachment(message_id, part)
        if not item["name"].lower().endswith(".xlsx"):
            raise WorkerError("此附件请下载后打开")
        if item.get("size", 0) > content_preview.MAX_ATTACHMENT:
            raise WorkerError("附件超过 8 MB，请下载后打开")
        chunks, size = [], 0
        def write(data):
            nonlocal size
            size += len(data)
            if size > content_preview.MAX_ATTACHMENT:
                raise WorkerError("附件超过 8 MB，请下载后打开")
            chunks.append(data)
        self.stream_attachment(message, item, write)
        return content_preview.xlsx_preview(b"".join(chunks))

    def notice_text(self, identifier):
        notice = self.store.notice(str(identifier or ""))
        if not notice:
            raise WorkerError("没有保存这封邮件的全文")
        mails = []
        for message_id in notice["message_ids"]:
            try:
                message = self.message_view(message_id, mark_read=False)
            except WorkerError:
                continue
            mails.append({"id": message_id, "from": message["from_name"] or message["from_addr"], "address": message["from_addr"],
                          "subject": message["subject"], "sent": message["sent"] or message["received"], "text": message.get("text") or "",
                          "cut": bool(message.get("text_cut"))})
        return {"mailbox": notice["mailbox"], "at": notice["at"], "mails": mails}

    def test_account(self, payload):
        existing = self.accounts.account(str(payload.get("id") or "")) if payload.get("id") else None
        record = self.accounts.build_account(payload, existing)
        record = self.oauth.prepare(record)
        proxy = self.accounts.proxy(record.get("proxy_id"))
        result = {"imap": False, "smtp": None}
        client = None
        try:
            client = imap_link.connect(record, proxy)
            status, data = client.select("INBOX", readonly=True)
            result.update(imap=status == "OK", messages=int(data[0] or 0) if status == "OK" else 0, junk=imap_link.decode_folder(imap_link.find_junk(client)))
        except Exception as error:
            raise WorkerError(imap_link.describe(error)) from None
        finally:
            imap_link.close(client)
        if record.get("smtp_host"):
            from . import net
            kind = net.SMTPStartTLS if record.get("smtp_security") == "starttls" else net.SMTPS
            try:
                smtp = kind(record["smtp_host"], record["smtp_port"], proxy=proxy)
                try:
                    oauth.smtp_login(smtp, record)
                    result["smtp"] = True
                finally:
                    try:
                        smtp.quit()
                    except Exception:
                        pass
            except Exception as error:
                result["smtp"], result["smtp_error"] = False, imap_link.describe(error) if not hasattr(error, "smtp_code") else "发信服务器登录失败"
        return result

    def save_account(self, payload):
        record, existing = self.accounts.save_account(payload)
        if existing and any(existing.get(k) != record.get(k) for k in ("address", "imap_host", "imap_port")):
            self.store.forget_account(record["id"])
        with self.lock:
            link = self.links.pop(record["id"], None)
            if link:
                link.stop()
        self.refresh_links()
        return record

    def delete_account(self, account_id, forget=True):
        self.accounts.delete_account(account_id)
        with self.lock:
            link = self.links.pop(account_id, None)
            if link:
                link.stop()
        if forget:
            self.store.forget_account(account_id)

    def backfill(self, account_id, days):
        link = self.links.get(account_id)
        if not link:
            raise WorkerError("这个邮箱没有在运行")
        days = max(1, min(int(days or 60), 365))
        link.queue_backfill("", time.time() - days * 86400)
        return {"queued": True}

    # ------------------------------------------------------------ nightly AI pass

    NIGHT_CLASSIFY_LIMIT, CLASSIFY_BATCH, CANDIDATE_BATCH, RENAME_BATCH = 600, 15, 10, 20

    def night_due(self, now=None):
        """After the configured Beijing time, once a day ("each" and "daily"; for "each" it only catches up on what
        the per-mail batches missed) or every three days ("every3"), while AI is on and no pass is running."""
        settings = self.accounts.ai()
        if not settings.get("enabled") or self.night_running.is_set():
            return False
        current = datetime.fromtimestamp(now or time.time(), ai_module.BEIJING)
        hour, minute = (int(x) for x in settings.get("night", "02:30").split(":"))
        if (current.hour, current.minute) < (hour, minute):
            return False
        last = (self.store.meta("night_last", {}) or {}).get("date")
        if not last:
            return True
        try:
            gap = (current.date() - datetime.strptime(last, "%Y-%m-%d").date()).days
        except ValueError:
            return True
        return gap >= (3 if settings.get("mode") == "every3" else 1)

    def start_night(self):
        if self.night_running.is_set():
            raise WorkerError("夜间整理正在进行")
        if not self.accounts.ai().get("enabled"):
            raise WorkerError("AI 功能已关闭，可在 AI 设置里开启")
        self.night_running.set()
        threading.Thread(target=self._night, daemon=True, name="night").start()
        return {"started": True}

    def _ai_live(self):
        """Per-mail mode: gather new mail for up to 90 seconds (or 15 messages), then sort it, check it against the
        collection tasks and rename what needs it, in one go."""
        while not self.stopping.is_set():
            try:
                first = self.live_queue.get(timeout=5)
            except queue.Empty:
                continue
            if self.stopping.is_set():
                break
            batch, deadline = [first], time.time() + 90
            while len(batch) < 15 and time.time() < deadline and not self.stopping.is_set():
                try:
                    batch.append(self.live_queue.get(timeout=max(.1, deadline - time.time())))
                except queue.Empty:
                    break
            if self.stopping.is_set():
                break
            settings = self.accounts.ai()
            if not settings.get("enabled") or settings.get("mode", "each") != "each":
                continue
            report = {"at": int(time.time()), "mails": len(batch), "classified": 0, "suggested": 0, "renamed": 0, "errors": []}
            with self.ai_lock:
                for step in (lambda: self._night_classify(report, batch), lambda: self._night_candidates(report), lambda: self._night_rename(report)):
                    try:
                        step()
                    except ai_module.AIError as error:
                        report["errors"].append(str(error))
                        break
                    except Exception as error:
                        report["errors"].append(type(error).__name__)
                        self.log("ai live: %s: %s" % (type(error).__name__, error))
            self.store.set_meta("live_last", report)

    def _night(self):
        report = {"date": datetime.now(ai_module.BEIJING).strftime("%Y-%m-%d"), "started": int(time.time()), "classified": 0,
                  "suggested": 0, "renamed": 0, "errors": []}
        self.ai_lock.acquire()
        try:
            for step in (self._night_classify, self._night_candidates, self._night_rename):
                try:
                    step(report)
                except ai_module.AIError as error:
                    report["errors"].append(str(error))
                    if "上限" in str(error) or "关闭" in str(error) or "密钥" in str(error):
                        break
                except Exception as error:
                    report["errors"].append("%s 失败：%s" % (step.__name__.replace("_night_", ""), type(error).__name__))
                    self.log("night %s: %s: %s" % (step.__name__, type(error).__name__, error))
        finally:
            self.ai_lock.release()
            report["finished"] = int(time.time())
            self.store.set_meta("night_last", report)
            self.night_running.clear()
            self.log("night pass: %d sorted, %d suggested, %d renamed, %d problem(s)" % (
                report["classified"], report["suggested"], report["renamed"], len(report["errors"])))

    def _night_classify(self, report, only=None):
        groups = self.accounts.groups()
        if not groups:
            return
        # mail never sorted, and recent mail without a level yet (also mail the owner grouped by hand: it keeps its group)
        recent = int(time.time()) - 60 * 86400
        # ai_level NULL: not judged yet; '' means the model gave no level, so it is not asked again
        wanted = "((ai_at IS NULL AND group_manual=0) OR (ai_level IS NULL AND received>?))"
        if only:
            marks = ",".join("?" * len(only))
            waiting = self.store.rows("SELECT id FROM messages WHERE id IN (%s) AND %s ORDER BY received DESC" % (marks, wanted), list(only) + [recent])
        else:
            waiting = self.store.rows("SELECT id FROM messages WHERE folder='INBOX' AND %s ORDER BY received DESC LIMIT ?" % wanted,
                                      (recent, self.NIGHT_CLASSIFY_LIMIT))
        ids = [row["id"] for row in waiting]
        for start in range(0, len(ids), self.CLASSIFY_BATCH):
            batch = [self.store.message(i) for i in ids[start:start + self.CLASSIFY_BATCH]]
            batch = [m for m in batch if m]
            chosen = self.ai.classify_batch(batch, groups)
            now = int(time.time())
            for message in batch:
                group, summary, level = chosen.get(message["id"], (None, None, None))
                changes = {"ai_at": now}
                if group:
                    changes.update(ai_summary=summary or message.get("ai_summary"), ai_level=level or "")
                    if not message.get("group_manual"):
                        changes["group_name"] = group
                    report["classified"] += 1
                self.store.update_message(message["id"], **changes)
            self.refresh_attention("id IN (%s)" % ",".join("?" * len(batch)), [m["id"] for m in batch])

    # ------------------------------------------------------------ attention

    def attention_of(self, message, rules=None, groups=None):
        """The owner's rule for the sender, else the model's level, else the group's default, else a guess from what the
        mail is: junk is low, a code is a notice, anything else is worth reading."""
        rules = self.store.meta("attention_rules", {}) if rules is None else rules
        level = rules.get(message["account_id"] + ":" + (message.get("from_addr") or "").lower())
        if level in store_module.ATTENTION:
            return level
        if message.get("ai_level") in store_module.ATTENTION:
            return message["ai_level"]
        group = next((g for g in (self.accounts.groups() if groups is None else groups) if g["name"] == message.get("group_name")), {})
        if group.get("attention") in store_module.ATTENTION:
            return group["attention"]
        labels = message.get("labels") or []
        if "垃圾邮件" in (json.loads(labels) if isinstance(labels, str) else labels):
            return "low"
        return "fyi" if message.get("code") else "read"

    def refresh_attention(self, where="attention IS NULL", args=()):
        rules, groups = self.store.meta("attention_rules", {}), self.accounts.groups()
        rows = self.store.rows("SELECT id, account_id, from_addr, ai_level, group_name, labels, code, attention FROM messages WHERE " + where, list(args))
        with self.store.lock:
            for row in rows:
                level = self.attention_of(row, rules, groups)
                if level != row["attention"]:
                    self.store.execute("UPDATE messages SET attention=? WHERE id=?", (level, row["id"]))
        return len(rows)

    def attention_rule(self, payload):
        """Always give this sender's mail the chosen level ('' returns it to the model and group defaults)."""
        message = self.store.message(str(payload.get("id") or ""))
        if not message or not message.get("from_addr"):
            raise WorkerError("没有可用的发件人地址")
        level = str(payload.get("level") or "")
        if level and level not in store_module.ATTENTION:
            raise WorkerError("关注度无效")
        key = message["account_id"] + ":" + message["from_addr"].lower()
        with self.store.lock:
            rules = self.store.meta("attention_rules", {})
            if level:
                rules[key] = level
            else:
                rules.pop(key, None)
            self.store.set_meta("attention_rules", rules)
        self.refresh_attention("account_id=? AND LOWER(from_addr)=?", (message["account_id"], message["from_addr"].lower()))
        return {"ok": True, "attention_rules": self.store.meta("attention_rules", {})}

    def remove_attention_rule(self, key):
        with self.store.lock:
            rules = self.store.meta("attention_rules", {})
            if rules.pop(str(key or ""), None) is None:
                raise WorkerError("这条规则已不存在")
            self.store.set_meta("attention_rules", rules)
        account_id, _, address = str(key).partition(":")
        self.refresh_attention("account_id=? AND LOWER(from_addr)=?", (account_id, address))
        return {"ok": True, "attention_rules": rules}

    def _night_candidates(self, report):
        previews, decisions, checkpoints = {}, [], []
        for task in self.tasks():
            config = task["config"]
            if not task["enabled"] or config.get("ended"):
                continue
            link = self.links.get(config["account_id"])
            if not link:
                continue
            checked = int(task["state"].get("ai_checked_until") or config["start"])
            rows = self.store.rows("SELECT id FROM messages WHERE account_id=? AND received>=? AND received>? AND (has_attach=1 OR link_attach=1) "
                                   "AND id NOT IN (SELECT message_id FROM submissions WHERE task_id=?) ORDER BY received LIMIT 200",
                                   (config["account_id"], config["start"], checked, task["id"]))
            messages = [m for m in (self.store.message(r["id"]) for r in rows) if m and not collect.matches(config, m)]
            messages = [m for m in messages if not self.store.row(
                "SELECT 1 AS x FROM submissions WHERE message_id=? AND task_id!=? AND ignored=0 AND pending=0", (m['id'], task['id']))]
            for message in messages:
                if message['id'] not in previews:
                    previews[message['id']] = self._collection_preview(message)
                message['collection_preview'] = previews[message['id']]
            picked = []
            for start in range(0, len(messages), self.CANDIDATE_BATCH):
                found = self.ai.task_candidates(config, messages[start:start + self.CANDIDATE_BATCH])
                picked.extend(found.items())
            if picked:
                decisions.append((link, task["id"], picked))
            if rows:
                task["state"]["ai_checked_until"] = max(m["received"] for m in (self.store.message(r["id"]) for r in rows) if m)
                checkpoints.append((json.dumps(task['state']), task['id']))
        certain = {}
        for _, task_id, picked in decisions:
            for message_id, suggestion in picked:
                if len(suggestion) > 2 and suggestion[2] is True:
                    certain.setdefault(message_id, set()).add(task_id)
        for link, task_id, picked in decisions:
            resolved = []
            for message_id, suggestion in picked:
                owners = certain.get(message_id, set())
                if len(owners) == 1 and task_id not in owners:
                    continue
                if len(owners) > 1:
                    suggestion = (suggestion[0], "多个项目归属冲突：" + suggestion[1], False)
                resolved.append((message_id, suggestion))
            if resolved:
                link.queue_collect(task_id, resolved)
                report['suggested'] += len(resolved)
        # Advance only after every competing project was evaluated and jobs were queued.
        # An AI failure partway through must leave earlier messages eligible for retry.
        for state, task_id in checkpoints:
            self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))

    def _collection_preview(self, message):
        """Read bounded XLSX text once per pass, preferring already collected files."""
        snippets = []
        for item in [a for a in message.get('attachments', []) if not a.get('inline') and a['name'].lower().endswith('.xlsx')][:2]:
            if item.get('size', 0) > content_preview.MAX_ATTACHMENT:
                continue
            try:
                saved = self.store.row("SELECT f.path FROM files f JOIN submissions s ON s.id=f.submission_id "
                                       "WHERE s.message_id=? AND f.part=? AND f.error='' LIMIT 1", (message['id'], item['part']))
                if saved and saved['path']:
                    with open(saved['path'], 'rb') as stream:
                        preview = content_preview.xlsx_preview(stream.read(content_preview.MAX_ATTACHMENT + 1))
                else:
                    preview = self.attachment_preview(message['id'], item['part'])
                snippets.append(item['name'] + ': ' + json.dumps(preview['sheets'], ensure_ascii=False)[:4500])
            except (ValueError, OSError, WorkerError, imap_link.LinkError):
                snippets.append(item['name'] + ': 预览不可用，不能推断附件内容')
        return '\n'.join(snippets)

    def _night_rename(self, report):
        for task in self.tasks():
            if task["config"].get("rename") != "ai":
                continue
            rows = self.store.rows("SELECT f.id, f.name, s.person, s.subject FROM files f JOIN submissions s ON s.id=f.submission_id "
                                   "WHERE f.task_id=? AND f.ai_checked=0 AND f.error='' AND s.pending=0 LIMIT 200", (task["id"],))
            for start in range(0, len(rows), self.RENAME_BATCH):
                batch = rows[start:start + self.RENAME_BATCH]
                names = self.ai.rename_files(task["config"], batch)
                for row in batch:
                    if row["id"] in names:
                        self.store.execute("UPDATE files SET display_name=?, ai_checked=1 WHERE id=?", (names[row["id"]], row["id"]))
                        report["renamed"] += 1
                    else:
                        self.store.execute("UPDATE files SET ai_checked=1 WHERE id=?", (row["id"],))

    def set_message_group(self, message_id, group):
        if not self.store.message(message_id):
            raise WorkerError("这封邮件已不在索引中")
        if group and group not in [g["name"] for g in self.accounts.groups()]:
            raise WorkerError("没有这个分组")
        if group:
            self.store.update_message(message_id, group_name=group, group_manual=1)
        else:
            self.store.update_message(message_id, group_name=None, group_manual=0, ai_at=None)

    def save_group(self, payload):
        record, existing = self.accounts.save_group(payload)
        if existing and existing.get("attention") != record.get("attention"):
            self.refresh_attention("group_name=?", (existing["name"],))
        if existing and existing["name"] != record["name"]:
            self.store.execute("UPDATE messages SET group_name=? WHERE group_name=?", (record["name"], existing["name"]))
            with self.store.lock:
                rules = self.store.meta("sender_rules", {})
                self.store.set_meta("sender_rules", {key: record["name"] if value == existing["name"] else value
                                                     for key, value in rules.items()})

    def delete_group(self, group_id):
        removed = self.accounts.delete_group(group_id)
        self.store.execute("UPDATE messages SET group_name=NULL, group_manual=0, ai_at=NULL WHERE group_name=?", (removed["name"],))
        with self.store.lock:
            rules = self.store.meta("sender_rules", {})
            self.store.set_meta("sender_rules", {key: value for key, value in rules.items() if value != removed["name"]})

    def record_sent(self, account, item, message_id):
        now = int(time.time())
        key = "s" + hashlib.sha256(message_id.encode()).hexdigest()[:20]
        self.store.add_message({"id": key, "account_id": account["id"], "folder": "SENT", "validity": "local",
                                "uid": int(hashlib.sha256(message_id.encode()).hexdigest()[:12], 16), "received": now, "sent": now,
                                "from_name": account.get("sender_name") or "", "from_addr": account["address"],
                                "to_addrs": ", ".join(item["to"]), "subject": item["subject"], "text": item["body"],
                                "text_at": now, "snippet": item["body"][:300], "message_id": message_id, "read": 1,
                                "html": "", "html_inline": {}})

    def mail_preferences(self):
        return {
            **{"read_on_open": True, "next_after_archive": True, "code_archive_minutes": 0, "show_images": True},
            **self.store.meta("mail_preferences", {})}

    def save_mail_preferences(self, payload):
        settings = self.mail_preferences()
        for key in ("read_on_open", "next_after_archive", "show_images"):
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise WorkerError("阅读偏好必须为开关值")
                settings[key] = payload[key]
        if "code_archive_minutes" in payload:
            if type(payload["code_archive_minutes"]) is not int or payload["code_archive_minutes"] not in (0, 10, 1440):
                raise WorkerError("验证码归档时间应为关闭、10 分钟或 24 小时")
            settings["code_archive_minutes"] = payload["code_archive_minutes"]
        self.store.set_meta("mail_preferences", settings)
        return {"mail_preferences": settings}

    def mark_messages(self, payload):
        changes = {}
        for key in ("read", "starred", "archived", "deleted"):
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise WorkerError("邮件状态必须为开关值")
                changes[key] = int(payload[key])
        if "snoozed_until" in payload:
            value = payload["snoozed_until"]
            if type(value) is not int or value < 0 or value > time.time() + 366 * 86400:
                raise WorkerError("提醒时间无效")
            changes["snoozed_until"] = value
        if "group" in payload:
            group = str(payload["group"] or "")
            if group and group not in [g["name"] for g in self.accounts.groups()]:
                raise WorkerError("没有这个分组")
            changes.update(group_name=group or None, group_manual=int(bool(group)))
        return {"ok": True, "count": self.store.bulk_mark(payload.get("ids"), changes)}

    def sender_rule(self, payload):
        message = self.store.message(str(payload.get("id") or ""))
        if not message or not message.get("from_addr"):
            raise WorkerError("没有可用的发件人地址")
        group = str(payload.get("group") or "")
        if group and group not in [g["name"] for g in self.accounts.groups()]:
            raise WorkerError("没有这个分组")
        with self.store.lock:
            rules = self.store.meta("sender_rules", {})
            key = message["account_id"] + ":" + message["from_addr"].lower()
            if group:
                rules[key] = group
            else:
                rules.pop(key, None)
            self.store.set_meta("sender_rules", rules)
            if group:
                self.store.execute("UPDATE messages SET group_name=?, group_manual=1 WHERE account_id=? AND LOWER(from_addr)=?",
                                   (group, message["account_id"], message["from_addr"].lower()))
        return {"ok": True}

    def mail_maintenance(self):
        now = int(time.time())
        self.refresh_attention()
        minutes = self.mail_preferences()["code_archive_minutes"]
        if minutes:
            self.store.execute("UPDATE messages SET archived=1 WHERE code<>'' AND received<=? AND starred=0 AND deleted=0 "
                               "AND snoozed_until=0 AND id NOT IN (SELECT message_id FROM submissions)", (now - minutes * 60,))
        with self.store.lock:
            for message in self.store.rows("SELECT id, subject FROM messages WHERE snoozed_until>0 AND snoozed_until<=? AND deleted=0", (now,)):
                self.store.update_message(message["id"], snoozed_until=0, read=0, archived=0)
                notice_id = "mail-remind-" + message["id"] + "-" + str(now)
                self.store.add_notice(notice_id, "", [message["id"]])
                self.store.add_event("notice", {"channels": ["app"], "kind": "mail", "notice": {
                    "id": notice_id, "at": now, "title": "邮件提醒",
                    "message": message["subject"], "source": "mail", "severity": "info", "origin": "mail:reminder"}})

    # ------------------------------------------------------------ overview and health

    def overview(self):
        public = self.accounts.public()
        counts = self.store.account_counts()
        for account in public["accounts"]:
            link = self.links.get(account["id"])
            account["status"] = dict(link.status) if link else {"state": "paused" if not account["enabled"] else "stopped"}
            if link:
                account["status"]["junk"] = imap_link.decode_folder(link.status.get("junk") or "")
                account["status"]["backfilling"] = bool(link.backfill or not link.jobs.empty())
            account["counts"] = counts.get(account["id"], {"total": 0, "unread": 0})
        public["labels"] = self.store.label_counts()
        public["group_counts"] = self.store.group_counts()
        public["group_unread"] = self.store.group_counts(unread=True)
        public["mail_preferences"] = self.mail_preferences()
        public["latest_codes"] = [m for m in self.store.messages(since=int(time.time()) - 600, folder="INBOX", limit=200) if m.get("code")][:5]
        public["sender_rules"] = self.store.meta("sender_rules", {})
        public["attention_rules"] = self.store.meta("attention_rules", {})
        public["attention_counts"] = self.store.attention_counts()
        public["quiet_senders"] = self.store.quiet_senders()
        public["ai"]["has_key"] = self.ai.has_key()
        public["tasks"] = []
        for task in self.tasks():
            view = self.task_view(task["id"])
            public["tasks"].append({"id": task["id"], "name": task["config"]["name"], "enabled": task["enabled"], "counts": view["counts"],
                                    "ended": task["config"].get("ended") or 0,
                                    "deadline": task["config"].get("deadline") or 0, "account_id": task["config"]["account_id"]})
        public["unread"] = sum(a["counts"]["unread"] for a in public["accounts"])
        public["health"] = self.health()
        return public

    def health(self):
        rss = 0
        try:
            with open("/proc/self/status") as stream:
                rss = next((int(line.split()[1]) * 1024 for line in stream if line.startswith("VmRSS:")), 0)
        except OSError:
            pass
        disk = shutil.disk_usage(self.data)
        mailboxes = []
        for account in self.accounts.accounts():
            link = self.links.get(account["id"])
            status = dict(link.status) if link else {"state": "paused" if not account.get("enabled", True) else "stopped"}
            mailboxes.append({"id": account["id"], "address": account["address"], "label": account.get("label", ""), "enabled": account.get("enabled", True),
                              "state": status.get("state"), "error": status.get("error", ""), "error_since": status.get("error_since", 0),
                              "last_ok": status.get("last_ok", 0)})
        return {"version": VERSION, "started": self.started, "now": int(time.time()), "mailboxes": mailboxes, "memory": rss,
                "disk_free": disk.free, "last_event": self.store.last_event(), "night_running": self.night_running.is_set(),
                "night_last": self.store.meta("night_last", {}), "live_last": self.store.meta("live_last", {})}
