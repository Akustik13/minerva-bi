"""
email_assistant/html_clean.py — чужий HTML, який Minerva вставляє у СВОЮ сторінку: цитата у відповіді й пересиланні,
збережена чернетка, запланований лист.

Форма листа (compose.html, картка запланованого листа) — сторінка Minerva без CSP, тож будь-який `<img onerror>`,
`<svg onload>` чи `javascript:` у цитаті виконався б від імені користувача. Тому — суворий allow-list на
`html.parser` (без сторонніх бібліотек), а не «прибрати script»:
  - теги з ALLOWED_TAGS лишаються; script / style / svg / math / iframe / object / embed / form / template, поля форм,
    <head> і <title> — разом із вмістом (DROP_WITH_CONTENT); решта невідомих (html, body, o:p…) — лише тег, текст
    лишається;
  - атрибути — лише з ALLOWED_ATTRS (жодних on…, id, name); class — лише службові класи Minerva (підпис);
  - style — без position, expression(, url(, javascript:, @import, behavior, -moz-binding і CSS-escape «\\»;
  - href — http(s), mailto, #якір; src — http(s), cid: (картинки листа), data:image/png|jpeg|gif|webp;
    інакше атрибут прибрано;
  - коментарі, <!DOCTYPE>, <?…?> — прибрано; текст і значення атрибутів екрануються заново;
  - закриваються лише відкриті тут теги (зайвий </div> не «вийде» з поля листа), незакриті — закриваються наприкінці.
"""
import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset({
    'a', 'abbr', 'address', 'article', 'b', 'bdi', 'bdo', 'big', 'blockquote', 'br', 'caption', 'center', 'cite',
    'code', 'col', 'colgroup', 'dd', 'del', 'details', 'dfn', 'div', 'dl', 'dt', 'em', 'figcaption', 'figure',
    'font', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'i', 'img', 'ins', 'kbd', 'li', 'main',
    'mark', 'ol', 'p', 'pre', 'q', 's', 'samp', 'section', 'small', 'span', 'strike', 'strong', 'sub', 'summary',
    'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'tt', 'u', 'ul', 'var', 'wbr',
})
VOID_TAGS = frozenset({'br', 'col', 'hr', 'img', 'wbr',
                       'area', 'base', 'embed', 'input', 'link', 'meta', 'param', 'source', 'track', 'keygen'})
DROP_WITH_CONTENT = frozenset({
    'script', 'style', 'svg', 'math', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'form', 'template',
    'noscript', 'noembed', 'noframes', 'xmp', 'plaintext', 'textarea', 'select', 'option', 'button', 'head', 'title',
    'canvas', 'audio', 'video', 'xml', 'portal',
})

GLOBAL_ATTRS = frozenset({'style', 'title', 'dir', 'lang', 'align', 'class'})
ALLOWED_ATTRS = {
    'a': {'href'},
    'img': {'src', 'alt', 'width', 'height', 'border', 'hspace', 'vspace'},
    'table': {'border', 'cellpadding', 'cellspacing', 'width', 'height', 'bgcolor'},
    'td': {'colspan', 'rowspan', 'width', 'height', 'valign', 'bgcolor', 'nowrap'},
    'th': {'colspan', 'rowspan', 'width', 'height', 'valign', 'bgcolor', 'nowrap', 'scope'},
    'tr': {'valign', 'bgcolor', 'height'},
    'tbody': {'valign'}, 'thead': {'valign'}, 'tfoot': {'valign'},
    'col': {'span', 'width'}, 'colgroup': {'span', 'width'},
    'font': {'color', 'face', 'size'},
    'ol': {'start', 'type'}, 'ul': {'type'}, 'li': {'value', 'type'},
    'hr': {'width', 'size', 'noshade', 'color'},
    'div': {'width'}, 'p': set(), 'span': set(),
}
URL_ATTRS = frozenset({'href', 'src'})
ALLOWED_CLASSES = frozenset({'em-sig-in', 'se-sig-div'})   # підпис у формі листа / запланованого листа
HREF_SCHEMES = frozenset({'http', 'https', 'mailto'})
SRC_SCHEMES = frozenset({'http', 'https', 'cid'})
_DATA_IMAGE = re.compile(r'^data:image/(png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]*$', re.IGNORECASE)
_CONTROL = re.compile(r'[\x00-\x20\x7f]+')
_STYLE_PROP = re.compile(r'^-?[a-z][a-z-]*$')
_STYLE_BAD_PROPS = frozenset({'position', 'behavior', '-moz-binding', '-ms-behavior', 'content'})
_STYLE_BAD_VALUE = re.compile(r'expression\s*\(|url\s*\(|image-set\s*\(|javascript:|vbscript:|@import|\\|<|>|/\*',
                              re.IGNORECASE)


