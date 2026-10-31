"""Reading IMAP replies and message structure without downloading whole messages.

* `parse_fetch` turns imaplib FETCH data into one dict per message.
* `walk` lists the parts of a BODYSTRUCTURE with their IMAP part numbers, so a single attachment can be
  fetched on its own, in slices.
* `Decoder` undoes base64 / quoted-printable a slice at a time.
* `safe_name` makes a file or folder name that Windows, Android and ZIP readers all accept.
"""
import base64
import binascii
import email.header
import email.utils
import html
import re
import urllib.parse
from datetime import datetime

from . import mail_html


class Literal(bytes):
    """A string that arrived as an IMAP literal ({n} followed by n bytes)."""


class Quoted(bytes):
    pass


_ATOM_END = b' ()"{'


def _lex_bytes(text, out):
    i, n = 0, len(text)
    while i < n:
        c = text[i:i + 1]
        if c in b" \r\n\t":
            i += 1
        elif c in b"()":
            out.append(c.decode())
            i += 1
        elif c == b'"':
            j, buf = i + 1, bytearray()
            while j < n and text[j:j + 1] != b'"':
                if text[j:j + 1] == b"\\" and j + 1 < n:
                    j += 1
                buf += text[j:j + 1]
                j += 1
            out.append(Quoted(bytes(buf)))
            i = j + 1
        else:
            j, depth = i, 0
            while j < n:
                ch = text[j:j + 1]
                if ch == b"[":
                    depth += 1
                elif ch == b"]":
                    depth -= 1
                elif depth <= 0 and (ch in _ATOM_END or ch in b"\r\n\t"):
                    break
                j += 1
            atom = text[i:j].decode("utf-8", "replace")
            out.append(None if atom.upper() == "NIL" else atom)
            i = j


def _tokens(data):
    out = []
    for item in data or []:
        if isinstance(item, tuple):
            head = re.sub(rb"\{\d+\}\s*$", b"", item[0].rstrip())
            _lex_bytes(head, out)
            out.append(Literal(item[1] or b""))
        elif isinstance(item, (bytes, bytearray)):
            _lex_bytes(bytes(item), out)
    return out


def _tree(tokens):
    stack, top = [], []
    current = top
    for token in tokens:
        if token == "(":
            child = []
            current.append(child)
            stack.append(current)
            current = child
        elif token == ")":
            if stack:
                current = stack.pop()
        else:
            current.append(token)
    return top


def parse_fetch(data):
    """FETCH data → list of dicts keyed by upper-case item name (UID, BODYSTRUCTURE, BODY[...] ...)."""
    tree, messages = _tree(_tokens(data)), []
    for index, node in enumerate(tree):
        if isinstance(node, list) and index > 0 and isinstance(tree[index - 1], str) and tree[index - 1].isdigit():
            item = {}
            for k in range(0, len(node) - 1, 2):
                key = node[k]
                if isinstance(key, str):
                    item[re.sub(r"<\d+>$", "", key.upper())] = node[k + 1]
            messages.append(item)
    return messages


def text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        for encoding in ("utf-8", "gb18030"):
            try:
                return bytes(value).decode(encoding)
            except UnicodeDecodeError:
                continue
        return bytes(value).decode("latin-1")
    return ""


def header(value, limit=300):
    """An RFC 2047 encoded header as readable text."""
    raw = text(value)
    try:
        decoded = str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        decoded = raw
    return "".join(c for c in decoded if ord(c) >= 32).strip()[:limit]


def _params(node):
    """A parameter list such as ("name" "a.xlsx" "charset" "utf-8") as a dict, with RFC 2231 forms joined."""
    pairs = {}
    if isinstance(node, list):
        for k in range(0, len(node) - 1, 2):
            pairs[text(node[k]).lower()] = text(node[k + 1])
    result = {}
    pieces = {}
    for key, value in pairs.items():
        match = re.fullmatch(r"([a-z-]+)\*(\d+)(\*?)", key)
        if match:
            pieces.setdefault(match.group(1), []).append((int(match.group(2)), value, bool(match.group(3))))
        elif key.endswith("*"):
            result[key[:-1]] = _rfc2231(value, True)
        else:
            result.setdefault(key, value)
    for name, parts in pieces.items():
        parts.sort()
        encoded = parts[0][2]
        joined = "".join(value for _, value, _ in parts)
        result[name] = _rfc2231(joined, encoded)
    return result


