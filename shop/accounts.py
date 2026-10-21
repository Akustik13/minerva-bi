"""Особистий кабінет покупців інтернет-магазину: реєстрація, вхід, пароль, профіль, кошик, замовлення.

Уся логіка акаунтів — тут; API (api/accounts.py) лише перевіряє ключ і викликає ці функції.

Безпека:
  • пароль — хеш Django (make_password); ключі сесій і посилань у листах — лише SHA-256 у БД;
  • підтвердження email вимагає пароль — чуже «заздалегідь зареєстроване» на вашу адресу
    посилання не дає доступу до кабінету; скидання пароля теж підтверджує email;
  • вхід: 5 невдалих спроб → блокування на 15 хв; невідомий email — та сама відповідь і час;
  • register / forgot / resend відповідають однаково, чи є акаунт (не розкривають, хто зареєстрований);
  • листи — не частіше 1 на хвилину і 10 на добу на акаунт і тип;
  • посилання в листах ведуть лише на домен сайту магазину (Shop.url), токен — у #фрагменті URL.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from .models import Shop, ShopCustomer, ShopCustomerSession, ShopCustomerToken, ShopOrder

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)
INVITE_TTL = timedelta(days=7)              # акаунт створив менеджер: посилання «встановити пароль»
SESSION_TTL = timedelta(hours=12)           # без «запам'ятати»: від останньої дії
SESSION_TTL_REMEMBER = timedelta(days=30)
SESSION_MAX_AGE = timedelta(days=90)        # абсолютний максимум сесії
MAX_FAILED_LOGINS = 5
LOCK_TIME = timedelta(minutes=15)
MAIL_INTERVAL = timedelta(seconds=60)
MAIL_PER_DAY = 10
MIN_PASSWORD = 10
CART_MAX_LINES = 100

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_CART_KEY_RE = re.compile(r"^(rq:)?[A-Za-z0-9._\-/]{1,64}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_VAT_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{2,13}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Час перевірки пароля для невідомого email — як для справжнього хеша
_DUMMY_HASH = None


class AccountError(Exception):
    """Помилка для API: HTTP-статус, машинний код (переклад — на сайті), додаткові поля."""

    def __init__(self, status: int, code: str, message: str = "", **extra):
        super().__init__(message or code)
        self.status, self.code, self.message, self.extra = status, code, message or code, extra


# ── Дрібні помічники ─────────────────────────────────────────────────────────

def normalize_email(value) -> str:
    return str(value or "").strip().lower()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, _hash(raw)


def _text(data: dict, key: str, max_len: int, bad: list, required=False) -> str:
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        bad.append(key)
        return ""
    value = value.strip()
    if len(value) > max_len or _CTRL_RE.search(value) or (required and not value):
        bad.append(key)
    return value


_COMMON = None


def password_problems(password, email: str = "") -> list[str]:
    """Коди проблем пароля (переклад — на сайті): too_short, too_long, numeric, common, like_email."""
    global _COMMON
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return ["password_too_short"]
    out = []
    if len(password) > 200:
        out.append("password_too_long")
    if password.isdigit():
        out.append("password_numeric")
    from django.contrib.auth.password_validation import CommonPasswordValidator
    from django.core.exceptions import ValidationError
    if _COMMON is None:
        _COMMON = CommonPasswordValidator()  # список ~20 000 паролів читається один раз
    try:
        _COMMON.validate(password)
    except ValidationError:
        out.append("password_common")
    local = normalize_email(email).split("@")[0]
    if local and len(local) >= 4 and local in password.lower():
        out.append("password_like_email")
    return out


def _check_password_or_raise(password, email):
    problems = password_problems(password, email)
    if problems:
        raise AccountError(400, "weak_password", "Пароль не відповідає вимогам.", problems=problems,
                           min_length=MIN_PASSWORD, fields=["password"])


def _valid_email(email: str) -> bool:
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        return False
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def check_link(shop: Shop, link, lang: str = "") -> str:
    """Адреса сторінки кабінету на сайті (у неї дописується #verify=… / #reset=…).
    Дозволено лише https (http — тільки localhost) і, якщо задано «Адреса сайту» магазину, — той самий домен.
    Перевірена адреса запам'ятовується в магазині (для листів з адмінки)."""
    link = str(link or "").strip()
    parts = urlsplit(link)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if not parts.hostname or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local) \
            or parts.fragment or len(link) > 300:
        raise AccountError(400, "invalid_link", "Некоректна адреса сторінки кабінету.")
    if shop.url:
        allowed = urlsplit(shop.url).hostname
        if allowed and parts.hostname != allowed and not local:
            # видно в адмінці: що саме надсилає сайт і що вписати в «Адреса сайту»
            note = f"{timezone.localtime():%d.%m.%Y %H:%M} {link}"[:400]
            Shop.objects.filter(pk=shop.pk).update(account_link_error=note)
            shop.account_link_error = note
            raise AccountError(400, "link_not_allowed",
                               f"Адреса кабінету ({parts.hostname}) не збігається з «Адреса сайту» магазину ({allowed}).")
    if shop.account_link_error:
        Shop.objects.filter(pk=shop.pk).update(account_link_error="")
        shop.account_link_error = ""
    if lang:
        _remember_link(shop, lang, link)
    return link


