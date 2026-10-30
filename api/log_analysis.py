"""Журнал API людською мовою й «📊 Аналіз журналу» (api/admin.APIRequestLogAdmin). Логіка лише тут:

  plain_line    рядок журналу одним реченням: «API RAG прочитав список товарів зі складом — 169 записів (сторінка 1,
                page_size 500)», «webshop створив замовлення PO-31728-001», «… отримав відмову 429 (…): денний ліміт»
                — з дії (view), методу, параметрів, відповіді, кількості й записів; список журналу, картка запиту, CSV
  analyse       за ключем (усі / один / невідомі) і періодом: запити по днях (читання / зміни / помилки), відмови за
                кодом, скільки записів віддано, які саме артикули / номери ключ отримав і скільки разів (з посиланнями
                на товар / замовлення), адреси, IP, програми, найдовші й найбільші запити, кожна зміна
  csv_response  відфільтрований журнал у CSV (Excel: «;» і BOM)
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, time, timedelta
from urllib.parse import parse_qsl

from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .key_insight import endpoint_key

READ_METHODS = ("GET", "HEAD")
PERIODS = (1, 7, 30)
RECORDS_SCAN_MAX = 3000               # рядків журналу для «які записи ключ отримав» (найновіші)
RECORDS_SHOW = 1000
WRITES_SHOW = 300

# дія → (що зробив — минулий час, що хотів — іменник). {one} — запис запиту (артикул, номер).
PHRASES = {
    "ping.get": ("перевірив підключення (ping)", "перевірка підключення"),
    "StockViewSet.list": ("прочитав залишки складу", "читання залишків складу"),
    "StockViewSet.retrieve": ("прочитав залишок товару {one}", "читання залишку товару {one}"),
    "StockViewSet.check": ("перевірив наявність для кошика", "перевірка наявності"),
    "StockViewSet.count": ("провів інвентаризацію", "інвентаризація"),
    "MovementViewSet.list": ("прочитав журнал рухів складу", "читання журналу рухів складу"),
    "MovementViewSet.retrieve": ("прочитав рух складу", "читання руху складу"),
    "MovementViewSet.create": ("записав рух складу", "запис руху складу"),
    "LocationViewSet.list": ("прочитав локації складу", "читання локацій складу"),
    "LocationViewSet.retrieve": ("прочитав локацію {one}", "читання локації"),
    "CategoryViewSet.list": ("прочитав категорії товарів", "читання категорій товарів"),
    "CategoryViewSet.retrieve": ("прочитав категорію товарів", "читання категорії товарів"),
    "ShopProductViewSet.list": ("прочитав каталог магазину", "читання каталогу магазину"),
    "ShopProductViewSet.retrieve": ("прочитав товар магазину {one}", "читання товару магазину {one}"),
    "ProductViewSet.list": ("прочитав список товарів", "читання списку товарів"),
    "ProductViewSet.retrieve": ("прочитав товар {one}", "читання товару {one}"),
    "ProductViewSet.create": ("створив товар {one}", "створення товару"),
    "ProductViewSet.update": ("змінив товар {one}", "зміна товару {one}"),
    "ProductViewSet.partial_update": ("змінив товар {one}", "зміна товару {one}"),
    "ProductViewSet.destroy": ("видаляв товар", "видалення товару"),
    "SalesOrderViewSet.list": ("прочитав список замовлень", "читання списку замовлень"),
    "SalesOrderViewSet.retrieve": ("прочитав замовлення {one}", "читання замовлення {one}"),
    "SalesOrderViewSet.create": ("створив замовлення {one}", "створення замовлення"),
    "SalesOrderViewSet.update": ("змінив замовлення {one}", "зміна замовлення {one}"),
    "SalesOrderViewSet.partial_update": ("змінив замовлення {one}", "зміна замовлення {one}"),
    "SalesOrderViewSet.cancel": ("скасував замовлення {one}", "скасування замовлення {one}"),
    "SalesOrderViewSet.destroy": ("видаляв замовлення", "видалення замовлення"),
    "ShipmentViewSet.list": ("прочитав відправлення й трекінг", "читання відправлень"),
    "ShipmentViewSet.retrieve": ("прочитав відправлення {one}", "читання відправлення"),
    "CustomerViewSet.list": ("прочитав клієнтів CRM", "читання клієнтів CRM"),
    "CustomerViewSet.retrieve": ("прочитав клієнта CRM #{one}", "читання клієнта CRM"),
    "CustomerViewSet.create": ("створив клієнта CRM #{one}", "створення клієнта CRM"),
    "CustomerViewSet.update": ("змінив клієнта CRM #{one}", "зміна клієнта CRM"),
    "CustomerViewSet.partial_update": ("змінив клієнта CRM #{one}", "зміна клієнта CRM"),
    "CustomerViewSet.destroy": ("видаляв клієнта CRM", "видалення клієнта CRM"),
    "WebhookViewSet.list": ("прочитав свої вебхуки", "читання вебхуків"),
    "WebhookViewSet.retrieve": ("прочитав вебхук #{one}", "читання вебхука"),
    "WebhookViewSet.create": ("створив вебхук #{one}", "створення вебхука"),
    "WebhookViewSet.update": ("змінив вебхук #{one}", "зміна вебхука"),
    "WebhookViewSet.partial_update": ("змінив вебхук #{one}", "зміна вебхука"),
    "WebhookViewSet.destroy": ("видалив вебхук", "видалення вебхука"),
    "WebhookViewSet.test": ("надіслав тестову подію вебхука", "тестова подія вебхука"),
    "WebhookViewSet.deliveries": ("прочитав доставки вебхука", "читання доставок вебхука"),
    "PurchaseOrderViewSet.list": ("прочитав закупівлі", "читання закупівель"),
    "PurchaseOrderViewSet.retrieve": ("прочитав закупівлю {one}", "читання закупівлі"),
    "PurchaseOrderViewSet.create": ("передав закупівлю {one}", "передача закупівлі"),
    "PurchaseOrderViewSet.partial_update": ("оновив закупівлю {one}", "оновлення закупівлі"),
    "PurchaseOrderViewSet.shipments": ("передав трек-номер закупівлі {one}", "трек-номер закупівлі"),
    "PurchaseOrderViewSet.shipments_by_ref": ("передав трек-номер закупівлі {one}", "трек-номер закупівлі"),
    "PurchaseOrderViewSet.deliveries": ("повідомив про доставку закупівлі {one}", "доставка закупівлі"),
    "PurchaseOrderViewSet.deliveries_by_ref": ("повідомив про доставку закупівлі {one}", "доставка закупівлі"),
    "PurchaseOrderViewSet.cancel": ("скасував закупівлю {one}", "скасування закупівлі"),
    "ShopShippingView.get": ("прочитав доставку магазину", "читання доставки магазину"),
    "ShopAssistantView.get": ("прочитав налаштування помічника сайту", "налаштування помічника сайту"),
    "SiteMailView.get": ("прочитав налаштування пошти сайту", "налаштування пошти сайту"),
    "ShopSearchAssistView.get": ("запитав підказки пошуку на сайті", "підказки пошуку"),
    "StaffLoginView.login": ("перевірив вхід персоналу сайту", "вхід персоналу сайту"),
    "SiteEventView.event": ("записав подію журналу сайту", "журнал сайту"),
    "PairView.post": ("підключив сайт кодом", "підключення сайту кодом"),
    "MediaFileView.get": ("забрав файл фото / даташита", "файл фото / даташита"),
    "MediaStatusView.report": ("звітував, які фото й даташити має сайт", "звіт сайту про фото"),
    "PaymentsStatusView.get": ("прочитав способи оплати", "читання способів оплати"),
    "StripeCheckoutView.post": ("почав оплату Stripe", "оплата Stripe"),
    "StripeSyncView.post": ("перевірив оплату Stripe", "перевірка оплати Stripe"),
    "account:StatusView.get": ("перевірив кабінет покупців", "кабінет покупців"),
    "account:RegisterView.post": ("зареєстрував покупця", "реєстрація покупця"),
    "account:ResendView.post": ("надіслав лист підтвердження ще раз", "лист підтвердження"),
    "account:VerifyView.post": ("підтвердив email покупця", "підтвердження email покупця"),
    "account:LoginView.post": ("увійшов як покупець", "вхід покупця"),
    "account:LogoutView.post": ("закрив сесію покупця", "вихід покупця"),
    "account:ForgotView.post": ("попросив скидання пароля покупця", "скидання пароля покупця"),
    "account:ResetView.post": ("задав новий пароль покупця", "новий пароль покупця"),
    "account:MeView.get": ("прочитав профіль покупця", "профіль покупця"),
    "account:MeView.patch": ("змінив профіль покупця", "зміна профілю покупця"),
    "account:MeView.delete": ("видалив акаунт покупця", "видалення акаунта покупця"),
    "account:PasswordView.post": ("змінив пароль покупця", "зміна пароля покупця"),
    "account:OrdersView.get": ("прочитав замовлення покупця", "замовлення покупця"),
    "account:OrderDetailView.get": ("прочитав замовлення покупця {one}", "замовлення покупця"),
    "account:OrderPayView.post": ("почав оплату замовлення покупця", "оплата замовлення покупця"),
    "account:OrderDocumentView.get": ("завантажив документ замовлення", "документ замовлення"),
    "account:CartView.get": ("прочитав кошик покупця", "кошик покупця"),
    "account:CartView.put": ("зберіг кошик покупця", "збереження кошика"),
    "knowledge:ChangesView.get": ("прочитав стрічку змін бази знань", "стрічка змін бази знань"),
    "knowledge:DocumentView.get": ("прочитав документ бази знань", "документ бази знань"),
    "knowledge:AttachmentView.get": ("забрав вкладення документа бази знань", "вкладення документа"),
    "knowledge:SnapshotCreateView.post": ("почав повну вигрузку (знімок) бази знань", "знімок бази знань"),
    "knowledge:SnapshotPageView.get": ("прочитав сторінку знімка бази знань", "сторінка знімка"),
    "knowledge:StatusView.post": ("надіслав статуси індексації", "статуси індексації"),
    "knowledge:SummaryView.get": ("прочитав підсумок бази знань", "підсумок бази знань"),
}

CODE_TEXT = {
    "daily_items_limit": "денний ліміт різних записів",
    "insufficient_stock": "недостатньо товару на складі",
    "sku_not_found": "товар не знайдено",
    "invalid_lines": "помилки в рядках замовлення",
    "already_shipped": "замовлення вже відправлене",
    "no_shop": "для ключа не налаштовано магазин",
    "below_min_qty": "менше за мінімальну кількість",
    "shipping_not_available": "доставка в цю країну недоступна",
    "payment_method_not_allowed": "спосіб оплати вимкнено",
    "payment_processing": "оплата вже обробляється",
    "over_receipt": "отримано більше, ніж замовлено",
    "not_external": "закупівля створена в Minerva",
    "line_received": "позицію вже отримано",
    "shop_key_forbidden": "ключ магазину не має доступу",
    "forbidden_scope": "немає права",
    "rate_limited": "забагато запитів",
    "not_authenticated": "потрібен вхід покупця",
    "accounts_disabled": "кабінет покупців вимкнено",
}
STATUS_TEXT = {400: "помилка в даних", 401: "ключ не прийнято", 403: "немає права", 404: "не знайдено",
               405: "метод не підтримується", 409: "конфлікт", 429: "ліміт запитів"}
TX_UA = {"Incoming": "прихід", "Outgoing": "списання", "Adjustment": "коригування"}
SKIP_PARAMS = ("page", "page_size", "with_stock", "format")


def _who(row) -> str:
    if row.key_name:
        return row.key_name
    if row.key_hint:
        return f"Невідомий ключ {row.key_hint}"
    return "Запит без ключа"


def _ekey(row) -> str:
    name, _, action = (row.view or "").partition(".")
    return endpoint_key(name, action or None, row.path, row.method) if name else ""


PRODUCT_VIEWS = ("StockViewSet", "ProductViewSet", "ShopProductViewSet", "MovementViewSet")


def _legacy(objects_text: str, view: str) -> list[str]:
    """Рядки журналу до 30.10.2026: лише перші 10 номерів у тексті («AMP-100, CABLE-M … +159»)."""
    kind = "sku" if (view or "").split(".", 1)[0] in PRODUCT_VIEWS else "id"
    return [f"{kind}:{v.strip()}" for v in (objects_text or "").split(" … ")[0].split(",") if v.strip()]


def _labels(row) -> list[str]:
    """Записи, які ключ отримав (лише вдалі відповіді)."""
    if row.status >= 400:
        return []
    if row.object_ids:
        return list(row.object_ids)
    return _legacy(row.objects_text, row.view)


def records_word(n: int) -> str:
    """1 запис, 3 записи, 169 записів."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} запис"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} записи"
    return f"{n} записів"