def _rfc2231(value, encoded):
    if not encoded:
        return value
    charset, _, rest = value.partition("'")
    _, _, data = rest.partition("'")
    if not data and not rest:
        data, charset = value, "utf-8"
    try:
        return urllib.parse.unquote(data, encoding=charset or "utf-8", errors="replace")
    except LookupError:
        return urllib.parse.unquote(data, errors="replace")


def walk(structure, prefix=""):
    """Every leaf part of a BODYSTRUCTURE as a dict: part, type, charset, encoding, size, filename, disposition, cid."""
    if not isinstance(structure, list) or not structure:
        return []
    if isinstance(structure[0], list):
        found = []
        for number, child in enumerate(structure, 1):
            if not isinstance(child, list):
                break                                     # the children come first, then the multipart subtype
            found.extend(walk(child, "%s.%d" % (prefix, number) if prefix else str(number)))
        return found
    part = prefix or "1"
    kind, sub = text(structure[0]).lower(), text(structure[1]).lower() if len(structure) > 1 else ""
    params = _params(structure[2]) if len(structure) > 2 else {}
    cid = text(structure[3]) if len(structure) > 3 else ""
    encoding = (text(structure[5]) if len(structure) > 5 else "") or "7bit"
    try:
        size = int(text(structure[6])) if len(structure) > 6 else 0
    except ValueError:
        size = 0
    extension = 7 + (1 if kind == "text" else 3 if (kind, sub) == ("message", "rfc822") else 0)
    disposition, dparams = "", {}
    if len(structure) > extension + 1 and isinstance(structure[extension + 1], list) and structure[extension + 1]:
        disposition = text(structure[extension + 1][0]).lower()
        dparams = _params(structure[extension + 1][1]) if len(structure[extension + 1]) > 1 else {}
    filename = header(dparams.get("filename") or params.get("name") or "", 255)
    if (kind, sub) == ("message", "rfc822") and not filename:
        filename = "转发的邮件.eml"
    return [{"part": part, "type": kind + "/" + sub, "charset": params.get("charset", ""), "encoding": encoding.lower(),
             "size": size, "filename": filename, "disposition": disposition, "cid": cid}]


def attachments(parts):
    """Parts a person would call attachments. Inline images (signatures, pasted pictures) are kept but marked."""
    found = []
    for item in parts:
        if not item["filename"]:
            continue
        if item["type"] in ("text/plain", "text/html") and item["disposition"] != "attachment":
            continue
        inline = item["disposition"] == "inline" or (item["disposition"] != "attachment" and item["type"].startswith("image/") and bool(item["cid"]))
        found.append({"part": item["part"], "name": item["filename"], "type": item["type"], "size": decoded_size(item),
                      "encoding": item["encoding"], "inline": inline})
    return found


def decoded_size(item):
    return item["size"] * 3 // 4 if item["encoding"] == "base64" else item["size"]


def html_part(parts):
    """The HTML part shown when reading, if the message has one."""
    return next((p for p in parts if p["type"] == "text/html" and p["disposition"] != "attachment" and not p["filename"]), None)


def inline_parts(stored, parts):
    """{part: {type, encoding}} of the inline images the cleaned HTML refers to (as inline:<part>)."""
    wanted = set(re.findall(r'inline:([0-9.]+)', stored or ""))
    return {p["part"]: {"type": p["type"], "encoding": p["encoding"]} for p in parts if p["part"] in wanted and p["type"].startswith("image/")}


def body_part(parts):
    """The part holding the message text: the first plain-text part, else the first HTML part."""
    plain = next((p for p in parts if p["type"] == "text/plain" and p["disposition"] != "attachment" and not p["filename"]), None)
    return plain or next((p for p in parts if p["type"] == "text/html" and p["disposition"] != "attachment" and not p["filename"]), None)


class Decoder:
    """Undo a content transfer encoding a slice at a time."""

    def __init__(self, encoding):
        self.encoding = (encoding or "").lower()
        self.rest = b""

    def feed(self, data):
        if self.encoding == "base64":
            data = self.rest + re.sub(rb"[^A-Za-z0-9+/=]", b"", data)
            cut = len(data) - len(data) % 4
            self.rest = data[cut:]
            return _b64(data[:cut])
        if self.encoding == "quoted-printable":
            data = self.rest + data
            cut = data.rfind(b"\n") + 1
            self.rest = data[cut:]
            return binascii.a2b_qp(data[:cut])
        return data

    def finish(self):
        rest, self.rest = self.rest, b""
        if not rest:
            return b""
        if self.encoding == "base64":
            return _b64(rest + b"=" * (-len(rest) % 4))
        if self.encoding == "quoted-printable":
            return binascii.a2b_qp(rest)
        return rest


