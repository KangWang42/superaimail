"""Collection tasks: gather the attachments of one kind of mail (for example "姓名+评分表") from one mailbox,
work out who sent each, and lay them out in folders the owner's computer can mirror.

Layout under the sync root:
    <mailbox address>/<task name>/01 已提交/<person>/<file>                      latest submission
    <mailbox address>/<task name>/01 已提交/<person>/历史版本/<YYYYMMDD-HHMM>_<file>   earlier ones
    <mailbox address>/<task name>/02 待确认/<YYYYMMDD-HHMM>_<sender>/<file>        not identified yet

Nothing here guesses a person when the evidence is ambiguous: such mail waits in 待确认 until the owner assigns it,
and the sender address is then remembered for later mail. A task keeps collecting after its deadline (late mail is
marked) until the owner ends it.

Archived copies can be renamed: by a template such as "{姓名}_{关键词}", or by the nightly AI pass, which only renames
files whose names say little ("新建 Microsoft Excel 工作表.xlsx"). The original name is always kept with the file.
"""
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import mime

BEIJING = timezone(timedelta(hours=8))
SUBMITTED, PENDING, HISTORY = "01 已提交", "02 待确认", "历史版本"
DEFAULT_EXTENSIONS = ["xlsx", "xls", "csv", "zip", "rar", "7z", "et"]
RENAME = ("template", "ai", "off")
PLACEHOLDERS = ("姓名", "关键词", "任务", "原文件名", "日期")
FILE_LIMIT = 50 * 1024 * 1024
TASK_LIMIT, ROSTER_LIMIT = 50, 500

_PREFIX = re.compile(r"^\s*(?:(?:回复|答复|转发|Re|RE|re|Fw|FW|fw|Fwd|FWD|AW|WG)\s*[:：]\s*)+")
_SEPARATORS = r"[+＋\-—–_、,，.。:：;；\s()（）【】\[\]<>《》\"'“”‘’/\\|]+"
_HONORIFIC = re.compile(r"(老师|教授|专家|博士|主任|医生|研究员|同学)$")
_CHINESE_NAME = re.compile(r"[一-龥·]{2,5}")


class TaskError(ValueError):
    pass


def beijing(ts, fmt="%Y%m%d-%H%M"):
    return datetime.fromtimestamp(ts, BEIJING).strftime(fmt)


def _text(value, limit):
    return "".join(c for c in str(value or "") if ord(c) >= 32)[:limit].strip()


def _words(value, limit=10):
    items = value if isinstance(value, list) else re.split(r"[,，、;；\n]+", str(value or ""))
    found = []
    for item in items:
        item = _text(item, 30)
        if item and item not in found:
            found.append(item)
    return found[:limit]


def parse_roster(value):
    """One person per line: a name, optionally followed by an address. Returns [{name, email}]."""
    lines = value if isinstance(value, list) else str(value or "").splitlines()
    people, seen = [], set()
    for line in lines:
        if isinstance(line, dict):
            name, address = _text(line.get("name"), 20), _text(line.get("email"), 120).lower()
        else:
            address_match = re.search(r"[^\s,，;；<>]+@[^\s,，;；<>]+", str(line))
            address = address_match.group(0).lower() if address_match else ""
            name = _text(re.sub(r"[^\s,，;；<>]+@[^\s,，;；<>]+", " ", str(line)).replace("<", " ").replace(">", " ").strip(" ,，;；\t"), 20)
        if not name or name in seen:
            continue
        seen.add(name)
        people.append({"name": name, "email": address})
    if len(people) > ROSTER_LIMIT:
        raise TaskError("名单最多 %d 人" % ROSTER_LIMIT)
    return people