def _one(row) -> str:
    labels = _labels(row)
    if labels and (row.items or len(labels)) == 1:
        return labels[0].split(":", 1)[1]
    if (row.result or {}).get("order_number"):
        return str(row.result["order_number"])
    tail = [p for p in row.path.split("/") if p]
    return tail[-1] if tail and tail[-1] not in ("cancel", "test", "deliveries", "shipments") else (
        tail[-2] if len(tail) >= 2 else "")


def _query_text(row) -> str:
    """«(сторінка 1, page_size 500) · фільтри: search=AMP»."""
    pairs = parse_qsl(row.query or "", keep_blank_values=True)
    params = dict(pairs)
    out = []
    if "page_size" in params or "page" in params:
        out.append(f"(сторінка {params.get('page') or 1}" + (f", page_size {params['page_size']}"
                                                               if params.get("page_size") else "") + ")")
    other = [f"{k}={v}" for k, v in pairs if k not in SKIP_PARAMS][:4]
    if other:
        out.append("· фільтри: " + ", ".join(other))
    return " ".join(out)


def _phrase(row, ekey: str) -> tuple[str, str]:
    done, noun = PHRASES.get(ekey) or (
        ("прочитав " if row.method in READ_METHODS else f"надіслав {row.method} ") + row.path,
        ("читання " if row.method in READ_METHODS else f"{row.method} ") + row.path)
    with_stock = dict(parse_qsl(row.query or "")).get("with_stock") in ("1", "true", "yes")
    if ekey in ("ProductViewSet.list", "ProductViewSet.retrieve") and with_stock:
        done, noun = done + " зі складом", noun + " зі складом"
    one = _one(row)
    return (done.replace("{one}", one).replace(" #", " #" if one else "").strip(),
            noun.replace("{one}", one).strip())


