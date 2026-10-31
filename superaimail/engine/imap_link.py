"""One long-lived IMAP session per mailbox, plus the reading helpers shared with on-demand requests.

The session keeps the inbox selected read-only and looks for new mail every few seconds, the way the panel did
before. Every few minutes it also reads the junk folder (mail from outside senders with attachments often lands
there), and between looks it works through queued backfills in small slices so new mail is never held up.
Nothing is ever marked read, moved or deleted on the server.
"""
import base64
import email
import imaplib
import queue
import re
import socket
import threading
import time

from . import mail_html
from . import mime
from . import net
from . import oauth

POLL, JUNK_EVERY, BACKFILL_SLICE = 3, 300, 25
RETRY = (20, 60, 120, 300)
TEXT_FETCH, CHUNK = 200 * 1024, 1024 * 1024
HTML_FETCH = 900 * 1024
FIRST_LOOK_DAYS = 60
HEADER_ITEMS = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE)]"
imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))          # NetEase servers refuse SELECT until the client identifies itself
imaplib._MAXLINE = 4 * 1024 * 1024


class LinkError(Exception):
    pass


def describe(error):
    if isinstance(error, LinkError):
        return str(error)
    if isinstance(error, net.NetError):
        return str(error)
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "连接邮箱服务器超时"
    if isinstance(error, (socket.gaierror, ConnectionError, OSError)):
        return "连不上邮箱服务器，请检查服务器地址、端口或出口代理"
    if isinstance(error, imaplib.IMAP4.error):
        return "邮箱服务器拒绝了请求"
    return "检查失败"


def connect(account, proxy):
    client = net.IMAP(account["imap_host"], account["imap_port"], proxy=proxy)
    try:
        oauth.imap_login(client, account)
    except imaplib.IMAP4.error:
        close(client)
        raise LinkError("登录失败：账号或授权码不对，或邮箱没有开启 IMAP") from None
    try:
        client._simple_command("ID", '("name" "mail-worker" "version" "1.0")')
    except Exception:
        pass
    return client


def close(client):
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        pass


def decode_folder(name):
    """Modified UTF-7 folder names (RFC 3501) as text, e.g. &V4NXPpCuTvY- -> 垃圾邮件."""
    def piece(match):
        data = match.group(1)
        if not data:
            return "&"
        data = data.replace(",", "/")
        return base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-16-be", "replace")
    return re.sub(r"&([A-Za-z0-9+,]*)-", piece, name)


def quote(name):
    return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


def find_junk(client):
    """The junk folder's raw name, from the \\Junk flag or a recognisable name; '' when there is none."""
    try:
        status, rows = client.list()
    except imaplib.IMAP4.error:
        return ""
    named = ""
    for row in rows or []:
        line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
        found = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if not found:
            continue
        name = found.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"')
        if "\\junk" in found.group("flags").lower():
            return name
        if not named and re.search(r"junk|spam|垃圾", decode_folder(name), re.I):
            named = name
    return named


def select(client, folder):
    status, data = client.select(quote(folder), readonly=True)
    if status != "OK":
        raise LinkError("打不开文件夹 " + decode_folder(folder))
    validity = (client.response("UIDVALIDITY")[1] or [b""])[0]
    return validity.decode("ascii", "replace") if isinstance(validity, bytes) else str(validity or "")


def search(client, criteria):
    status, data = client.uid("SEARCH", None, criteria)
    if status != "OK":
        raise LinkError("邮箱服务器不接受查询")
    return sorted(int(x) for x in (data[0] or b"").split())


def since(ts):
    return "SINCE " + time.strftime("%d-%b-%Y", time.gmtime(ts - 86400))


def _body_key(item):
    return next((v for k, v in item.items() if k.startswith("BODY[")), None)


def fetch_messages(client, uids):
    """Headers, structure and the start of the text of each message. Returns dicts ready for the store."""
    if not uids:
        return []
    status, data = client.uid("FETCH", ",".join(str(u) for u in uids), "(UID INTERNALDATE RFC822.SIZE BODYSTRUCTURE %s)" % HEADER_ITEMS)
    if status != "OK":
        raise LinkError("读取邮件失败")
    found = []
    for item in mime.parse_fetch(data):
        try:
            uid = int(item.get("UID") or 0)
        except (TypeError, ValueError):
            continue
        if not uid:
            continue
        headers = email.message_from_bytes(bytes(_body_key(item) or b""))
        parts = mime.walk(item.get("BODYSTRUCTURE"))
        name, address = mime.sender(headers.get("From"))
        message = {"uid": uid, "received": mime.internal_date(item.get("INTERNALDATE")), "sent": mime.sent_date(headers.get("Date")),
                   "from_name": name, "from_addr": address, "to_addrs": ",".join(mime.address_list(", ".join(filter(None, [headers.get("To"), headers.get("Cc")])))),
                   "subject": mime.header(headers.get("Subject")) or "（无主题）", "size": int(mime.text(item.get("RFC822.SIZE")) or 0),
                   "unsubscribe": mime.header(headers.get("List-Unsubscribe"), 4000),
                   "message_id": mime.header(headers.get("Message-ID"), 300), "attachments": mime.attachments(parts)}
        message["received"] = message["received"] or message["sent"] or int(time.time())
        body = mime.body_part(parts)
        message["body_part"] = body["part"] if body else ""
        if parts:
            reading = read_reading(client, uid, parts, body)
            message.update(reading)
        else:
            text, cut = read_whole(client, uid, message)
            message.update(text=text, text_cut=cut, html="", html_inline={})
        found.append(message)
    return found