def build_config(payload, existing, account_ids):
    """Validate a task edit and return its configuration."""
    old = dict(existing or {})
    name = _text(payload.get("name", old.get("name")), 40)
    if not name:
        raise TaskError("请填写任务名称")
    account_id = str(payload.get("account_id", old.get("account_id")) or "")
    if account_id not in account_ids:
        raise TaskError("请选择收件邮箱")
    keywords = _words(payload.get("keywords", old.get("keywords", [])))
    if not keywords:
        raise TaskError("请至少填写一个主题关键词")

    def stamp(key, required):
        value = payload.get(key, old.get(key, 0))
        if value in (None, ""):
            value = 0
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            raise TaskError("时间格式无效") from None
        if required and value <= 0:
            raise TaskError("请填写开始时间")
        return value

    start, deadline = stamp("start", True), stamp("deadline", False)
    if deadline and deadline <= start:
        raise TaskError("截止时间应晚于开始时间")
    try:
        remind = int(payload.get("remind_hours", old.get("remind_hours", 48)))
    except (TypeError, ValueError):
        raise TaskError("提前提醒需为小时数") from None
    if not 0 <= remind <= 24 * 30:
        raise TaskError("提前提醒应在 0 到 720 小时之间")
    extensions = [e.lower().lstrip(".") for e in _words(payload.get("extensions", old.get("extensions", DEFAULT_EXTENSIONS)), 20)]
    subject_format = _text(payload.get("subject_format", old.get("subject_format", "{姓名}+{关键词}")), 60) or "{姓名}+{关键词}"
    roster = parse_roster(payload["roster"]) if "roster" in payload else old.get("roster", [])
    rename = payload.get("rename", old.get("rename", "template"))
    if rename not in RENAME:
        raise TaskError("改名方式无效")
    template = _text(payload.get("name_template", old.get("name_template", "{姓名}_{关键词}")), 60) or "{姓名}_{关键词}"
    unknown = set(re.findall(r"\{([^{}]*)\}", template)) - set(PLACEHOLDERS)
    if unknown:
        raise TaskError("命名模板里有不认识的占位：" + "、".join(sorted(unknown)))
    return {"name": name, "account_id": account_id, "keywords": keywords, "subject_format": subject_format, "start": start,
            "deadline": deadline, "remind_hours": remind, "extensions": extensions or DEFAULT_EXTENSIONS, "roster": roster,
            "senders": dict(old.get("senders", {})), "description": _text(payload.get("description", old.get("description", "")), 2000),
            "rename": rename, "name_template": template, "ended": int(old.get("ended") or 0)}


# ---------------------------------------------------------------- matching and identifying

def strip_subject(subject):
    return _PREFIX.sub("", subject or "").strip()


def matches(config, message):
    """Whether a message belongs to the task: on time, and the keyword is in its subject or an attachment name."""
    if message["received"] < config["start"] or message.get("account_id") != config["account_id"]:
        return False
    files = [a for a in message.get("attachments", []) if not a.get("inline")]
    subject = message.get("subject") or ""
    return any(k in subject for k in config["keywords"]) or any(k in a["name"] for a in files for k in config["keywords"])


_NOT_NAMES = {"材料", "意见", "附件", "补交", "补发", "更新", "修改", "最新", "版本", "文件", "提交", "回复", "评审", "评阅", "标书", "表格",
              "最终版", "修订", "已填", "填写", "汇总", "通知", "收到", "谢谢", "请查收", "第二版", "终稿"}


def name_from_subject(subject, keywords):
    """A person's name written next to the keyword, as in "张三+评分表". Subjects without the keyword say nothing."""
    value = strip_subject(subject)
    if not any(keyword in value for keyword in keywords):
        return ""
    for keyword in sorted(keywords, key=len, reverse=True):
        value = value.replace(keyword, " ")
    names = []
    for piece in re.split(_SEPARATORS, value):
        piece = _HONORIFIC.sub("", piece.strip())
        if _CHINESE_NAME.fullmatch(piece) and piece not in _NOT_NAMES and piece not in names:
            names.append(piece)
    return names[0] if len(names) == 1 else ""