def _write_details(row, ekey: str) -> str:
    r = row.result or {}
    parts = []
    if ekey == "MovementViewSet.create" and r.get("tx_type"):
        parts.append(f"{TX_UA.get(r['tx_type'], r['tx_type'])} {r.get('qty', '')} × {r.get('sku', '')}"
                     + (f", доступно після: {r['available_after']}" if r.get("available_after") is not None else ""))
    if r.get("counts"):
        parts.append("; ".join(f"{c.get('sku')}: {c.get('previous')} → {c.get('counted')}" for c in r["counts"][:5])
                     + (f" … +{len(r['counts']) - 5}" if len(r["counts"]) > 5 else ""))
    if ekey == "SalesOrderViewSet.create" and r.get("status"):
        parts.append(f"статус {r['status']}")
    if ekey == "SalesOrderViewSet.cancel" and r.get("restocked_transactions") is not None:
        parts.append(f"рухів складу повернено: {r['restocked_transactions']}")
    if isinstance(r.get("fields"), list) and r["fields"] and ekey not in ("MovementViewSet.create",):
        parts.append("поля: " + ", ".join(r["fields"][:8]) + (" …" if len(r["fields"]) > 8 else ""))
    elif isinstance(r.get("fields"), int):
        parts.append(f"записів у запиті: {r['fields']}")
    if r.get("ignored_fields"):
        parts.append("відкинуто: " + ", ".join(r["ignored_fields"]))
    return " (" + "; ".join(parts) + ")" if parts else ""


