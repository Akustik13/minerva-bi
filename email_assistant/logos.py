"""
email_assistant/logos.py — логотип відправника замість букви в кружку (список листів).

Значок сайту домену відправника (apple-touch-icon / icon зі сторінки, інакше /favicon.ico). Minerva бере його
сама з сайту цього домену, не через сторонні сервіси (браузер не повідомляє нікому, з ким ви листуєтесь), раз на
домен і у фоні: перше відкриття списку — буква, далі — логотип з копії MEDIA_ROOT/email_logos/.
Без логотипа: поштові сервіси (gmail.com…, `crm_link.FREE_MAIL_DOMAINS`) і домени, адреса яких — локальна /
приватна мережа (листом не можна змусити Minerva звертатися всередину мережі). Не знайшли — повтор за
`RETRY_DAYS`. Лише растрові формати за першими байтами (без SVG). Вимикається — `EmailSettings.sender_logos`.
"""
import hashlib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

from django.conf import settings

logger = logging.getLogger('email_assistant')

DIR = 'email_logos'
RETRY_DAYS = 30
MAX_PAGE = 400_000
MAX_IMAGE = 300_000
TIMEOUT = 5
PARALLEL = 3
DOMAIN_RE = re.compile(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$')
TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'gif': 'image/gif', 'ico': 'image/x-icon', 'webp': 'image/webp'}

_pending = set()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(PARALLEL)


def domain_for(address: str) -> str:
    """Домен для логотипа: eu.digikey.com → digikey.com; поштові сервіси й погані адреси → ''."""
    from email_assistant.crm_link import FREE_MAIL_DOMAINS
    address = (address or '').strip().lower()
    if '@' not in address:
        return ''
    domain = address.rsplit('@', 1)[1].strip('.> ')
    parts = domain.split('.')
    if len(parts) >= 3 and parts[-2] in ('co', 'com', 'org', 'net', 'ac', 'gov') and len(parts[-1]) == 2:
        domain = '.'.join(parts[-3:])
    elif len(parts) >= 2:
        domain = '.'.join(parts[-2:])
    if domain in FREE_MAIL_DOMAINS or not DOMAIN_RE.match(domain):
        return ''
    return domain


def _base(domain: str) -> str:
    return os.path.join(settings.MEDIA_ROOT, DIR, hashlib.sha1(domain.encode()).hexdigest())


def cached(domain: str):
    """(шлях, тип) копії логотипа або None."""
    base = _base(domain)
    for ext, ctype in TYPES.items():
        if os.path.exists(f'{base}.{ext}'):
            return f'{base}.{ext}', ctype
    return None


# ── 🖼 Свій значок (людина вибрала): для адреси або домену, замість автоматичного; «буква» — без логотипа;
#    стандартний значок — емодзі на кольоровому кружку (файл <ключ>.icon: «емодзі\nколір», SVG малює icon_svg) ──
CUSTOM = 'custom'
CUSTOM_MAX = 1_000_000
KEY_RE = re.compile(r'^(?:[a-z0-9._%+-]+@)?[a-z0-9-]+(\.[a-z0-9-]+)+$')
LETTER = 'letter'
ICON = 'icon'
STANDARD_ICONS = ('🏢', '🏭', '🏬', '🛒', '📦', '🚚', '✈️', '🚢', '🏦', '💶', '🧾', '📑', '⚙️', '🔧', '🔬', '🧪',
                  '💡', '📡', '🔌', '🖥️', '📱', '🛰️', '🎓', '🏥', '🏛️', '⚖️', '📰', '📣', '🤝', '👤', '👥', '⭐',
                  '❤️', '🔒', '🤖', '🌐', '📬', '🏠')
STANDARD_COLORS = ('#1a73e8', '#0f9d58', '#f4b400', '#db4437', '#8e24aa', '#00897b', '#6d4c41', '#546e7a',
                   '#263238', '#ffffff')
COLOR_RE = re.compile(r'^#[0-9a-f]{6}$')


