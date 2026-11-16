"""
email_assistant/desktop.py — 🖥 «Пошта Minerva» для Windows (desktop/mail_app, Electron): окрема програма, що
показує ту саму пошту /email/ у своєму вікні — вигляд і функції збігаються з браузером і оновлюються разом із
Minerva. Тут — те, що потрібно з боку сервера (сторінки — email_assistant/views_desktop.py):

  is_app(request) / app_version(request) — запит із програми (User-Agent «MinervaMail/<версія>»): пошта без
                                бокового меню адмінки, без посилання «🖥 Пошта для Windows»;
  keep_session(request)       — програма лишається підключеною: сесія діє APP_SESSION_DAYS від останнього звернення
                                (оновлюється, коли лишилось менше KEEP_BELOW_DAYS — не на кожен запит);
  new_pairing(user, base_url) — одноразовий код (PAIR_TTL) і посилання minerva-mail://pair?url=…&code=… —
                                «🔗 Підключити програму» в браузері відкриває програму вже з адресою й кодом;
  pairing_user(code)          — чий код (не використовуючи) — сторінка підтвердження у вікні програми;
  redeem(code, ip)            — використати код (лише раз; з однієї адреси — PAIR_PER_HOUR спроб) → користувач;
  pair_state(user, key)       — сторінка «🖥 Пошта для Windows» опитує, чи програма вже підключилась.

Паролів програма не зберігає: вхід — звичайна сторінка входу Minerva у вікні програми або код зі сторінки
«🖥 Пошта для Windows»; сесія (cookie) живе в профілі вікна програми, «Вийти» в меню програми її закриває.
"""
import datetime
import hashlib
import re
import secrets
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

UA_RE = re.compile(r"\bMinervaMail/(\d[\w.\-]{0,19})")
SCHEME = "minerva-mail"
PAIR_TTL = 600                 # код підключення діє 10 хв
PAIR_PER_HOUR = 20             # спроб використати код з однієї адреси за годину
APP_SESSION_DAYS = 30          # сесія програми — 30 днів від останнього звернення
KEEP_BELOW_DAYS = 23           # продовжувати, коли лишилось менше (≈ раз на тиждень, а не на кожен запит)
APP_URL = getattr(settings, "MINERVA_MAIL_APP_URL",
                  "https://github.com/Akustik13/minerva-bi/releases/download/mail-desktop/Minerva-Mail-Setup.exe")
RELEASE_URL = getattr(settings, "MINERVA_MAIL_RELEASE_URL",
                      "https://github.com/Akustik13/minerva-bi/releases/tag/mail-desktop")
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


class PairError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash(value) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def app_version(request) -> str:
    """Версія програми з User-Agent («MinervaMail/1.0.0») або '' — звичайний браузер."""
    m = UA_RE.search(request.META.get("HTTP_USER_AGENT", "") if request is not None else "")
    return m.group(1) if m else ""


def is_app(request) -> bool:
    return bool(app_version(request))