def read_reading(client, uid, parts, body=None):
    """text, text_cut, html (cleaned, may be empty) and html_inline of one message whose parts are known. An HTML-only
    message's text is taken from its cleaned HTML; otherwise the plain part is the text and the HTML part is kept apart."""
    body = body if body is not None else mime.body_part(parts)
    rich = mime.html_part(parts)
    cids = {p["cid"]: p["part"] for p in parts if p.get("cid")}
    stored = ""
    if rich and body and body["part"] == rich["part"]:
        markup, cut = read_html(client, uid, rich)
        stored = mail_html.clean(markup, cids)
        text = mail_html.text(stored)[:20000]
    else:
        text, cut = read_text(client, uid, body)
        if rich:
            stored = mail_html.clean(read_html(client, uid, rich)[0], cids)
    return {"text": text, "text_cut": cut, "html": stored, "html_inline": mime.inline_parts(stored, parts)}


def fetch_reading(client, uid):
    """read_reading for a message indexed before HTML was kept: reads its structure first. None if it is gone."""
    status, data = client.uid("FETCH", str(uid), "(BODYSTRUCTURE)")
    items = mime.parse_fetch(data) if status == "OK" else []
    if not items:
        return None
    parts = mime.walk(items[0].get("BODYSTRUCTURE"))
    return dict(read_reading(client, uid, parts), body_part=(mime.body_part(parts) or {}).get("part", "")) if parts else None


def read_html(client, uid, part):
    """The decoded HTML part, at most HTML_FETCH bytes of it, and whether it was longer."""
    status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[%s]<0.%d>)" % (part["part"], HTML_FETCH))
    items = mime.parse_fetch(data) if status == "OK" else []
    raw = bytes(_body_key(items[0]) or b"") if items else b""
    return mime.decode_text(raw, part["encoding"], part["charset"]), len(raw) >= HTML_FETCH


def read_text(client, uid, body):
    if not body:
        return "", False
    status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[%s]<0.%d>)" % (body["part"], TEXT_FETCH))
    items = mime.parse_fetch(data) if status == "OK" else []
    raw = bytes(_body_key(items[0]) or b"") if items else b""
    value = mime.decode_text(raw, body["encoding"], body["charset"])
    if body["type"] == "text/html":
        value = mime.plain_text(value)
    return mime.tidy(value)[:20000], len(raw) >= TEXT_FETCH


def read_whole(client, uid, message):
    """Fallback when a server sends no usable BODYSTRUCTURE: the first 512 KB, parsed with the email package."""
    status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[]<0.524288>)")
    items = mime.parse_fetch(data) if status == "OK" else []
    raw = bytes(_body_key(items[0]) or b"") if items else b""
    parsed = email.message_from_bytes(raw)
    plain, rich = "", ""
    for part in parsed.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            value = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, AttributeError):
            value = ""
        if part.get_content_subtype() == "html":
            rich = rich or value
        else:
            plain = plain or value
    return (mime.tidy(plain) or mime.tidy(mime.plain_text(rich)))[:20000], len(raw) >= 524288


