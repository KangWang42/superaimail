"""The worker's SQLite database: indexed mail, folder progress, collection tasks and the event feed the panel reads.

One connection guarded by a lock; the volume is a few thousand messages, so simplicity wins over pooling.
Message text is optional (fetched when first read) and is kept as long as its row, BODY_DAYS, so any indexed mail
opens without asking the mail server again; message rows go after KEEP_DAYS unless a collection task still refers to
them; notice texts after TEXT_DAYS.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib

KEEP_DAYS, TEXT_DAYS, EVENT_DAYS = 180, 90, 14
BODY_DAYS = KEEP_DAYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, folder TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
  received INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0, from_name TEXT, from_addr TEXT, to_addrs TEXT,
  subject TEXT, size INTEGER, message_id TEXT, has_attach INTEGER NOT NULL DEFAULT 0, attachments TEXT NOT NULL DEFAULT '[]',
  body_part TEXT, snippet TEXT, code TEXT, labels TEXT NOT NULL DEFAULT '[]', ai_summary TEXT, link_attach INTEGER NOT NULL DEFAULT 0,
  text TEXT, text_at INTEGER, text_cut INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0,
  group_name TEXT, group_manual INTEGER NOT NULL DEFAULT 0, ai_at INTEGER, ai_tag TEXT,
  UNIQUE (account_id, folder, validity, uid));
CREATE INDEX IF NOT EXISTS messages_by_time ON messages (received DESC);
CREATE INDEX IF NOT EXISTS messages_by_account ON messages (account_id, received DESC);
CREATE TABLE IF NOT EXISTS folders (account_id TEXT, folder TEXT, validity TEXT, last_uid INTEGER, primed_at INTEGER,
  PRIMARY KEY (account_id, folder));
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, created INTEGER, enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, message_id TEXT NOT NULL, person TEXT NOT NULL DEFAULT '',
  pending INTEGER NOT NULL DEFAULT 1, ignored INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0, reason TEXT, flags TEXT NOT NULL DEFAULT '[]',
  received INTEGER NOT NULL, subject TEXT, from_name TEXT, from_addr TEXT, suggest TEXT, UNIQUE (task_id, message_id));
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, submission_id TEXT NOT NULL, name TEXT NOT NULL,
  size INTEGER, sha256 TEXT, path TEXT, part TEXT, error TEXT, display_name TEXT, ai_checked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, hashed TEXT NOT NULL, at INTEGER NOT NULL, mailbox TEXT, message_ids TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS notices_by_hash ON notices (hashed);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0);
"""

LIST_FIELDS = ("id", "account_id", "folder", "received", "sent", "from_name", "from_addr", "subject", "has_attach", "attachments",
               "snippet", "code", "labels", "ai_summary", "ai_tag", "link_attach", "read", "starred", "size", "group_name", "group_manual", "archived", "deleted", "snoozed_until", "attention")
# How much of the owner's attention a mail deserves (core.Worker.attention_of): act needs them to do something, read is worth
# reading, fyi is an automatic notice, low is promotion. NULL until the scheduler fills it in, listed as read meanwhile.
ATTENTION = ("act", "read", "fyi", "low")
LEVEL_SETS = {"focus": ("act", "read"), "quiet": ("fyi", "low"), **{level: (level,) for level in ATTENTION}}


