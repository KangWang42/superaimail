"""Mail bodies for reading: an HTML part cleaned to a safe subset, and two pages built from it.

`clean(markup, cid_parts)` keeps what a mail needs to look like itself (layout tables, inline styles, <style> blocks,
images, links) and drops everything that could run, submit, embed or escape the frame: scripts, event attributes, forms,
frames, objects, SVG, base and meta tags, javascript: and other unsafe URLs, CSS expressions and imports. Inline images
(`cid:`) become `inline:<part>` so the page can ask for them later.

`page(...)` builds the document a client shows in a script-less frame:
  * "original": the sender's layout on a white page; remote images are replaced by blank boxes until images are allowed.
  * "simple": the mail's words and structure in our own typography (presentation removed, layout tables flattened,
    data tables kept, quoted replies folded); plain-text mail is shown this way too.
`text(markup)` is the readable plain text used for search, AI, notices and the desktop pet; `links(markup)` finds the
mail's action links (confirm, verify, view …) with their own words.
"""
import colorsys
import html
import re
import urllib.parse
from html.parser import HTMLParser

# Characters with no visible width that mail tools pad previews with (U+034F, soft hyphens, zero-width and directional
# marks …). Left in, they turn into screens of blank lines.
INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁤"
                       "⁪-⁯ㅤ﻿ﾠ]")
SPACES = re.compile("[  -   ]")

MAX_INPUT = 900 * 1024
MAX_NODES = 40000

VOID = {"br", "img", "hr", "col", "wbr", "area", "source", "input", "meta", "link", "base"}
# Removed with everything inside them.
DROP = {"script", "noscript", "iframe", "frame", "frameset", "object", "embed", "applet", "svg", "math", "template",
        "canvas", "audio", "video", "select", "textarea", "button", "input", "option", "title", "head", "xml"}
ALLOWED = {"a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "big", "blockquote", "br", "caption", "center",
           "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure",
           "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main",
           "mark", "nav", "ol", "p", "pre", "q", "s", "samp", "section", "small", "span", "strike", "strong", "sub",
           "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var", "wbr"}
BLOCK = {"address", "article", "aside", "blockquote", "center", "dd", "details", "div", "dl", "dt", "figcaption",
         "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
         "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "caption"}
ATTRS = {"href", "src", "alt", "title", "width", "height", "align", "valign", "bgcolor", "color", "border", "cellpadding",
         "cellspacing", "colspan", "rowspan", "style", "class", "id", "dir", "lang", "face", "size", "start", "type",
         "span", "background", "datetime", "open", "role"}
PRESENTATION = {"style", "class", "id", "width", "height", "align", "valign", "bgcolor", "color", "border", "cellpadding",
                "cellspacing", "face", "size", "background"}


class Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag, attrs=None, text=None):
        self.tag, self.attrs, self.children, self.text = tag, attrs or {}, [], text