def reason(row) -> str:
    """Чому відмова — людською мовою."""
    if row.code in CODE_TEXT:
        return CODE_TEXT[row.code] + (f" {row.scope}" if row.code == "forbidden_scope" and row.scope else "")
    if row.note:
        return row.note
    if row.status == 403 and row.scope:
        return f"немає права {row.scope}"
    if row.status >= 500:
        return "помилка сервера Minerva"
    return STATUS_TEXT.get(row.status, "") or (row.code or "")


def plain_line(row) -> str:
    """Рядок журналу одним реченням (див. опис модуля)."""
    ekey = _ekey(row)
    done, noun = _phrase(row, ekey)
    who = _who(row)
    if row.status >= 400:
        why = reason(row)
        return f"{who} отримав відмову {row.status} ({noun})" + (f": {why}" if why else "")
    text = f"{who} {done}"
    if ekey == "SalesOrderViewSet.create" and (row.result or {}).get("created") is False:
        text = f"{who} повторно надіслав замовлення {_one(row)} — воно вже було, нового не створено"
    if row.method in READ_METHODS:
        if row.items is not None and (row.items != 1 or ekey.endswith(".list")):
            text += f" — {records_word(row.items)}"
        q = _query_text(row)
        if q:
            text += " " + q
    else:
        text += _write_details(row, ekey)
    if row.note and row.status < 400:
        text += f" · {row.note}"
    return text