def _b64(data):
    if not data:
        return b""
    try:
        return base64.b64decode(data)
    except (binascii.Error, ValueError):
        cleaned = data.split(b"=")[0]
        return base64.b64decode(cleaned[: len(cleaned) - len(cleaned) % 4] or b"")


def decode_text(raw, encoding, charset):
    decoder = Decoder(encoding)
    data = decoder.feed(raw) + decoder.finish()
    for name in (charset or "utf-8", "utf-8", "gb18030"):
        try:
            return data.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def plain_text(markup):
    """The words of an HTML message, one line per paragraph, row or line break."""
    value = re.sub(r"(?is)<(style|script|head|title)\b.*?</\1\s*>|<!--.*?-->", " ", markup or "")
    value = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|blockquote|section|article|ul|ol|pre)\s*>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", " ", value))


def tidy(value):
    value = mail_html.SPACES.sub(" ", mail_html.INVISIBLE.sub("", value or ""))
    lines = [re.sub(r"[ \t ​-‍﻿]+", " ", line).strip() for line in (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    value = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return "".join(c for c in value if c == "\n" or ord(c) >= 32)


_CODE_WORDS = "验证码|校验码|动态码|确认码|安全码|验证代码|登录代码|临时代码|动态口令|verification code|security code|one-time code|passcode"


def verification_code(value):
    """A code next to a word such as 验证码 or "verification code"; empty when there is none."""
    value = value or ""
    found = re.search(r"(?:%s)[^0-9A-Za-z]{0,24}([0-9]{4,8})(?![0-9A-Za-z])" % _CODE_WORDS, value, re.I)
    if not found:
        found = re.search(r"(?<![0-9A-Za-z])([0-9]{4,8})(?![0-9A-Za-z])[^0-9。\n]{0,18}(?:验证码|校验码|动态码|is your)", value, re.I)
    if not found:
        found = re.search(r"(?:验证码|校验码|verification code|security code)[^0-9A-Za-z]{0,24}([A-Z0-9]{5,8})(?![A-Za-z0-9])", value)
        if found and not re.search(r"[0-9]", found.group(1)):
            found = None
    return found.group(1) if found else ""


_BIG_ATTACHMENT = re.compile(r"超大附件|大附件|云附件|文件中转站|附件下载地址|下载附件|ftn\.qq\.com|wx\.mail\.qq\.com/ftn|"
                             r"mail\.163\.com/large|attach\.qiye\.163\.com|dl\.qiye\.163\.com|large attachment", re.I)


def has_link_attachment(value):
    return bool(_BIG_ATTACHMENT.search(value or ""))


def address_list(value):
    return [address.lower() for _, address in email.utils.getaddresses([header(value, 2000)]) if "@" in address][:30]


def sender(value):
    name, address = email.utils.parseaddr(header(value, 400))
    return name.strip().strip('"') or "", address.lower()


def internal_date(value):
    """INTERNALDATE such as 17-Jul-2026 02:44:25 +0800 as a Unix time; 0 when it cannot be read."""
    try:
        return int(datetime.strptime(text(value).strip(), "%d-%b-%Y %H:%M:%S %z").timestamp())
    except ValueError:
        return 0


def sent_date(value):
    try:
        return int(email.utils.parsedate_to_datetime(header(value)).timestamp())
    except Exception:
        return 0


_RESERVED = {"CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)), *("LPT%d" % i for i in range(1, 10))}


def safe_name(name, limit=100, fallback="未命名"):
    """A single path component that is valid on Windows and cannot climb out of its folder."""
    value = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in str(name or "")).strip().strip(".").strip()
    value = re.sub(r"\s+", " ", value)
    if not value or value in (".", ".."):
        value = fallback
    stem, dot, ext = value.rpartition(".")
    if not dot or len(ext) > 10 or not stem:
        stem, ext = value, ""
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    if ext:
        stem = stem[: max(1, limit - len(ext) - 1)].rstrip(" .")
        return stem + "." + ext
    return stem[:limit].rstrip(" .") or fallback


def extension(name):
    stem, dot, ext = (name or "").rpartition(".")
    return ext.lower() if dot and stem else ""
