"""
Вихідні вебхуки Minerva → зовнішні системи (магазин).

Потік:  сигнал моделі → (після commit) emit() створює WebhookDelivery для кожного
підписаного вебхука → миттєва спроба у фоновому потоці → невдалі повторює
`manage.py send_webhooks` (cron) з наростаючою паузою.

Запит до отримувача:
    POST <url>
    Content-Type: application/json
    X-Minerva-Event: stock.changed
    X-Minerva-Delivery: <uuid>            ← однаковий при повторах (дедуплікація)
    X-Minerva-Signature: t=<unix>,v1=<hex>  hex = HMAC_SHA256(secret, f"{t}.{body}")
    body: {"id": <uuid>, "event": ..., "created_at": ..., "data": {...}}
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from rest_framework.utils.encoders import JSONEncoder
from django.db import connection, transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.utils import timezone

logger = logging.getLogger(__name__)

# Паузи між повторами: 1 хв, 5 хв, 30 хв, 2 год, 6 год, 12 год, 24 год → далі failed
RETRY_DELAYS = [60, 300, 1800, 7200, 21600, 43200, 86400]
TIMEOUT = 10


# ── Підпис ────────────────────────────────────────────────────────────────────

def sign(secret: str, body: str, ts: int | None = None) -> str:
    ts = int(ts or time.time())
    mac = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: str, header: str, tolerance: int = 300) -> bool:
    """Перевірка підпису на стороні отримувача (той самий алгоритм, що в документації)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, ts), header)


# ── Хто що отримує ────────────────────────────────────────────────────────────

def event_scope(event: str, shop_key: bool = False) -> str | None:
    """Право ключа, без якого вебхук цього ключа подію не отримує. Сайт магазину (ключ прив'язаний до магазину)
    отримує stock.changed лише про товари свого магазину — без stock:read."""
    if event.startswith("order."):
        return "orders:read"
    if event.startswith("shipment."):
        return "shipments:read"
    if event == "stock.changed":
        return None if shop_key else "stock:read"
    if event.startswith("purchase_order."):
        return "purchases:read"
    return None


def hook_shop(hook):
    """Магазин вебхука: ключа, що його створив (прив'язаний до магазину), або сайту, підключеного кодом
    («🔗 Підключити сайт» — адреса …/api/shop/webhook.php); інакше None (вебхук адміністратора)."""
    from shop.models import Shop
    key = hook.api_key if hook.api_key_id else None
    if key is not None:
        if key.shop_id:
            return Shop.objects.filter(pk=key.shop_id).first()
        if key.default_source:
            return Shop.objects.filter(slug=key.default_source).first()
        return None
    if hook.url.rstrip("/").endswith("/api/shop/webhook.php"):
        from shop.models import ShopPairing
        origin = hook.url.rstrip("/")[:-len("/api/shop/webhook.php")]
        p = (ShopPairing.objects.filter(site_url=origin, used_at__isnull=False).select_related("shop")
             .order_by("-used_at").first())
        return p.shop if p else None
    return None


def _payload_for(hook, event, data, source):
    """Дані події для цього вебхука або None — не надсилати: права ключа й межі магазину."""
    if event == "ping":
        return data
    shop = hook_shop(hook)
    key = hook.api_key if hook.api_key_id else None
    if key is not None:
        scope = event_scope(event, shop is not None)
        if scope and not key.has_scope(scope):
            return None
    if shop is None:
        return data
    if event.startswith(("order.", "shipment.")):
        return data if source == shop.slug else None
    if event == "stock.changed":
        from shop.models import ShopListing
        skus = set(ShopListing.objects.filter(shop=shop, is_visible=True).values_list("product__sku", flat=True))
        items = [i for i in data.get("items", []) if i.get("sku") in skus]
        return {**data, "items": items} if items else None
    if event.startswith("purchase_order."):
        return None                      # закупівлі — не для сайту магазину
    return data