class _Builder(HTMLParser):
    """A lenient tree of the markup: unknown end tags are ignored, unclosed elements end with their parent."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.styles = []
        self.skip = None          # tag whose content is being dropped
        self.depth = 0
        self.count = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip:
                self.depth += 1
            return
        if tag == "style":
            self.skip, self.depth, self.style = "style", 1, []
            return
        if tag in DROP:
            if tag not in VOID:
                self.skip, self.depth = tag, 1
            return
        self.count += 1
        if self.count > MAX_NODES:
            return
        if tag in ("p", "li", "tr", "td", "th", "dt", "dd", "option"):
            # an unclosed sibling ends where the next one starts
            for index in range(len(self.stack) - 1, 0, -1):
                current = self.stack[index].tag
                if current == tag or (tag in ("td", "th") and current in ("td", "th")):
                    del self.stack[index:]
                    break
                if current in ("table", "ul", "ol", "dl", "tr" if tag in ("td", "th") else "table", "div"):
                    break
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs if k})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and not self.skip and self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip:
                self.depth -= 1
                if self.depth <= 0:
                    if self.skip == "style":
                        self.styles.append("".join(self.style))
                    self.skip = None
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.skip:
            if self.skip == "style":
                self.style.append(data)
            return
        if data:
            self.stack[-1].children.append(Node(None, text=data))


def _tree(markup):
    builder = _Builder()
    try:
        builder.feed((markup or "")[:MAX_INPUT])
        builder.close()
    except Exception:
        pass
    return builder.root, builder.styles


# ---------------------------------------------------------------- URLs and CSS

def _safe_url(value, image=False):
    value = INVISIBLE.sub("", value or "").strip()
    lowered = re.sub(r"[\s\x00-\x1f]", "", value).lower()
    if image and lowered.startswith("data:image/") and not lowered.startswith("data:image/svg"):
        return value
    scheme = urllib.parse.urlsplit(lowered).scheme
    if scheme in ("http", "https"):
        return value
    if not image and scheme in ("mailto", "tel"):
        return value
    return ""


_CSS_BAD = [re.compile(r"@(import|charset|namespace)\b[^;{}]*;?", re.I), re.compile(r"(behavior|-moz-binding)\s*:[^;}]*", re.I),
            re.compile(r"expression\s*\([^)]*\)?", re.I), re.compile(r"(java|vb)script\s*:", re.I)]
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)


def _css(value, images=True):
    value = INVISIBLE.sub("", value or "")
    for pattern in _CSS_BAD:
        value = pattern.sub("", value)

    def url(match):
        target = _safe_url(match.group(2), image=True)
        if not target or (not images and not target.lower().startswith("data:")):
            return "none"
        return "url(%s)" % ('"' + target.replace('"', "%22") + '"')
    value = _CSS_URL.sub(url, value)
    return re.sub(r"position\s*:\s*fixed", "position:static", value, flags=re.I)


# ---------------------------------------------------------------- cleaning

def clean(markup, cid_parts=None):
    """The safe subset of a mail's HTML, serialised; `cid_parts` maps a Content-ID (without <>) to a MIME part."""
    cid_parts = {str(k).strip("<>").lower(): v for k, v in (cid_parts or {}).items()}
    root, styles = _tree(markup)

    def walk(node):
        out = []
        for child in node.children:
            if child.tag is None:
                out.append(html.escape(INVISIBLE.sub("", child.text), quote=False))
                continue
            inner = walk(child)
            if child.tag not in ALLOWED:
                out.append(inner)
                continue
            attrs = []
            for key, value in child.attrs.items():
                if key not in ATTRS or key.startswith("on"):
                    continue
                if key == "href":
                    value = _safe_url(value)
                    if not value:
                        continue
                elif key == "src":
                    raw = (value or "").strip()
                    if raw.lower().startswith("cid:"):
                        part = cid_parts.get(urllib.parse.unquote(raw[4:]).strip("<>").lower())
                        if not part:
                            continue
                        value = "inline:" + part
                    else:
                        value = _safe_url(value, image=True)
                        if not value:
                            continue
                elif key == "background":
                    value = _safe_url(value, image=True)
                    if not value:
                        continue
                elif key == "style":
                    value = _css(value)
                elif key in ("class", "id"):
                    value = re.sub(r"[^\w\- ]", "", value)[:200]
                else:
                    value = INVISIBLE.sub("", value)[:500]
                attrs.append(' %s="%s"' % (key, html.escape(value, quote=True)))
            if child.tag == "img" and not any(a.startswith(" src=") for a in attrs):
                continue
            if child.tag == "a" and any(a.startswith(" href=") for a in attrs):
                attrs.append(' target="_blank" rel="noopener noreferrer"')
            head = "<" + child.tag + "".join(attrs) + ">"
            out.append(head if child.tag in VOID else head + inner + "</" + child.tag + ">")
        return "".join(out)

    body = walk(root)
    css = "\n".join(_css(block) for block in styles)[:200000]
    return ("<style>" + css.replace("</", "<\\/") + "</style>" if css.strip() else "") + body


def has_markup(stored):
    return bool(stored and re.search(r"<(?!style)[a-z]", stored, re.I))


# ---------------------------------------------------------------- reading the cleaned markup

def _words(node, out):
    for child in node.children:
        if child.tag is None:
            out.append(child.text)
        else:
            _words(child, out)


