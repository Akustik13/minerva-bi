"""Адмінка сайту магазину (/admin/ на сайті) — сторона Minerva.

- ShopStaff: хто з користувачів Minerva має доступ до адмінки сайту і з якою роллю (адміністратор / менеджер /
  перегляд). Вхід на сайті — логіном і паролем Minerva: сайт питає staff_login своїм ключем магазину;
  пароль у сайт не потрапляє (сайт сам кешує власний хеш для входу, коли Minerva недоступна).
- ShopPairing: одноразовий код «🔗 Підключити сайт» → сайт обмінює його на ключ API магазину й секрет
  вебхука (pair) — ключі не треба переносити файлами.
- ShopSiteEvent: журнал адмінки сайту (вхід, зміни налаштувань — лише назви полів, підключення).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Shop, ShopPairing, ShopSiteEvent, ShopStaff

LOCK_FAILS, LOCK_MINUTES = 5, 15          # невдалі спроби входу на один логін
PAIR_MINUTES = 30                          # скільки діє код підключення
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без 0/O, 1/I
SITE_SCOPES = ["products:read", "orders:read", "orders:write"]
ACCOUNT_SCOPES = ["accounts:read", "accounts:write"]
EVENTS_KEEP = 500


class SiteAdminError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def record_event(shop: Shop, site: str, user: str, action: str, details: dict | None = None) -> ShopSiteEvent:
    event = ShopSiteEvent.objects.create(shop=shop, site=(site or "")[:120], user=(user or "")[:150],
                                         action=(action or "")[:40], details=details or {})
    old = ShopSiteEvent.objects.filter(shop=shop).order_by("-at").values_list("pk", flat=True)[EVENTS_KEEP:]
    ShopSiteEvent.objects.filter(pk__in=list(old)).delete()
    return event


# ── Вхід персоналу на сайті ─────────────────────────────────────────────────────────────────────────────────

def staff_login(shop: Shop, login: str, password: str, site: str = "") -> dict:
    login = (login or "").strip()[:150]
    if not login or not password:
        raise SiteAdminError("bad_request", "Вкажіть логін і пароль.", 400)
    lock = f"siteadmin:fail:{shop.pk}:{hashlib.sha256(login.lower().encode()).hexdigest()[:20]}"
    if (cache.get(lock) or 0) >= LOCK_FAILS:
        raise SiteAdminError("locked", f"Забагато невдалих спроб — зачекайте {LOCK_MINUTES} хвилин.", 429)
    user = (get_user_model().objects.filter(Q(username__iexact=login) | Q(email__iexact=login), is_active=True)
            .order_by("pk").first())
    staff = ShopStaff.objects.filter(shop=shop, user=user, is_active=True).first() if user else None
    if user is None:
        make_password(password)   # той самий час відповіді, що й для наявного користувача
    if not user or not user.check_password(password) or not staff:
        cache.set(lock, (cache.get(lock) or 0) + 1, LOCK_MINUTES * 60)
        record_event(shop, site, login, "login_failed")
        raise SiteAdminError("invalid", "Неправильний логін або пароль, або немає доступу до цього сайту.", 401)
    cache.delete(lock)
    ShopStaff.objects.filter(pk=staff.pk).update(last_site_login=timezone.now())
    record_event(shop, site, user.get_username(), "login", {"role": staff.role})
    return {"ok": True, "username": user.get_username(), "name": user.get_full_name() or user.get_username(),
            "email": user.email, "role": staff.role, "role_label": staff.get_role_display(),
            "permissions": staff.permissions}


# ── Код підключення сайту ───────────────────────────────────────────────────────────────────────────────────

def _code_hash(code: str) -> str:
    return hashlib.sha256((code or "").replace("-", "").replace(" ", "").upper().encode()).hexdigest()


def new_pairing(shop: Shop, user=None) -> tuple[str, object]:
    """Новий код (попередні невикористані коди магазину скасовуються). Код показується один раз."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
    code = f"{raw[:5]}-{raw[5:]}"
    ShopPairing.objects.filter(shop=shop, used_at__isnull=True).delete()
    pairing = ShopPairing.objects.create(shop=shop, code_hash=_code_hash(code), created_by=user,
                                         expires_at=timezone.now() + timedelta(minutes=PAIR_MINUTES))
    return code, pairing.expires_at


def _site_origin(site_url: str) -> tuple[str, str]:
    parts = urlsplit((site_url or "").strip())
    local = parts.hostname in ("localhost", "127.0.0.1")   # локальна перевірка
    if not parts.hostname or parts.username or parts.password or not (parts.scheme == "https" or
                                                                        (parts.scheme == "http" and local)):
        raise SiteAdminError("bad_site", "Сайт має працювати через https://.", 400)
    return f"{parts.scheme}://{parts.netloc}", parts.hostname


@transaction.atomic
def pair(code: str, site_url: str, minerva_url: str) -> dict:
    """Сайт обмінює код на ключ API магазину й вебхук на свою адресу. Попередні ключі, видані цьому сайту
    кодом, вимикаються (ключі, створені вручну, не чіпаються)."""
    from api.models import APIKey, Webhook, _webhook_secret
    p = (ShopPairing.objects.select_for_update()
         .filter(code_hash=_code_hash(code), used_at__isnull=True, expires_at__gt=timezone.now())
         .select_related("shop", "created_by").first())
    if p is None:
        raise SiteAdminError("invalid_code", "Код недійсний, уже використаний або протермінований — створіть новий "
                                             "у Minerva (картка магазину → «🔗 Підключити сайт»).", 400)
    origin, host = _site_origin(site_url)
    shop = p.shop
    old = [k for k in ShopPairing.objects.filter(shop=shop, site_url=origin, api_key__isnull=False)
           .values_list("api_key_id", flat=True)]
    APIKey.objects.filter(pk__in=old).update(is_active=False)
    key = APIKey.objects.create(name=f"Сайт {host}", shop=shop,
                                scopes=SITE_SCOPES + (ACCOUNT_SCOPES if shop.accounts_enabled else []))
    hook = Webhook.objects.filter(url=f"{origin}/api/shop/webhook.php").first() or Webhook(
        url=f"{origin}/api/shop/webhook.php")
    hook.name, hook.is_active, hook.secret = f"Сайт {host}", True, _webhook_secret()
    hook.events = hook.events or ["stock.changed"]
    hook.save()
    p.used_at, p.site_url, p.api_key = timezone.now(), origin, key
    p.save(update_fields=["used_at", "site_url", "api_key"])
    record_event(shop, host, p.created_by.get_username() if p.created_by else "", "pair",
                 {"key": key.name, "webhook": hook.name, "replaced_keys": len(old)})
    return {"ok": True, "shop": shop.slug, "shop_name": shop.name, "minerva_url": minerva_url.rstrip("/"),
            "api_key": key.key, "webhook_secret": hook.secret, "site_url": origin}