def can_use(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


def keep_session(request) -> bool:
    """Запит із програми від людини, що увійшла: сесія діє APP_SESSION_DAYS від останнього звернення. Звичайного
    браузера не чіпає. → True, якщо строк продовжено."""
    if not is_app(request) or not can_use(getattr(request, "user", None)):
        return False
    session = getattr(request, "session", None)
    if session is None:
        return False
    try:
        if session.get_expiry_age() >= KEEP_BELOW_DAYS * 86400:
            return False
        session.set_expiry(timezone.now() + datetime.timedelta(days=APP_SESSION_DAYS))
        return True
    except Exception:   # noqa: BLE001 — сесія без бази (тести, cookie-сесії) — пошта працює й так
        return False


# ── 🔔 Нові листи для сповіщень Windows ──

NEW_MAX = 5                    # листів у відповіді (більше — програма показує одне «N нових листів»)


def new_messages(account, after, limit=NEW_MAX) -> list:
    """Непрочитані листи «Вхідних» скриньки, новіші за after (pk), найновіші першими — для сповіщення Windows:
    [{pk, from, subject}]. Як лічильник «Вхідних»: без спаму, видалених, архівних гілок; лише від інших людей."""
    from email_assistant.models import EmailMessage, EmailThread
    from email_assistant.views import _inbox_q
    archived = EmailThread.objects.filter(account=account, is_archived=True).values('id')
    rows = (EmailMessage.objects.filter(account=account, pk__gt=after, is_read=False, is_deleted=False,
                                        is_spam=False, folder='inbox')
            .filter(_inbox_q(account)).exclude(thread_id__in=archived)
            .exclude(from_email__iexact=account.email_address)
            .order_by('-pk').values('pk', 'from_name', 'from_email', 'subject')[:max(1, min(limit, 20))])
    return [{'pk': r['pk'], 'from': (r['from_name'] or r['from_email'] or '').strip()[:120],
             'subject': (r['subject'] or '').strip()[:200]} for r in rows]


# ── 🔗 Підключення програми кодом ──

def pair_link(base_url, code) -> str:
    return f"{SCHEME}://pair?url={quote(base_url.rstrip('/'), safe='')}&code={quote(code, safe='')}"


def new_pairing(user, base_url) -> dict:
    """Одноразовий код для «🔗 Підключити програму» → {key, code, url, link, expires_in}. Код у базі не
    зберігається — лише його SHA-256 у спільному кеші на PAIR_TTL."""
    from core.shared_cache import cache
    if not can_use(user):
        raise PairError("Підключати програму можуть лише співробітники Minerva.", 403)
    code = secrets.token_urlsafe(24)
    key = _hash(code)
    cache.set(f"mail-app-pair:{key}", {"user": user.pk}, PAIR_TTL)
    base = base_url.rstrip("/")
    return {"key": key[:16], "code": code, "url": base, "link": pair_link(base, code), "expires_in": PAIR_TTL}


def _user_of(data):
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=(data or {}).get("user")).first()
    return user if can_use(user) else None


def pairing_user(code):
    """Користувач, якому належить код, або None (немає / застарів / уже використано). Код не використовується."""
    from core.shared_cache import cache
    code = str(code or "").strip()
    if not code or len(code) > 100:
        return None
    key = _hash(code)
    if cache.get(f"mail-app-pair-used:{key}"):
        return None
    return _user_of(cache.get(f"mail-app-pair:{key}"))


def _rate_limited(ip) -> bool:
    from core.shared_cache import cache
    rate_key = f"mail-app-pair-ip:{ip or '-'}:{timezone.now().strftime('%Y%m%d%H')}"
    cache.add(rate_key, 0, 3700)
    try:
        tries = cache.incr(rate_key)
    except ValueError:
        tries = 1
    return tries > PAIR_PER_HOUR


def redeem(code, ip=None, device=""):
    """Використати код (лише раз) → користувач. PairError — зрозуміла причина: код невідомий / застарів / уже
    використаний (410), людина більше не має доступу (403), забагато спроб (429)."""
    from core.shared_cache import cache
    if _rate_limited(ip):
        raise PairError("Забагато спроб підключення — спробуйте за годину.", 429)
    code = str(code or "").strip()
    key = _hash(code)
    data = cache.get(f"mail-app-pair:{key}") if code and len(code) <= 100 else None
    if not data or not cache.add(f"mail-app-pair-used:{key}", 1, PAIR_TTL + 60):
        raise PairError("Код підключення недійсний, уже використаний або застарів — у браузері на сторінці "
                        "«🖥 Пошта Minerva для Windows» натисніть «🔗 Підключити програму» ще раз.", 410)
    cache.delete(f"mail-app-pair:{key}")
    user = _user_of(data)
    if user is None:
        raise PairError("Цей акаунт Minerva вимкнено або він не має доступу до пошти.", 403)
    cache.set(f"mail-app-paired:{key[:16]}", {"user": user.pk, "device": str(device or "")[:80]}, PAIR_TTL + 300)
    return user


def pair_state(user, key) -> dict:
    """Сторінка «🖥 Пошта для Windows» опитує: {paired: False} — програма ще не використала код."""
    from core.shared_cache import cache
    key = str(key or "")
    if not _KEY_RE.match(key):
        return {"paired": False, "expired": True}
    state = cache.get(f"mail-app-paired:{key}")
    if not state or state.get("user") != getattr(user, "pk", None):
        return {"paired": False}
    return {"paired": True, "device": state.get("device") or ""}
