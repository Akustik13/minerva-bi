"""Перевірка зв'язку Minerva ↔ сайт магазину — кнопка «🔌 Перевірити зв'язок із сайтом» у картці магазину.

Сайту для цього нічого нового не потрібно — перевіряється те, що він уже вміє:
1. ключі API магазину (видає лише Minerva; сайт тримає ключ у private/shop-config.php): активні, права,
   коли сайт востаннє звертався;
2. Minerva → сайт: GET <сайт>/api/shop/catalog.php. Щоб відповісти, сайт читає свої налаштування, а коли його
   кеш застарів — сам звертається до Minerva ключем; тож видно і зворотний бік (сайт → Minerva);
3. вебхук на цей сайт — підписаний ping (секрет у Minerva = 'webhook_secret' на сайті);
4. останній звіт сайту про фото й даташити.
Нічого не змінює ні в Minerva, ні на сайті; лишається лише запис доставки ping (як у дії вебхуків «📡 ping»).
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

import requests
from django.utils import timezone
from django.utils.timesince import timesince

from .models import Shop, ShopMediaSite

OK, WARN, ERROR, INFO = "ok", "warn", "error", "info"
# Сайт може під час відповіді сам звернутися до Minerva (до 4 запитів по 20 с), тож чекаємо довше звичайного
TIMEOUT = (5, 45)
USER_AGENT = "Minerva-ShopCheck/1.0"
SYNC_PATH = "api/shop/media_sync.php"
# Права ключа сайту: каталог, доставка, фото (products:read), замовлення й оплати (orders:*), кабінет (accounts:*)
REQUIRED_SCOPES = {
    "products:read": "каталог, доставка, фото",
    "orders:read": "стан оплат",
    "orders:write": "приймання замовлень",
}
ACCOUNT_SCOPES = {"accounts:read": "кабінет покупця", "accounts:write": "кабінет покупця"}


def _item(level, title, detail=""):
    return {"level": level, "title": title, "detail": detail}


def _when(dt) -> str:
    return f"{timezone.localtime(dt):%d.%m.%Y %H:%M} ({timesince(dt)} тому)"


def site_bases(shop: Shop, url: str | None = None) -> list[str]:
    """Де може лежати api/shop/ сайту: корінь, який сайт повідомив сам (синхронізація фото, той самий домен), і
    корінь домену. Повідомлений може застаріти, коли сайт переїхав в іншу папку, — тож пробуються обидва.
    [] — адреса не підходить."""
    raw = (shop.url if url is None else url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return []
    out = []
    sync = ShopMediaSite.objects.filter(shop=shop).values_list("sync_url", flat=True).first() or ""
    if sync.endswith(SYNC_PATH) and urlsplit(sync).hostname == parts.hostname:
        out.append(f"{parts.scheme}://{parts.netloc}{urlsplit(sync[:-len(SYNC_PATH)]).path}")
    root = f"{parts.scheme}://{parts.netloc}/"
    return out + [root] if root not in out else out


def check(shop: Shop, url: str | None = None) -> dict:
    """Усі перевірки по черзі. url — адреса з форми (ще не збережена); None — «Адреса сайту» магазину."""
    from . import services
    checks = []
    bases, base = site_bases(shop, url), ""
    if url is not None and url.strip() and url.strip() != (shop.url or "").strip():
        checks.append(_item(INFO, "Перевіряється адреса з поля", f"{url.strip()} — ще не збережена в магазині."))

    started = timezone.now()
    found, base = _catalog(shop, bases) if bases else ([], "")
    keys = services.keys_for_shop(shop)   # після запиту до сайту: «востаннє звертався» враховує і його
    checks += _keys(shop, keys)
    if not bases:
        checks.append(_item(ERROR, "Адреса сайту",
                            "Вкажіть «Адреса сайту» повністю, напр. https://www.sevskiy.de — без неї Minerva "
                            "не знає, куди звертатися."))
    else:
        checks += found
        checks += _callback(keys, started)
        checks += _webhook(shop, base)
        checks += _media(shop, base)
    errors = sum(c["level"] == ERROR for c in checks)
    warnings = sum(c["level"] == WARN for c in checks)
    summary = ("❌ Є проблеми зі зв'язком" if errors else "⚠️ Зв'язок є, але є зауваження" if warnings
               else "✅ Зв'язок Minerva ↔ сайт працює")
    return {"url": base, "summary": summary, "errors": errors, "warnings": warnings, "checks": checks,
            "checked_at": timezone.localtime().strftime("%d.%m.%Y %H:%M:%S")}


def _keys(shop: Shop, keys) -> list:
    from . import services
    if not keys:
        return [_item(ERROR, "Ключ API",
                      f"Немає активного ключа для «{shop.name}». Ключ видає лише Minerva: REST API → API Токени → "
                      f"новий ключ, поле «Магазин» = «{shop.name}»; на сайті його вставляють у "
                      f"private/shop-config.php ('minerva_token').")]
    out = []
    today = timezone.localdate()
    need = dict(REQUIRED_SCOPES, **(ACCOUNT_SCOPES if shop.accounts_enabled else {}))
    for key, mode in keys:
        how = {services.KEY_LINKED: "прив'язаний до магазину", services.KEY_SOURCE: f"за джерелом «{shop.slug}»",
               services.KEY_DEFAULT: "без прив'язки — працює з магазином за замовчуванням"}[mode]
        title = f"Ключ «{key.name}»"
        if key.expires_at and key.expires_at < today:
            out.append(_item(ERROR, title, f"Прострочений з {key.expires_at:%d.%m.%Y} — сайт отримує відмову. "
                                           "Продовжте «Дійсний до» в картці ключа."))
            continue
        missing = [f"{scope} ({why})" for scope, why in need.items() if not key.has_scope(scope)]
        used = (f"сайт востаннє звертався {_when(key.last_used)}" if key.last_used
                else "цим ключем ще ніхто не звертався")
        if missing:
            out.append(_item(WARN, title, f"{how}; бракує прав: {', '.join(missing)}. Додайте їх у картці ключа. "
                                          f"{used[0].upper() + used[1:]}."))
        else:
            out.append(_item(OK if key.last_used else WARN, title, f"{how}, права в порядку; {used}."))
    return out


def _catalog(shop: Shop, bases: list[str]) -> tuple[list, str]:
    """Minerva → сайт: каталог з першого кореня, де він є (HTTP 404 — пробується наступний). → (пункти, корінь)"""
    for i, base in enumerate(bases):
        found = _catalog_at(shop, base)
        if not found[0].pop("missing", False) or i == len(bases) - 1:
            return found, base


def _catalog_at(shop: Shop, base: str) -> list:
    """Каталог магазину з сайту, порівняння з Minerva. Перший пункт з missing=True — тут api/shop/ немає."""
    title = "Minerva → сайт"
    url = base + "api/shop/catalog.php"
    try:
        r = requests.get(url, params={"lang": "de"}, timeout=TIMEOUT, allow_redirects=False,
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except requests.exceptions.SSLError:
        return [_item(ERROR, title, f"{url}: сертифікат HTTPS сайту не прийнято (прострочений або для іншого домену).")]
    except requests.exceptions.Timeout:
        return [_item(ERROR, title, f"{url}: сайт не відповів за {TIMEOUT[1]} с.")]
    except requests.exceptions.RequestException as e:
        return [_item(ERROR, title, f"{url}: сайт недоступний ({type(e).__name__}) — перевірте адресу й домен.")]
    if r.is_redirect:
        return [_item(ERROR, title, f"Сайт переадресовує на {r.headers.get('Location', '?')} — вкажіть у «Адреса сайту» "
                                    "кінцеву адресу (з https:// і www, якщо сайт їх вимагає).")]
    if r.status_code == 404:
        return [dict(_item(ERROR, title, f"{url}: немає (HTTP 404). Домен має вказувати на папку public (dist) "
                                         "сайту, де лежить api/shop/."), missing=True)]
    if r.status_code == 503:
        return [_item(ERROR, title, "Сайт відповідає, але магазин на ньому не запускається (HTTP 503). Найчастіше: "
                                    "немає або неповний private/shop-config.php (адреса Minerva, ключ, token_secret), "
                                    "PHP не може писати в private/runtime, або сайт ще жодного разу не отримав "
                                    "каталог з Minerva (неправильний ключ чи адреса Minerva).")]
    if r.status_code != 200:
        return [_item(ERROR, title, f"{url}: HTTP {r.status_code}.")]
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or "enabled" not in data:
        return [_item(ERROR, title, f"{url} відповідає не як API магазину — перевірте адресу сайту.")]
    if not data.get("enabled"):
        return [_item(WARN, title, "Сайт відповідає, але магазин на ньому вимкнено ('enabled' => false "
                                   "у private/shop-config.php).")]
    out = [_item(OK, title, f"Сайт {base} відповідає, магазин увімкнено.")]
    out += _freshness(data)
    out += _compare(shop, data)
    return out


def _freshness(data: dict) -> list:
    title = "Сайт → Minerva"
    try:
        updated = datetime.fromisoformat(str(data.get("updated_at", "")).replace("Z", "+00:00"))
    except ValueError:
        updated = None
    when = _when(updated) if updated else "невідомо коли"
    if data.get("stale"):
        return [_item(ERROR, title, f"Сайт не зміг оновити каталог з Minerva і показує стару копію ({when}). "
                                    "Перевірте на сайті в private/shop-config.php адресу Minerva ('minerva_url') і "
                                    "ключ ('minerva_token'), а на хостингу — вихідні HTTPS-з'єднання.")]
    return [_item(OK, title, f"Каталог отримано з Minerva {when}.")]


def _compare(shop: Shop, data: dict) -> list:
    from inventory.models import Product
    out = []
    n_site = len(data.get("products") or [])
    n_minerva = (Product.objects.filter(is_active=True, shop_listings__shop=shop, shop_listings__is_visible=True)
                 .distinct().count())
    if n_site == n_minerva:
        out.append(_item(OK, "Товари", f"На сайті {n_site} — як у Minerva."))
    else:
        out.append(_item(WARN, "Товари", f"На сайті {n_site}, у Minerva для сайту — {n_minerva}. Якщо асортимент щойно "
                                         "змінювався, сайт оновиться після свого кешу (або одразу через вебхук)."))
    zones = shop.shipping_zones.filter(is_active=True).count()
    site_zones = data.get("shipping_zones")
    if not zones:
        out.append(_item(INFO, "Доставка", "У Minerva зони не задані — сайт рахує доставку за своїми правилами "
                                           "з shop-config.php."))
    elif site_zones is None:
        out.append(_item(WARN, "Доставка", f"У Minerva {zones} зон, а сайт їх не отримав — стара версія сайту "
                                           "або ключу бракує права products:read."))
    else:
        out.append(_item(OK, "Доставка", f"Зон з Minerva на сайті: {len(site_zones)} (у Minerva {zones})."))
    if shop.accounts_enabled and not data.get("accounts"):
        out.append(_item(WARN, "Кабінет покупця", "Увімкнено в Minerva, але сайт його не бачить — ключу бракує "
                                                  "прав accounts:read / accounts:write."))
    elif data.get("accounts"):
        out.append(_item(OK, "Кабінет покупця", "Сайт бачить, що кабінет увімкнено."))
    methods = [m.get("label") or m.get("id") for m in data.get("payment_methods") or [] if isinstance(m, dict)]
    online = data.get("online_payment") or {}
    stripe = ""
    if online.get("stripe"):
        stripe = (f"; Stripe підключено ({online.get('mode') or 'режим невідомий'})" if online.get("connected")
                  else "; Stripe вибрано, але не підключено")
    out.append(_item(INFO, "Оплата на сайті", (", ".join(methods) or "способи не задано") + stripe))
    return out


def _callback(keys, started) -> list:
    """Чи звернувся сайт до Minerva під час перевірки (кеш сайту був застарілий) — доказ зв'язку в обидва боки."""
    from api.models import APIKey
    used = [k.name for k in APIKey.objects.filter(pk__in=[k.pk for k, _ in keys], last_used__gte=started)]
    if used:
        return [_item(OK, "В обидва боки", f"Під час перевірки сайт сам звернувся до Minerva ключем "
                                           f"«{', '.join(used)}» — зв'язок працює в обидва боки.")]
    return []


