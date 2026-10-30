"""«🔍 Що бачить і що може змінити ключ» — картка ключа API (api/admin.APIKeyAdmin.insight_view). Логіка лише тут:

  capabilities  кожна адреса /api/v1/ з коду (guide/api_docs.endpoints): право (як api.permissions.required_scope),
                чи дозволено ЦЬОМУ ключу, що повертає (поля серіалізатора, звужені «Лише ці поля» й ключем сайту),
                які поля приймає й що змінює запис — людською мовою
  restrictions  обмеження ключа: активний / строк, магазин, IP, ліміти, поля, записів за добу, журнал
  preview       «👁 Приклад даних»: GET як цей ключ усередині Minerva (той самий ланцюжок, що й зовнішній запит —
                як консоль API), позначений атрибутом запиту key_limits.PREVIEW_ATTR (ззовні не задається): без лімітів
                і лічильників «різних записів», без last_used і без рядка журналу. Лише адреси PREVIEW — без побічних
                дій і секретів у відповіді; поля-секрети все одно маскуються
  simulate      «🧪 Що змінить запит»: тіло перевіряє серіалізатор дії (is_valid, без save) + лише читання бази
                (товар за артикулом, залишок, наявне замовлення) у транзакції, яка завжди відкочується. Дії, які без
                наслідків не перевірити (лист, Stripe, вебхук назовні, сервіс закупівель), — пояснення замість запуску
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from types import SimpleNamespace

from django.db import transaction
from django.http import QueryDict
from django.urls import Resolver404, resolve
from django.utils import timezone

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/"
PREVIEW_ROWS = 5                    # записів у «👁 Приклад даних» за замовчуванням
PREVIEW_MAX_ROWS = 20
PREVIEW_TEXT_MAX = 60_000            # символів JSON у відповіді сторінці
SECRET_KEY_RE = re.compile(r"(secret|password|passwd|token|api_key|apikey|bridge_key|access_key|private_key|session)$",
                           re.I)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
NOT_DELETED = "Не підтримується (405) — через API це не видаляється."

SECTIONS = [
    ("stock", "📦 Склад"), ("products", "🏷 Товари"), ("orders", "🧾 Замовлення"), ("shipments", "🚚 Доставка"),
    ("customers", "👥 Клієнти CRM"), ("purchases", "📥 Закупівлі"), ("shop", "🏪 Магазин і сайт"),
    ("account", "👤 Кабінет покупця"), ("payments", "💳 Оплати"), ("webhooks", "🔔 Вебхуки"),
    ("knowledge", "🧠 База знань (RAG)"), ("service", "🔌 Службові"),
]


def endpoint_key(view: str, action: str | None, path: str, method: str = "GET") -> str:
    """Ключ адреси для текстів: «ProductViewSet.list», «ShopShippingView.get», «account:MeView.patch».
    Кабінет покупця й база знань мають однакові назви класів (StatusView) — розрізняються адресою."""
    area = ""
    if path.startswith(PREFIX + "shop/account/") or path.rstrip("/") == PREFIX + "shop/account":
        area = "account:"
    elif path.startswith(PREFIX + "knowledge/"):
        area = "knowledge:"
    return f"{area}{view}.{action or (method or 'GET').lower()}"


def section_of(path: str) -> str:
    rest = path[len(PREFIX):] if path.startswith(PREFIX) else path
    if rest.startswith("shop/account"):
        return "account"
    if rest.startswith(("shop/payments", "payments/")):
        return "payments"
    if rest.startswith("shop/"):
        return "shop"
    for prefix, name in (("stock", "stock"), ("locations", "stock"), ("products", "products"),
                         ("categories", "products"), ("orders", "orders"), ("shipments", "shipments"),
                         ("customers", "customers"), ("purchase-orders", "purchases"), ("webhooks", "webhooks"),
                         ("knowledge", "knowledge")):
        if rest.startswith(prefix):
            return name
    return "service"


# ── Тексти: що робить адреса ─────────────────────────────────────────────────

SUMMARIES = {
    "ping.get": "Перевірка підключення: назва ключа і його права",
    "StockViewSet.list": "Залишки всіх товарів: на складі, резерв, доступно, очікується",
    "StockViewSet.retrieve": "Залишок одного товару з розбивкою по локаціях",
    "StockViewSet.check": "Перевірка наявності для кошика",
    "StockViewSet.count": "Інвентаризація — встановити фактичний залишок",
    "MovementViewSet.list": "Журнал рухів складу",
    "MovementViewSet.retrieve": "Один рух складу",
    "MovementViewSet.create": "Прихід / списання / коригування складу",
    "LocationViewSet.list": "Локації складу",
    "LocationViewSet.retrieve": "Одна локація складу",
    "CategoryViewSet.list": "Категорії товарів",
    "CategoryViewSet.retrieve": "Одна категорія товарів",
    "ShopProductViewSet.list": "Каталог магазину ключа: ціни зі ступенями, наявність, фото, даташити",
    "ShopProductViewSet.retrieve": "Один товар каталогу магазину",
    "ProductViewSet.list": "Товари Minerva (?with_stock=1 — разом із залишками)",
    "ProductViewSet.retrieve": "Один товар Minerva",
    "ProductViewSet.create": "Створити товар",
    "ProductViewSet.update": "Змінити товар (усі поля)",
    "ProductViewSet.partial_update": "Змінити окремі поля товару",
    "ProductViewSet.destroy": "Видалити товар",
    "SalesOrderViewSet.list": "Замовлення",
    "SalesOrderViewSet.retrieve": "Одне замовлення з позиціями й відправленнями",
    "SalesOrderViewSet.create": "Створити замовлення",
    "SalesOrderViewSet.update": "Змінити замовлення (усі поля)",
    "SalesOrderViewSet.partial_update": "Змінити окремі поля замовлення",
    "SalesOrderViewSet.destroy": "Видалити замовлення",
    "SalesOrderViewSet.cancel": "Скасувати замовлення",
    "ShipmentViewSet.list": "Відправлення й трекінг",
    "ShipmentViewSet.retrieve": "Одне відправлення",
    "CustomerViewSet.list": "Клієнти CRM",
    "CustomerViewSet.retrieve": "Один клієнт CRM",
    "CustomerViewSet.create": "Створити клієнта CRM",
    "CustomerViewSet.update": "Змінити клієнта CRM (усі поля)",
    "CustomerViewSet.partial_update": "Змінити окремі поля клієнта CRM",
    "CustomerViewSet.destroy": "Видалити клієнта CRM",
    "WebhookViewSet.list": "Вебхуки цього ключа",
    "WebhookViewSet.retrieve": "Один вебхук цього ключа",
    "WebhookViewSet.create": "Створити вебхук",
    "WebhookViewSet.update": "Змінити вебхук (усі поля)",
    "WebhookViewSet.partial_update": "Змінити поля вебхука",
    "WebhookViewSet.destroy": "Видалити вебхук",
    "WebhookViewSet.deliveries": "Останні 50 доставок вебхука",
    "WebhookViewSet.test": "Тестова подія ping на адресу вебхука",
    "PurchaseOrderViewSet.list": "Закупівлі (замовлення постачальникам)",
    "PurchaseOrderViewSet.retrieve": "Одна закупівля з позиціями, трекінгом і доставками",
    "PurchaseOrderViewSet.create": "Передати закупівлю із зовнішньої CRM",
    "PurchaseOrderViewSet.partial_update": "Оновити закупівлю зовнішньої CRM",
    "PurchaseOrderViewSet.shipments": "Трек-номер закупівлі",
    "PurchaseOrderViewSet.shipments_by_ref": "Трек-номер закупівлі (номер у тілі)",
    "PurchaseOrderViewSet.deliveries": "«Доставлено» — оприбуткування закупівлі",
    "PurchaseOrderViewSet.deliveries_by_ref": "«Доставлено» (номер у тілі)",
    "PurchaseOrderViewSet.cancel": "Скасувати закупівлю зовнішньої CRM",
}

EFFECTS = {
    "StockViewSet.check": "Лише перевіряє наявність — нічого не змінює (тому досить права читання).",
    "StockViewSet.count": "Інвентаризація: встановлює фактичний залишок товару на локації — Minerva записує рух "
                          "«Коригування» на різницю.",
    "MovementViewSet.create": "Записує рух складу — прихід (+), списання (−) або коригування — і змінює залишок. "
                              "Повтор з тим самим external_key рух не дублює.",
    "ProductViewSet.create": "Створює новий товар у каталозі Minerva.",
    "ProductViewSet.update": "Змінює картку товару (PUT — потрібні всі обов'язкові поля).",
    "ProductViewSet.partial_update": "Змінює окремі поля картки товару (назва, ціни, точка замовлення…).",
    "ProductViewSet.destroy": NOT_DELETED,
    "SalesOrderViewSet.create": "Створює замовлення з позиціями за артикулами; склад — резерв або списання за "
                                "налаштуваннями складу. Повтор з тим самим джерелом і номером повертає наявне "
                                "замовлення. shop: true — ціни лише з Minerva, quote: true — запит пропозиції без складу.",
    "SalesOrderViewSet.update": "Змінює поля замовлення (статус, адреса, оплата…); зміна статусу рухає склад: "
                                "«Скасовано» — товар повертається, «Відправлено» — списання.",
    "SalesOrderViewSet.partial_update": "Змінює окремі поля замовлення (статус, адреса, оплата…); зміна статусу "
                                        "рухає склад: «Скасовано» — товар повертається, «Відправлено» — списання.",
    "SalesOrderViewSet.destroy": NOT_DELETED,
    "SalesOrderViewSet.cancel": "Скасовує замовлення й повертає товар на склад (restock=false — лише знімає резерв). "
                                "Відправлене чи доставлене — відмова 409.",
    "CustomerViewSet.create": "Створює клієнта в CRM.",
    "CustomerViewSet.update": "Змінює дані клієнта CRM (PUT — потрібні всі обов'язкові поля).",
    "CustomerViewSet.partial_update": "Змінює окремі поля клієнта CRM.",
    "CustomerViewSet.destroy": NOT_DELETED,
    "WebhookViewSet.create": "Створює вебхук цього ключа: Minerva надсилатиме події (склад, замовлення, доставка) "
                             "на вказану адресу.",
    "WebhookViewSet.update": "Змінює адресу, події чи стан вебхука цього ключа.",
    "WebhookViewSet.partial_update": "Змінює адресу, події чи стан вебхука цього ключа.",
    "WebhookViewSet.destroy": "Видаляє вебхук цього ключа.",
    "WebhookViewSet.test": "Надсилає тестову подію ping на адресу вебхука (справжній запит назовні).",
    "PurchaseOrderViewSet.create": "Передає замовлення постачальнику із зовнішньої CRM: створює або оновлює "
                                   "закупівлю й позиції (постачальник — за назвою чи email, нового створює).",
    "PurchaseOrderViewSet.partial_update": "Оновлює закупівлю зовнішньої CRM: позиції, дати, номер у постачальника.",
    "PurchaseOrderViewSet.shipments": "Записує трек-номер вхідного відправлення закупівлі.",
    "PurchaseOrderViewSet.shipments_by_ref": "Записує трек-номер вхідного відправлення закупівлі.",
    "PurchaseOrderViewSet.deliveries": "«Доставлено»: оприбутковує товар на склад (або лише позначає «прибуло» — "
                                       "за налаштуваннями складу).",
    "PurchaseOrderViewSet.deliveries_by_ref": "«Доставлено»: оприбутковує товар на склад (або лише позначає "
                                              "«прибуло» — за налаштуваннями складу).",
    "PurchaseOrderViewSet.cancel": "Скасовує закупівлю зовнішньої CRM.",
    "account:StatusView.get": "Читання; з ?link_de= / ?link_en= Minerva запам'ятовує адреси кабінету для листів.",
    "account:RegisterView.post": "Створює акаунт покупця магазину й надсилає лист підтвердження.",
    "account:ResendView.post": "Надсилає лист підтвердження ще раз.",
    "account:VerifyView.post": "Підтверджує email покупця, задає пароль і відкриває сесію.",
    "account:LoginView.post": "Перевіряє пароль покупця й відкриває сесію кабінету.",
    "account:LogoutView.post": "Закриває сесію покупця.",
    "account:ForgotView.post": "Надсилає покупцю лист для скидання пароля.",
    "account:ResetView.post": "Задає новий пароль покупця за посиланням з листа.",
    "account:MeView.patch": "Змінює профіль покупця.",
    "account:MeView.delete": "Видаляє акаунт покупця (потрібен його пароль).",
    "account:PasswordView.post": "Змінює пароль покупця.",
    "account:OrderPayView.post": "Створює сторінку оплати Stripe для замовлення покупця.",
    "account:CartView.put": "Зберігає кошик покупця.",
    "StripeCheckoutView.post": "Створює сторінку оплати Stripe для замовлення (сума — лише з Minerva).",
    "StripeSyncView.post": "Питає Stripe про стан оплати й оновлює оплату замовлення.",
    "StripeWebhookView.post": "Для Stripe, без ключа API: подія оплати, справжність — за підписом Stripe.",
    "StaffLoginView.login": "Перевіряє логін і пароль персоналу сайту за Minerva й записує вхід у журнал сайту.",
    "SiteEventView.event": "Записує подію в журнал адмінки сайту (лише назви полів).",
    "MediaStatusView.report": "Записує звіт сайту, які фото й даташити він уже зберіг.",
    "PairView.post": "Без ключа: підключає сайт одноразовим кодом — видає новий ключ API й вебхук.",
    "ShopAssistantView.get": "Читання; ?site=… — записує звіт сайту про його налаштування помічника. "
                             "Відповідь містить ключ веб-моста помічника.",
    "SiteMailView.get": "Читання; ?mail_site=… — записує звіт сайту. Відповідь містить пароль SMTP скриньки магазину.",
    "ShopSearchAssistView.get": "Записує запит у «🔎 Пошук на сайті» і може викликати AI чи RAG (платні запити).",
    "knowledge:ChangesView.get": "Читання стрічки змін; Minerva фіксує звернення RAG (стан на сторінці «База знань»).",
    "knowledge:SnapshotCreateView.post": "Створює знімок бази знань для повної вигрузки в RAG.",
    "knowledge:StatusView.post": "Записує статуси індексації документів від RAG.",
}

# Відповіді без серіалізатора — що в них
RETURNS_TEXT = {
    "ping.get": "ok, server_time, key (назва ключа), scopes, default_source",
    "ShopShippingView.get": "configured, currency, free_shipping, allowed_countries, zones[] (країни, ціни доставки)",
    "ShopAssistantView.get": "shop, managed, enabled, upstream_url, bridge_key (лише ключу, прив'язаному до магазину)",
    "SiteMailView.get": "managed, smtp {host, port, security, user, password}, from, from_name, reply_to, bcc, notify",
    "ShopSearchAssistView.get": "mode, together[] (часто купують разом), ai / rag — підказки",
    "PaymentsStatusView.get": "stripe {enabled, mode}",
    "MediaFileView.get": "файл фото чи даташита (не JSON)",
    "WebhookViewSet.deliveries": "id, event, status, attempts, response_code, next_attempt_at, created_at, "
                                 "delivered_at, payload (без тіла відповіді отримувача)",
    "PurchaseOrderViewSet.list": "id / code, source, external_id, status, supplier, дати, total, lines[] (sku, "
                                 "qty_ordered, qty_received, unit_price), shipments[], receipts[]",
    "PurchaseOrderViewSet.retrieve": "id / code, source, external_id, status, supplier, дати, total, lines[] (sku, "
                                     "qty_ordered, qty_received, unit_price), shipments[], receipts[]",
    "account:StatusView.get": "enabled, password_min_length",
    "account:MeView.get": "профіль покупця (email, компанія, адреси, Kundennummer)",
    "account:OrdersView.get": "замовлення й запити покупця",
    "account:OrderDetailView.get": "одне замовлення покупця з позиціями й документами",
    "account:OrderDocumentView.get": "PDF документа замовлення (не JSON)",
    "account:CartView.get": "кошик покупця {items: {sku: qty}}",
    "knowledge:ChangesView.get": "події стрічки: листи й документи для RAG (схеми api/schemas/knowledge)",
    "knowledge:DocumentView.get": "документ бази знань (лист чи файл) за схемою",
    "knowledge:AttachmentView.get": "файл вкладення (не JSON)",
    "knowledge:SnapshotPageView.get": "сторінка знімка: документи й ревізії",
    "knowledge:SummaryView.get": "підсумок бази знань для звірки",
}

# Тіла без серіалізатора — що приймають
ACCEPTS_TEXT = {
    "SalesOrderViewSet.cancel": "restock (true — повернути товар на склад, false — лише зняти резерв)",
    "WebhookViewSet.test": "—",
    "PurchaseOrderViewSet.create": "external_id, supplier {name, email}, order_date, expected_date, currency, "
                                   "lines[] {line_id, sku, qty, unit_price}, strict",
    "PurchaseOrderViewSet.partial_update": "ті самі поля, що й створення",
    "PurchaseOrderViewSet.shipments": "tracking_number, carrier, status, expected_date",
    "PurchaseOrderViewSet.shipments_by_ref": "external_id (або code), tracking_number, carrier, status",
    "PurchaseOrderViewSet.deliveries": "delivery_id або tracking_number, lines[] {sku, qty}, allow_over",
    "PurchaseOrderViewSet.deliveries_by_ref": "external_id (або code), delivery_id, lines[] {sku, qty}, allow_over",
    "PurchaseOrderViewSet.cancel": "reason",
    "StaffLoginView.login": "login, password, site",
    "SiteEventView.event": "user, action, fields[], site",
    "MediaStatusView.report": "have[], failed {sha256: текст}, sync_url",
    "PairView.post": "code, site_url",
    "StripeCheckoutView.post": "order_number, success_url, cancel_url, lang",
    "StripeSyncView.post": "order_number",
    "StripeWebhookView.post": "подія Stripe (підпис Stripe-Signature)",
    "account:RegisterView.post": "email, password, company, name, phone, vat_id, адреса, confirm_b2b, lang, link",
    "account:ResendView.post": "email, link, lang",
    "account:VerifyView.post": "token, password, remember",
    "account:LoginView.post": "email, password, remember",
    "account:LogoutView.post": "— (сесія — заголовок X-Customer-Session)",
    "account:ForgotView.post": "email, link, lang",
    "account:ResetView.post": "token, password, remember",
    "account:MeView.patch": "поля профілю покупця (компанія, контакт, адреси)",
    "account:MeView.delete": "password",
    "account:PasswordView.post": "current_password, new_password",
    "account:OrderPayView.post": "success_url, cancel_url",
    "account:CartView.put": "items {sku: qty}",
    "knowledge:SnapshotCreateView.post": "types (схеми api/schemas/knowledge)",
    "knowledge:StatusView.post": "статуси документів від RAG (схеми api/schemas/knowledge)",
}


def _input_serializers() -> dict:
    """Дії, що перевіряють тіло не тим серіалізатором, що віддає відповідь (views.py)."""
    from . import serializers as s
    return {"StockViewSet.check": s.StockCheckSerializer, "StockViewSet.count": s.StockCountSerializer,
            "MovementViewSet.create": s.MovementCreateSerializer, "SalesOrderViewSet.create": s.OrderCreateSerializer}


# «👁 Приклад даних» — лише читання без побічних дій і без секретів у відповіді
PREVIEW = {
    "ping.get", "StockViewSet.list", "StockViewSet.retrieve", "MovementViewSet.list", "MovementViewSet.retrieve",
    "LocationViewSet.list", "LocationViewSet.retrieve", "CategoryViewSet.list", "CategoryViewSet.retrieve",
    "ShopProductViewSet.list", "ShopProductViewSet.retrieve", "ProductViewSet.list", "ProductViewSet.retrieve",
    "SalesOrderViewSet.list", "SalesOrderViewSet.retrieve", "ShipmentViewSet.list", "ShipmentViewSet.retrieve",
    "CustomerViewSet.list", "CustomerViewSet.retrieve", "WebhookViewSet.list", "WebhookViewSet.retrieve",
    "WebhookViewSet.deliveries", "PurchaseOrderViewSet.list", "PurchaseOrderViewSet.retrieve",
    "ShopShippingView.get", "PaymentsStatusView.get",
}
NO_PREVIEW = {
    "ShopAssistantView.get": "відповідь містить ключ веб-моста помічника",
    "SiteMailView.get": "відповідь містить пароль SMTP",
    "ShopSearchAssistView.get": "записує запит у журнал пошуку й може викликати платний AI",
    "MediaFileView.get": "відповідь — файл, не дані",
    "account:": "потрібна сесія покупця; читання запам'ятовує адреси кабінету",
    "knowledge:": "протокол RAG: читання стрічки змінює її стан",
}

SIMULATE_NO = {
    "WebhookViewSet.test": "надсилає справжній запит на адресу вебхука — Minerva його не виконує",
    "PurchaseOrderViewSet.": "тіло закупівлі перевіряє сервіс закупівель лише під час запису (постачальник, позиції, "
                             "доставки) — без наслідків не перевірити",
    "account:": "акаунти покупців: листи, сесії й паролі — без наслідків не перевірити",
    "StripeCheckoutView.": "звертається до Stripe",
    "StripeSyncView.": "звертається до Stripe",
    "StripeWebhookView.": "лише для Stripe, без ключа",
    "knowledge:": "протокол RAG — тіло перевіряють схеми api/schemas/knowledge",
    "StaffLoginView.": "перевіряє пароль персоналу й пише журнал сайту",
    "SiteEventView.": "службовий запис журналу сайту",
    "MediaStatusView.": "службовий звіт сайту",
    "PairView.": "видає новий ключ — лише справжнім кодом підключення",
}


def _lookup(table: dict, ekey: str):
    """Значення за точним ключем або за початком («account:», «PurchaseOrderViewSet.»)."""
    if ekey in table:
        return table[ekey]
    for prefix, value in table.items():
        if prefix.endswith((":", ".")) and ekey.startswith(prefix):
            return value
    return None


# ── Ключ: магазин, обмеження ─────────────────────────────────────────────────

class KeyContext:
    """Що про ключ треба всім частинам сторінки — один раз."""

    def __init__(self, key):
        from shop.assistant import key_is_bound
        from shop.services import shop_for_key
        from .key_limits import fields_of
        from .views import _bound_shop
        self.key = key
        self.scopes = set(key.scopes or [])
        self.bound = _bound_shop(SimpleNamespace(auth=key))       # ключ сайту: лише свій магазин
        self.shop = shop_for_key(key)                             # магазин для /shop/… (і за замовчуванням)
        self.site_bound = bool(self.shop and key_is_bound(self.shop, key))
        self.fields = fields_of(key)
        today = timezone.localdate()
        self.expired = bool(key.expires_at and key.expires_at < today)
        self.works = key.is_active and not self.expired


def restrictions(key, ctx: KeyContext | None = None) -> list[dict]:
    """Обмеження ключа людською мовою: [{icon, title, text, level ok|warn|err|info}]."""
    from django.conf import settings
    from .key_limits import usage
    from .models import SCOPE_CHOICES
    ctx = ctx or KeyContext(key)
    out = []
    if not key.is_active:
        out.append({"icon": "⛔", "title": "Ключ вимкнений", "level": "err",
                    "text": "Будь-який запит отримає 401. Увімкніть «Активний», щоб ключ знову працював."})
    elif ctx.expired:
        out.append({"icon": "⛔", "title": "Строк минув", "level": "err",
                    "text": f"Дійсний до {key.expires_at:%d.%m.%Y} — тепер будь-який запит отримає 401."})
    else:
        out.append({"icon": "✅", "title": "Ключ працює", "level": "ok",
                    "text": f"Дійсний до {key.expires_at:%d.%m.%Y}." if key.expires_at else "Без строку дії."})
    labels = dict(SCOPE_CHOICES)
    out.append({"icon": "🔑", "title": "Права", "level": "info" if ctx.scopes else "warn",
                "text": "; ".join(f"{labels.get(s, s)} ({s})" for s in sorted(ctx.scopes))
                        or "Жодного права — ключ може лише перевірити підключення (/ping/)."})
    if ctx.bound is not None:
        out.append({"icon": "🏪", "title": f"Ключ сайту магазину «{ctx.bound}»", "level": "info",
                    "text": f"Замовлення — лише цього магазину (джерело {ctx.bound.slug}); зміна замовлення — лише поля "
                            "оплати; товари (/products/) — лише видимі в магазині, без закупівельної ціни, точки "
                            "замовлення й приміток; вебхуки — лише події цього магазину."})
    elif ctx.shop is not None:
        out.append({"icon": "🏪", "title": "Магазин", "level": "info",
                    "text": f"Ключ до магазину не прив'язаний: адреси /shop/… працюють з магазином за замовчуванням "
                            f"«{ctx.shop}», а замовлення й товари він бачить усі (як інші ключі з цими правами)."})
    else:
        out.append({"icon": "🏪", "title": "Магазин", "level": "info",
                    "text": "Магазину немає — адреси /shop/… відповідатимуть 404 «no_shop»."})
    if key.default_source:
        out.append({"icon": "🏷", "title": "Джерело замовлень", "level": "info",
                    "text": f"Замовлення без source отримують джерело «{key.default_source}»."})
    out.append({"icon": "🛡", "title": "Дозволені IP-адреси", "level": "info",
                "text": ", ".join(x.strip() for x in key.allowed_ips.replace("\n", ",").split(",") if x.strip())
                        if key.allowed_ips.strip() else "З будь-якої адреси."})
    used = usage(key)
    server = (getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES") or {}).get("apikey", "120/min")
    limits = [f"за хвилину: {key.limit_minute}" if key.limit_minute else f"за хвилину: загальний ліміт сервера ({server})"]
    for field, label in (("limit_hour", "за годину"), ("limit_day", "за добу")):
        value = getattr(key, field)
        limits.append(f"{label}: {value} (використано {used[field]})" if value else f"{label}: без ліміту")
    out.append({"icon": "⏱", "title": "Ліміти запитів", "level": "info", "text": "; ".join(limits) + "."})
    out.append({"icon": "🔢", "title": "Різних записів за добу", "level": "info",
                "text": f"{key.items_per_day} (сьогодні вже {used['items_per_day']}); уже переглянуті — без обмежень, "
                        "решта у списках приховується." if key.items_per_day else "Без ліміту."})
    out.append({"icon": "✂", "title": "Лише ці поля у відповідях", "level": "info",
                "text": ", ".join(ctx.fields) + " — у записах лишаються тільки вони." if ctx.fields else "Усі поля."})
    out.append({"icon": "📜", "title": "Журнал запитів", "level": "warn" if key.log_mode == key.LOG_OFF else "info",
                "text": key.get_log_mode_display() + "."})
    hooks = key.webhooks.count()
    if hooks:
        out.append({"icon": "🔔", "title": "Вебхуки ключа", "level": "info",
                    "text": f"{hooks} — Minerva сама надсилає їм події (те, на що в ключа є право)."})
    return out


# ── Адреси: право, доступ, поля ──────────────────────────────────────────────

def _is_viewset(cls) -> bool:
    from rest_framework.viewsets import ViewSetMixin
    return issubclass(cls, ViewSetMixin)


def _action(cls, action: str, method: str) -> str:
    """Дія, як її бачать права й журнал: дія ViewSet, клас-атрибут action APIView (login…) або метод."""
    if _is_viewset(cls):
        return action
    return getattr(cls, "action", None) or method.lower()


def scope_of(cls, action: str, method: str) -> tuple[str | None, bool]:
    """(право, чи потрібен ключ) — тим самим правилом, що api.permissions.required_scope."""
    from .knowledge_views import KnowledgeView
    from .permissions import HasAPIKeyScope, required_scope
    if issubclass(cls, KnowledgeView):
        return f"knowledge:{cls.scope_mode}", True
    if HasAPIKeyScope not in (getattr(cls, "permission_classes", None) or []):
        return None, False                                       # без ключа (код підключення, підпис Stripe)
    view = cls()
    if _is_viewset(cls):
        view.action = action
    return required_scope(SimpleNamespace(method=method.upper()), view), True


def _view(cls, action, method, query=""):
    view = cls()
    if _is_viewset(cls):
        view.action = action
    view.request = SimpleNamespace(method=method.upper(), query_params=QueryDict(query), auth=None, user=None,
                                   data={})
    view.format_kwarg, view.kwargs, view.args = None, {}, ()
    return view


def _serializer(cls, action, method, query=""):
    try:
        return _view(cls, action, method, query).get_serializer_class()
    except Exception:  # noqa: BLE001 — в'юшка без серіалізатора або хоче справжній запит
        return getattr(cls, "serializer_class", None)


def _field_names(serializer_class, writable=False) -> list[str]:
    if serializer_class is None:
        return []
    try:
        fields = serializer_class().fields
    except Exception:  # noqa: BLE001
        return []
    if writable:
        return [n for n, f in fields.items() if not f.read_only]
    return [n for n, f in fields.items() if not f.write_only]


EXTRA_RESPONSE = {"StockViewSet.retrieve": ["locations", "buildable"]}


def _returns(ctx: KeyContext, cls, action, ekey) -> dict:
    """Що повертає читання: поля (звужені ключем) і пояснення."""
    from .serializers import ProductSerializer
    notes = []
    text = RETURNS_TEXT.get(ekey, "")
    fields = [] if text else _field_names(_serializer(cls, action, "GET")) + EXTRA_RESPONSE.get(ekey, [])
    if ekey in ("ProductViewSet.list", "ProductViewSet.retrieve"):
        extra = [f for f in _field_names(_serializer(cls, action, "GET", "with_stock=1")) if f not in fields]
        if extra:
            notes.append("?with_stock=1 додає: " + ", ".join(extra))
        if ctx.bound is not None:
            hidden = [f for f in ProductSerializer.INTERNAL_FIELDS if f in fields]
            fields = [f for f in fields if f not in hidden]
            notes.append("ключ сайту не отримує: " + ", ".join(hidden))
    total = len(fields)
    if ctx.fields and fields:
        got = [f for f in fields if f in ctx.fields]
        if got:
            notes.append(f"«Лише ці поля»: ключ отримає {len(got)} з {total}")
        else:
            notes.append("жодне з «Лише ці поля» тут не повертається — записи прийдуть порожніми")
        fields = got
    elif ctx.fields and text and ekey.startswith("PurchaseOrderViewSet."):
        notes.append("«Лише ці поля» діють і тут: " + ", ".join(ctx.fields))
    return {"fields": fields, "total": total, "text": text, "notes": notes}


def _accepts(ctx: KeyContext, cls, action, ekey, method) -> dict:
    """Що приймає запис: поля тіла, які ключ може надіслати / змінити."""
    from .views import NoDeleteMixin, SHOP_ORDER_FIELDS
    if action == "destroy":
        return {"fields": [], "text": "—" if not issubclass(cls, NoDeleteMixin) else "", "notes": []}
    text = ACCEPTS_TEXT.get(ekey, "")
    serializer = _input_serializers().get(ekey) or (None if text else _serializer(cls, action, method))
    fields = _field_names(serializer, writable=True) if serializer else []
    notes = []
    if ekey in ("SalesOrderViewSet.update", "SalesOrderViewSet.partial_update") and ctx.bound is not None:
        fields = [f for f in fields if f in SHOP_ORDER_FIELDS]
        notes.append("ключ сайту змінює лише поля оплати; решту Minerva відкидає (ignored_fields)")
    return {"fields": fields, "text": text, "notes": notes}


def _access(ctx: KeyContext, cls, action, scope, uses_key, ekey) -> tuple[str, str]:
    """(allowed | denied | unsupported | nokey, пояснення) для цього ключа."""
    from .views import NoDeleteMixin
    if not uses_key:
        return "nokey", "без ключа"
    if action == "destroy" and issubclass(cls, NoDeleteMixin):
        return "unsupported", "не підтримується (405)"
    if ekey.startswith("knowledge:") and ctx.key.shop_id:
        return "denied", "ключ магазину не має доступу до бази знань"
    if scope is None:
        return "allowed", "будь-який ключ"
    if scope in ctx.scopes:
        return "allowed", f"право {scope}"
    return "denied", f"немає права {scope}"


def _notes(ctx: KeyContext, ekey: str) -> list[str]:
    """Як ключ сайту / магазин звужують саме цю адресу."""
    out = []
    view = ekey.split(".", 1)[0]
    if view == "SalesOrderViewSet" and ctx.bound is not None:
        out.append(f"лише замовлення магазину «{ctx.bound}» (джерело {ctx.bound.slug}); чуже — 404")
    if ekey == "SalesOrderViewSet.create":
        source = (f"{ctx.shop.slug} (з shop: true)" if ctx.shop else "") or ""
        default = ctx.key.default_source or "api"
        out.append(f"джерело нових замовлень: source із запиту, інакше «{default}»"
                   + (f"; замовлення магазину — «{source}»" if source else ""))
    if view == "ProductViewSet" and ctx.bound is not None:
        out.append(f"лише товари, видимі в магазині «{ctx.bound}»")
    if view == "WebhookViewSet":
        out.append("лише вебхуки, створені цим ключем"
                   + (f"; події лише магазину «{ctx.bound}»" if ctx.bound is not None else ""))
    if view in ("ShopProductViewSet", "ShopShippingView", "PaymentsStatusView", "StripeCheckoutView",
                "StripeSyncView", "MediaFileView", "MediaStatusView") or ekey.startswith("account:"):
        if ctx.shop is None:
            out.append("магазину немає — відмова no_shop")
        else:
            out.append(f"магазин «{ctx.shop}»" + ("" if ctx.site_bound else " (за замовчуванням)"))
    if ekey.startswith("account:") and ctx.shop is not None and not ctx.shop.accounts_enabled:
        out.append("кабінет покупців у магазині вимкнено — 403")
    if view in ("ShopSearchAssistView", "StaffLoginView", "SiteEventView") and not ctx.site_bound:
        out.append("лише для ключа, прив'язаного до магазину — цей отримає 403")
    if view in ("ShopAssistantView", "SiteMailView") and not ctx.site_bound:
        out.append("ключ не прив'язаний до магазину — секретів у відповіді немає")
    return out


def capabilities(key, ctx: KeyContext | None = None, detail: bool = True) -> list[dict]:
    """Кожна адреса API × метод для цього ключа (див. опис модуля). detail=False — без полів (картка ключа)."""
    from guide import api_docs
    ctx = ctx or KeyContext(key)
    rows = []
    for ep in api_docs.endpoints():
        cls, path = ep["cls"], ep["path"]
        for m in ep["methods"]:
            method = m["method"]
            action = _action(cls, m["action"], method)
            ekey = endpoint_key(cls.__name__, action, path, method)
            scope, uses_key = scope_of(cls, action, method)
            access, access_text = _access(ctx, cls, action, scope, uses_key, ekey)
            kind = "read" if method == "GET" else ("read_post" if scope and scope.endswith(":read") else "write")
            row = {"path": path, "method": method, "key": ekey, "section": section_of(path), "kind": kind,
                   "summary": SUMMARIES.get(ekey) or m["summary"] or "", "scope": scope or "", "access": access,
                   "access_text": access_text, "effect": EFFECTS.get(ekey, ""), "notes": _notes(ctx, ekey),
                   "preview": ekey in PREVIEW, "no_preview": _lookup(NO_PREVIEW, ekey) or "",
                   "simulate": method in WRITE_METHODS and ekey in SIMULATORS,
                   "no_simulate": _lookup(SIMULATE_NO, ekey) or "", "body": m["body"],
                   "params": re.findall(r"\{(\w+)\}", path)}
            if detail and method == "GET":
                row["returns"] = _returns(ctx, cls, action, ekey)
            elif detail:
                row["accepts"] = _accepts(ctx, cls, action, ekey, method)
            rows.append(row)
    return rows


# Приклади тіл для «🧪 Що змінить запит» (інакше — приклад гіда з полів серіалізатора)
SIM_EXAMPLES = {
    "POST /api/v1/orders/": {"order_number": "WS-1001", "client": "Muster GmbH", "email": "kunde@example.com",
                             "addr_country": "DE", "currency": "EUR", "lines": [{"sku": "FT220407", "qty": 2}]},
    "PATCH /api/v1/orders/{pk}/": {"payment_status": "paid", "payment_reference": "pi_123"},
    "POST /api/v1/orders/{pk}/cancel/": {"restock": True},
    "POST /api/v1/stock-movements/": {"sku": "FT220407", "tx_type": "Incoming", "qty": 10, "external_key": "grn-001"},
    "POST /api/v1/stock/count/": [{"sku": "FT220407", "qty": 25}],
    "POST /api/v1/stock/check/": {"items": [{"sku": "FT220407", "qty": 2}]},
    "POST /api/v1/products/": {"sku": "FT220499", "name": "Filter 2.4 GHz", "sale_price": "19.90"},
    "PATCH /api/v1/products/{pk}/": {"sale_price": "24.90"},
    "POST /api/v1/customers/": {"name": "Max Mustermann", "email": "kunde@example.com", "company": "Muster GmbH",
                                "country": "DE"},
    "PATCH /api/v1/customers/{pk}/": {"phone": "+49 30 123456"},
    "POST /api/v1/webhooks/": {"name": "shop", "url": "https://shop.example.com/api/shop/webhook.php",
                               "events": ["order.status_changed"]},
    "PATCH /api/v1/webhooks/{pk}/": {"is_active": False},
}


def examples(rows) -> dict:
    """{«POST /api/v1/orders/»: тіло} для рядків, які можна перевірити."""
    out = {}
    for r in rows:
        name = f'{r["method"]} {r["path"]}'
        if r["simulate"] and r["method"] != "DELETE" and (name in SIM_EXAMPLES or r["body"]):
            out[name] = SIM_EXAMPLES.get(name, r["body"])
    return out


def grouped(rows) -> list[tuple[str, list]]:
    by = {}
    for row in rows:
        by.setdefault(row["section"], []).append(row)
    return [(title, by[name]) for name, title in SECTIONS if name in by]


def summary(rows) -> dict:
    """Для картки ключа: скільки адрес читає / змінює / заборонено."""
    allowed = [r for r in rows if r["access"] == "allowed"]
    return {"read": sum(1 for r in allowed if r["kind"] != "write"),
            "write": sum(1 for r in allowed if r["kind"] == "write"),
            "denied": sum(1 for r in rows if r["access"] == "denied"),
            "write_paths": [f'{r["method"]} {r["path"]}' for r in allowed if r["kind"] == "write"]}


# ── 👁 Приклад даних ─────────────────────────────────────────────────────────

def match(path: str, method: str):
    """Адреса API → (клас, дія, параметри адреси, ключ тексту) або None."""
    path = (path or "").strip()
    if not path.startswith(PREFIX) or ".." in path or "//" in path or any(ord(c) < 32 for c in path):
        return None
    try:
        found = resolve(path)
    except Resolver404:
        return None
    callback = found.func
    cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
    if cls is None:
        return None
    actions = getattr(callback, "actions", None)
    if actions:
        action = actions.get(method.lower())
        if action is None:
            return None
    else:
        if not hasattr(cls, method.lower()):
            return None
        action = getattr(cls, "action", None) or method.lower()
    return SimpleNamespace(cls=cls, action=action, kwargs=found.kwargs,
                           key=endpoint_key(cls.__name__, action, path, method))


def _mask(data):
    """Поля-секрети (secret, password, token, …) — замасковані, навіть якщо адреса їх і віддає."""
    from core.secrets import mask
    if isinstance(data, dict):
        return {k: (mask(v) if isinstance(v, str) and v and SECRET_KEY_RE.search(str(k)) else _mask(v))
                for k, v in data.items()}
    if isinstance(data, list):
        return [_mask(v) for v in data]
    return data


def _dispatch(request, key, path: str, query: str):
    """GET як цей ключ усередині Minerva — тим самим ланцюжком middleware, що й зовнішній запит (як консоль API,
    dashboard/api_views._dispatch_internal), але з позначкою перегляду: атрибут PREVIEW_ATTR на самому об'єкті
    запиту. Заголовок чи параметр адреси його не задають — лише цей код. Ключ іде в заголовку лише всередині."""
    import sys
    from io import BytesIO
    from django.core.handlers.base import BaseHandler
    from django.core.handlers.wsgi import WSGIRequest
    from django.utils import translation
    from .key_limits import PREVIEW_ATTR
    environ = {
        "REQUEST_METHOD": "GET", "SCRIPT_NAME": "", "QUERY_STRING": query or "",
        "PATH_INFO": path.encode("utf-8").decode("iso-8859-1"),        # WSGI: байти UTF-8 як latin-1
        "SERVER_NAME": request.META.get("SERVER_NAME") or "localhost", "SERVER_PORT": str(request.get_port() or "80"),
        "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_HOST": request.get_host(), "REMOTE_ADDR": "127.0.0.1",
        "HTTP_AUTHORIZATION": f"Token {key.key}", "HTTP_ACCEPT": "application/json", "CONTENT_LENGTH": "0",
        "wsgi.version": (1, 0), "wsgi.url_scheme": request.scheme, "wsgi.input": BytesIO(b""),
        "wsgi.errors": sys.stderr, "wsgi.multithread": True, "wsgi.multiprocess": True, "wsgi.run_once": False,
    }
    internal = WSGIRequest(environ)
    setattr(internal, PREVIEW_ATTR, True)
    lang = translation.get_language()
    try:
        handler = BaseHandler()
        handler.load_middleware()
        response = handler.get_response(internal)
    finally:
        if lang:
            translation.activate(lang)
    content = b"".join(response.streaming_content) if response.streaming else response.content
    return response, content, internal


def preview(request, key, path: str, query: str = "") -> dict:
    """«👁 Приклад даних»: що ключ отримав би зараз (див. опис модуля). Лише GET і лише адреси PREVIEW."""
    from .key_limits import _items
    found = match(path, "GET")
    if found is None:
        return {"ok": False, "error": "Такої адреси API немає (або це не GET)."}
    if found.key not in PREVIEW:
        why = _lookup(NO_PREVIEW, found.key) or "адреса має побічні дії"
        return {"ok": False, "error": f"Цю адресу перегляд не відкриває: {why}."}
    if not key.is_active:
        return {"ok": False, "status": 401, "error": "Ключ вимкнений — будь-який запит отримає 401."}
    if key.expires_at and key.expires_at < timezone.localdate():
        return {"ok": False, "status": 401, "error": f"Строк ключа минув {key.expires_at:%d.%m.%Y} — запит отримає 401."}
    params = QueryDict(query or "", mutable=True)
    for name in ("format", "page_size"):
        params.pop(name, None)
    rows = PREVIEW_ROWS
    try:
        rows = max(1, min(int(QueryDict(query or "").get("page_size") or PREVIEW_ROWS), PREVIEW_MAX_ROWS))
    except ValueError:
        pass
    if found.action == "list":
        params["page_size"] = str(rows)
    query = params.urlencode()
    response, content, internal = _dispatch(request, key, path, query)
    text = content.decode(getattr(response, "charset", None) or "utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    data = _mask(data)
    items, _kind, _single = _items(data) if data is not None else (None, None, False)
    notes = ["Перегляд не рахується в ліміти й «різних записів за добу», не змінює «Останнє використання» й не "
             "пишеться в журнал."]
    if key.allowed_ips.strip():
        notes.append("«Дозволені IP-адреси» не перевірялись: запит іде з Minerva, а не з адреси програми.")
    quota = data.get("quota") if isinstance(data, dict) else None
    if quota and quota.get("hidden"):
        notes.append(f"Ліміт «різних записів за добу»: приховано {quota['hidden']} — ключ сьогодні вже бачив "
                     f"{quota.get('used')} з {quota.get('items_per_day')}.")
    log_note = getattr(internal, "api_log_note", "")
    if log_note and log_note not in " ".join(notes):
        notes.append(log_note)
    pretty = json.dumps(data, ensure_ascii=False, indent=2) if data is not None else text
    return {
        "ok": response.status_code < 400, "status": response.status_code, "url": path + (f"?{query}" if query else ""),
        "shown": len(items) if items is not None else None,
        "total": data.get("count") if isinstance(data, dict) and isinstance(data.get("count"), int) else None,
        "fields": sorted(items[0].keys()) if items and isinstance(items[0], dict) else [],
        "json": pretty[:PREVIEW_TEXT_MAX], "truncated": len(pretty) > PREVIEW_TEXT_MAX, "notes": notes,
    }


# ── 🧪 Що змінить запит ─────────────────────────────────────────────────────

class _Rollback(Exception):
    pass


def _errors(errors, prefix="") -> list[str]:
    """Помилки серіалізатора → «lines[0].sku: Вкажіть 'sku' або 'product'.»."""
    out = []
    if isinstance(errors, dict):
        for name, value in errors.items():
            label = name if name != "non_field_errors" else ""
            out += _errors(value, f"{prefix}.{label}" if prefix and label else (label or prefix))
    elif isinstance(errors, list):
        if all(isinstance(e, str) or hasattr(e, "code") for e in errors):
            out += [f"{prefix}: {e}" if prefix else str(e) for e in errors]
        else:
            for i, value in enumerate(errors):
                if value:
                    out += _errors(value, f"{prefix}[{i}]")
    else:
        out.append(f"{prefix}: {errors}" if prefix else str(errors))
    return out


def _num(value) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _invalid(serializer) -> dict:
    return {"ok": False, "status": 400, "verdict": "Відмова 400: дані не пройшли перевірку — Minerva нічого не змінить.",
            "errors": _errors(serializer.errors)}


def _fail(status, text, **extra) -> dict:
    return {"ok": False, "status": status, "verdict": text, **extra}


def _fake_request(key):
    return SimpleNamespace(auth=key, user=None, method="POST", query_params=QueryDict(""),
                           build_absolute_uri=lambda url="/": url)


def _diff(instance, data: dict) -> list[str]:
    """«поле: було → стане» для полів, які зміняться."""
    out, same = [], 0
    for name, new in data.items():
        old = getattr(instance, name, None)
        if hasattr(old, "all"):                          # зв'язки «багато до багатьох» — без порівняння
            out.append(f"{name}: → {new}")
            continue
        if old == new or (old in (None, "") and new in (None, "")):
            same += 1
            continue
        out.append(f"{name}: «{_num(old) if old not in (None, '') else '—'}» → «{_num(new) if new not in (None, '') else '—'}»")
    if same:
        out.append(f"без змін полів: {same}")
    return out


def _sim_movement(c) -> dict:
    from inventory.models import InventoryTransaction
    from inventory.services import stock
    from inventory.services.stock import StockError
    from .serializers import MovementCreateSerializer
    s = MovementCreateSerializer(data=c.body)
    if not s.is_valid():
        return _invalid(s)
    d = s.validated_data
    product = stock.resolve_sku(d["sku"])
    if not product:
        return _fail(404, f"Відмова 404: товару «{d['sku']}» немає — рух не запишеться.")
    if d.get("external_key") and InventoryTransaction.objects.filter(external_key=d["external_key"]).exists():
        return {"ok": True, "status": 200, "verdict": f"Повтор: рух з external_key «{d['external_key']}» уже є — "
                                                      "Minerva поверне його, залишок не зміниться."}
    try:
        location = stock.get_location(d.get("location"))
        qty = stock.signed_qty(d["tx_type"], d["qty"])
        stock._validate_qty(product, qty)
        stock._check_location_kind(product, location)
    except StockError as e:
        return _fail(400, f"Відмова 400 ({e.code}): {e}")
    from inventory.models import InventorySettings, Product
    on_loc = stock.annotate_stock(Product.objects.filter(pk=product.pk), location=location).get()._on_hand
    if qty < 0 and not InventorySettings.get().allow_negative_stock and on_loc + qty < 0:
        return _fail(409, f"Відмова 409 (insufficient_stock): на «{location.code}» є {_num(on_loc)}, списується "
                          f"{_num(-qty)} — від'ємний залишок у налаштуваннях складу заборонено.")
    available = stock.get_available(product)
    kind = {"Incoming": "Прихід", "Outgoing": "Списання", "Adjustment": "Коригування"}[d["tx_type"]]
    return {"ok": True, "status": 201, "verdict": "Запит пройде: Minerva запише рух складу.",
            "effects": [f"{kind} {_num(qty)} × «{product.sku}» на «{location.code}»",
                        f"на «{location.code}»: {_num(on_loc)} → {_num(on_loc + qty)}",
                        f"доступно всього: {_num(available)} → {_num(available + qty)}"]}


def _sim_count(c) -> dict:
    from inventory.models import Product
    from inventory.services import stock
    from inventory.services.stock import StockError
    from .serializers import StockCountSerializer
    many = isinstance(c.body, list)
    s = StockCountSerializer(data=c.body, many=many)
    if not s.is_valid():
        return _invalid(s)
    effects = []
    for row in (s.validated_data if many else [s.validated_data]):
        product = stock.resolve_sku(row["sku"])
        if not product:
            return _fail(400, f"Відмова 400 (sku_not_found): товару «{row['sku']}» немає — увесь запит не виконається.")
        try:
            location = stock.get_location(row.get("location"))
            stock._check_location_kind(product, location)
        except StockError as e:
            return _fail(400, f"Відмова 400 ({e.code}): {e} — увесь запит не виконається.")
        if not product.is_fractional_unit() and row["qty"] != int(row["qty"]):
            return _fail(400, f"Відмова 400 (fractional_qty): «{product.sku}» вимірюється в штуках.")
        current = stock.annotate_stock(Product.objects.filter(pk=product.pk), location=location).get()._on_hand
        delta = row["qty"] - current
        effects.append(f"«{product.sku}» на «{location.code}»: {_num(current)} → {_num(row['qty'])}"
                       + (f" (коригування {'+' if delta > 0 else ''}{_num(delta)})" if delta else " — без змін"))
    return {"ok": True, "status": 200, "verdict": "Запит пройде: Minerva встановить ці залишки.", "effects": effects}


def _sim_check(c) -> dict:
    from inventory.services import stock
    from .serializers import StockCheckSerializer
    s = StockCheckSerializer(data=c.body)
    if not s.is_valid():
        return _invalid(s)
    effects = []
    for item in stock.check_availability(s.validated_data["items"]):
        if not item["found"]:
            effects.append(f"«{item['sku']}»: товару немає")
        else:
            effects.append(f"«{item['sku']}»: просять {_num(item['requested'])}, доступно {_num(item['available'])} "
                           + ("✓" if item["ok"] else "✗ бракує"))
    return {"ok": True, "status": 200, "verdict": "Лише читання — нічого не зміниться.", "effects": effects}


def _model_sim(c, serializer_class, queryset, noun: str, ident: str) -> dict:
    instance = None
    if c.action in ("update", "partial_update"):
        instance = queryset.filter(pk=c.kwargs.get("pk")).first()
        if instance is None:
            return _fail(404, f"Відмова 404: {noun} #{c.kwargs.get('pk')} цей ключ не бачить.")
    s = serializer_class(instance, data=c.body, partial=c.action == "partial_update",
                         context={"request": _fake_request(c.key), "public_only": c.bound is not None})
    if not s.is_valid():
        return _invalid(s)
    if instance is None:
        name = s.validated_data.get(ident) or s.validated_data.get("name") or ""
        return {"ok": True, "status": 201, "verdict": f"Запит пройде: Minerva створить {noun} «{name}».",
                "effects": [f"{k}: {_num(v)}" for k, v in s.validated_data.items()][:30]}
    return {"ok": True, "status": 200, "verdict": f"Запит пройде: Minerva змінить {noun} «{getattr(instance, ident, '')}».",
            "effects": _diff(instance, s.validated_data)}


def _sim_product(c) -> dict:
    from inventory.models import Product
    from .serializers import ProductSerializer
    qs = Product.objects.all()
    if c.bound is not None:
        qs = qs.filter(shop_listings__shop=c.bound, shop_listings__is_visible=True).distinct()
    return _model_sim(c, ProductSerializer, qs, "товар", "sku")


def _sim_customer(c) -> dict:
    from crm.models import Customer
    from .serializers import CustomerSerializer
    return _model_sim(c, CustomerSerializer, Customer.objects.all(), "клієнта", "name")


def _orders(c):
    from sales.models import SalesOrder
    qs = SalesOrder.objects.all()
    return qs.filter(source=c.bound.slug) if c.bound is not None else qs


def _stock_effect(affects_stock: bool) -> str:
    from inventory.models import InventorySettings
    if not affects_stock:
        return "склад не зміниться (замовлення не впливає на склад)"
    s = InventorySettings.get()
    if s.deduct_on == s.DeductOn.CREATION:
        return "склад: товар спишеться одразу"
    shipped = s.deduct_on == s.DeductOn.SHIPPED
    if s.use_reservation:
        return f"склад: товар зарезервується до {'відправки' if shipped else 'доставки'}"
    return f"склад: товар спишеться при {'відправці' if shipped else 'доставці'}"


def _sim_order_create(c) -> dict:
    from inventory.models import InventorySettings, Product
    from inventory.services import stock
    from sales.models import SalesOrder
    from shop.services import shop_for_key
    from .serializers import OrderCreateSerializer
    s = OrderCreateSerializer(data=c.body)
    if not s.is_valid():
        return _invalid(s)
    d = dict(s.validated_data)
    is_shop, is_quote = d.get("shop", False), d.get("quote", False)
    shop = shop_for_key(c.key) if (is_shop or is_quote) else None
    if is_shop and not is_quote and shop is None:
        return _fail(400, "Відмова 400 (no_shop): для ключа не налаштовано магазин.")
    source = shop.slug if shop is not None else (d.get("source") or c.key.default_source or "api").strip()
    number = str(d["order_number"])
    existing = SalesOrder.objects.filter(source=source, order_number=number).first()
    if existing is None and is_shop and not is_quote:
        from shop.numbering import find_by_site_ref
        existing = find_by_site_ref(shop, number)
    if existing is not None:
        return {"ok": True, "status": 200, "verdict": f"Повтор: замовлення «{existing.order_number}» (джерело "
                                                      f"{existing.source}) уже є — Minerva поверне його, нового не створить."}
    lines, errors, wanted = [], [], []
    for i, ln in enumerate(d["lines"]):
        product = Product.objects.filter(pk=ln["product"]).first() if ln.get("product") else stock.resolve_sku(ln["sku"])
        raw = ln.get("sku") or (product.sku if product else str(ln.get("product")))
        if not product and is_quote:
            lines.append(f"«{raw}» × {_num(ln['qty'])} — поза каталогом, піде в нотатку")
            continue
        if not product:
            errors.append(f"рядок {i}: «{raw}» — товар не знайдено")
            continue
        if not product.is_fractional_unit() and ln["qty"] != int(ln["qty"]):
            errors.append(f"рядок {i}: «{raw}» — кількість має бути цілою")
            continue
        unit = ln.get("unit_price") if ln.get("unit_price") is not None else product.sale_price
        price = "" if is_shop or is_quote else (f" по {_num(unit)}" if unit is not None else " (без ціни)")
        lines.append(f"«{product.sku}» × {_num(ln['qty'])}{price}")
        wanted.append({"sku": product.sku, "qty": ln["qty"]})
    if errors:
        return _fail(400, "Відмова 400 (invalid_lines): помилки в рядках замовлення — нічого не створиться.", errors=errors)
    affects = (d.get("affects_stock", True) and not is_quote
               and not (shop is not None and shop.is_test and not shop.test_affects_stock))
    must_check = d.get("check_stock", False) or (not is_shop and not InventorySettings.get().allow_negative_stock)
    if affects and must_check and wanted:
        short = [a for a in stock.check_availability(wanted) if not a["ok"]]
        if short:
            return _fail(409, "Відмова 409 (insufficient_stock): недостатньо товару на складі.",
                         errors=[f"«{a['sku']}»: просять {_num(a['requested'])}, доступно {_num(a['available'])}"
                                 for a in short])
    kind = "запит пропозиції (QUOTE, без цін і складу)" if is_quote else ("замовлення магазину" if is_shop else "замовлення")
    effects = [f"створиться {kind} «{number}» у джерелі «{source}»"] + lines + [_stock_effect(affects)]
    notes = []
    if is_shop and not is_quote:
        notes.append("Ціни магазину (ступені), мінімальні кількості й доставка перевіряються лише справжнім запитом; "
                     "у Minerva замовлення отримає номер PO-<Kundennummer>-NNN.")
    return {"ok": True, "status": 201, "verdict": "Запит пройде: Minerva створить замовлення.", "effects": effects,
            "notes": notes}


def _sim_order_update(c) -> dict:
    from .serializers import SalesOrderDetailSerializer
    from .views import SHOP_ORDER_FIELDS
    order = _orders(c).filter(pk=c.kwargs.get("pk")).first()
    if order is None:
        return _fail(404, f"Відмова 404: замовлення #{c.kwargs.get('pk')} цей ключ не бачить.")
    if not isinstance(c.body, dict):
        return _fail(400, "Відмова 400: очікується JSON-об'єкт.")
    data, notes = c.body, []
    partial = c.action == "partial_update"
    if c.bound is not None:
        ignored = sorted(k for k in data if k not in SHOP_ORDER_FIELDS)
        data = {k: v for k, v in data.items() if k in SHOP_ORDER_FIELDS}
        partial = True
        if ignored:
            notes.append("Ключ сайту: Minerva відкине поля " + ", ".join(ignored) + " (ignored_fields).")
    s = SalesOrderDetailSerializer(order, data=data, partial=partial)
    if not s.is_valid():
        return _invalid(s)
    effects = _diff(order, s.validated_data)
    if "status" in s.validated_data and s.validated_data["status"] != order.status:
        notes.append("Зміна статусу рухає склад (inventory/services/stock.py): «Скасовано» повертає товар, "
                     "«Відправлено» / «Доставлено» — списання за налаштуваннями складу.")
    return {"ok": True, "status": 200, "verdict": f"Запит пройде: Minerva змінить замовлення «{order.order_number}».",
            "effects": effects or ["нічого не зміниться"], "notes": notes}


def _sim_order_cancel(c) -> dict:
    order = _orders(c).filter(pk=c.kwargs.get("pk")).prefetch_related("lines__product").first()
    if order is None:
        return _fail(404, f"Відмова 404: замовлення #{c.kwargs.get('pk')} цей ключ не бачить.")
    if order.status in ("shipped", "delivered"):
        return _fail(409, f"Відмова 409 (already_shipped): замовлення вже має статус «{order.status}».")
    body = c.body if isinstance(c.body, dict) else {}
    restock = str(body.get("restock", "true")).lower() not in ("0", "false", "no")
    if order.status == "cancelled":
        return {"ok": True, "status": 200, "verdict": f"Замовлення «{order.order_number}» уже скасоване — повторне "
                                                      "скасування склад не рухає."}
    effects = [f"статус: «{order.status}» → «cancelled»"]
    lines = [f"«{l.sku_raw or (l.product.sku if l.product_id else '?')}» × {_num(l.qty)}" for l in order.lines.all()]
    if not order.affects_stock:
        effects.append("склад не зміниться (замовлення не впливає на склад)")
    elif restock:
        effects.append("резерви знімуться, списане повернеться на склад: " + (", ".join(lines) or "позицій немає"))
    else:
        effects.append("лише зніметься резерв; списане лишиться списаним (restock=false)")
    return {"ok": True, "status": 200, "verdict": f"Запит пройде: Minerva скасує замовлення «{order.order_number}».",
            "effects": effects}


def _sim_webhook(c) -> dict:
    from .models import Webhook
    from .serializers import WebhookSerializer
    instance = None
    if c.action in ("update", "partial_update"):
        instance = Webhook.objects.filter(api_key=c.key, pk=c.kwargs.get("pk")).first()
        if instance is None:
            return _fail(404, f"Відмова 404: вебхука #{c.kwargs.get('pk')} цей ключ не створював.")
    s = WebhookSerializer(instance, data=c.body, partial=c.action == "partial_update",
                          context={"request": _fake_request(c.key)})
    if not s.is_valid():
        return _invalid(s)
    d = s.validated_data
    notes = [f"Події лише магазину «{c.bound}» (order_sources = {c.bound.slug})."] if c.bound is not None else []
    if instance is None:
        return {"ok": True, "status": 201, "verdict": f"Запит пройде: Minerva створить вебхук «{d.get('name')}».",
                "effects": [f"адреса: {d.get('url')}", "події: " + (", ".join(d.get("events") or []) or "усі")],
                "notes": notes}
    return {"ok": True, "status": 200, "verdict": f"Запит пройде: Minerva змінить вебхук «{instance.name}».",
            "effects": _diff(instance, d), "notes": notes}


def _sim_webhook_delete(c) -> dict:
    from .models import Webhook
    hook = Webhook.objects.filter(api_key=c.key, pk=c.kwargs.get("pk")).first()
    if hook is None:
        return _fail(404, f"Відмова 404: вебхука #{c.kwargs.get('pk')} цей ключ не створював.")
    return {"ok": True, "status": 204, "verdict": f"Запит пройде: Minerva видалить вебхук «{hook.name}» ({hook.url}).",
            "effects": ["події на цю адресу більше не надсилатимуться"]}


def _sim_not_deleted(c) -> dict:
    return _fail(405, "Відмова 405: " + NOT_DELETED)


SIMULATORS = {
    "MovementViewSet.create": _sim_movement, "StockViewSet.count": _sim_count, "StockViewSet.check": _sim_check,
    "ProductViewSet.create": _sim_product, "ProductViewSet.update": _sim_product,
    "ProductViewSet.partial_update": _sim_product, "ProductViewSet.destroy": _sim_not_deleted,
    "SalesOrderViewSet.create": _sim_order_create, "SalesOrderViewSet.update": _sim_order_update,
    "SalesOrderViewSet.partial_update": _sim_order_update, "SalesOrderViewSet.cancel": _sim_order_cancel,
    "SalesOrderViewSet.destroy": _sim_not_deleted,
    "CustomerViewSet.create": _sim_customer, "CustomerViewSet.update": _sim_customer,
    "CustomerViewSet.partial_update": _sim_customer, "CustomerViewSet.destroy": _sim_not_deleted,
    "WebhookViewSet.create": _sim_webhook, "WebhookViewSet.update": _sim_webhook,
    "WebhookViewSet.partial_update": _sim_webhook, "WebhookViewSet.destroy": _sim_webhook_delete,
}


def simulate(key, method: str, path: str, body_text: str) -> dict:
    """«🧪 Що змінить запит» (див. опис модуля): {ok, status, verdict, effects[], errors[], notes[], ran}."""
    method = (method or "").upper()
    if method not in WRITE_METHODS:
        return {"ok": False, "verdict": "Симуляція — лише для POST / PUT / PATCH / DELETE; читання — «👁 Приклад даних».",
                "ran": False}
    found = match(path, method)
    if found is None:
        return {"ok": False, "verdict": f"Такої адреси API для {method} немає.", "ran": False}
    ctx = KeyContext(key)
    scope, uses_key = scope_of(found.cls, found.action, method)
    access, access_text = _access(ctx, found.cls, found.action, scope, uses_key, found.key)
    if access == "denied":
        return {"ok": False, "status": 403, "verdict": f"Відмова 403: {access_text} — Minerva нічого не змінить.",
                "ran": False}
    if not ctx.works:
        return {"ok": False, "status": 401, "verdict": "Відмова 401: ключ вимкнений або прострочений.", "ran": False}
    sim = SIMULATORS.get(found.key)
    if sim is None:
        why = _lookup(SIMULATE_NO, found.key) or "для цієї дії перевірки без наслідків немає"
        return {"ok": None, "verdict": f"Не перевіряється: {why}.", "effect": EFFECTS.get(found.key, ""), "ran": False}
    try:
        body = json.loads(body_text) if (body_text or "").strip() else {}
    except ValueError as e:
        return {"ok": False, "status": 400, "verdict": f"Тіло — не JSON: {e}", "ran": False}
    c = SimpleNamespace(key=key, method=method, action=found.action, kwargs=found.kwargs, body=body, bound=ctx.bound)
    result = {}
    try:
        with transaction.atomic():         # лише читання — але й випадковий запис (налаштування за замовчуванням) зникне
            result = sim(c)
            raise _Rollback
    except _Rollback:
        pass
    except Exception as e:  # noqa: BLE001 — перевірка не ламає сторінку; нічого не записано (відкат)
        logger.exception("api key simulation %s %s", method, path)
        return {"ok": False, "verdict": f"Перевірка не вдалась: {type(e).__name__}: {str(e)[:200]}", "ran": False}
    result.setdefault("effects", [])
    result.setdefault("errors", [])
    result.setdefault("notes", [])
    result["ran"] = True
    result["effect"] = EFFECTS.get(found.key, "")
    result["notes"].append("Перевірено без запису: серіалізатор дії й читання бази; Minerva нічого не змінила.")
    return result