def _remember_link(shop: Shop, lang: str, link: str):
    """Адреса кабінету потрібна листам з адмінки (скидання пароля, повторне підтвердження)."""
    links = dict(shop.account_links or {})
    if links.get(lang) != link:
        links[lang] = link
        Shop.objects.filter(pk=shop.pk).update(account_links=links)
        shop.account_links = links


def _lang(value) -> str:
    return value if value in ("de", "en") else "de"


# ── Листи ────────────────────────────────────────────────────────────────────

def _may_mail(customer: ShopCustomer, kind: str = "") -> bool:
    """Захист від «бомбардування» листами: не частіше MAIL_INTERVAL і MAIL_PER_DAY посилань типу на добу."""
    now = timezone.now()
    if customer.last_mail_at and now - customer.last_mail_at < MAIL_INTERVAL:
        return False
    if kind and customer.tokens.filter(kind=kind, created_at__gte=now - timedelta(days=1)).count() >= MAIL_PER_DAY:
        return False
    return True


def _mail(customer: ShopCustomer, kind: str, *, link: str = "", throttle: bool = True, **vars) -> bool:
    """Надсилає лист покупцю. Помилка SMTP не ламає запит — пишеться в акаунт (видно в адмінці)."""
    from . import mail
    now = timezone.now()
    if throttle and not _may_mail(customer):
        return False
    try:
        mail.send(customer.shop, customer.email, kind, customer.lang, name=customer.name, link=link, **vars)
        error = ""
    except Exception as e:  # SMTP недоступний / не налаштований
        import logging
        logging.getLogger("shop").warning("Shop mail %s → %s failed: %s", kind, customer.email, e)
        error = f"{timezone.localtime(now):%d.%m.%Y %H:%M} {kind}: {e}"[:255]
    ShopCustomer.objects.filter(pk=customer.pk).update(last_mail_at=now, last_mail_error=error)
    customer.last_mail_at, customer.last_mail_error = now, error
    return not error


def _issue(customer: ShopCustomer, kind: str, ttl: timedelta | None = None) -> str:
    """Нове посилання для листа; попередні такого типу перестають діяти."""
    customer.tokens.filter(kind=kind, used_at__isnull=True).update(used_at=timezone.now())
    raw, digest = _new_token()
    ttl = ttl or (VERIFY_TTL if kind == ShopCustomerToken.VERIFY else RESET_TTL)
    ShopCustomerToken.objects.create(customer=customer, kind=kind, token_hash=digest,
                                     expires_at=timezone.now() + ttl)
    return raw


def _mail_link(link: str, kind: str, raw: str) -> str:
    return f"{link}#{kind}={raw}"