def _visible(node):
    out = []
    _words(node, out)
    return SPACES.sub(" ", INVISIBLE.sub("", "".join(out))).strip()


def text(markup):
    """Readable plain text: one line per paragraph, list item or table row; cells separated by spaces."""
    root, _ = _tree(markup)
    parts = []

    def walk(node):
        for child in node.children:
            if child.tag is None:
                parts.append(child.text)
                continue
            if child.tag in ("br",):
                parts.append("\n")
                continue
            if hidden(child):
                continue
            if child.tag == "li":
                parts.append("\n• ")
            elif child.tag in ("td", "th"):
                parts.append("  ")
            elif child.tag in BLOCK or child.tag == "hr":
                parts.append("\n")
            if child.tag == "img":
                continue
            walk(child)
            if child.tag in BLOCK and child.tag not in ("td", "th", "tr", "li", "dt", "dd"):
                parts.append("\n")
    walk(root)
    return tidy("".join(parts))


def tidy(value):
    value = SPACES.sub(" ", INVISIBLE.sub("", value or ""))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    value = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return "".join(c for c in value if c == "\n" or ord(c) >= 32)


ACTION_WORDS = re.compile(r"\b(confirm|verify|accept|agree|activate|approve|yes\b|i am|review (the )?(submission|manuscript|paper)|view (the )?(submission|manuscript|"
                          r"invitation|order|document)|sign in|log in|reset|download|join|respond|reply now)|确认|同意|验证|激活|"
                          r"接受|登录|查看(订单|详情|稿件|邀请)|下载|重置|立即(参加|加入|查看)|是的", re.I)
LOGIN_WORDS = re.compile(r"magic.link|sign.in|log.in|verify|验证|登录", re.I)


def links(markup, limit=3):
    """The mail's own action links: [{label, url, host}], in reading order, at most `limit`."""
    root, _ = _tree(markup)
    found = []

    def walk(node):
        for child in node.children:
            if child.tag is None:
                continue
            if child.tag == "a":
                url = _safe_url(child.attrs.get("href", ""))
                label = re.sub(r"\s+", " ", _visible(child))
                if url.lower().startswith("https://") and 2 <= len(label) <= 48 and ACTION_WORDS.search(label) and not re.search(r"unsubscribe|退订|取消订阅", label, re.I):
                    if url not in [f["url"] for f in found]:
                        found.append({"label": label, "url": url, "host": urllib.parse.urlsplit(url).hostname or ""})
                continue
            walk(child)
    walk(root)
    return found[:limit]


# ---------------------------------------------------------------- pages

FONT = ('-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", '
        '"Noto Sans CJK SC", "Source Han Sans SC", Roboto, "Helvetica Neue", Arial, sans-serif')
MONO = 'ui-monospace, "SFMono-Regular", Consolas, "Cascadia Mono", Menlo, monospace'

ORIGINAL_CSS = """
html{background:#fff;color:#1f2328;color-scheme:light}
body{margin:0;padding:16px;font:15px/1.6 %(font)s;-webkit-text-size-adjust:100%%;text-size-adjust:100%%;overflow-wrap:break-word}
img{max-width:100%%;height:auto}
table{max-width:100%%}
pre{white-space:pre-wrap}
img.mail-img-off{background:#eef0f3;outline:1px dashed #d0d7de;outline-offset:-1px}
a{color:#0b63c5}
@media (max-width:700px){
body{padding:10px}
body *{max-width:100%%!important;box-sizing:border-box}
td,th{word-break:break-word}
img{height:auto!important}
}
""" % {"font": FONT}

ORIGINAL_DARK_CSS = """
html{background:transparent;color:#e4e6ea;color-scheme:dark}
body{margin:0;padding:16px;font:15px/1.6 %(font)s;-webkit-text-size-adjust:100%%;text-size-adjust:100%%;overflow-wrap:break-word}
img{max-width:100%%;height:auto}
table{max-width:100%%}
pre{white-space:pre-wrap}
img.mail-img-off{background:#2a2d33;outline:1px dashed #3d414a;outline-offset:-1px}
a{color:#8ab4f8}
.mail-on-light,.mail-on-light *{color:#1f2328!important}
.mail-on-light .mail-on-dark,.mail-on-light .mail-on-dark *{color:#e4e6ea!important}
@media (max-width:700px){
body{padding:10px}
body *{max-width:100%%!important;box-sizing:border-box}
td,th{word-break:break-word}
img{height:auto!important}
}
""" % {"font": FONT}