def _icon_ok(icon: str) -> bool:
    """Стандартний або свій емодзі: до 8 символів, без звичайних літер / цифр / розмітки."""
    return icon in STANDARD_ICONS or (0 < len(icon) <= 8 and not any(ch.isascii() for ch in icon))


def icon_svg(path: str) -> bytes:
    """SVG стандартного значка (емодзі на кружку) з файла <ключ>.icon."""
    from html import escape
    try:
        with open(path, encoding='utf-8') as fh:
            icon, _, color = fh.read().partition('\n')
    except OSError:
        icon, color = '', ''
    icon, color = icon.strip(), color.strip().lower()
    if not _icon_ok(icon):
        icon = '✉️'
    if not COLOR_RE.match(color):
        color = STANDARD_COLORS[0]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" '
            f'fill="{color}"/><text x="32" y="34" text-anchor="middle" dominant-baseline="central" font-size="34" '
            f'font-family="Segoe UI Emoji, Apple Color Emoji, Noto Color Emoji, sans-serif">{escape(icon)}</text>'
            '</svg>').encode('utf-8')


def _custom_dir() -> str:
    return os.path.join(settings.MEDIA_ROOT, DIR, CUSTOM)


def custom_map() -> dict:
    """{ключ (адреса або домен): (шлях | LETTER, mtime)} — свої значки, один перелік теки на сторінку."""
    out = {}
    try:
        names = os.listdir(_custom_dir())
    except OSError:
        return out
    for name in names:
        key, _, ext = name.rpartition('.')
        if not KEY_RE.match(key) or (ext not in TYPES and ext not in (LETTER, ICON)):
            continue
        path = os.path.join(_custom_dir(), name)
        out[key] = (LETTER if ext == LETTER else path, int(os.path.getmtime(path)))
    return out


def set_custom(key: str, data: bytes = b'', letter: bool = False, icon: str = '', color: str = '') -> str:
    """Свій значок для адреси / домену (data — PNG / JPG / GIF / ICO / WEBP), icon — стандартний значок (емодзі на
    кружку кольору color), letter — буква без логотипа, усе порожнє — назад автоматичний.
    → що тепер: 'image' | 'icon' | 'letter' | 'auto'. ValueError — не той файл / значок."""
    key = (key or '').strip().lower()
    if not KEY_RE.match(key):
        raise ValueError('Неправильна адреса або домен')
    kind = _kind(data) if data else ''
    if data and not kind:
        raise ValueError('Потрібне зображення PNG, JPG, GIF, ICO або WEBP')
    if len(data) > CUSTOM_MAX:
        raise ValueError('Зображення завелике (до 1 МБ)')
    icon, color = (icon or '').strip(), (color or '').strip().lower()
    if icon and not _icon_ok(icon):
        raise ValueError('Виберіть значок зі списку або один емодзі')
    if icon and not COLOR_RE.match(color):
        color = STANDARD_COLORS[0]
    os.makedirs(_custom_dir(), exist_ok=True)
    for ext in list(TYPES) + [LETTER, ICON]:
        try:
            os.remove(os.path.join(_custom_dir(), f'{key}.{ext}'))
        except OSError:
            pass
    if data:
        with open(os.path.join(_custom_dir(), f'{key}.{kind}'), 'wb') as fh:
            fh.write(data)
        return 'image'
    if icon:
        with open(os.path.join(_custom_dir(), f'{key}.{ICON}'), 'w', encoding='utf-8') as fh:
            fh.write(f'{icon}\n{color}')
        return 'icon'
    if letter:
        open(os.path.join(_custom_dir(), f'{key}.{LETTER}'), 'wb').close()
        return 'letter'
    return 'auto'


def logo_for(address: str, custom: dict | None = None):
    """(ключ для /email/logo/<ключ>/, версія) для рядка листа: свій значок адреси → свій домену → автоматичний
    домену; «буква» — ('', ''); версія стандартного значка починається з «i» (кружок без білої рамки)."""
    custom = custom_map() if custom is None else custom
    address = (address or '').strip().lower()
    domain = domain_for(address)
    full = address.split('@', 1)[1] if '@' in address else ''
    for key in (address, full, domain):
        if key and key in custom:
            path, mtime = custom[key]
            if path == LETTER:
                return ('', '')
            return (key, ('i' if path.endswith('.' + ICON) else '') + str(mtime))
    return (domain, '') if domain else ('', '')