def send_verification(customer: ShopCustomer, link: str, throttle: bool = True) -> bool:
    if throttle and not _may_mail(customer, ShopCustomerToken.VERIFY):
        return False
    raw = _issue(customer, ShopCustomerToken.VERIFY)
    return _mail(customer, ShopCustomerToken.VERIFY, link=_mail_link(link, "verify", raw), throttle=False,
                 hours=int(VERIFY_TTL.total_seconds() // 3600))


def send_reset(customer: ShopCustomer, link: str, throttle: bool = True) -> bool:
    if throttle and not _may_mail(customer, ShopCustomerToken.RESET):
        return False
    raw = _issue(customer, ShopCustomerToken.RESET)
    return _mail(customer, ShopCustomerToken.RESET, link=_mail_link(link, "reset", raw), throttle=False,
                 minutes=int(RESET_TTL.total_seconds() // 60))


def send_invite(customer: ShopCustomer, link: str) -> bool:
    """Акаунт створив менеджер: лист з посиланням «встановити пароль» (7 днів). Встановлення пароля
    підтверджує email (як скидання пароля) — пароль менеджер не задає і не бачить."""
    raw = _issue(customer, ShopCustomerToken.RESET, ttl=INVITE_TTL)
    return _mail(customer, "invite", link=_mail_link(link, "reset", raw), throttle=False,
                 days=INVITE_TTL.days)


def has_password(customer: ShopCustomer) -> bool:
    from django.contrib.auth.hashers import is_password_usable
    return bool(customer.password) and is_password_usable(customer.password)


def remember_links(shop: Shop, links: dict) -> None:
    """Сайт повідомляє адреси сторінки кабінету ({мова: адреса}) — для листів з адмінки."""
    for lang, link in (links or {}).items():
        if lang in ("de", "en") and link:
            try:
                check_link(shop, link, lang)
            except AccountError:
                pass


def admin_link(customer: ShopCustomer) -> str:
    """Адреса кабінету для листа з адмінки: остання, яку надсилав сайт (мова покупця або будь-яка)."""
    links = customer.shop.account_links or {}
    return links.get(customer.lang) or next(iter(links.values()), "")


# ── Профіль ──────────────────────────────────────────────────────────────────

PROFILE_FIELDS = [  # (поле API, поле моделі, макс. довжина)
    ("company", "company", 160), ("name", "name", 120), ("phone", "phone", 60), ("vat_id", "vat_id", 30),
    ("street", "street", 200), ("zip", "zip", 20), ("city", "city", 100), ("country", "country", 2),
]
SHIP_FIELDS = [("company", "ship_company", 160), ("name", "ship_name", 120), ("street", "ship_street", 200),
               ("zip", "ship_zip", 20), ("city", "ship_city", 100), ("country", "ship_country", 2),
               ("phone", "ship_phone", 60)]


def _apply_profile(customer: ShopCustomer, data: dict, *, require_company=True) -> list[str]:
    """Переносить дані профілю з запиту; повертає список некоректних полів."""
    bad = []
    for api_name, field, max_len in PROFILE_FIELDS:
        if api_name in data or api_name in ("company", "name"):
            value = _text(data, api_name, max_len, bad,
                          required=api_name == "name" or (api_name == "company" and require_company))
            if api_name in ("country", "vat_id"):
                value = value.upper().replace(" ", "")
            setattr(customer, field, value)
    if customer.country and not _COUNTRY_RE.match(customer.country):
        bad.append("country")
    if customer.vat_id and not _VAT_RE.match(customer.vat_id):
        bad.append("vat_id")
    ship = data.get("shipping")
    if isinstance(ship, dict):
        customer.ship_separate = bool(ship.get("separate"))
        for api_name, field, max_len in SHIP_FIELDS:
            value = _text(ship, api_name, max_len, bad)
            if api_name == "country":
                value = value.upper()
                if value and not _COUNTRY_RE.match(value):
                    bad.append("shipping.country")
            setattr(customer, field, value)
        if customer.ship_separate:
            for api_name, field, _ in SHIP_FIELDS:
                if api_name in ("name", "street", "zip", "city", "country") and not getattr(customer, field):
                    bad.append(f"shipping.{api_name}")
    if "lang" in data:
        customer.lang = _lang(data.get("lang"))
    return sorted(set(bad))


def profile(customer: ShopCustomer) -> dict:
    return {
        "email": customer.email,
        "company": customer.company, "name": customer.name, "phone": customer.phone, "vat_id": customer.vat_id,
        "address": {"street": customer.street, "zip": customer.zip, "city": customer.city, "country": customer.country},
        "shipping": {"separate": customer.ship_separate, "company": customer.ship_company, "name": customer.ship_name,
                     "street": customer.ship_street, "zip": customer.ship_zip, "city": customer.ship_city,
                     "country": customer.ship_country, "phone": customer.ship_phone},
        "lang": customer.lang,
        "created_at": customer.created_at,
        "last_login_at": customer.last_login_at,
        "cart_count": len(customer.cart or {}),
        "orders_count": customer.orders.count(),
    }


def update_profile(customer: ShopCustomer, data: dict) -> ShopCustomer:
    bad = _apply_profile(customer, data)
    if bad:
        raise AccountError(400, "invalid", "Перевірте поля.", fields=bad)
    customer.save()
    return customer


# ── Реєстрація і підтвердження ───────────────────────────────────────────────

def register(shop: Shop, data: dict, link: str) -> None:
    """Нова реєстрація. Відповідь для сайту завжди однакова (не розкриває, чи є акаунт)."""
    lang = _lang(data.get("lang"))
    link = check_link(shop, link, lang)
    email = normalize_email(data.get("email"))
    password = data.get("password")
    bad = [] if _valid_email(email) else ["email"]
    probe = ShopCustomer(shop=shop, email=email, lang=lang)
    bad += _apply_profile(probe, data)
    if bad:
        raise AccountError(400, "invalid", "Перевірте поля.", fields=sorted(set(bad)))
    _check_password_or_raise(password, email)
    if not data.get("confirm_b2b"):
        raise AccountError(400, "confirm_b2b", "Потрібне підтвердження покупки як підприємство.", fields=["confirm_b2b"])

    existing = ShopCustomer.objects.filter(shop=shop, email=email).first()
    if existing and existing.is_verified:
        # власник адреси дізнається про спробу; пароль не змінюється
        _raise_if_mail_failed(existing, _mail(existing, "exists", link=link))
        return
    with transaction.atomic():
        if existing:  # непідтверджена реєстрація: остання перемагає (власник адреси може «перезаписати» чужу)
            customer = existing
            for _, field, _ in PROFILE_FIELDS:
                setattr(customer, field, getattr(probe, field))
            customer.lang = lang
        else:
            customer = probe
        customer.password = make_password(password)
        customer.save()
        customer.sessions.all().delete()
    _raise_if_mail_failed(customer, send_verification(customer, link, throttle=bool(existing)))


def _raise_if_mail_failed(customer: ShopCustomer, sent: bool):
    """Реєстрація: лист не пішов через SMTP — сказати покупцю (акаунт лишається, повторна реєстрація надішле знову).
    Однаково для нового й наявного акаунта, тож це не розкриває, чи зареєстрована адреса."""
    if not sent and customer.last_mail_error:
        raise AccountError(503, "mail_failed", "Лист не надіслано: " + customer.last_mail_error)


def resend_verification(shop: Shop, email, link, lang="") -> None:
    link = check_link(shop, link, _lang(lang) if lang else "")
    customer = ShopCustomer.objects.filter(shop=shop, email=normalize_email(email), is_active=True).first()
    if customer and not customer.is_verified:
        send_verification(customer, link)


def _find_token(shop: Shop, raw, kind: str) -> ShopCustomerToken:
    raw = str(raw or "")
    token = None
    if _TOKEN_RE.match(raw):
        token = (ShopCustomerToken.objects.select_related("customer", "customer__shop")
                 .filter(token_hash=_hash(raw), kind=kind, customer__shop=shop).first())
    if not token or token.used_at or token.expires_at < timezone.now() or not token.customer.is_active:
        raise AccountError(400, "invalid_token", "Посилання недійсне або застаріле.")
    return token


def verify_email(shop: Shop, raw_token, password, remember=False) -> tuple[ShopCustomer, str, ShopCustomerSession]:
    """Підтвердження email за посиланням з листа + пароль (той, що при реєстрації). Одразу вхід."""
    token = _find_token(shop, raw_token, ShopCustomerToken.VERIFY)
    customer = token.customer
    _check_lock(customer)
    if not isinstance(password, str) or not check_password(password, customer.password):
        _login_failed(customer)
        raise AccountError(401, "invalid_credentials", "Невірний пароль.")
    now = timezone.now()
    with transaction.atomic():
        token.used_at = now
        token.save(update_fields=["used_at"])
        if not customer.is_verified:
            customer.email_verified_at = now
            customer.save(update_fields=["email_verified_at", "updated_at"])
        on_verified(customer)
    raw, session = start_session(customer, remember)
    return customer, raw, session


def on_verified(customer: ShopCustomer):
    """Email підтверджено: старі замовлення з цією адресою — в кабінет, зв'язок з клієнтом CRM."""
    attach_guest_orders(customer)
    link_crm(customer)


def link_crm(customer: ShopCustomer):
    if customer.crm_customer_id:
        return
    from crm.models import Customer
    crm = Customer.objects.filter(email__iexact=customer.email).order_by("-updated_at").first()
    if crm is None:
        order = (ShopOrder.objects.filter(customer=customer).select_related("order")
                 .order_by("-order__order_date", "-pk").first())
        crm = order.order.crm_customer if order else None
    if crm is not None:
        ShopCustomer.objects.filter(pk=customer.pk).update(crm_customer=crm)
        customer.crm_customer = crm


# ── Вхід, сесії ──────────────────────────────────────────────────────────────

def _check_lock(customer: ShopCustomer):
    if customer.locked_until and customer.locked_until > timezone.now():
        retry = int((customer.locked_until - timezone.now()).total_seconds()) + 1
        raise AccountError(429, "locked", "Забагато невдалих спроб. Спробуйте пізніше.", retry_after=retry)


def _login_failed(customer: ShopCustomer):
    fails = customer.failed_logins + 1
    update = {"failed_logins": fails}
    if fails >= MAX_FAILED_LOGINS:
        update = {"failed_logins": 0, "locked_until": timezone.now() + LOCK_TIME}
    ShopCustomer.objects.filter(pk=customer.pk).update(**update)
    for k, v in update.items():
        setattr(customer, k, v)


def start_session(customer: ShopCustomer, remember=False) -> tuple[str, ShopCustomerSession]:
    raw, digest = _new_token()
    now = timezone.now()
    remember = bool(remember)
    session = ShopCustomerSession.objects.create(
        customer=customer, token_hash=digest, remember=remember, last_used_at=now,
        expires_at=now + (SESSION_TTL_REMEMBER if remember else SESSION_TTL))
    ShopCustomer.objects.filter(pk=customer.pk).update(last_login_at=now, failed_logins=0, locked_until=None)
    customer.last_login_at, customer.failed_logins, customer.locked_until = now, 0, None
    return raw, session


def login(shop: Shop, email, password, remember=False) -> tuple[ShopCustomer, str, ShopCustomerSession]:
    global _DUMMY_HASH
    customer = ShopCustomer.objects.filter(shop=shop, email=normalize_email(email)).select_related("shop").first()
    if customer is None or not isinstance(password, str):
        if _DUMMY_HASH is None:
            _DUMMY_HASH = make_password("dummy-password-for-timing")
        check_password(str(password or ""), _DUMMY_HASH)
        raise AccountError(401, "invalid_credentials", "Невірний email або пароль.")
    _check_lock(customer)
    if not check_password(password, customer.password):
        _login_failed(customer)
        raise AccountError(401, "invalid_credentials", "Невірний email або пароль.")
    if not customer.is_active:
        raise AccountError(403, "account_disabled", "Акаунт вимкнено.")
    if not customer.is_verified:
        raise AccountError(403, "email_not_verified", "Email ще не підтверджено.")
    raw, session = start_session(customer, remember)
    return customer, raw, session


def authenticate(shop: Shop, raw) -> tuple[ShopCustomer, ShopCustomerSession] | None:
    """Покупець за ключем сесії (заголовок X-Customer-Session). Сесія подовжується від останньої дії."""
    raw = str(raw or "")
    if not _TOKEN_RE.match(raw):
        return None
    session = (ShopCustomerSession.objects.select_related("customer", "customer__shop")
               .filter(token_hash=_hash(raw), customer__shop=shop).first())
    now = timezone.now()
    if session is None:
        return None
    customer = session.customer
    if session.expires_at < now or session.created_at < now - SESSION_MAX_AGE \
            or not customer.is_active or not customer.is_verified:
        session.delete()
        return None
    if now - session.last_used_at > timedelta(minutes=5):  # не писати в БД на кожен запит
        ttl = SESSION_TTL_REMEMBER if session.remember else SESSION_TTL
        session.last_used_at, session.expires_at = now, now + ttl
        session.save(update_fields=["last_used_at", "expires_at"])
    return customer, session


def logout(session: ShopCustomerSession | None):
    if session is not None and session.pk:
        session.delete()


# ── Пароль ───────────────────────────────────────────────────────────────────

def forgot_password(shop: Shop, email, link, lang="") -> None:
    link = check_link(shop, link, _lang(lang) if lang else "")
    customer = ShopCustomer.objects.filter(shop=shop, email=normalize_email(email), is_active=True).first()
    if customer:
        send_reset(customer, link)


def reset_password(shop: Shop, raw_token, password, remember=False) -> tuple[ShopCustomer, str, ShopCustomerSession]:
    """Новий пароль за посиланням з листа: підтверджує й email, завершує всі сесії, одразу вхід."""
    token = _find_token(shop, raw_token, ShopCustomerToken.RESET)
    customer = token.customer
    _check_password_or_raise(password, customer.email)
    now = timezone.now()
    with transaction.atomic():
        token.used_at = now
        token.save(update_fields=["used_at"])
        customer.tokens.filter(used_at__isnull=True).update(used_at=now)
        customer.sessions.all().delete()
        was_verified = customer.is_verified
        customer.password = make_password(password)
        customer.email_verified_at = customer.email_verified_at or now
        customer.failed_logins, customer.locked_until = 0, None
        customer.save()
        if not was_verified:
            on_verified(customer)
    raw, session = start_session(customer, remember)
    _mail(customer, "changed", link=admin_link(customer), throttle=False)
    return customer, raw, session


def change_password(customer: ShopCustomer, session: ShopCustomerSession, current, new) -> None:
    _check_lock(customer)
    if not isinstance(current, str) or not check_password(current, customer.password):
        _login_failed(customer)
        raise AccountError(400, "invalid_password", "Поточний пароль невірний.", fields=["current_password"])
    _check_password_or_raise(new, customer.email)
    customer.password = make_password(new)
    customer.failed_logins = 0
    customer.save(update_fields=["password", "failed_logins", "updated_at"])
    customer.sessions.exclude(pk=session.pk).delete()  # інші пристрої — вийти
    _mail(customer, "changed", link=admin_link(customer), throttle=False)


def delete_account(customer: ShopCustomer, password) -> None:
    """Видалення акаунта покупцем (DSGVO): логін, адреси, кошик. Замовлення лишаються (строки зберігання)."""
    if not isinstance(password, str) or not check_password(password, customer.password):
        raise AccountError(400, "invalid_password", "Пароль невірний.", fields=["password"])
    _mail(customer, "deleted", throttle=False)
    customer.delete()


# ── Кошик ────────────────────────────────────────────────────────────────────

def clean_cart(items) -> dict:
    if not isinstance(items, dict) or len(items) > CART_MAX_LINES:
        raise AccountError(400, "invalid_cart", "Некоректний кошик.")
    out = {}
    for key, qty in items.items():
        if not isinstance(key, str) or not _CART_KEY_RE.match(key):
            raise AccountError(400, "invalid_cart", f"Некоректний артикул: {key!r}"[:120])
        try:
            q = Decimal(str(qty))
        except Exception:
            raise AccountError(400, "invalid_cart", "Некоректна кількість.")
        if not q.is_finite() or q <= 0 or q > 1_000_000:
            raise AccountError(400, "invalid_cart", "Некоректна кількість.")
        out[key] = int(q) if q == q.to_integral_value() else float(q)
    return out


def save_cart(customer: ShopCustomer, items) -> dict:
    customer.cart = clean_cart(items)
    customer.cart_updated_at = timezone.now()
    customer.save(update_fields=["cart", "cart_updated_at", "updated_at"])
    return cart_payload(customer)


def cart_payload(customer: ShopCustomer) -> dict:
    return {"items": customer.cart or {}, "updated_at": customer.cart_updated_at}


# ── Замовлення в кабінеті ────────────────────────────────────────────────────

BILLING_FIELDS = {"company": 160, "name": 120, "street": 200, "zip": 20, "city": 100, "country": 2,
                  "vat_id": 30, "email": 254, "phone": 60}


def clean_billing(data) -> dict:
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, max_len in BILLING_FIELDS.items():
        value = data.get(key)
        if isinstance(value, str) and value.strip() and not _CTRL_RE.search(value):
            value = value.strip()[:max_len]
            out[key] = value.upper() if key in ("country", "vat_id") else value
    return out


def record_order(order, shop: Shop, *, session_token=None, vat_rate=None, billing=None, lang="",
                 requested=None) -> ShopOrder:
    """Замовлення / запит із сайту: зв'язок з акаунтом (сесія або підтверджений email) і дані для кабінету."""
    customer = None
    if session_token:
        found = authenticate(shop, session_token)
        customer = found[0] if found else None
    if customer is None and order.email:
        customer = ShopCustomer.objects.filter(shop=shop, email=normalize_email(order.email),
                                               email_verified_at__isnull=False, is_active=True).first()
    billing = clean_billing(billing)
    requested = [{"sku": str(r["sku"])[:64], "qty": float(r["qty"])} for r in (requested or [])][:60]
    info, _ = ShopOrder.objects.update_or_create(order=order, defaults={
        "shop": shop, "customer": customer, "vat_rate": vat_rate, "billing": billing,
        "requested": requested, "lang": _lang(lang) if lang else "",
    })
    if customer is not None:
        _fill_profile_from_order(customer, order, billing)
        link_crm(customer)
    return info


def _fill_profile_from_order(customer: ShopCustomer, order, billing: dict):
    """Перше замовлення з кабінету: порожню адресу профілю заповнити з замовлення."""
    if customer.street or not billing.get("street"):
        return
    for field in ("company", "name", "street", "zip", "city", "country", "vat_id", "phone"):
        if not getattr(customer, field) and billing.get(field):
            setattr(customer, field, billing[field][:ShopCustomer._meta.get_field(field).max_length])
    customer.save()


def attach_guest_orders(customer: ShopCustomer) -> int:
    """Замовлення магазину з тим самим email (оформлені без входу) — у кабінет підтвердженого покупця."""
    from sales.models import SalesOrder
    n = 0
    qs = (SalesOrder.objects.filter(source=customer.shop.slug, email__iexact=customer.email)
          .select_related("shop_info"))
    for order in qs:
        info = getattr(order, "shop_info", None)
        if info is None:
            ShopOrder.objects.create(order=order, shop=customer.shop, customer=customer)
            n += 1
        elif info.customer_id is None:
            info.customer = customer
            info.save(update_fields=["customer"])
            n += 1
    return n


def _money(value) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _tracking(order) -> list[dict]:
    from api.serializers import carrier_tracking_url
    out, seen = [], set()
    for s in order.shipments.all():
        if s.status in ("cancelled", "draft", "error") or not s.tracking_number:
            continue
        seen.add(s.tracking_number)
        out.append({"carrier": s.carrier.name, "number": s.tracking_number, "status": s.status,
                    "url": carrier_tracking_url(s.carrier.carrier_type, s.tracking_number),
                    "delivered_at": s.delivered_at})
    if order.tracking_number and order.tracking_number not in seen:
        out.append({"carrier": order.shipping_courier, "number": order.tracking_number, "status": "",
                    "url": carrier_tracking_url(order.shipping_courier, order.tracking_number), "delivered_at": None})
    return out


def order_summary(info: ShopOrder) -> dict:
    o = info.order
    quote = o.document_type == "QUOTE"
    net = None if quote else _money(o.total_price)
    shipping = None if quote else _money(o.shipping_cost or 0)
    vat = gross = None
    if net is not None and info.vat_rate is not None:
        vat = _money((net + shipping) * info.vat_rate / 100)
        gross = net + shipping + vat
    return {
        "number": o.order_number,
        "kind": "quote" if quote else "order",
        "date": o.order_date,
        "status": o.status,
        "payment_method": o.payment_method,
        "payment_status": o.payment_status,
        "currency": o.currency,
        "net": net, "shipping": shipping, "vat_rate": info.vat_rate, "vat": vat, "gross": gross,
        "items": len(o.lines.all()) + len(info.requested or []),
        # онлайн-оплата (Stripe) ще не завершена — у кабінеті кнопка «Jetzt bezahlen»
        "payment_open": (not quote and o.status != "cancelled" and o.payment_method == "stripe"
                         and o.payment_status in ("", "unpaid", "pending", "failed")
                         and not any(p.status == "processing" for p in o.shop_payments.all())),
        "tracking": _tracking(o),
    }


def order_detail(info: ShopOrder) -> dict:
    o = info.order
    data = order_summary(info)
    data["lines"] = [{
        "sku": ln.product.sku, "name": ln.product.name, "name_en": getattr(ln.product, "name_export", "") or "",
        "qty": ln.qty, "unit_price": ln.unit_price, "total": _money(ln.total_price),
    } for ln in o.lines.all()] + [{  # запит пропозиції: позиції поза каталогом Minerva
        "sku": r["sku"], "name": "", "name_en": "", "qty": r["qty"], "unit_price": None, "total": None,
    } for r in (info.requested or [])]
    data["shipping_address"] = {
        "company": o.ship_company or o.client, "name": o.ship_name or o.contact_name, "street": o.addr_street,
        "zip": o.addr_zip, "city": o.addr_city, "country": o.addr_country, "phone": o.ship_phone or o.phone,
    }
    data["billing"] = info.billing or {
        "company": o.client, "name": o.contact_name, "email": o.email, "vat_id": o.buyer_vat_id}
    data["dates"] = {"received": o.order_date, "shipped": o.shipped_at,
                     "delivered": timezone.localdate(o.delivered_at) if o.delivered_at else None}
    data["offer"] = _offer_for_quote(o) if data["kind"] == "quote" else None
    return data


def _offer_for_quote(order) -> dict | None:
    """Запит пропозиції: чи вже надіслано Angebot (offers)."""
    try:
        from offers.models import Offer
    except Exception:
        return None
    offer = (Offer.objects.filter(source_quote=order, sent_at__isnull=False).order_by("-sent_at").first())
    if offer is None:
        return None
    return {"number": offer.number, "status": offer.status, "sent_at": offer.sent_at,
            "valid_until": offer.valid_until}


def orders_qs(customer: ShopCustomer):
    return (ShopOrder.objects.filter(customer=customer)
            .select_related("order")
            .prefetch_related("order__lines__product", "order__shipments__carrier", "order__shop_payments")
            .order_by("-order__order_date", "-order__pk"))


def find_order(customer: ShopCustomer, number) -> ShopOrder:
    info = orders_qs(customer).filter(order__order_number=str(number or "")[:64]).first()
    if info is None:
        raise AccountError(404, "not_found", "Замовлення не знайдено.")
    return info


# ── Обслуговування ───────────────────────────────────────────────────────────

def cleanup(now=None) -> dict:
    """Прострочені сесії й посилання, непідтверджені реєстрації старші 30 днів (без замовлень)."""
    now = now or timezone.now()
    stats = {
        "sessions": ShopCustomerSession.objects.filter(expires_at__lt=now).delete()[0],
        "tokens": ShopCustomerToken.objects.filter(expires_at__lt=now - timedelta(days=7)).delete()[0],
    }
    stale = ShopCustomer.objects.filter(email_verified_at__isnull=True, created_at__lt=now - timedelta(days=30),
                                        orders__isnull=True)
    stats["unverified"] = stale.delete()[1].get("shop.ShopCustomer", 0)
    return stats