# ---- colours for the dark page: hue kept, lightness turned where it would glare or vanish

NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192),
         "whitesmoke": (245, 245, 245), "gainsboro": (220, 220, 220), "lightgray": (211, 211, 211), "lightgrey": (211, 211, 211),
         "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169), "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105),
         "ivory": (255, 255, 240), "snow": (255, 250, 250), "ghostwhite": (248, 248, 255), "aliceblue": (240, 248, 255),
         "beige": (245, 245, 220), "linen": (250, 240, 230), "navy": (0, 0, 128), "red": (255, 0, 0), "blue": (0, 0, 255),
         "green": (0, 128, 0), "maroon": (128, 0, 0), "purple": (128, 0, 128), "teal": (0, 128, 128), "olive": (128, 128, 0)}
COLOR = re.compile(r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3,4}\b|rgba?\(\s*[\d.%\s,/]+\)|\b(?:" + "|".join(NAMED) + r")\b", re.I)


def _rgba(token):
    token = token.strip().lower()
    if token.startswith("#"):
        digits = token[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        try:
            values = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
        except ValueError:
            return None
        if len(values) < 3:
            return None
        return values[0], values[1], values[2], (values[3] / 255 if len(values) > 3 else 1.0)
    if token.startswith("rgb"):
        parts = [x for x in re.split(r"[\s,/()]+", token[token.index("(") + 1:]) if x]
        try:
            rgb = [float(x[:-1]) * 2.55 if x.endswith("%") else float(x) for x in parts[:3]]
            alpha = float(parts[3][:-1]) / 100 if len(parts) > 3 and parts[3].endswith("%") else float(parts[3]) if len(parts) > 3 else 1.0
        except (ValueError, IndexError):
            return None
        return int(min(255, rgb[0])), int(min(255, rgb[1])), int(min(255, rgb[2])), alpha
    return NAMED.get(token, (0, 0, 0)) + (1.0,) if token in NAMED else None


def _turn(token, role):
    found = _rgba(token)
    if not found:
        return token
    r, g, b, a = found
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if role == "fg":
        if l >= 0.6:
            return token
        l = 0.9 - l * 0.45
    elif role == "border":
        if l < 0.55:
            return token
        l, s = 0.2 + (1 - l) * 0.35, s * 0.7
    else:
        if l < 0.55:
            return token
        l, s = 0.11 + (1 - l) * 0.45, s * 0.75
    r, g, b = (round(x * 255) for x in colorsys.hls_to_rgb(h, l, s))
    return "#%02x%02x%02x" % (r, g, b) if a >= 1 else "rgba(%d,%d,%d,%.3g)" % (r, g, b, a)


def _turn_value(value, role):
    return COLOR.sub(lambda m: _turn(m.group(0), role), value)


DECLARATION = re.compile(r"(?<![\w-])(color|background-color|background|background-image|border(?:-top|-right|-bottom|-left)?(?:-color)?|"
                         r"outline(?:-color)?|text-decoration-color|fill|stroke)\s*:\s*([^;{}]+)", re.I)


def _dark_css(value):
    """Declarations of a style attribute or sheet with their colours turned for the dark page."""
    def one(match):
        prop = match.group(1).lower()
        role = "fg" if prop in ("color", "fill", "stroke", "text-decoration-color") else "border" if prop.startswith(("border", "outline")) else "bg"
        return match.group(1) + ":" + _turn_value(match.group(2), role)
    return DECLARATION.sub(one, value)


def _drop_dark_rules(css):
    """The mail's own @media (prefers-color-scheme: dark) blocks, left out so they are not turned a second time."""
    out, index = [], 0
    for match in re.finditer(r"@media[^{]*prefers-color-scheme\s*:\s*dark[^{]*\{", css, re.I):
        if match.start() < index:
            continue
        out.append(css[index:match.start()])
        depth, cursor = 1, match.end()
        while cursor < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[cursor], 0)
            cursor += 1
        index = cursor
    out.append(css[index:])
    return "".join(out)