def stream_part(client, uid, part, encoding, write, limit):
    """Copy one decoded part to `write`, a megabyte of the encoded form at a time. Returns the decoded size."""
    decoder, offset, total = mime.Decoder(encoding), 0, 0
    while True:
        status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[%s]<%d.%d>)" % (part, offset, CHUNK))
        items = mime.parse_fetch(data) if status == "OK" else []
        raw = _body_key(items[0]) if items else None
        if raw is None:
            if offset == 0:
                raise LinkError("邮箱服务器没有返回这个附件")
            break
        raw = bytes(raw)
        piece = decoder.feed(raw)
        total += len(piece)
        if total > limit:
            raise LinkError("附件超过 %d MB 上限" % (limit // 1024 // 1024))
        if piece:
            write(piece)
        offset += len(raw)
        if len(raw) < CHUNK:
            break
    rest = decoder.finish()
    if rest:
        write(rest)
        total += len(rest)
    return total


class Link(threading.Thread):
    """Watches one mailbox until it is paused, removed or the worker stops."""

    def __init__(self, worker, account_id):
        super().__init__(daemon=True, name="link-" + account_id)
        self.worker, self.account_id = worker, account_id
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.jobs = queue.Queue()
        self.status = {"state": "connecting", "error": "", "error_since": 0, "last_ok": 0, "failures": 0, "last_mail": 0, "junk": ""}
        self.selected = None                                     # (folder, UIDVALIDITY)
        self.backfill = None                                     # job in progress: {folder, uids}

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def queue_backfill(self, folder, since_ts):
        """folder '' means the inbox and the junk folder. The request is also written down, so a restart
        part-way through starts it again instead of leaving the history half indexed."""
        self._remember(since_ts)
        self.jobs.put({"folder": folder, "since": int(since_ts)})
        self.wake.set()

    def _remember(self, since_ts):
        key = "backfill:" + self.account_id
        pending = self.worker.store.meta(key)
        self.worker.store.set_meta(key, int(min(since_ts, pending)) if pending else int(since_ts))

    def queue_collect(self, task_id, items):
        """File messages the nightly AI check picked for a task: [(message id, (person, reason))]."""
        self.jobs.put({"collect": task_id, "items": list(items)})
        self.wake.set()

    def _set(self, **changes):
        self.status.update(changes)

    def _select(self, client, folder):
        if not self.selected or self.selected[0] != folder:
            self.selected = (folder, select(client, folder))
        return self.selected[1]

    def run(self):
        client, opened_with, failures, junk_at = None, None, 0, 0
        pending = self.worker.store.meta("backfill:" + self.account_id)
        if pending:
            self.jobs.put({"folder": "", "since": int(pending)})       # interrupted by a restart: start it again
        while not self.stopping.is_set():
            account = self.worker.accounts.account(self.account_id)
            if not account or not account.get("enabled", True):
                self._set(state="paused", error="")
                break
            key = tuple(account.get(k) for k in ("address", "imap_host", "imap_port", "secret", "proxy_id"))
            if client is not None and opened_with != key:
                close(client)
                client, self.selected = None, None
            wait = POLL
            try:
                if client is None:
                    self._set(state="connecting")
                    client = connect(self.worker.oauth.prepare(account), self.worker.accounts.proxy(account.get("proxy_id")))
                    opened_with, self.selected = key, None
                    self._set(junk=find_junk(client))
                else:
                    client.noop()
                self._check(client, account, "INBOX", live=True)
                if self.status["junk"] and time.time() - junk_at >= JUNK_EVERY:
                    self._check(client, account, self.status["junk"], live=False)
                    junk_at = time.time()
                busy = self._backfill(client, account)
                if self.selected and self.selected[0] != "INBOX":
                    self._select(client, "INBOX")
                failures = 0
                self._set(state="online", error="", error_since=0, failures=0, last_ok=int(time.time()))
                wait = 0.2 if busy else POLL
            except Exception as error:
                close(client)
                client, self.selected = None, None
                failures += 1
                self._set(state="error", error=describe(error), error_since=self.status["error_since"] or int(time.time()), failures=failures)
                self.worker.log("mailbox %s: %s (%s)" % (self.account_id, describe(error), type(error).__name__))
                wait = RETRY[min(failures, len(RETRY)) - 1]
            self.wake.wait(wait)
            self.wake.clear()
        close(client)

    def _check(self, client, account, folder, live):
        validity = self._select(client, folder)
        known = self.worker.store.folder(account["id"], folder)
        if known is None or known["validity"] != validity:
            uids = search(client, "ALL")
            self.worker.store.set_folder(account["id"], folder, validity, uids[-1] if uids else 0)
            self._remember(int(time.time()) - FIRST_LOOK_DAYS * 86400)
            self.jobs.put({"folder": folder, "since": int(time.time()) - FIRST_LOOK_DAYS * 86400})
            return
        last = known["last_uid"] or 0
        fresh = [uid for uid in search(client, "UID %d:*" % (last + 1)) if uid > last]
        for start in range(0, len(fresh), 20):
            batch = fresh[start:start + 20]
            self.worker.ingest(client, account, folder, validity, batch, live)
            self.worker.store.set_folder(account["id"], folder, validity, batch[-1])
            if live:
                self._set(last_mail=int(time.time()))

    def _backfill(self, client, account):
        """Work through one slice of a queued backfill. Returns True while more remains."""
        if self.backfill is None:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return False
            if "collect" in job:
                self.selected = None                             # the job selects whichever folder each message is in
                self.worker.collect_suggested(client, account, job["collect"], job["items"])
                return not self.jobs.empty()
            folders = [job["folder"]] if job["folder"] else ["INBOX"] + ([self.status["junk"]] if self.status["junk"] else [])
            for folder in folders[1:]:
                self.jobs.put({"folder": folder, "since": job["since"]})
            folder = folders[0]
            validity = self._select(client, folder)
            self.backfill = {"folder": folder, "validity": validity, "uids": search(client, since(job["since"]))}
        job = self.backfill
        validity = self._select(client, job["folder"])
        if validity != job["validity"]:
            self.backfill = None
            return not self.jobs.empty()
        batch, job["uids"] = job["uids"][:BACKFILL_SLICE], job["uids"][BACKFILL_SLICE:]
        if batch:
            self.worker.ingest(client, account, job["folder"], validity, batch, live=False)
        if not job["uids"]:
            self.backfill = None
        more = bool(job["uids"]) or not self.jobs.empty()
        if not more:
            self.worker.store.set_meta("backfill:" + self.account_id, 0)
        return more