def safe_url(value: str, attr: str) -> str:
    """Адреса посилання / картинки або '' (небезпечна схема, відносна адреса, data: не картинка)."""
    compact = _CONTROL.sub('', value or '')
    if not compact:
        return ''
    if attr == 'href' and compact.startswith('#'):
        return value.strip()
    if attr == 'src' and _DATA_IMAGE.match(compact):
        return compact
    m = re.match(r'^([a-z][a-z0-9+.-]*):', compact, re.IGNORECASE)
    if not m:
        return ''   # відносна адреса (/admin/…) у листі нічого не означає — і не має вести всередину Minerva
    allowed = HREF_SCHEMES if attr == 'href' else SRC_SCHEMES
    return value.strip() if m.group(1).lower() in allowed else ''


def safe_style(value: str) -> str:
    """CSS атрибута style лише з безпечних властивостей (без position, url(), expression() …)."""
    out = []
    for decl in (value or '').split(';'):
        prop, sep, val = decl.partition(':')
        prop, val = prop.strip().lower(), val.strip()
        if not sep or not val or not _STYLE_PROP.match(prop) or prop in _STYLE_BAD_PROPS:
            continue
        if _STYLE_BAD_VALUE.search(val):
            continue
        out.append(f'{prop}:{val}')
    return ';'.join(out)


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open = []          # відкриті дозволені теги
        self.skip = None        # (тег, глибина) — усередині тегу, що прибирається з вмістом

    # ── теги ──
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        tag = (tag or '').lower()
        if self.skip is not None:
            if self.skip[0] == 'head' and tag == 'body':
                self.skip = None   # </head> можна не писати — <body> його закриває
                return
            if tag == self.skip[0] and not closed and tag not in VOID_TAGS:
                self.skip = (tag, self.skip[1] + 1)
            return
        if tag in DROP_WITH_CONTENT:
            if not closed and tag not in VOID_TAGS:
                self.skip = (tag, 1)
            return
        if tag not in ALLOWED_TAGS:
            return
        parts = [tag]
        allowed = GLOBAL_ATTRS | ALLOWED_ATTRS.get(tag, set())
        for name, value in attrs:
            name = (name or '').lower()
            if name not in allowed or name.startswith('on'):
                continue
            value = value if value is not None else ''
            if name in URL_ATTRS:
                value = safe_url(value, name)
                if not value:
                    continue
            elif name == 'style':
                value = safe_style(value)
                if not value:
                    continue
            elif name == 'class':
                value = ' '.join(c for c in value.split() if c in ALLOWED_CLASSES)
                if not value:
                    continue
            parts.append(f'{name}="{html.escape(value, quote=True)}"')
        self.out.append('<' + ' '.join(parts) + '>')
        if tag in VOID_TAGS:
            return
        if closed:
            self.out.append(f'</{tag}>')
        else:
            self.open.append(tag)

    def handle_endtag(self, tag):
        tag = (tag or '').lower()
        if self.skip is not None:
            if tag == self.skip[0]:
                depth = self.skip[1] - 1
                self.skip = (tag, depth) if depth > 0 else None
            return
        if tag in VOID_TAGS or tag not in self.open:
            return
        while self.open:   # закрити й вкладені незакриті теги — розмітка лишається цілою
            last = self.open.pop()
            self.out.append(f'</{last}>')
            if last == tag:
                break

    # ── вміст ──
    def handle_data(self, data):
        if self.skip is None and data:
            self.out.append(html.escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def result(self) -> str:
        self.close()
        while self.open:
            self.out.append(f'</{self.open.pop()}>')
        return ''.join(self.out)


def clean_html(raw: str) -> str:
    """Безпечний HTML для вставки в сторінку Minerva (див. опис модуля). Порожнє / None → ''."""
    if not raw:
        return ''
    parser = _Cleaner()
    try:
        parser.feed(str(raw))
        return parser.result().strip()
    except Exception:  # noqa: BLE001 — зламаний HTML: краще текст без розмітки, ніж чужий код
        return html.escape(re.sub(r'<[^>]*>', ' ', str(raw)), quote=False).strip()