# ── 📊 Аналіз ────────────────────────────────────────────────────────────────

def period(params, today=None) -> tuple:
    """(від, до, днів) з ?days=1|7|30 або ?from=&to= (дати включно)."""
    today = today or timezone.localdate()
    try:
        start = datetime.strptime(params.get("from", ""), "%Y-%m-%d").date()
        end = datetime.strptime(params.get("to", ""), "%Y-%m-%d").date() if params.get("to") else today
        if start <= end:
            return start, end, (end - start).days + 1
    except ValueError:
        pass
    try:
        days = int(params.get("days") or 7)
    except ValueError:
        days = 7
    days = max(1, min(days, 366))
    return today - timedelta(days=days - 1), today, days


def filtered(params, today=None):
    """Журнал за фільтрами сторінки: key (pk / unknown), період, st (ok / err / 401 / 403 / 429 / 5xx), rw."""
    from .models import APIRequestLog
    start, end, _days = period(params, today)
    tz = timezone.get_current_timezone()
    qs = APIRequestLog.objects.filter(
        created_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz),
        created_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))
    key = params.get("key", "")
    if key == "unknown":
        qs = qs.filter(key__isnull=True)
    elif key.isdigit():
        qs = qs.filter(key_id=int(key))
    st = params.get("st", "")
    if st == "ok":
        qs = qs.filter(status__lt=400)
    elif st == "err":
        qs = qs.filter(status__gte=400)
    elif st == "5xx":
        qs = qs.filter(status__gte=500)
    elif st in ("401", "403", "429"):
        qs = qs.filter(status=int(st))
    rw = params.get("rw", "")
    if rw == "read":
        qs = qs.filter(method__in=READ_METHODS)
    elif rw == "write":
        qs = qs.exclude(method__in=READ_METHODS)
    return qs


ENTITY_OF_VIEW = {
    "SalesOrderViewSet": "order", "CustomerViewSet": "customer", "ShipmentViewSet": "shipment",
    "MovementViewSet": "movement", "WebhookViewSet": "webhook", "PurchaseOrderViewSet": "purchase",
    "LocationViewSet": "location",
}
ENTITY_TEXT = {"product": "товар", "order": "замовлення", "customer": "клієнт CRM", "shipment": "відправлення",
               "movement": "рух складу", "webhook": "вебхук", "purchase": "закупівля", "location": "локація",
               "other": "запис"}