# Common Chinese surnames. Without a roster, a word next to the keyword only counts as a name when it starts with one,
# so subjects such as "第二组评分表" or "最终评分表" wait for the owner instead of becoming a person.
_COMPOUND_SURNAMES = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "长孙", "宇文", "司徒", "夏侯", "轩辕", "令狐", "端木", "独孤", "南宫", "西门", "百里")
_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "常温康施文牛樊葛邢安齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕单季裴霍涂成苗谷盛曲翁冉骆蓝路游辛靳管柴蒙鲍华喻祁蒲房滕屈饶解牟艾尤阳时穆农司卓古吉缪简车项连芦麦褚娄窦戚岑景党宫费卜冷晏席卫米柏宗瞿桂全佟应臧闵苟邬边卞姬师和仇栾隋商刁沙荣巫寇桑郎甄丛仲虞敖巩明佘池查麻苑迟邝"
    "官封谈匡鞠惠荆乐冀郁胥南班储原栗燕楚鄢劳谌奚皮粟冼蔺楼盘满闻位厉伊仝区郜海阚花权强帅屠豆朴盖练廉禹井祖漆巴丰支卿国狄平计索宣晋相初门云容敬来扈晁芮都普阙浦戈伏鹿薄邸雍辜羊阿乌母裘亓修邰赫杭况那宿鲜印逯隆茹诸战慕危玉银亢嵇公哈湛宾戎勾茅利於呼居揭干但尉冶斯元束檀衣信展阴昝智幸奉植衡富尧闭由"
)


def looks_like_name(word):
    return len(word) <= 4 and (word[:2] in _COMPOUND_SURNAMES or word[0] in _SURNAMES)


def name_from_attachment(filename, keywords):
    stem = filename.rsplit(".", 1)[0]
    parsed = name_from_subject(stem, keywords)
    if parsed:
        return parsed
    # A named submission need not repeat the task keyword: e.g. "5-5 周小云 12份.xlsx".
    match = re.fullmatch(r"(?:\d+(?:-\d+)*[\s_-]+)?([\u4e00-\u9fff]{2,4}?)(?:老师)?(?:[\s_-]+\d+份(?:[（(_-].*)?)?", stem.strip())
    name = match[1] if match else ""
    return name if name and name not in _NOT_NAMES and looks_like_name(name) else ""


def is_forwarded(message):
    return bool(re.search(r"聊天记录|^\s*(?:转发|fw|fwd)\s*[:：]", message.get("subject") or "", re.I))


def regular_subject(config, subject, person):
    keyword = config["keywords"][0]
    expected = config["subject_format"].replace("{姓名}", person).replace("{关键词}", keyword)
    squash = lambda s: re.sub(r"\s+", "", s).replace("＋", "+")
    return squash(strip_subject(subject)) == squash(expected)


def identify(config, message):
    """Who sent it. Returns (person, reason it waits for the owner, flags); person is '' when it waits."""
    address = (message.get("from_addr") or "").lower()
    sender_name = (message.get("from_name") or "").strip()
    roster = [p["name"] for p in config.get("roster", [])]
    by_mail = {p["email"]: p["name"] for p in config.get("roster", []) if p.get("email")}
    flags = []
    # What the mail itself says comes first: one address can send for several people (a secretary, a forward).
    places = [message.get("subject") or ""] + [a["name"] for a in message.get("attachments", []) if not a.get("inline")]
    hits = sorted({name for name in roster if any(name in place for place in places)})
    if len(hits) == 1:
        return hits[0], "", flags
    if len(hits) > 1:
        return "", "出现多个名单中的姓名：" + "、".join(hits), flags
    parsed = name_from_subject(message.get("subject") or "", config["keywords"])
    from_files = {name_from_attachment(a["name"], config["keywords"]) for a in message.get("attachments", []) if not a.get("inline")}
    from_files.discard("")
    explicit = from_files | ({parsed} if parsed else set())
    if len(explicit) > 1:
        return "", "出现多个提交人姓名：" + "、".join(sorted(explicit)), flags
    if not parsed and from_files:
        parsed = next(iter(from_files))
        flags.append("按附件名识别")
    if parsed:
        if roster and parsed not in roster:
            return "", "名单外：" + parsed, flags
        if not roster and not looks_like_name(parsed):
            return "", "看不出是姓名：" + parsed, flags
        return parsed, "", flags
    # Nothing in the subject or file names: fall back to who sent it.
    if is_forwarded(message):
        return "", "转发材料未明确提交人", flags
    if address and address in by_mail:
        return by_mail[address], "", flags
    if address and address in config.get("senders", {}):
        return config["senders"][address], "", flags
    named = _HONORIFIC.sub("", sender_name)
    if named in roster or (not roster and _CHINESE_NAME.fullmatch(named)):
        flags.append("按发件人名称识别")
        return named, "", flags
    if roster and named and not _CHINESE_NAME.fullmatch(named):
        return "", "认不出提交人", flags
    if roster and _CHINESE_NAME.fullmatch(named):
        return "", "名单外：" + named, flags
    return "", "认不出提交人", flags