def _luminance(token):
    colour = COLOR.search(token or "")
    found = _rgba(colour.group(0)) if colour else None
    if not found:
        return None
    channel = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(x / 255) for x in found[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


BACKGROUND = re.compile(r"(?<![\w-])background(?:-color)?\s*:\s*([^;{}]+)", re.I)


def _background(value):
    """The first colour a background declaration sets, if any."""
    found = BACKGROUND.search(value or "")
    colour = COLOR.search(found.group(1)) if found else None
    return colour.group(0) if colour else None


def _class_backgrounds(css):
    """class name -> background colour set for it in the mail's style sheet (simple rules only)."""
    found = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        colour = _background(body)
        if colour:
            for name in re.findall(r"\.([\w-]+)", selectors):
                found.setdefault(name, colour)
    return found


def _darken(node, classes=None, bright=False):
    """Turn colours for the dark page. Where a background stays bright after turning (luminance above 0.35, e.g. a
    saturated brand button), its text is marked to stay dark (mail-on-light); a dark background inside it is marked
    back (mail-on-dark)."""
    classes = classes or {}
    for child in node.children:
        if child.tag is None:
            continue
        attrs = child.attrs
        own = attrs.get("bgcolor") or _background(attrs.get("style")) or next(
            (classes[name] for name in attrs.get("class", "").split() if name in classes), None)
        if "bgcolor" in attrs:
            attrs["bgcolor"] = _turn_value(attrs["bgcolor"], "bg")
        if "color" in attrs:
            attrs["color"] = _turn_value(attrs["color"], "fg")
        if "style" in attrs:
            attrs["style"] = _dark_css(attrs["style"])
        here = bright
        if own:
            level = _luminance(_turn_value(own, "bg"))
            if level is not None:
                here = level > 0.35
                if here != bright:
                    attrs["class"] = (attrs.get("class", "") + (" mail-on-light" if here else " mail-on-dark")).strip()
        _darken(child, classes, here)


SIMPLE_CSS = """
:root{color-scheme:%(scheme)s}
html{background:transparent}
body{margin:0;padding:0 0 8px;color:%(ink)s;font:16px/1.75 %(font)s;-webkit-text-size-adjust:100%%;text-size-adjust:100%%;overflow-wrap:anywhere;word-break:normal}
.mail{max-width:42em}
p,ul,ol,dl,pre,table.data,figure{margin:0 0 .9em}
div:empty,p:empty,span:empty{display:none}
h1,h2,h3,h4,h5,h6{line-height:1.35;margin:1.1em 0 .5em;font-weight:650}
h1{font-size:1.35em}h2{font-size:1.2em}h3{font-size:1.08em}h4,h5,h6{font-size:1em}
a{color:%(link)s;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px}
img{max-width:100%%;height:auto;border-radius:6px;vertical-align:middle}
blockquote{margin:.8em 0;padding:.1em 0 .1em 1em;border-left:3px solid %(rule)s;color:%(muted)s}
pre,code,kbd,samp{font-family:%(mono)s;font-size:.92em}
pre{white-space:pre-wrap;background:%(code)s;padding:.8em 1em;border-radius:8px}
ul,ol{padding-left:1.4em}li{margin:.2em 0}
hr{border:0;border-top:1px solid %(rule)s;margin:1.2em 0}
table.data{border-collapse:collapse;font-size:.94em;display:block;overflow-x:auto;max-width:100%%}
table.data td,table.data th{border:1px solid %(rule)s;padding:.35em .6em;text-align:left;vertical-align:top}
table.data th{background:%(code)s;font-weight:600}
table.layout,table.layout>*,table.layout>*>tr,table.layout>*>tr>td,table.layout>*>tr>th,table.layout>tr,table.layout>tr>td{display:block;width:auto;border:0;padding:0;margin:0}
table.layout td:not(:empty){margin-bottom:.35em}
table.kv{border-collapse:collapse;margin:0 0 .9em;display:table}
table.kv td{padding:.15em 1em .15em 0;vertical-align:top;border:0}
table.kv td:first-child{color:%(muted)s;white-space:nowrap}
table.bullet{margin:0 0 .3em}
table.bullet td:first-child{color:inherit;padding-right:.6em}
li>div,li>p,li>table.layout,li>table.layout>*,li>table.layout>*>tr,li>table.layout>*>tr>td{display:inline;margin:0}
details.quote{margin:1.2em 0 0;color:%(muted)s}
details.quote>summary{cursor:pointer;font-size:.9em;list-style:none;padding:.2em 0}
details.quote>summary::before{content:"··· ";letter-spacing:1px}
.sig{color:%(muted)s;font-size:.94em}
.img-alt{color:%(muted)s;font-style:italic;font-size:.92em}
.para{margin:0 0 .9em}
"""

THEMES = {"light": {"scheme": "light", "ink": "#1f2328", "muted": "#59636e", "link": "#0b63c5", "rule": "#d1d9e0", "code": "#f6f8fa"},
          "dark": {"scheme": "dark", "ink": "#e6edf3", "muted": "#9198a1", "link": "#6cb6ff", "rule": "#3d444d", "code": "#161b22"}}

QUOTE_CLASSES = re.compile(r"gmail_quote|yahoo_quoted|moz-cite-prefix|protonmail_quote|divrplyfwdmsg|appendonsend|"
                           r"qmbox_quote|ntes-mailmaster-quote|mail-quote|readmsgbody", re.I)
REPLY_HEADER = re.compile(r"^(\s*(在|于).{2,80}(写道|wrote)\s*[:：]\s*$|\s*on .{4,120} wrote:\s*$|\s*-{2,}\s*(original message|"
                          r"原始邮件|转发的邮件|forwarded message)\s*-{2,}\s*$|\s*(发件人|from)\s*[:：].+$)", re.I)


def _is_tiny(node):
    def number(key):
        try:
            return int(re.sub(r"[^\d]", "", node.attrs.get(key, "")) or 0)
        except ValueError:
            return 0
    w, h = number("width"), number("height")
    return (0 < w <= 3) or (0 < h <= 3)


def _remote(src):
    return src.lower().startswith(("http://", "https://"))


def _shown_image(node, images):
    src = node.attrs.get("src", "")
    return bool(src) and not _is_tiny(node) and (src.startswith(("inline:", "data:")) or (images and _remote(src)))


def _has_content(node, images):
    """Whether the simple view would show anything for this element: words, a shown image or a rule."""
    for child in node.children:
        if child.tag is None:
            if SPACES.sub("", INVISIBLE.sub("", child.text)).strip():
                return True
        elif child.tag == "hr" or (child.tag == "img" and _shown_image(child, images)):
            return True
        elif child.tag == "a" and child.attrs.get("href") and any(c.tag == "img" and (c.attrs.get("alt") or "").strip() for c in child.children):
            return True
        elif child.tag not in ("br", "img") and _has_content(child, images):
            return True
    return False


def _rows(table):
    found = []
    for child in table.children:
        if child.tag == "tr":
            found.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            found.extend(c for c in child.children if c.tag == "tr")
    return found


def _table_kind(table):
    if _has_nested_table(table):
        return "layout"
    if _has(table, "th"):
        return "data"
    rows = _rows(table)
    cells = [[c for c in row.children if c.tag in ("td", "th")] for row in rows]
    cells = [row for row in cells if any(_visible(c) for c in row)]
    if len(cells) >= 2 and all(len(row) == 2 and 0 < len(_visible(row[0])) <= 16 for row in cells):
        return "kv"
    if cells and all(len(row) == 2 and re.fullmatch(r"[•·●▪◦‣\-–—*]|\d{1,2}[.)、]?", _visible(row[0])) for row in cells):
        return "kv bullet"
    return "layout"


def _serialize(node, mode, images, inline_url, blocked, label_images=False):
    """The cleaned tree as HTML for one view. `blocked` counts remote images left out. In the simple view an image's
    alt text stands in for it only when it is the whole content of a link (`label_images`), so the link keeps a name."""
    out = []
    breaks = 0
    for child in node.children:
        if child.tag is None:
            if mode == "simple" and not SPACES.sub("", INVISIBLE.sub("", child.text)).strip():
                out.append(" " if child.text else "")
                continue
            breaks = 0
            out.append(html.escape(child.text, quote=False))
            continue
        tag, attrs = child.tag, dict(child.attrs)
        if mode == "simple":
            if hidden(child):
                continue
            if tag == "br":
                breaks += 1
                if breaks > 2:
                    continue
            elif tag != "img":
                breaks = 0
            if tag in BLOCK | {"span", "font", "a", "b", "strong", "em", "i", "u", "small", "big"} and tag != "hr" and not _has_content(child, images):
                if not (tag == "a" and child.attrs.get("href") and any(c.tag == "img" for c in child.children)):
                    continue
        if tag == "img":
            src = attrs.get("src", "")
            if src.startswith("inline:"):
                attrs["src"] = inline_url(src[7:])
            elif _remote(src) and _is_tiny(child):
                continue                                   # tracking pixel
            elif _remote(src) and not images:
                blocked[0] += 1
                if mode == "simple":
                    alt = (attrs.get("alt") or "").strip()
                    if label_images and alt and len(alt) <= 80:
                        out.append('<span class="img-alt">' + html.escape(alt) + "</span>")
                    continue
                attrs.pop("src", None)
                attrs["alt"] = ""
                attrs["class"] = (attrs.get("class", "") + " mail-img-off").strip()
        if mode == "simple":
            if tag in ("font", "span", "center", "big", "small"):
                tag = "span" if tag != "center" else "div"
            for key in list(attrs):
                if key in PRESENTATION and not (tag == "img" and key == "class" and "mail-img-off" in attrs[key]):
                    attrs.pop(key)
            if tag == "table":
                attrs["class"] = _table_kind(child)
            if tag in ("div", "blockquote") and QUOTE_CLASSES.search(" ".join([child.attrs.get("class", ""), child.attrs.get("id", "")])) \
                    or (tag == "blockquote" and child.attrs.get("type", "").lower() == "cite"):
                inner = _serialize(child, mode, images, inline_url, blocked)
                out.append('<details class="quote"><summary>显示引用的原文</summary>' + inner + "</details>")
                continue
        elif not images and "style" in attrs:
            attrs["style"] = _css(attrs["style"], images=False)
        if not images and "background" in attrs and _remote(attrs["background"]):
            attrs.pop("background")
        rendered = "".join(' %s="%s"' % (k, html.escape(v, quote=True)) for k, v in attrs.items())
        head = "<" + tag + rendered + ">"
        labels = mode == "simple" and tag == "a" and not _visible(child)
        out.append(head if tag in VOID else head + _serialize(child, mode, images, inline_url, blocked, labels or (label_images and tag != "a")) + "</" + tag + ">")
    return "".join(out)


HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|max-height\s*:\s*0(?![.\d])|"
                    r"opacity\s*:\s*0(?:\.0+)?(?![.\d])|font-size\s*:\s*[01](?:px)?(?![.\d])|line-height\s*:\s*0(?![.\d])", re.I)