def _entity(view: str, kind: str) -> str:
    if kind == "sku":
        return "product"
    if kind == "order_number":
        return "order"
    return ENTITY_OF_VIEW.get(view.split(".", 1)[0], "other")


def _links(records: list[dict]):
    """Посилання на товар / замовлення / клієнта в Minerva (одним запитом на вид)."""
    from django.urls import NoReverseMatch, reverse
    from inventory.models import Product
    skus = {r["value"] for r in records if r["entity"] == "product"}
    products = dict(Product.objects.filter(sku__in=skus).values_list("sku", "pk")) if skus else {}
    search = {"product": "admin:inventory_product_changelist", "order": "admin:sales_salesorder_changelist",
              "purchase": "admin:inventory_purchaseorder_changelist"}
    change = {"order": "admin:sales_salesorder_change", "customer": "admin:crm_customer_change",
              "shipment": "admin:shipping_shipment_change", "movement": "admin:inventory_inventorytransaction_change",
              "webhook": "admin:api_webhook_change"}
    for r in records:
        url = ""
        try:
            if r["entity"] == "product" and r["value"] in products:
                url = reverse("admin:inventory_product_change", args=[products[r["value"]]])
            elif r["kind"] == "id" and r["entity"] in change and r["value"].isdigit():
                url = reverse(change[r["entity"]], args=[int(r["value"])])
            elif r["entity"] in search:
                from urllib.parse import quote
                url = reverse(search[r["entity"]]) + "?q=" + quote(r["value"])
        except NoReverseMatch:
            url = ""
        r["url"] = url


def record_links(row) -> list[dict]:
    """Записи одного рядка журналу з посиланнями (картка запиту)."""
    records = []
    for item in _labels(row):
        kind, _, value = item.partition(":")
        entity = _entity(row.view or "", kind)
        records.append({"kind": kind, "value": value, "entity": entity})
    _links(records)
    return records


def records_seen(qs) -> dict:
    """Які записи ключ отримав: [{kind, value, entity, n (запитів), last}] за найновішими RECORDS_SCAN_MAX рядками."""
    counter, last, meta = Counter(), {}, {}
    rows = (qs.filter(status__lt=400, items__gt=0).order_by("-created_at")
            .values_list("object_ids", "objects_text", "view", "created_at")[:RECORDS_SCAN_MAX])
    scanned = 0
    for object_ids, objects_text, view, created in rows.iterator(chunk_size=500):   # до 2000 записів у рядку
        scanned += 1
        labels = object_ids or _legacy(objects_text, view)
        for item in set(labels):
            kind, _, value = item.partition(":")
            ident = (_entity(view or "", kind), value)
            counter[ident] += 1
            last.setdefault(ident, created)
            meta.setdefault(ident, kind)
    records = [{"entity": e, "value": v, "kind": meta[(e, v)], "n": n, "last": last[(e, v)],
                "entity_text": ENTITY_TEXT.get(e, "запис")} for (e, v), n in counter.most_common()]
    shown = records[:RECORDS_SHOW]
    _links(shown)
    by_entity = Counter(r["entity"] for r in records)
    return {"rows": shown, "total": len(records), "scanned": scanned, "limited": scanned >= RECORDS_SCAN_MAX,
            "by_entity": [(ENTITY_TEXT.get(e, e), n) for e, n in by_entity.most_common()]}