def _webhook(shop: Shop, base: str) -> list:
    """Підписаний ping на вебхук цього сайту: сайт приймає лише з правильним секретом."""
    from api.models import Webhook, WebhookDelivery
    from api.webhooks import emit
    host = urlsplit(base).hostname
    hooks = [h for h in Webhook.objects.filter(is_active=True) if urlsplit(h.url).hostname == host]
    if not hooks:
        return [_item(WARN, "Вебхук", "На цей сайт вебхук не налаштовано: зміни складу й цін з'являться на сайті "
                                      "лише після його кешу (зазвичай хвилина-дві). Налаштування: REST API → "
                                      f"Вебхуки, URL {base}api/shop/webhook.php, подія stock.changed; секрет "
                                      "вставити на сайті в private/shop-config.php ('webhook_secret').")]
    out = []
    for hook in hooks:
        (d,) = emit("ping", {"message": "Minerva connection check", "shop": shop.slug}, only=hook, sync=True)
        d.refresh_from_db()
        title = f"Вебхук «{hook.name}»"
        if d.status == WebhookDelivery.SUCCESS:
            out.append(_item(OK, title, f"Сайт прийняв підписаний ping (HTTP {d.response_code}) — секрет збігається."))
            continue
        # тестовий ping не повторювати (cron повторює звичайні події)
        WebhookDelivery.objects.filter(pk=d.pk, status=WebhookDelivery.PENDING).update(status=WebhookDelivery.FAILED)
        if d.response_code == 401:
            detail = ("Сайт відхилив підпис (HTTP 401): секрет вебхука в Minerva не збігається з 'webhook_secret' "
                      "у private/shop-config.php сайту.")
        elif d.response_code:
            detail = f"Сайт відповів HTTP {d.response_code} на {hook.url}."
        else:
            detail = f"Немає відповіді від {hook.url}: {d.response_body[:160]}"
        out.append(_item(ERROR, title, detail))
    return out


def _media(shop: Shop, base: str) -> list:
    site = ShopMediaSite.objects.filter(shop=shop).first()
    if not site or not site.reported_at:
        return [_item(INFO, "Фото й даташити", "Сайт ще не звітував, які копії з Minerva має (звіт приходить після "
                                               "оновлення каталогу, коли «🖼 Фото й даташити» увімкнено).")]
    host = urlsplit(site.sync_url).hostname or ""
    other = f" Звіт надіслав інший сайт: {host}." if host and host != urlsplit(base).hostname else ""
    return [_item(INFO, "Фото й даташити", f"Останній звіт сайту {_when(site.reported_at)}: копій на сайті "
                                           f"{len(site.have or [])}, помилок {len(site.failed or {})}.{other}")]