# Mail received after the deadline is still collected; it is marked, not counted against anyone (there is no roster).
LATE = "截止后收到"


def submission_flags(config, message, person, files):
    flags = []
    if config.get("deadline") and message["received"] > config["deadline"]:
        flags.append(LATE)
    if person and not regular_subject(config, message.get("subject") or "", person):
        flags.append("主题不规范")
    if message.get("link_attach"):
        flags.append("网盘链接附件")
    if not files and not message.get("link_attach"):
        flags.append("没有附件")
    odd = sorted({mime.extension(f["name"]) or "无扩展名" for f in files if mime.extension(f["name"]) not in config["extensions"]})
    if odd:
        flags.append("附件格式：" + "、".join(odd))
    return flags


# ---------------------------------------------------------------- layout

def _unique(path, used):
    if path not in used:
        used.add(path)
        return path
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    number = 2
    while True:
        candidate = "%s/%s (%d)%s" % (folder, stem, number, "." + ext if ext else "")
        if candidate not in used:
            used.add(candidate)
            return candidate
        number += 1


def archive_name(config, submission, item):
    """The file name used in the archive. Waiting mail keeps its own names: there is no person to put in them yet."""
    original = item["name"]
    if submission["pending"] or config.get("rename", "template") == "off":
        return original
    if config.get("rename") == "ai":
        return item.get("display_name") or original
    ext = mime.extension(original)
    stem = original[: -len(ext) - 1] if ext else original
    values = {"姓名": submission["person"], "关键词": config["keywords"][0], "任务": config["name"], "原文件名": stem,
              "日期": beijing(submission["received"], "%Y%m%d")}
    rendered = re.sub(r"\{(%s)\}" % "|".join(PLACEHOLDERS), lambda m: values[m.group(1)], config.get("name_template") or "{姓名}_{关键词}")
    rendered = rendered.strip(" _-") or stem
    return rendered + ("." + ext if ext else "")


def layout(task, account_address, submissions, files):
    """Where every kept file belongs, relative to the sync root. Returns [{path, file_id, sha256, size, received}]."""
    base = mime.safe_name(account_address, 120) + "/" + mime.safe_name(task["config"]["name"], 60)
    by_submission = {}
    for item in files:
        if item.get("path") and not item.get("error"):
            by_submission.setdefault(item["submission_id"], []).append(item)
    active = [s for s in submissions if not s["ignored"]]
    latest = {}
    for sub in sorted((s for s in active if not s["pending"]), key=lambda s: (s["received"], s["id"])):
        latest[sub["person"]] = sub["id"]
    entries, used = [], set()
    for sub in sorted(active, key=lambda s: (s["received"], s["id"])):
        if sub["pending"]:
            folder = "%s/%s/%s_%s" % (base, PENDING, beijing(sub["received"]), mime.safe_name(sub["from_name"] or sub["from_addr"], 40))
            prefix = ""
        elif latest.get(sub["person"]) == sub["id"]:
            folder, prefix = "%s/%s/%s" % (base, SUBMITTED, mime.safe_name(sub["person"], 30)), ""
        else:
            folder, prefix = "%s/%s/%s/%s" % (base, SUBMITTED, mime.safe_name(sub["person"], 30), HISTORY), beijing(sub["received"]) + "_"
        for item in sorted(by_submission.get(sub["id"], []), key=lambda f: f["id"]):
            path = _unique(folder + "/" + prefix + mime.safe_name(archive_name(task["config"], sub, item), 100), used)
            entries.append({"path": path, "file_id": item["id"], "sha256": item["sha256"], "size": item["size"], "received": sub["received"]})
    return entries


# ---------------------------------------------------------------- status

