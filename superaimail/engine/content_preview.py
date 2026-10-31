"""Bounded text-only previews and explicit links from untrusted email content."""
import io
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

MAX_ATTACHMENT = 8 * 1024 * 1024
MAX_XML = 4 * 1024 * 1024
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def https_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        if len(value) > 4000 or any(ord(c) < 32 for c in value):
            return ""
        return value
    except ValueError:
        return ""


def action_links(text, unsubscribe=""):
    links = []
    for match in re.finditer(r"https://[^\s<>\"'）)]+", text or ""):
        url = https_url(match.group(0))
        context = (text[max(0, match.start()-90):match.end()+30]).lower()
        if url and re.search(r"magic.link|sign.in|log.in|verify|验证|登录", context):
            links.append({"kind": "login", "url": url, "host": urllib.parse.urlsplit(url).hostname})
            break
    for raw in re.findall(r"<([^>]+)>", unsubscribe or ""):
        url = https_url(raw)
        if url:
            links.append({"kind": "unsubscribe", "url": url, "host": urllib.parse.urlsplit(url).hostname})
            break
    return links


def xlsx_preview(data):
    """Read cached cell values only. Never evaluate formulas, macros or external links."""
    if len(data) > MAX_ATTACHMENT:
        raise ValueError("附件超过 8 MB，请下载后打开")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("无法读取此工作簿，请下载后打开") from None
    with archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
            raise ValueError("工作簿解压后过大，请下载后打开")

        def xml(name):
            item = archive.getinfo(name)
            if item.file_size > MAX_XML:
                raise ValueError("工作表过大，请下载后打开")
            raw = archive.read(item)
            if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("工作簿含不支持的 XML 声明")
            return ET.fromstring(raw)

        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(x.itertext())[:500] for x in xml("xl/sharedStrings.xml").findall("s:si", NS)]
        sheets = sorted(n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[:5]
        result = []
        for name in sheets:
            rows = []
            for row in xml(name).findall("s:sheetData/s:row", NS)[:50]:
                cells = [""] * 20
                for cell in row.findall("s:c", NS):
                    ref = re.match(r"([A-Z]+)\d+$", cell.get("r", ""))
                    if not ref:
                        continue
                    index = 0
                    for char in ref.group(1):
                        index = index * 26 + ord(char)-64
                    if not 1 <= index <= 20:
                        continue
                    value = cell.findtext("s:v", "", NS)
                    if cell.get("t") == "s":
                        value = strings[int(value)] if value.isdigit() and int(value) < len(strings) else ""
                    elif cell.get("t") == "inlineStr":
                        value = "".join(cell.find("s:is", NS).itertext()) if cell.find("s:is", NS) is not None else ""
                    elif cell.find("s:f", NS) is not None and not value:
                        value = "（公式无缓存值）"
                    cells[index-1] = value[:500]
                while cells and not cells[-1]:
                    cells.pop()
                rows.append(cells)
            result.append({"name": name.rsplit("/", 1)[-1], "rows": rows})
        return {"sheets": result, "note": "仅预览前 5 张工作表、50 行和 20 列；显示原始或公式缓存值，不计算公式或解析评分。"}