def analyse(qs, start, end) -> dict:
    """Усе для сторінки «📊 Аналіз журналу» (див. опис модуля)."""
    err = Q(status__gte=400)
    reads = Q(method__in=READ_METHODS)
    totals = qs.aggregate(
        n=Count("id"), reads=Count("id", filter=reads), writes=Count("id", filter=~reads), errors=Count("id", filter=err),
        denied=Count("id", filter=Q(status__in=(401, 403, 429))),
        sent=Sum("items", filter=reads & Q(status__lt=400)), avg_ms=Avg("duration_ms"),
        ips=Count("ip", distinct=True), keys=Count("key", distinct=True), first=Min("created_at"),
        last=Max("created_at"))
    by_day = {r["day"]: r for r in qs.annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
              .values("day").annotate(reads=Count("id", filter=reads & ~err), writes=Count("id", filter=~reads & ~err),
                                      errors=Count("id", filter=err), sent=Sum("items", filter=reads & ~err))
              .order_by("day")}
    days, day = [], start
    while day <= end:
        r = by_day.get(day) or {}
        days.append({"day": day.strftime("%d.%m"), "reads": r.get("reads", 0), "writes": r.get("writes", 0),
                     "errors": r.get("errors", 0), "sent": r.get("sent") or 0})
        day += timedelta(days=1)
    errors = list(qs.filter(err).values("status", "code").annotate(n=Count("id"), last=Max("created_at"),
                                                                    note=Max("note")).order_by("-n")[:20])
    for e in errors:
        e["text"] = CODE_TEXT.get(e["code"]) or e["note"] or STATUS_TEXT.get(e["status"], "")
    endpoints = list(qs.values("method", "view").annotate(
        n=Count("id"), errors=Count("id", filter=err), sent=Sum("items", filter=reads & ~err),
        avg_ms=Avg("duration_ms"), max_ms=Max("duration_ms"), path=Max("path")).order_by("-n")[:20])
    for e in endpoints:
        e["avg_ms"] = int(e["avg_ms"] or 0)
        name, _, action = (e["view"] or "").partition(".")
        noun = (PHRASES.get(endpoint_key(name, action or None, e["path"] or "", e["method"])) or ("", ""))[1]
        e["what"] = noun.replace(" #{one}", "").replace(" {one}", "")
    ips = list(qs.exclude(ip=None).values("ip").annotate(
        n=Count("id"), errors=Count("id", filter=err), keys=Count("key", distinct=True), first=Min("created_at"),
        last=Max("created_at")).order_by("-n")[:30])
    agents = list(qs.values("user_agent").annotate(n=Count("id"), last=Max("created_at")).order_by("-n")[:20])
    slowest = list(qs.select_related("key").order_by("-duration_ms")[:10])
    largest = list(qs.select_related("key").filter(items__isnull=False).order_by("-items")[:10])
    writes_qs = qs.exclude(method__in=READ_METHODS).select_related("key").order_by("-created_at")
    writes = list(writes_qs[:WRITES_SHOW])
    for row in slowest + largest + writes:
        row.plain = plain_line(row)
    totals["avg_ms"] = int(totals["avg_ms"] or 0)
    totals["sent"] = totals["sent"] or 0
    return {
        "totals": totals, "days": days, "errors": errors,
        "endpoints": endpoints, "ips": ips, "agents": agents, "slowest": slowest, "largest": largest,
        "writes": writes, "writes_total": writes_qs.count(), "records": records_seen(qs),
    }


CSV_HEADER = ["Коли", "Ключ", "IP", "Метод", "Адреса", "Параметри", "Дія", "Право", "Відповідь", "Код помилки",
              "Записів", "Усі записи відповіді", "Що змінилось", "Примітка", "мс", "Програма", "Що сталося"]


def _cell(value):
    """Комірка CSV: текст, що починається з = + - @, Excel не прийме за формулу (User-Agent, адреса — від програми)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def csv_response(qs, filename: str):
    import json
    from django.http import HttpResponse
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.write(chr(0xFEFF))                   # BOM — Excel відкриває UTF-8 без «кракозябр»
    writer = csv.writer(resp, delimiter=";")
    writer.writerow(CSV_HEADER)
    for row in qs.select_related("key").order_by("created_at").iterator(chunk_size=2000):
        writer.writerow([_cell(v) for v in (
            timezone.localtime(row.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            row.key_name or (f"невідомий {row.key_hint}" if row.key_hint else ""), row.ip or "", row.method, row.path,
            row.query, row.view, row.scope, row.status, row.code, "" if row.items is None else row.items,
            " ".join(_labels(row)), json.dumps(row.result, ensure_ascii=False) if row.result else "", row.note,
            row.duration_ms, row.user_agent, plain_line(row))])
    return resp