def overview(task, submissions, files, now=None):
    """Per-person status and the counts shown on the task card."""
    now = now or time.time()
    config = task["config"]
    files_by = {}
    for item in files:
        files_by.setdefault(item["submission_id"], []).append({"id": item["id"], "name": item["name"], "size": item["size"],
                                                              "display_name": item.get("display_name") or "", "error": item.get("error") or ""})
    people = {}
    for sub in sorted(submissions, key=lambda s: s["received"]):
        if sub["ignored"] or sub["pending"]:
            continue
        person = people.setdefault(sub["person"], {"name": sub["person"], "email": "", "count": 0, "submissions": []})
        person["count"] += 1
        person["submissions"].append(_public_submission(sub, files_by))
    rows = []
    for entry in config.get("roster", []):
        person = people.pop(entry["name"], None)
        rows.append(_person_row(entry["name"], entry.get("email", ""), person))
    for name, person in people.items():
        rows.append(_person_row(name, "", person, outside=bool(config.get("roster"))))
    pending = [_public_submission(s, files_by) for s in sorted(submissions, key=lambda s: -s["received"]) if s["pending"] and not s["ignored"]]
    ignored = [_public_submission(s, files_by) for s in sorted(submissions, key=lambda s: -s["received"]) if s["ignored"]]
    submitted = sum(1 for r in rows if r["status"] == "submitted")
    expected = len(config.get("roster", []))
    deadline = config.get("deadline") or 0
    return {"id": task["id"], "enabled": bool(task["enabled"]), "config": config, "people": rows, "pending": pending, "ignored": ignored,
            "counts": {"expected": expected, "submitted": submitted, "missing": sum(1 for r in rows if r["status"] == "missing"),
                       "pending": len(pending), "duplicates": sum(max(0, r["count"]-1) for r in rows), "late": sum(1 for r in rows if r["late"]),
                       "mails": sum(1 for s in submissions if not s["ignored"]),
                       "files": sum(len(files_by.get(s["id"], [])) for s in submissions if not s["ignored"])},
            "deadline_left": int(deadline - now) if deadline else None}


def _public_submission(sub, files_by):
    flags = json.loads(sub["flags"]) if isinstance(sub["flags"], str) else sub["flags"]
    return {"id": sub["id"], "message_id": sub["message_id"], "person": sub["person"], "reason": sub.get("reason") or "",
            "flags": [LATE if f == "逾期" else f for f in flags], "received": sub["received"],
            "subject": sub["subject"], "from_name": sub["from_name"], "from_addr": sub["from_addr"], "manual": bool(sub["manual"]),
            "suggest": sub.get("suggest") or "",
            "files": files_by.get(sub["id"], [])}


def _person_row(name, address, person, outside=False):
    if not person:
        return {"name": name, "email": address, "status": "missing", "count": 0, "latest": 0, "late": False, "flags": [], "submissions": [],
                "outside": outside}
    last = person["submissions"][-1]
    return {"name": name, "email": address or last["from_addr"], "status": "submitted", "count": person["count"], "latest": last["received"],
            "late": LATE in last["flags"], "flags": last["flags"], "submissions": person["submissions"], "outside": outside}


def summary_text(task_view, final):
    config, counts = task_view["config"], task_view["counts"]
    missing = [p["name"] for p in task_view["people"] if p["status"] == "missing"]
    when = beijing(config["deadline"], "%m-%d %H:%M") if config.get("deadline") else ""
    head = "%s：已收到 %d 人%s" % (config["name"], counts["submitted"], "（名单 %d 人）" % counts["expected"] if counts["expected"] else "")
    lines = [head + ("（已截止 %s）" % when if final else "（截止 %s）" % when if when else "")]
    if missing:
        lines.append("未交：" + "、".join(missing[:40]) + ("等 %d 人" % len(missing) if len(missing) > 40 else ""))
    if counts["pending"]:
        lines.append("另有 %d 封待确认" % counts["pending"])
    if counts["late"]:
        lines.append("其中 %d 人在截止后提交" % counts["late"])
    return "\n".join(lines)


def due_reminders(task, now):
    """Which reminders are due: 'before' (remind_hours ahead of the deadline) and 'final' (at the deadline)."""
    config, state = task["config"], task.get("state") or {}
    deadline = config.get("deadline") or 0
    due = []
    if not deadline or not task["enabled"] or config.get("ended"):
        return due
    if config.get("remind_hours") and now >= deadline - config["remind_hours"] * 3600 and now < deadline and not state.get("before"):
        due.append("before")
    if now >= deadline and not state.get("final") and now - deadline < 6 * 3600:
        due.append("final")
    return due


def new_id(prefix):
    return prefix + secrets.token_hex(6)