def message_key(account_id, folder, validity, uid):
    return "m" + hashlib.sha1(("%s|%s|%s|%d" % (account_id, folder, validity, uid)).encode()).hexdigest()[:15]


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if new:
            os.chmod(path, 0o600)
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
            if "ai_tag" not in columns:                       # added after the first deployment
                self.db.execute("ALTER TABLE messages ADD COLUMN ai_tag TEXT")

            if "unsubscribe" not in columns:
                self.db.execute("ALTER TABLE messages ADD COLUMN unsubscribe TEXT NOT NULL DEFAULT ''")
            for name in ("archived", "deleted", "snoozed_until"):
                if name not in columns:
                    self.db.execute("ALTER TABLE messages ADD COLUMN " + name + " INTEGER NOT NULL DEFAULT 0")
            if "html" not in columns:                       # the cleaned HTML part, zlib-compressed (mail_html.clean)
                self.db.execute("ALTER TABLE messages ADD COLUMN html BLOB")
                self.db.execute("ALTER TABLE messages ADD COLUMN html_inline TEXT")
            if "attention" not in columns:                  # ai_level is the model's view, attention the one shown (rules first)
                self.db.execute("ALTER TABLE messages ADD COLUMN ai_level TEXT")
                self.db.execute("ALTER TABLE messages ADD COLUMN attention TEXT")

    def bulk_mark(self, ids, changes):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200 or any(not isinstance(i, str) for i in ids):
            raise ValueError("请选择 1 到 200 封邮件")
        if not changes or set(changes) - {"read", "starred", "archived", "deleted", "snoozed_until", "group_name", "group_manual"}:
            raise ValueError("不支持的邮件操作")
        ids = list(dict.fromkeys(ids))
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                marks = ",".join("?" for _ in ids)
                count = self.db.execute("SELECT COUNT(*) FROM messages WHERE id IN (" + marks + ")", ids).fetchone()[0]
                if count != len(ids):
                    raise ValueError("部分邮件已不在索引中，请刷新后重试")
                self.db.execute("UPDATE messages SET " + ",".join(k + "=?" for k in changes) + " WHERE id IN (" + marks + ")",
                                list(changes.values()) + ids)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return len(ids)

    def execute(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def row(self, sql, args=()):
        found = self.rows(sql, args)
        return found[0] if found else None

    # ------------------------------------------------------------ folders

    def folder(self, account_id, folder):
        return self.row("SELECT * FROM folders WHERE account_id=? AND folder=?", (account_id, folder))

    def set_folder(self, account_id, folder, validity, last_uid):
        self.execute("INSERT INTO folders VALUES (?,?,?,?,?) ON CONFLICT (account_id, folder) DO UPDATE SET validity=excluded.validity, "
                     "last_uid=excluded.last_uid", (account_id, folder, validity, last_uid, int(time.time())))

    def forget_account(self, account_id):
        with self.lock:
            self.db.execute("DELETE FROM folders WHERE account_id=?", (account_id,))
            self.db.execute("DELETE FROM messages WHERE account_id=? AND id NOT IN (SELECT message_id FROM submissions)", (account_id,))

    # ------------------------------------------------------------ messages

    def has_message(self, key):
        return self.row("SELECT 1 AS x FROM messages WHERE id=?", (key,)) is not None

    def add_message(self, item):
        item = _pack_html(dict(item))
        columns = list(item)
        values = [json.dumps(item[c], ensure_ascii=False) if isinstance(item[c], (list, dict)) else item[c] for c in columns]
        self.execute("INSERT OR IGNORE INTO messages (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))), values)

    def update_message(self, key, **fields):
        if not fields:
            return
        fields = _pack_html(fields)
        values = [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in fields.values()]
        self.execute("UPDATE messages SET %s WHERE id=?" % ",".join(k + "=?" for k in fields), values + [key])

    def message(self, key):
        found = self.row("SELECT * FROM messages WHERE id=?", (key,))
        return _decode(found) if found else None

    def html(self, key):
        """(cleaned HTML or "" when the mail has none, {part: {type, encoding}} of its inline images); (None, {}) when
        the HTML has not been read yet."""
        found = self.row("SELECT html, html_inline FROM messages WHERE id=?", (key,))
        if not found or found["html"] is None:
            return None, {}
        return zlib.decompress(found["html"]).decode("utf-8") if found["html"] else "", json.loads(found["html_inline"] or "{}")

    def messages(self, account_id="", label="", query="", attach=None, folder="", before=0, since=0, until=0, starred=None,
                 unread=None, group="", task="", limit=50, view="inbox", sender="", before_id="", level=""):
        where, args = self._list_filter(account_id, label, query, attach, folder, before, since, until, starred, unread,
                                        group, task, view, sender, before_id, level)
        sql = "SELECT %s FROM messages%s ORDER BY received DESC, id LIMIT ?" % (",".join(LIST_FIELDS), " WHERE " + " AND ".join(where) if where else "")
        items = [_decode(row) for row in self.rows(sql, args + [max(1, min(int(limit), 200))])]
        filed = self.filed_tasks([item["id"] for item in items])
        for item in items:
            item["tasks"] = filed.get(item["id"], [])
        return items

    def filed_tasks(self, message_ids):
        """{message id: [{"id", "name", "pending"}]}: the collection tasks each mail was filed to (ignored filings left out)."""
        out = {}
        for start in range(0, len(message_ids), 500):
            chunk = message_ids[start:start + 500]
            if not chunk:
                continue
            rows = self.rows("SELECT s.message_id, s.task_id, s.pending, t.config FROM submissions s JOIN tasks t ON t.id=s.task_id "
                             "WHERE s.ignored=0 AND s.message_id IN (%s) ORDER BY s.received" % ",".join("?" * len(chunk)), chunk)
            for row in rows:
                try:
                    name = json.loads(row["config"] or "{}").get("name") or ""
                except ValueError:
                    name = ""
                out.setdefault(row["message_id"], []).append({"id": row["task_id"], "name": name, "pending": bool(row["pending"])})
        return out

    def read_matching(self, **filters):
        """Marks every unread mail the list with these filters shows (all pages) as read; returns how many."""
        where, args = self._list_filter(**dict(filters, unread=True))
        return self.execute("UPDATE messages SET read=1 WHERE " + " AND ".join(where), args).rowcount

    def archive_matching(self, **filters):
        """Archives what the list with these filters shows, except starred mail and mail a collection task filed."""
        where, args = self._list_filter(**dict(filters, view="inbox"))
        where += ["starred=0", "id NOT IN (SELECT message_id FROM submissions)"]
        return self.execute("UPDATE messages SET archived=1 WHERE " + " AND ".join(where), args).rowcount

    def attention_counts(self):
        """{level: {"total", "unread"}} for the inbox as it is listed now."""
        out = {level: {"total": 0, "unread": 0} for level in ATTENTION}
        for row in self.rows("SELECT COALESCE(attention,'read') AS a, COUNT(*) AS n, SUM(CASE WHEN read=0 THEN 1 ELSE 0 END) AS u FROM messages "
                             "WHERE folder='INBOX' AND deleted=0 AND archived=0 AND snoozed_until<=? GROUP BY a", (int(time.time()),)):
            if row["a"] in out:
                out[row["a"]] = {"total": row["n"], "unread": row["u"] or 0}
        return out

    def quiet_senders(self, limit=6):
        """Who sends the unread notices and promotions waiting in the inbox, most first."""
        return self.rows("SELECT MAX(COALESCE(NULLIF(from_name,''),from_addr)) AS name, COUNT(*) AS count FROM messages "
                         "WHERE folder='INBOX' AND deleted=0 AND archived=0 AND snoozed_until<=? AND read=0 AND attention IN ('fyi','low') "
                         "GROUP BY LOWER(from_addr) ORDER BY count DESC LIMIT ?", (int(time.time()), limit))

    def _list_filter(self, account_id="", label="", query="", attach=None, folder="", before=0, since=0, until=0, starred=None,
                     unread=None, group="", task="", view="inbox", sender="", before_id="", level=""):
        where, args = [], []
        if level:
            if level not in LEVEL_SETS:
                raise ValueError("关注度筛选无效")
            where.append("COALESCE(attention,'read') IN (%s)" % ",".join("?" * len(LEVEL_SETS[level]))); args.extend(LEVEL_SETS[level])
        if view not in ("inbox", "archive", "trash", "snoozed", "all", "sent"):
            raise ValueError("邮件视图无效")
        where.append("deleted=?"); args.append(1 if view == "trash" else 0)
        if view in ("inbox", "archive"):
            where.append("archived=?"); args.append(1 if view == "archive" else 0)
            where.append("snoozed_until<=?"); args.append(int(time.time()))
        elif view == "snoozed":
            where.append("snoozed_until>?"); args.append(int(time.time()))
        if view == "sent":
            where.append("folder='SENT'")
        elif view == "inbox":
            where.append("folder<>'SENT'")
        if sender:
            where.append("LOWER(from_addr)=LOWER(?)"); args.append(sender)
        if task:
            where.append("id IN (SELECT message_id FROM submissions WHERE task_id=? AND ignored=0)"); args.append(task)
        if group:
            where.append("COALESCE(group_name, '')=?"); args.append("" if group == "未分组" else group)
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if folder:
            where.append("folder=?"); args.append(folder)
        if label:
            where.append("labels LIKE ? ESCAPE '\\'"); args.append('%' + _like(json.dumps(label, ensure_ascii=False)) + '%')
        if attach is not None:
            where.append("has_attach=?"); args.append(1 if attach else 0)
        if starred is not None:
            where.append("starred=?"); args.append(1 if starred else 0)
        if unread is not None:
            where.append("read=?"); args.append(0 if unread else 1)
        if before:
            if before_id:
                where.append("(received<? OR (received=? AND id>?))"); args.extend([int(before), int(before), before_id])
            else:
                where.append("received<?"); args.append(int(before))
        if since:
            where.append("received>=?"); args.append(int(since))
        if until:
            where.append("received<?"); args.append(int(until))
        for word in (query or "").split()[:6]:
            pattern = "%" + _like(word) + "%"
            where.append("(subject LIKE ? ESCAPE '\\' OR from_name LIKE ? ESCAPE '\\' OR from_addr LIKE ? ESCAPE '\\' OR snippet LIKE ? ESCAPE '\\' "
                         "OR attachments LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            args.extend([pattern] * 6)
        return where, args

    def label_counts(self):
        counts = {}
        for row in self.rows("SELECT labels, COUNT(*) AS n FROM messages WHERE deleted=0 AND archived=0 GROUP BY labels"):
            for label in json.loads(row["labels"] or "[]"):
                counts[label] = counts.get(label, 0) + row["n"]
        return counts

    def group_counts(self, unread=False):
        return {row["g"] or "未分组": row["n"] for row in self.rows("SELECT group_name AS g, COUNT(*) AS n FROM messages "
                "WHERE folder='INBOX' AND deleted=0 AND archived=0 AND snoozed_until<=? " + ("AND read=0 " if unread else "") + "GROUP BY group_name", (int(time.time()),))}

    def senders(self, group="", account_id=""):
        where = "deleted=0 AND archived=0 AND folder='INBOX' AND snoozed_until<=?"
        args = [int(time.time())]
        if group:
            where += " AND COALESCE(group_name,'')=?"; args.append("" if group == "未分组" else group)
        if account_id:
            where += " AND account_id=?"; args.append(account_id)
        return self.rows("SELECT account_id, LOWER(from_addr) AS address, MAX(from_name) AS name, COUNT(*) AS count, "
                         "MIN(received) AS first_received, MAX(received) AS last_received, "
                         "SUM(CASE WHEN received>=? THEN 1 ELSE 0 END) AS last_30_days FROM messages WHERE " + where +
                         " GROUP BY account_id, LOWER(from_addr) ORDER BY count DESC LIMIT 200", [int(time.time())-30*86400]+args)

    def archive_sender(self, account_id, address, group=""):
        if not account_id or not address:
            raise ValueError("请选择邮箱和发件人")
        where = "account_id=? AND LOWER(from_addr)=LOWER(?) AND deleted=0 AND archived=0 AND folder='INBOX' AND snoozed_until<=?"
        args = [account_id, address, int(time.time())]
        if group:
            where += " AND COALESCE(group_name,'')=?"
            args.append("" if group == "未分组" else group)
        return self.execute("UPDATE messages SET archived=1 WHERE " + where, args).rowcount

    def account_counts(self):
        return {row["account_id"]: {"total": row["total"], "unread": row["unread"]} for row in
                self.rows("SELECT account_id, COUNT(*) AS total, SUM(CASE WHEN read=0 AND folder='INBOX' AND archived=0 AND deleted=0 AND snoozed_until=0 THEN 1 ELSE 0 END) AS unread "
                          "FROM messages GROUP BY account_id")}

    # ------------------------------------------------------------ events

    def add_event(self, kind, payload):
        self.execute("INSERT INTO events (at, kind, payload) VALUES (?,?,?)", (int(time.time()), kind, json.dumps(payload, ensure_ascii=False)))

    def events(self, after, limit=100):
        return [dict(row, payload=json.loads(row["payload"])) for row in
                self.rows("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (int(after), limit))]

    def last_event(self):
        found = self.row("SELECT MAX(seq) AS seq FROM events")
        return found["seq"] or 0

    def add_notice(self, notice_id, mailbox, message_ids):
        hashed = hashlib.sha256(("mail:" + notice_id).encode()).hexdigest()
        self.execute("INSERT OR REPLACE INTO notices VALUES (?,?,?,?,?)", (notice_id, hashed, int(time.time()), mailbox, json.dumps(message_ids)))

    def notice(self, identifier):
        found = self.row("SELECT * FROM notices WHERE id=? OR hashed=?", (identifier, identifier))
        return dict(found, message_ids=json.loads(found["message_ids"])) if found else None

    def meta(self, key, default=None):
        found = self.row("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(found["value"]) if found else default

    def set_meta(self, key, value):
        self.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    # ------------------------------------------------------------ AI usage

    def ai_calls(self, day):
        found = self.row("SELECT calls FROM ai_usage WHERE day=?", (day,))
        return found["calls"] if found else 0

    def count_ai_call(self, day):
        self.execute("INSERT INTO ai_usage VALUES (?,1) ON CONFLICT (day) DO UPDATE SET calls=calls+1", (day,))

    # ------------------------------------------------------------ housekeeping

    def purge(self, now=None):
        now = now or time.time()
        with self.lock:
            self.db.execute("UPDATE messages SET text=NULL, text_at=NULL, html=NULL, html_inline=NULL WHERE text_at IS NOT NULL AND text_at<?",
                            (int(now - BODY_DAYS * 86400),))
            self.db.execute("DELETE FROM messages WHERE received<? AND starred=0 AND id NOT IN (SELECT message_id FROM submissions)",
                            (int(now - KEEP_DAYS * 86400),))
            self.db.execute("DELETE FROM events WHERE at<?", (int(now - EVENT_DAYS * 86400),))
            self.db.execute("DELETE FROM notices WHERE at<?", (int(now - TEXT_DAYS * 86400),))


def _like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pack_html(fields):
    """The html field compressed for storage; the inline-image map as JSON."""
    if isinstance(fields.get("html"), str):
        fields["html"] = zlib.compress(fields["html"].encode("utf-8"), 6) if fields["html"] else b""
    if isinstance(fields.get("html_inline"), dict):
        fields["html_inline"] = json.dumps(fields["html_inline"])
    return fields


def _decode(row):
    if "html" in row:
        # Rows carry only whether the HTML is known; the page itself is read with Store.html.
        blob = row.pop("html")
        row["has_html"] = None if blob is None else bool(blob)
        row.pop("html_inline", None)
    for key in ("attachments", "labels"):
        if key in row and isinstance(row[key], str):
            row[key] = json.loads(row[key] or "[]")
    for key in ("has_attach", "link_attach", "read", "starred", "text_cut", "group_manual"):
        if key in row:
            row[key] = bool(row[key])
    return row