def hidden(node):
    """Content the sender hid from readers, such as the preview line meant only for the inbox list."""
    return "hidden" in node.attrs or bool(HIDDEN.search(node.attrs.get("style", "")))


def _has(node, tag):
    return any(c.tag == tag or (c.tag and c.tag != "table" and _has(c, tag)) for c in node.children)


def _has_nested_table(node):
    return any(c.tag == "table" or (c.tag and _has_nested_table(c)) for c in node.children)


def _document(head_css, extra_css, body, has_html, blocked, theme):
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><base target=\"_blank\">"
            '<meta name="mail-has-html" content="%d"><meta name="mail-blocked-images" content="%d"><meta name="mail-theme" content="%s">'
            "<style>%s</style>%s</head><body>%s</body></html>") % (1 if has_html else 0, blocked, theme, head_css, extra_css, body)


def page(stored, plain, mode="original", theme="light", images=False, inline_url=lambda part: ""):
    """The reading page of one mail. `stored` is clean() output or empty; `plain` is the stored plain text."""
    theme = theme if theme in THEMES else "light"
    has_html = has_markup(stored)
    blocked = [0]
    if has_html and mode == "original":
        root, styles = _tree(stored)
        dark = theme == "dark"
        css = "\n".join(_css(s, images=images) for s in styles)
        if dark:
            try:
                _darken(root, _class_backgrounds("\n".join(styles)))
                css = _dark_css(_drop_dark_rules(css))
            except Exception:
                return page(stored, plain, mode, "light", images, inline_url)      # the mail as it is, rather than nothing
        body = _serialize(root, "original", images, inline_url, blocked)
        return _document(ORIGINAL_DARK_CSS if dark else ORIGINAL_CSS, "<style>" + css.replace("</", "<\\/") + "</style>" if css.strip() else "",
                         body, True, blocked[0], "dark" if dark else "light")
    simple_css = SIMPLE_CSS % dict(THEMES[theme], font=FONT, mono=MONO)
    if has_html:
        root, _ = _tree(stored)
        body = '<div class="mail">' + _serialize(root, "simple", images, inline_url, blocked) + "</div>"
    else:
        body = '<div class="mail">' + plain_page(plain) + "</div>"
    return _document(simple_css, "", body, has_html, blocked[0], theme)