def _missing_recently(domain: str) -> bool:
    try:
        return time.time() - os.path.getmtime(_base(domain) + '.none') < RETRY_DAYS * 86400
    except OSError:
        return False


def _public(host: str) -> bool:
    """Усі адреси хоста — публічний інтернет (не локальна / приватна мережа)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def _get(url: str, limit: int):
    """GET з перевіркою кожної адреси (і після переадресації) → (адреса, байти) або None."""
    import requests
    for _ in range(4):
        host = urlparse(url).hostname or ''
        if urlparse(url).scheme not in ('http', 'https') or not _public(host):
            return None
        try:
            r = requests.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True,
                             headers={'User-Agent': 'Mozilla/5.0 (Minerva mail; logo)'})
        except requests.RequestException:
            return None
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get('Location', ''))
            r.close()
            continue
        if r.status_code != 200:
            r.close()
            return None
        data = b''
        for chunk in r.iter_content(16384):
            data += chunk
            if len(data) > limit:
                r.close()
                return None
        return url, data
    return None


def _kind(data: bytes) -> str:
    """Формат за першими байтами (не за розширенням і не за заголовком)."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data.startswith(b'\x00\x00\x01\x00'):
        return 'ico'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return ''


_LINK_RE = re.compile(r'<link\b[^>]*>', re.I)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')


def icon_urls(page_url: str, html: str) -> list:
    """Значки зі сторінки: apple-touch-icon, далі icon за розміром (більший — краще), без SVG."""
    found = []
    for tag in _LINK_RE.findall(html or ''):
        attrs = {k.lower(): v.strip('"\'') for k, v in _ATTR_RE.findall(tag)}
        rel, href = attrs.get('rel', '').lower(), attrs.get('href', '')
        if not href or 'icon' not in rel or href.lower().split('?')[0].endswith('.svg') \
                or 'svg' in attrs.get('type', '').lower() or 'mask-icon' in rel:
            continue
        size = max([int(n) for n in re.findall(r'(\d+)x\d+', attrs.get('sizes', ''))] or [0])
        found.append((0 if 'apple-touch-icon' in rel else 1, -size, urljoin(page_url, href)))
    return [u for *_k, u in sorted(found)]


def fetch(domain: str) -> bool:
    """Знайти й зберегти логотип домену; не знайшли — позначка `.none` (повтор за RETRY_DAYS)."""
    os.makedirs(os.path.join(settings.MEDIA_ROOT, DIR), exist_ok=True)
    candidates = []
    for root in (f'https://{domain}/', f'https://www.{domain}/'):
        page = _get(root, MAX_PAGE)
        if page:
            candidates += icon_urls(page[0], page[1].decode('utf-8', 'ignore'))
            candidates.append(urljoin(page[0], '/favicon.ico'))
            break
    else:
        candidates.append(f'https://{domain}/favicon.ico')
    for url in list(dict.fromkeys(candidates))[:5]:
        got = _get(url, MAX_IMAGE)
        kind = _kind(got[1]) if got else ''
        if kind:
            with open(f'{_base(domain)}.{kind}', 'wb') as f:
                f.write(got[1])
            return True
    with open(_base(domain) + '.none', 'w') as f:
        f.write(domain)
    return False


def start(domain: str):
    """У фоні, якщо копії ще немає і нещодавно не шукали (одночасно — до PARALLEL доменів)."""
    if not domain or cached(domain) or _missing_recently(domain):
        return
    with _lock:
        if domain in _pending:
            return
        _pending.add(domain)

    def work():
        try:
            with _slots:
                fetch(domain)
        except Exception as e:  # noqa: BLE001 — логотип не повинен нічого ламати
            logger.warning('sender logo %s: %s', domain, e)
        finally:
            with _lock:
                _pending.discard(domain)

    threading.Thread(target=work, daemon=True).start()