def unsafe_target(url: str, allow_private: bool = False) -> str:
    """Причина, чому на цю адресу не можна слати вебхук, або "". Завжди заборонено: localhost, link-local
    (169.254… — метадані хмар), multicast, зарезервовані; приватні мережі (NAS, RAG-ПК, контейнери Docker) — лише
    вебхукам, які створив адміністратор у Minerva (allow_private), не ключам API."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return "Некоректна адреса."
    if not host:
        return "Немає адреси сервера."
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            return "Адреса localhost заборонена для вебхуків."
        if host.endswith((".local", ".internal", ".lan")) or "." not in host:
            return "" if allow_private else                 "Адреси внутрішньої мережі (імена контейнерів, .local) — лише для вебхуків адміністратора."
        if not getattr(settings, "WEBHOOK_RESOLVE_DNS", True):
            return ""
        try:
            addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(host, None)}
        except (socket.gaierror, UnicodeError, ValueError):
            return ""                    # не резолвиться — запит просто не дійде
    for a in addrs:
        if a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified or a.is_reserved:
            return f"Адреса {a} заборонена для вебхуків."
        if a.is_private and not allow_private:
            return f"Адреса {a} — внутрішня мережа: лише для вебхуків адміністратора."
    return ""


# ── Створення подій ───────────────────────────────────────────────────────────

def emit(event: str, data: dict, source: str | None = None, only=None, sync=False):
    """Ставить подію в чергу для всіх підписаних вебхуків. Повертає список доставок."""
    from .models import Webhook, WebhookDelivery

    hooks = [only] if only else [h for h in Webhook.objects.filter(is_active=True).select_related("api_key")
                                 if h.wants(event, source)]
    if not hooks:
        return []
    data = json.loads(json.dumps(data, cls=JSONEncoder))
    created = timezone.now()
    deliveries = []
    for h in hooks:
        own = data if only else _payload_for(h, event, data, source)
        if own is None:
            continue
        d = WebhookDelivery(webhook=h, event=event, payload={})
        d.payload = {"id": str(d.id), "event": event, "created_at": created.isoformat(), "data": own}
        d.save()
        deliveries.append(d)
    if not deliveries:
        return []
    if sync:
        _send_many([d.pk for d in deliveries])
    else:
        _dispatch([d.pk for d in deliveries])
    return deliveries


def _dispatch(ids):
    if getattr(settings, "WEBHOOKS_SYNC", False):
        _send_many(ids)
        return
    threading.Thread(target=_send_many, args=(ids, True), daemon=True,
                     name="MinervaWebhookSender").start()


def _send_many(ids, close_connection=False):
    from .models import WebhookDelivery
    try:
        for d in WebhookDelivery.objects.filter(pk__in=ids, status=WebhookDelivery.PENDING) \
                                        .select_related("webhook"):
            deliver(d)
    except Exception:
        logger.exception("webhooks: send failed")
    finally:
        if close_connection:
            connection.close()


def deliver(d):
    """Одна спроба доставки. Повертає True при 2xx."""
    import requests
    from .models import WebhookDelivery

    hook = d.webhook
    body = json.dumps(d.payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Minerva-Webhooks/1.0",
        "X-Minerva-Event": d.event,
        "X-Minerva-Delivery": str(d.id),
        "X-Minerva-Signature": sign(hook.secret, body),
    }
    d.attempts += 1
    now = timezone.now()
    ok = False
    problem = unsafe_target(hook.url, allow_private=not hook.api_key_id)   # адреса могла змінитись у DNS
    if problem:
        d.response_code = None
        d.response_body = problem[:1000]
        d.attempts = max(d.attempts, len(RETRY_DELAYS) + 1)                # без повторів
    else:
        try:
            # без переходів за переадресацією: інакше отримувачем могла б стати адреса внутрішньої мережі
            r = requests.post(hook.url, data=body.encode("utf-8"), headers=headers, timeout=TIMEOUT,
                              allow_redirects=False)
            d.response_code = r.status_code
            d.response_body = (r.text or "")[:1000]
            ok = 200 <= r.status_code < 300
        except requests.RequestException as e:
            d.response_code = None
            d.response_body = f"{type(e).__name__}: {e}"[:1000]

    if ok:
        d.status, d.delivered_at = WebhookDelivery.SUCCESS, now
        hook.consecutive_failures = 0
    else:
        hook.consecutive_failures += 1
        if d.attempts > len(RETRY_DELAYS):
            d.status = WebhookDelivery.FAILED
        else:
            d.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[d.attempts - 1])
    d.save(update_fields=["status", "attempts", "response_code", "response_body",
                          "next_attempt_at", "delivered_at"])
    hook.last_delivery_at, hook.last_status_code = now, d.response_code
    hook.save(update_fields=["last_delivery_at", "last_status_code", "consecutive_failures"])
    return ok


def send_due(limit=200):
    """Повтори для cron: надсилає всі доставки, час яких настав."""
    from .models import WebhookDelivery
    due = (WebhookDelivery.objects
           .filter(status=WebhookDelivery.PENDING, next_attempt_at__lte=timezone.now(),
                   webhook__is_active=True)
           .select_related("webhook").order_by("next_attempt_at")[:limit])
    sent = failed = 0
    for d in due:
        if deliver(d):
            sent += 1
        else:
            failed += 1
    return sent, failed


# ── Дані подій ────────────────────────────────────────────────────────────────

def stock_payload(product_ids):
    from inventory.models import Product
    from inventory.services.stock import annotate_stock
    from .serializers import StockSerializer
    qs = annotate_stock(Product.objects.filter(pk__in=product_ids)).order_by("sku")
    return {"items": StockSerializer(qs, many=True).data}


def order_payload(order, previous_status=None):
    data = {
        "id": order.pk, "source": order.source, "order_number": order.order_number,
        "status": order.status, "order_date": order.order_date,
        "total_price": order.total_price, "currency": order.currency,
        "tracking_number": order.tracking_number, "shipped_at": order.shipped_at,
        "delivered_at": order.delivered_at,
    }
    if previous_status is not None:
        data["previous_status"] = previous_status
    return data


def purchase_payload(po, changed_by="minerva"):
    from .purchases import purchase_data
    return {"purchase_order": purchase_data(po), "changed_by": changed_by}


def shipment_payload(shipment, previous_status=None):
    from .serializers import ShipmentSerializer
    data = dict(ShipmentSerializer(shipment).data)
    if previous_status is not None:
        data["previous_status"] = previous_status
    return data


# ── Сигнали ───────────────────────────────────────────────────────────────────
# stock.changed групується: усі рухи однієї транзакції БД → одна подія після commit.

_local = threading.local()


def _flush_stock():
    ids = getattr(_local, "stock_ids", None) or set()
    _local.stock_ids = None
    if ids:
        try:
            emit("stock.changed", stock_payload(ids))
        except Exception:
            logger.exception("webhooks: stock.changed failed")


def _queue_stock(product_id):
    ids = getattr(_local, "stock_ids", None)
    registered = any(item[1] is _flush_stock for item in connection.run_on_commit)
    if ids is None or not registered:
        # Нова транзакція / autocommit / попередня відкотилась і flush не відбувся
        _local.stock_ids = {product_id}
        transaction.on_commit(_flush_stock)  # в autocommit виконується одразу
    else:
        ids.add(product_id)


def _on_tx_change(sender, instance, **kwargs):
    if instance.product_id:
        _queue_stock(instance.product_id)


# Поля товару, зміна яких має оновити каталог інтернет-магазину
_SHOP_FIELDS = ("sale_price", "is_active", "name", "name_export",
                "category", "unit_type", "lead_time_days", "image_url", "image", "datasheet_url",
                "lifecycle_status", "successor_id")


def _product_pre_save(sender, instance, **kwargs):
    instance._wh_shop_old = (sender.objects.filter(pk=instance.pk).values(*_SHOP_FIELDS).first()
                             if instance.pk else None)


def _product_post_save(sender, instance, created, **kwargs):
    old = getattr(instance, "_wh_shop_old", None)
    if created or old is None:
        return  # новий товар з'являється в магазині лише через позицію (ShopListing → свій сигнал)
    new = {f: getattr(instance, f) for f in _SHOP_FIELDS}
    new["image"] = instance.image.name if instance.image else ""
    old["image"] = old.get("image") or ""
    if any(old[f] != new[f] for f in _SHOP_FIELDS) and instance.shop_listings.filter(is_visible=True).exists():
        _queue_stock(instance.pk)


def _order_pre_save(sender, instance, **kwargs):
    if not hasattr(instance, "_wh_old_status"):
        instance._wh_old_status = (sender.objects.filter(pk=instance.pk)
                                   .values_list("status", flat=True).first()) if instance.pk else None


def _order_post_save(sender, instance, created, **kwargs):
    old = getattr(instance, "_wh_old_status", None)
    if hasattr(instance, "_wh_old_status"):
        del instance._wh_old_status
    pk = instance.pk

    def _send(event, prev=None):
        def run():
            from sales.models import SalesOrder
            try:
                order = SalesOrder.objects.get(pk=pk)
                emit(event, order_payload(order, prev), source=order.source)
            except Exception:
                logger.exception("webhooks: %s failed", event)
        transaction.on_commit(run)

    if created:
        _send("order.created")
    elif old and old != instance.status:
        _send("order.status_changed", old)


def _shipment_pre_save(sender, instance, **kwargs):
    instance._wh_old = (sender.objects.filter(pk=instance.pk)
                        .values_list("status", "tracking_number").first()) if instance.pk else None


def _shipment_post_save(sender, instance, created, **kwargs):
    old = getattr(instance, "_wh_old", None)
    old_status, old_tn = old or (None, "")
    if not created and old_status == instance.status and old_tn == instance.tracking_number:
        return
    if created and not instance.tracking_number:
        return  # чернетка без трекінгу — шлемо, коли з'явиться номер або зміниться статус
    pk = instance.pk

    def run():
        from shipping.models import Shipment
        try:
            s = Shipment.objects.select_related("order", "carrier").get(pk=pk)
            emit("shipment.updated", shipment_payload(s, old_status),
                 source=s.order.source if s.order_id else None)
        except Exception:
            logger.exception("webhooks: shipment.updated failed")
    transaction.on_commit(run)


# purchase_order.updated: усі зміни закупівлі за транзакцію (замовлення, позиції, відправлення, доставки) —
# одна подія після commit. changed_by — хто змінив: джерело ключа API (purchases_changed_by) або "minerva",
# щоб зовнішня система могла не реагувати на власні зміни.

@contextlib.contextmanager
def purchases_changed_by(label):
    previous = getattr(_local, "po_changed_by", None)
    _local.po_changed_by = label
    try:
        yield
    finally:
        _local.po_changed_by = previous


def _flush_purchases():
    from inventory.models import PurchaseOrder
    queued = getattr(_local, "po_ids", None) or {}
    _local.po_ids = None
    for pk, changed_by in sorted(queued.items()):
        try:
            po = PurchaseOrder.objects.select_related("supplier").filter(pk=pk).first()
            if po:
                emit("purchase_order.updated", purchase_payload(po, changed_by),
                     source=po.external_source or None)
        except Exception:
            logger.exception("webhooks: purchase_order.updated failed")


def _queue_purchase(po_id):
    if not po_id:
        return
    changed_by = getattr(_local, "po_changed_by", None) or "minerva"
    ids = getattr(_local, "po_ids", None)
    registered = any(item[1] is _flush_purchases for item in connection.run_on_commit)
    if ids is None or not registered:
        _local.po_ids = {po_id: changed_by}
        transaction.on_commit(_flush_purchases)
    else:
        ids[po_id] = changed_by


_PO_FIELDS = ("status", "expected_date", "received_date", "tracking_number", "shipping_service",
              "supplier_order_no", "supplier_id", "delivered_reported_at")
_PO_LINE_FIELDS = ("qty_ordered", "qty_received", "product_id")
_INCOMING_FIELDS = ("status", "tracking_number", "expected_date", "received_date", "carrier_status_label")


def _capture(fields):
    def pre_save_handler(sender, instance, **kwargs):
        instance._wh_po_old = (sender.objects.filter(pk=instance.pk).values_list(*fields).first()
                               if instance.pk else None)
    return pre_save_handler


def _changed(instance, fields, created):
    old = getattr(instance, "_wh_po_old", None)
    return created or old is None or old != tuple(getattr(instance, f) for f in fields)


def _po_post_save(sender, instance, created, **kwargs):
    if _changed(instance, _PO_FIELDS, created):
        _queue_purchase(instance.pk)


def _po_line_post_save(sender, instance, created, **kwargs):
    if _changed(instance, _PO_LINE_FIELDS, created):
        _queue_purchase(instance.purchase_order_id)


def _incoming_post_save(sender, instance, created, **kwargs):
    if _changed(instance, _INCOMING_FIELDS, created):
        _queue_purchase(instance.purchase_order_id)


def _receipt_post_save(sender, instance, created, **kwargs):
    if created:
        _queue_purchase(instance.purchase_order_id)


_po_pre_save = _capture(_PO_FIELDS)
_po_line_pre_save = _capture(_PO_LINE_FIELDS)
_incoming_pre_save = _capture(_INCOMING_FIELDS)


def connect_signals():
    from inventory.models import (IncomingShipment, InventoryTransaction, Product, PurchaseOrder,
                                  PurchaseOrderLine, PurchaseReceipt)
    from sales.models import SalesOrder
    from shipping.models import Shipment

    post_save.connect(_on_tx_change, sender=InventoryTransaction, dispatch_uid="wh_tx_save")
    post_delete.connect(_on_tx_change, sender=InventoryTransaction, dispatch_uid="wh_tx_delete")
    pre_save.connect(_product_pre_save, sender=Product, dispatch_uid="wh_product_pre")
    post_save.connect(_product_post_save, sender=Product, dispatch_uid="wh_product_post")
    pre_save.connect(_order_pre_save, sender=SalesOrder, dispatch_uid="wh_order_pre")
    post_save.connect(_order_post_save, sender=SalesOrder, dispatch_uid="wh_order_post")
    pre_save.connect(_shipment_pre_save, sender=Shipment, dispatch_uid="wh_ship_pre")
    post_save.connect(_shipment_post_save, sender=Shipment, dispatch_uid="wh_ship_post")
    pre_save.connect(_po_pre_save, sender=PurchaseOrder, dispatch_uid="wh_po_pre")
    post_save.connect(_po_post_save, sender=PurchaseOrder, dispatch_uid="wh_po_post")
    pre_save.connect(_po_line_pre_save, sender=PurchaseOrderLine, dispatch_uid="wh_po_line_pre")
    post_save.connect(_po_line_post_save, sender=PurchaseOrderLine, dispatch_uid="wh_po_line_post")
    pre_save.connect(_incoming_pre_save, sender=IncomingShipment, dispatch_uid="wh_incoming_pre")
    post_save.connect(_incoming_post_save, sender=IncomingShipment, dispatch_uid="wh_incoming_post")
    post_save.connect(_receipt_post_save, sender=PurchaseReceipt, dispatch_uid="wh_po_receipt_post")