# ---------------------------------------------------------------- plain text as a page

URL = re.compile(r"https?://[^\s<>\"'）)】」，。]+", re.I)


def link_label(url):
    parts = urllib.parse.urlsplit(url)
    rest = (parts.path or "") + ("?" + parts.query if parts.query else "")
    if len(url) <= 60:
        return url
    short = (parts.hostname or "") + (rest[:24] + "…" if len(rest) > 1 else "")
    return short


def _linkify(line):
    out, last = [], 0
    for match in URL.finditer(line):
        url = match.group(0).rstrip(".,;:!?")
        out.append(html.escape(line[last:match.start()], quote=False))
        out.append('<a href="%s" title="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (
            html.escape(url, quote=True), html.escape(url, quote=True), html.escape(link_label(url))))
        last = match.start() + len(url)
    out.append(html.escape(line[last:], quote=False))
    return "".join(out)


def plain_page(value):
    """Plain-text mail as paragraphs: blank lines split paragraphs, quoted lines and the earlier messages of a reply
    are folded, the signature after "-- " is muted, links are clickable with short labels."""
    lines = tidy(value).split("\n") if value else []
    body, quote_from = [], None
    signature = None
    for index, line in enumerate(lines):
        if quote_from is None and REPLY_HEADER.match(line) and index > 0:
            quote_from = index
            break
        if signature is None and line.strip() in ("--", "-- ", "—", "——"):
            signature = index
    main = lines[:quote_from] if quote_from is not None else lines
    sig = []
    if signature is not None and (quote_from is None or signature < quote_from):
        main, sig = lines[:signature], lines[signature + 1:quote_from] if quote_from is not None else lines[signature + 1:]
    rest = lines[quote_from:] if quote_from is not None else []

    def paragraphs(block):
        html_parts, current, quoted = [], [], []

        def flush():
            if current:
                html_parts.append('<p class="para">' + "<br>".join(_linkify(l) for l in current) + "</p>")
                current.clear()
            if quoted:
                html_parts.append("<blockquote>" + "<br>".join(_linkify(l) for l in quoted) + "</blockquote>")
                quoted.clear()
        for line in block:
            if line.startswith(">"):
                if current:
                    flush()
                quoted.append(line.lstrip("> "))
            elif not line.strip():
                flush()
            else:
                if quoted:
                    flush()
                current.append(line)
        flush()
        return "".join(html_parts)
    out = paragraphs(main)
    if sig:
        out += '<div class="sig">' + paragraphs(sig) + "</div>"
    if rest:
        out += '<details class="quote"><summary>显示引用的原文</summary>' + paragraphs(rest) + "</details>"
    return out or '<p class="para">（这封邮件没有文字内容）</p>'
