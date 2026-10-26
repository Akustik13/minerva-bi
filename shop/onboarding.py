"""Дорожня карта підключення магазину: що вже зроблено, що ні й що робити далі (картка магазину, список).

Лише читає базу — без звернень до сайту, тож стан видно одразу. Живу перевірку робить «🔌 Перевірити зв'язок
із сайтом» (shop/connection.py). Крок: {key, title, kind, state, detail, hint, go, link}:
- kind: required — без цього магазин не працює; recommended — працює, але краще зробити; optional — за бажанням;
- state: done — готово; warn — працює, але є що виправити; todo — не зроблено; skip — вимкнено (за бажанням);
- go — поле картки (назва поля або #id елемента), до якого веде кнопка; link — {url, label} іншої сторінки.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from .models import Shop

DONE, WARN, TODO, SKIP = "done", "warn", "todo", "skip"
REQUIRED, RECOMMENDED, OPTIONAL = "required", "recommended", "optional"
QUIET_DAYS = 7    # сайт довше не звертався до Minerva — попередження
REPORT_DAYS = 3   # звіти сайтів за цей час — які сайти зараз працюють з ключем магазину


def _step(key, title, kind, state, detail="", hint="", go="", link=None) -> dict:
    return {"key": key, "title": title, "kind": kind, "state": state, "detail": detail, "hint": hint,
            "go": go, "link": link}


def _host(url) -> str:
    return (urlsplit(str(url or "")).hostname or "").lower()


def _when(dt) -> str:
    return f"{timezone.localtime(dt):%d.%m.%Y %H:%M} ({timesince(dt)} тому)"


def site_keys(shop: Shop) -> list:
    """Активні ключі API цього магазину: поле «Магазин» або «Джерело замовлень» = код (без прив'язки до іншого)."""
    from api.models import APIKey
    today = timezone.localdate()
    keys = APIKey.objects.filter(is_active=True).filter(
        Q(shop=shop) | Q(shop__isnull=True, default_source=shop.slug)).order_by("-last_used", "name")
    return [k for k in keys if not k.expires_at or k.expires_at >= today]


def site_hosts(shop: Shop) -> list[str]:
    """Сайти, які останні REPORT_DAYS днів працювали з ключем цього магазину (з їхніх звітів)."""
    from django.utils.dateparse import parse_datetime
    from .models import ShopMediaSite
    since = timezone.now() - timedelta(days=REPORT_DAYS)
    hosts = set()
    for reports in (shop.site_mail_reports or {}, shop.assistant_sites or {}):
        for host, r in reports.items():
            at = parse_datetime(str((r or {}).get("at") or ""))
            if at is not None and at >= since:
                hosts.add(host.lower())
    media = ShopMediaSite.objects.filter(shop=shop, reported_at__gte=since).values_list("sync_url", flat=True).first()
    if _host(media):
        hosts.add(_host(media))
    return sorted(hosts)


# ── Кроки ────────────────────────────────────────────────────────────────────

def _identity(shop, pending) -> dict:
    from . import copying
    from .mail import public_name
    detail = f"«{shop.name}» · код {shop.slug} · покупці бачать «{public_name(shop)}»"
    left = [f for f in ("name", "slug", "public_name") if f in pending]
    if left:
        return _step("identity", "Назва й код", REQUIRED, TODO, detail,
                     "Після копіювання ще не змінено: " + ", ".join(copying.labels(shop, left))
                     + ". Код стане «Джерелом» замовлень цього магазину.", go=left[0])
    hint = "Код — це «Джерело» в замовленнях, за ним фільтруються продажі; після перших замовлень його не змінюйте."
    if not shop.public_name:
        hint = ("«Назва для покупців» не задана — у листах і на сторінці оплати буде «" + public_name(shop)
                + "». " + hint)
    return _step("identity", "Назва й код", REQUIRED, DONE, detail, hint, go="public_name")


def _url(shop, pending) -> dict:
    host = _host(shop.url)
    hint = ("Повна адреса, з якої покупці відкривають сайт, напр. https://shop.example.com: на неї ведуть посилання "
            "в листах (кабінет, оплата), за нею Minerva перевіряє зв'язок і знаходить вебхук сайту.")
    if not host:
        return _step("url", "Адреса сайту", REQUIRED, TODO, "не задано", hint, go="url")
    if "url" in pending:
        return _step("url", "Адреса сайту", REQUIRED, TODO, f"{shop.url} — як в оригіналі", hint, go="url")
    same = [s.name for s in Shop.objects.exclude(pk=shop.pk).only("name", "url") if _host(s.url) == host]
    if same:
        return _step("url", "Адреса сайту", REQUIRED, WARN, shop.url,
                     f"Та сама адреса — у магазині «{', '.join(same)}». Один сайт — один магазин: інакше каталог, "
                     "акаунти й замовлення змішаються.", go="url")
    if urlsplit(shop.url).scheme != "https":
        return _step("url", "Адреса сайту", REQUIRED, WARN, shop.url,
                     "Сайт без HTTPS: кабінет покупця й оплата потребують https://.", go="url")
    return _step("url", "Адреса сайту", REQUIRED, DONE, shop.url, go="url")


def _active(shop) -> dict:
    if shop.is_active:
        return _step("active", "Магазин увімкнено", REQUIRED, DONE,
                     "увімкнено" + (" · за замовчуванням для ключів без прив'язки" if shop.is_default else ""),
                     go="is_active")
    return _step("active", "Магазин увімкнено", REQUIRED, TODO, "вимкнено",
                 "Поки магазин вимкнено, сайт з його ключем не отримує ні каталогу, ні замовлень. Копія магазину "
                 "створюється вимкненою — увімкніть «Активний», коли кроки нижче готові.", go="is_active")


def _products(shop) -> dict:
    listings = reverse("admin:shop_shoplisting_changelist") + f"?shop__id__exact={shop.pk}"
    total = shop.listings.count()
    visible = shop.listings.filter(is_visible=True).count()
    if not visible:
        return _step("products", "Товари", REQUIRED, TODO,
                     f"{total} позицій, усі сховані" if total else "товарів немає",
                     "Додайте товари: «Каталог → додати в магазин» (галочки, потім дія) або блок «🏪 Інтернет-магазини» "
                     "в картці товару. Ціна — своя для магазину або від базових цін.",
                     link={"url": reverse("admin:shop_shopproduct_changelist"), "label": "Каталог → додати в магазин"})
    return _step("products", "Товари", REQUIRED, DONE, f"показується {visible} з {total}",
                 link={"url": listings, "label": "Асортимент і ціни"})


def _shipping(shop) -> dict:
    zones = [z for z in shop.shipping_zones.all() if z.is_active]
    if not zones:
        return _step("shipping", "Доставка", REQUIRED, TODO, "зон немає",
                     "Без зон сайт рахує доставку за власними налаштуваннями, а Minerva не перевіряє країну. "
                     "«🌍 Створити базові зони» — Німеччина, Європа, США, світ; далі поправте ціни й терміни.",
                     go="zones_tools")
    free = (f" · безкоштовно від {shop.free_shipping_threshold:.2f}"
            if shop.free_shipping_enabled and shop.free_shipping_threshold is not None else "")
    return _step("shipping", "Доставка", REQUIRED, DONE, f"{len(zones)} зон: " + ", ".join(z.name for z in zones) + free,
                 go="zones_tools")


def _payment(shop) -> dict:
    labels = dict(Shop.PAYMENT_CHOICES)
    methods = [m for m in (shop.payment_methods or []) if m in labels]
    if not methods:
        return _step("payment", "Оплата", REQUIRED, TODO, "не вибрано жодного способу",
                     "Виберіть, що покупець бачить у касі: передоплата, рахунок, Stripe.", go="payment_methods")
    detail = ", ".join(labels[m].split(" (")[0] for m in methods)
    bank = "Банківські реквізити для передоплати — на сайті /admin/ → «💳 Оплата»." if "prepayment" in methods else ""
    if "stripe" in methods and not shop.stripe_secret_key:
        state = TODO if methods == ["stripe"] else WARN
        return _step("payment", "Оплата", REQUIRED, state, detail,
                     "Stripe вибрано, але ключ не задано — покупці Stripe не побачать. Вставте ключ у «💳 Оплата "
                     "Stripe» (спершу тестовий sk_test_…).", go="stripe_secret_key")
    if "stripe" in methods and not shop.stripe_webhook_secret:
        return _step("payment", "Оплата", REQUIRED, WARN, detail,
                     "Stripe працює без вебхука: Minerva сама питає Stripe про оплати кожні "
                     f"{shop.stripe_poll_minutes or '—'} хв. Щоб оплата позначалась одразу — додайте в Stripe вебхук "
                     "з адресою з блоку «💳 Оплата Stripe» і вставте його секрет. " + bank, go="stripe_webhook_secret")
    return _step("payment", "Оплата", REQUIRED, DONE, detail, bank, go="payment_methods")


def _key(shop) -> dict:
    from core.secrets import mask
    from .connection import ACCOUNT_SCOPES, REQUIRED_SCOPES
    keys = site_keys(shop)
    pair_hint = ("Найпростіше — кнопка «🔗 Підключити сайт (код)» у блоці «🌐 Адмінка сайту»: Minerva сама видасть сайту "
                 "ключ з потрібними правами й налаштує вебхук. Вручну — REST API → API Токени, поле «Магазин» = цей "
                 "магазин; на сайті ключ — /admin/ → «🔌 Minerva».")
    if not keys:
        return _step("key", "Ключ API для сайту", REQUIRED, TODO, "немає активного ключа цього магазину", pair_hint,
                     go="site_admin_col")
    need = set(REQUIRED_SCOPES) | (set(ACCOUNT_SCOPES) if shop.accounts_enabled else set())
    names = ", ".join(f"«{k.name}» {mask(k.key)}" for k in keys)
    good = [k for k in keys if need <= set(k.scopes or [])]
    if not good:
        missing = sorted(need - set(keys[0].scopes or []))
        return _step("key", "Ключ API для сайту", REQUIRED, WARN, f"{names}: бракує прав {', '.join(missing)}",
                     "Додайте права в картці ключа або підключіть сайт кодом — новий ключ отримає їх сам.",
                     link={"url": reverse("admin:api_apikey_change", args=[keys[0].pk]), "label": "Картка ключа"})
    hint = ("Ключів кілька: кожен сайт — свій ключ; той, що більше не потрібен, вимкніть."
            if len(keys) > 1 else "Ключ видає лише Minerva; на сайті він лежить у /admin/ → «🔌 Minerva».")
    return _step("key", "Ключ API для сайту", REQUIRED, DONE, names, hint, go="keys_col")


def _staff(shop) -> dict:
    from .models import ShopStaff
    staff = list(shop.staff.filter(is_active=True, user__is_active=True).select_related("user"))
    if not staff:
        return _step("staff", "Доступ до адмінки сайту", REQUIRED, TODO, "нікого",
                     "Унизу «👥 Доступ до адмінки сайту» додайте себе з роллю «Адміністратор сайту»: без цього не "
                     "ввійдете в /admin/ сайту логіном Minerva (ключі, пошта, оплата на сайті).", go="#staff-group")
    roles = dict(ShopStaff.ROLES)
    detail = ", ".join(f"{s.user.get_full_name() or s.user.username} ({roles.get(s.role, s.role).split(' —')[0]})"
                       for s in staff)
    if not any(s.role == ShopStaff.ROLE_ADMIN for s in staff):
        return _step("staff", "Доступ до адмінки сайту", REQUIRED, WARN, detail,
                     "Немає адміністратора сайту — ніхто не зможе змінити ключі чи перепідключити сайт.",
                     go="#staff-group")
    return _step("staff", "Доступ до адмінки сайту", REQUIRED, DONE, detail, go="#staff-group")


def _connected(shop) -> dict:
    keys = site_keys(shop)
    used = max((k.last_used for k in keys if k.last_used), default=None)
    own = _host(shop.url)
    hosts = site_hosts(shop)
    if used is None:
        return _step("connected", "Сайт підключено", REQUIRED, TODO, "сайт ще не звертався з ключем цього магазину",
                     "На сайті: /admin/ → «🔌 Minerva» → «🔗 Підключити до Minerva кодом»; код — кнопка «🔗 Підключити "
                     "сайт (код)» у блоці «🌐 Адмінка сайту» (діє 30 хв). Сайт отримає власний ключ і вебхук. Потім — "
                     "«🔌 Перевірити зв'язок із сайтом».", go="site_admin_col")
    detail = f"останнє звернення {_when(used)}" + (f" · сайти: {', '.join(hosts)}" if hosts else "")
    others = [h for h in hosts if h != own]
    if own and others:
        return _step("connected", "Сайт підключено", REQUIRED, WARN, detail,
                     f"Ключем цього магазину користується ще {', '.join(others)}. Сайти одного магазину мають спільні "
                     "акаунти покупців, кошики й замовлення. Для тестового чи іншого сайту — окремий магазин "
                     "(«📋 Копіювати магазин») і підключення кодом.", go="site_admin_col")
    if timezone.now() - used > timedelta(days=QUIET_DAYS):
        return _step("connected", "Сайт підключено", REQUIRED, WARN, detail,
                     f"Сайт не звертався понад {QUIET_DAYS} днів. Перевірте, чи працює сайт і чи не змінився ключ, — "
                     "«🔌 Перевірити зв'язок із сайтом».", go="connection_col")
    return _step("connected", "Сайт підключено", REQUIRED, DONE, detail,
                 "Повна перевірка (каталог, ключ, вебхук, фото) — «🔌 Перевірити зв'язок із сайтом».",
                 go="connection_col")


def _webhook(shop) -> dict:
    from api.models import Webhook
    from .connection import site_bases
    own = _host(shop.url)
    hooks = [h for h in Webhook.objects.filter(is_active=True) if own and _host(h.url) == own]
    expected = (site_bases(shop) or [""])[0] + "api/shop/webhook.php"
    listing = {"url": reverse("admin:api_webhook_changelist"), "label": "REST API → Вебхуки"}
    if not hooks:
        return _step("webhook", "Вебхук на сайт", RECOMMENDED, TODO, "не налаштовано",
                     "Без вебхука зміни складу й цін з'являються на сайті лише після його кешу (1–2 хв). Підключення "
                     "кодом налаштовує вебхук саме; вручну — REST API → Вебхуки, URL " + expected
                     + ", секрет — на сайті /admin/ → «🔌 Minerva».", link=listing)
    bad = [h for h in hooks if h.last_status_code and not 200 <= h.last_status_code < 300]
    if bad:
        h = bad[0]
        when = f" ({_when(h.last_delivery_at)})" if h.last_delivery_at else ""
        if h.last_status_code == 404 and h.url != expected:
            hint = f"Сайт не знайшов адресу {h.url}. Правильна адреса — {expected}: змініть URL у картці вебхука."
        elif h.last_status_code == 401:
            hint = "Сайт відхилив підпис: секрет вебхука в Minerva не збігається з секретом на сайті (/admin/ → «🔌 Minerva»)."
        else:
            hint = "Сайт відповідає помилкою — перевірте «🔌 Перевірити зв'язок із сайтом»."
        return _step("webhook", "Вебхук на сайт", RECOMMENDED, WARN, f"«{h.name}»: HTTP {h.last_status_code}{when}",
                     hint, link={"url": reverse("admin:api_webhook_change", args=[h.pk]), "label": "Картка вебхука"})
    h = hooks[0]
    detail = h.url + (f" · остання доставка {_when(h.last_delivery_at)}" if h.last_delivery_at else " · доставок ще не було")
    return _step("webhook", "Вебхук на сайт", RECOMMENDED, DONE, detail, link=listing)


def _mail(shop) -> dict:
    from . import mail, site_mail
    text = mail.describe_sender(shop)
    site = ("сайт надсилає з цієї ж скриньки" if shop.site_mail_managed
            else "сайт — власні налаштування (/admin/ → «Пошта»)")
    detail = f"{text} · {site}"
    if site_mail.sender_account(shop) is None:
        return _step("mail", "Пошта магазину", RECOMMENDED, WARN, detail,
                     "«Відправник листів покупцям» не вибрано — листи кабінету й оплат ідуть зі скриньки сповіщень "
                     "Minerva. Виберіть скриньку магазину («➕ Додати скриньку в Minerva»): відповіді покупців тоді "
                     "приходять у пошту Minerva й прив'язуються до клієнта в CRM.", go="mail_sender")
    problem = site_mail.problem(shop)
    if problem or "⚠" in text:
        return _step("mail", "Пошта магазину", RECOMMENDED, WARN, detail,
                     problem or "Виправте порт або режим шифрування скриньки в Email → Акаунти.", go="mail_sender")
    hint = "" if shop.site_mail_managed else ("Щоб і підтвердження замовлень із сайту йшли з цієї скриньки — "
                                              "«Сайт надсилає листи з цієї ж скриньки».")
    return _step("mail", "Пошта магазину", RECOMMENDED, DONE, detail, hint,
                 go="mail_sender" if shop.site_mail_managed else "site_mail_managed")


def _order(shop) -> dict:
    from sales.models import SalesOrder
    orders = SalesOrder.objects.filter(source=shop.slug)
    last = orders.order_by("-order_date", "-pk").first()
    link = {"url": reverse("admin:sales_salesorder_changelist") + f"?source={shop.slug}", "label": "Замовлення магазину"}
    if last is None:
        return _step("order", "Тестове замовлення", RECOMMENDED, TODO, "замовлень ще немає",
                     f"Оформіть на сайті замовлення з передоплатою: воно з'явиться в «Продажах» з джерелом «{shop.slug}», "
                     "прийдуть листи покупцю й команді. Потім скасуйте його — товар повернеться на склад.", link=link)
    date = f"{last.order_date:%d.%m.%Y}" if last.order_date else ""
    return _step("order", "Тестове замовлення", RECOMMENDED, DONE,
                 f"замовлень: {orders.count()} · останнє {last.order_number} {date}".strip(), link=link)


def _accounts(shop) -> dict:
    if not shop.accounts_enabled:
        return _step("accounts", "Кабінет покупців", OPTIONAL, SKIP, "вимкнено",
                     "За бажанням: реєстрація, вхід, статус замовлень і кошик на сайті — «Особистий кабінет покупців».",
                     go="accounts_enabled")
    links = shop.account_links or {}
    count = shop.customers.count()
    if shop.account_link_error:
        return _step("accounts", "Кабінет покупців", OPTIONAL, WARN, f"акаунтів: {count}", shop.account_link_error,
                     go="accounts_tools")
    if not links:
        return _step("accounts", "Кабінет покупців", OPTIONAL, WARN, f"акаунтів: {count} · сторінка кабінету ще невідома",
                     "Відкрийте на сайті сторінку кабінету (Konto / Account) — сайт повідомить її адресу; без неї "
                     "посилання в листах (підтвердження, новий пароль) не ведуть на сайт.", go="accounts_tools")
    return _step("accounts", "Кабінет покупців", OPTIONAL, DONE,
                 f"акаунтів: {count} · кабінет: {next(iter(links.values()))}", go="accounts_tools")


def _media(shop) -> dict:
    from .models import ShopMediaSettings, ShopMediaSite
    link = {"url": reverse("admin:shop_productmedia_changelist"), "label": "🖼 Фото й даташити"}
    if not ShopMediaSettings.objects.filter(pk=1, enabled=True).exists():
        return _step("media", "Фото й даташити з Minerva", OPTIONAL, SKIP, "вимкнено",
                     "За бажанням: сайт бере фото й даташити товарів з Minerva й тримає копію в себе.", link=link)
    site = ShopMediaSite.objects.filter(shop=shop).first()
    if site is None or site.reported_at is None:
        return _step("media", "Фото й даташити з Minerva", OPTIONAL, WARN, "сайт ще не звітував",
                     "Сайт забирає файли сам після запиту каталогу; можна запустити з «🖼 Фото й даташити».", link=link)
    detail = f"на сайті {len(site.have or [])} файлів · звіт {_when(site.reported_at)}"
    if site.failed:
        return _step("media", "Фото й даташити з Minerva", OPTIONAL, WARN, f"{detail} · помилок: {len(site.failed)}",
                     "Частину файлів сайт не завантажив — деталі в «🖼 Фото й даташити».", link=link)
    return _step("media", "Фото й даташити з Minerva", OPTIONAL, DONE, detail, link=link)


def _assistant(shop) -> dict:
    from .assistant import describe
    if shop.assistant_mode == Shop.ASSISTANT_OFF:
        return _step("assistant", "Помічник Minerva на сайті", OPTIONAL, SKIP, "вимкнено", go="assistant_mode")
    if shop.assistant_mode == Shop.ASSISTANT_SITE:
        return _step("assistant", "Помічник Minerva на сайті", OPTIONAL, SKIP, describe(shop),
                     "Щоб керувати чатом з Minerva — «Увімкнено» в блоці «🤖 Помічник Minerva на сайті», потім "
                     "«🧪 Перевірити RAG».", go="assistant_mode")
    return _step("assistant", "Помічник Minerva на сайті", OPTIONAL, DONE, describe(shop),
                 "Відповіді перевіряє «🧪 Перевірити RAG».", go="assistant_test_col")


def _search(shop) -> dict:
    from . import search_assist
    if shop.search_assist == Shop.SEARCH_OFF:
        return _step("search", "Підказки в пошуку", OPTIONAL, SKIP, "вимкнено — звичайний пошук",
                     "За бажанням: розпізнавання частоти й роз'єму в запиті, схожі товари, кабелі, «часто купують "
                     "разом» (без AI, безкоштовно) або ще й AI-підказки чи підказки нашого RAG.", go="search_assist")
    if shop.search_assist != Shop.SEARCH_RULES:
        u = search_assist.usage(shop)
        parts, problems = ["правила"], []
        if shop.search_assist in Shop.SEARCH_WITH_AI:
            parts.append(f"AI ({shop.search_ai_model}, сьогодні {u.get('calls', 0)} з {shop.search_ai_daily_limit}, "
                         f"≈ {search_assist.spent(u)})")
            if search_assist.ai_ready(shop):
                problems.append("AI-підказки: " + search_assist.ai_ready(shop))
        if shop.search_assist in Shop.SEARCH_WITH_RAG:
            parts.append(f"RAG (сьогодні {u.get('rag_calls', 0)} з {shop.search_ai_daily_limit})")
            if search_assist.rag_ready(shop):
                problems.append("RAG-підказки: " + search_assist.rag_ready(shop))
        detail = " + ".join(parts)
        if problems:
            return _step("search", "Підказки в пошуку", OPTIONAL, WARN, detail,
                         "Не працюють " + "; ".join(problems) + ". Сайт показує підказки за правилами.", go="search_col")
        return _step("search", "Підказки в пошуку", OPTIONAL, DONE, detail, go="search_col")
    return _step("search", "Підказки в пошуку", OPTIONAL, DONE, "розумний пошук і підказки за правилами (без AI)",
                 go="search_assist")


def steps(shop: Shop) -> list[dict]:
    from . import copying
    pending = set(copying.pending(shop)) if shop.review_fields else set()
    return [_identity(shop, pending), _url(shop, pending), _active(shop), _products(shop), _shipping(shop),
            _payment(shop), _key(shop), _staff(shop), _connected(shop),
            _webhook(shop), _mail(shop), _order(shop),
            _accounts(shop), _media(shop), _assistant(shop), _search(shop)]


def summary(items: list[dict]) -> dict:
    """{required, passed, next, warnings, complete}: passed — обов'язкові готові або з попередженням."""
    required = [s for s in items if s["kind"] == REQUIRED]
    passed = [s for s in required if s["state"] in (DONE, WARN)]
    nxt = (next((s for s in required if s["state"] == TODO), None)
           or next((s for s in items if s["kind"] == RECOMMENDED and s["state"] == TODO), None)
           or next((s for s in items if s["state"] == WARN), None))
    count = lambda state: sum(1 for s in items if s["state"] == state)  # noqa: E731
    return {"required": len(required), "passed": len(passed), "next": nxt, "total": len(items),
            "done": count(DONE), "warnings": count(WARN), "skipped": count(SKIP),
            "critical": sum(1 for s in required if s["state"] == TODO),   # обов'язкові, не зроблені
            "todo": sum(1 for s in items if s["kind"] != REQUIRED and s["state"] == TODO),
            "complete": len(passed) == len(required)}
