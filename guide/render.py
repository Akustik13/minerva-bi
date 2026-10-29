"""
Markdown гіда → HTML (свій невеликий рендер, без залежностей: увесь текст екранується, теги — лише наші).

Блоки: # … ####, абзаци, списки «- » / «1. » (вкладені — відступ 2+ пробіли), ```код```, > цитата / підказка
(«> 💡 …», «> ⚠️ …»), таблиці | a | b |, «---». Рядок {{api_endpoints}} / {{api_endpoints:orders}} — таблиця
API з коду (api_docs.py). У тексті: **жирне**, *курсив*, `код`, [текст](адреса):
  guide:розділ/сторінка[#якір] → /guide/…/; /адреса Minerva — як є; http(s):// — нова вкладка; інше — лише текст.
{{base_url}} — адреса цього сервера (і в коді прикладів).
"""
import html
import re

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_UL_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_DIRECTIVE_RE = re.compile(r"^\{\{\s*api_endpoints(?::([\w/.-]+))?\s*\}\}$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])")
CALLOUTS = {"💡": "tip", "⚠️": "warn", "⚠": "warn", "ℹ️": "info", "✅": "ok", "❗": "warn"}


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.U).strip()
    return re.sub(r"[\s_]+", "-", text)[:60] or "section"


def link_href(target: str):
    """→ (адреса, зовнішня) або (None, False) — тоді посилання лишається текстом."""
    if target.startswith("guide:"):
        slug, _, anchor = target[6:].partition("#")
        return f"/guide/{slug.strip('/')}/" + (f"#{anchor}" if anchor else ""), False
    if target.startswith("/") and not target.startswith("//"):
        return target, False
    if target.startswith("#"):
        return target, False
    if re.match(r"^https?://", target):
        return target, True
    return None, False


def inline(text: str, base_url: str = "") -> str:
    """Рядок → HTML: екранування, `код`, посилання, **жирне**, *курсив*."""
    text = text.replace("{{base_url}}", base_url)
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_RE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)

    def link(m):
        label, target = m.group(1), m.group(2)
        href, external = link_href(target)
        label_html = _emphasis(html.escape(label))
        if href is None:
            return keep(label_html)
        extra = ' target="_blank" rel="noopener noreferrer"' if external else ""
        cls = ' class="gd-ext"' if external else (' class="gd-guide"' if target.startswith("guide:") else "")
        return keep(f'<a href="{html.escape(href)}"{cls}{extra}>{label_html}</a>')

    text = _LINK_RE.sub(link, text)
    text = _emphasis(html.escape(text))
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _emphasis(escaped: str) -> str:
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return _ITALIC_RE.sub(r"<em>\1</em>", escaped)


def _table(rows, base_url):
    def cells(row):
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]
    head, body = cells(rows[0]), [cells(r) for r in rows[2:]]
    out = ['<div class="gd-table"><table><thead><tr>']
    out += [f"<th>{inline(c, base_url)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{inline(c, base_url)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _list(items, base_url):
    """items: [(відступ, ordered, текст)] → вкладені <ul>/<ol>."""
    out, stack = [], []
    for indent, ordered, text in items:
        while stack and indent < stack[-1][0]:
            out.append("</li></%s>" % stack.pop()[1])
        if not stack or indent > stack[-1][0]:
            tag = "ol" if ordered else "ul"
            stack.append((indent, tag))
            out.append(f"<{tag}><li>")
        else:
            out.append("</li><li>")
        out.append(inline(text, base_url))
    while stack:
        out.append("</li></%s>" % stack.pop()[1])
    return "".join(out)


def render(body: str, base_url: str = "", directives=None):
    """Markdown → (html, зміст [(рівень, id, текст)]). directives(name, arg) → HTML для {{…}} рядків."""
    lines = body.replace("\r\n", "\n").split("\n")
    out, toc, used_ids = [], [], set()
    i, n = 0, len(lines)
    para = []

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(s.strip() for s in para), base_url) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            lang = re.sub(r"[^\w-]", "", stripped[3:].strip())
            code = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            text = "\n".join(code).replace("{{base_url}}", base_url)
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f'<div class="gd-code"><button type="button" class="gd-copy" title="Копіювати">📋</button>'
                       f"<pre><code{cls}>{html.escape(text)}</code></pre></div>")
            continue
        if not stripped:
            flush()
            i += 1
            continue
        m = _DIRECTIVE_RE.match(stripped)
        if m:
            flush()
            out.append(directives("api_endpoints", m.group(1)) if directives else "")
            i += 1
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            flush()
            level, text = len(m.group(1)), m.group(2).strip()
            anchor = base = slugify(re.sub(r"[`*]", "", text))
            k = 2
            while anchor in used_ids:
                anchor, k = f"{base}-{k}", k + 1
            used_ids.add(anchor)
            if level in (2, 3):
                toc.append((level, anchor, re.sub(r"[`*]", "", text)))
            out.append(f'<h{level} id="{anchor}">{inline(text, base_url)}</h{level}>')
            i += 1
            continue
        if re.match(r"^-{3,}$", stripped):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            first = quote[0] if quote else ""
            kind = next((cls for mark, cls in CALLOUTS.items() if first.startswith(mark)), "note")
            paragraphs, current = [], []
            for q in quote + [""]:                       # порожній рядок «>» — новий абзац
                if q:
                    current.append(q)
                elif current:
                    paragraphs.append(" ".join(current))
                    current = []
            inner = "".join(f"<p>{inline(p, base_url)}</p>" for p in paragraphs)
            out.append(f'<div class="gd-callout gd-{kind}">{inner}</div>')
            continue
        if stripped.startswith("|") and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            flush()
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows, base_url))
            continue
        if _UL_RE.match(line) or _OL_RE.match(line):
            flush()
            items = []
            while i < n:
                cur = lines[i]
                mu, mo = _UL_RE.match(cur), _OL_RE.match(cur)
                if mu:
                    items.append((len(mu.group(1)), False, mu.group(2)))
                elif mo:
                    items.append((len(mo.group(1)), True, mo.group(3)))
                elif cur.strip() and cur.startswith("  ") and items:          # продовження пункту
                    indent, ordered, text = items[-1]
                    items[-1] = (indent, ordered, text + " " + cur.strip())
                else:
                    break
                i += 1
            out.append(_list(items, base_url))
            continue
        para.append(line)
        i += 1
    flush()
    return "\n".join(out), toc


def links(body: str) -> list:
    """Усі цілі посилань [текст](ціль) поза блоками коду — для перевірки."""
    out, in_code = [], False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            text = _CODE_RE.sub("", line)
            out += [m.group(2) for m in _LINK_RE.finditer(text)]
    return out


def directives_used(body: str) -> list:
    return [m.group(1) or "" for line in body.splitlines() if (m := _DIRECTIVE_RE.match(line.strip()))]
