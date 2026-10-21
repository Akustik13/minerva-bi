"""Онлайн-оплата замовлень магазину через Stripe Checkout.

Хід оплати:
  1. Сайт створює замовлення (payment_method = stripe, payment_status = pending) і просить Minerva
     сторінку оплати: POST /api/v1/shop/payments/stripe/checkout/ → start_checkout().
  2. Minerva рахує суму сама (нетто + доставка + ПДВ за ставкою, яку записав сайт у ShopOrder),
     створює Stripe Checkout Session і повертає її адресу; покупець платить на сторінці Stripe.
  3. Stripe надсилає вебхук (POST /api/v1/payments/stripe/webhook/<код магазину>/, підпис HMAC) →
     handle_webhook() → замовлення «Оплачено» (payment_reference = PaymentIntent).
     Запасний шлях без вебхука — sync(): Minerva сама питає Stripe про стан сесії.

Ключі Stripe — лише в Minerva (Shop.stripe_secret_key / stripe_webhook_secret); сайт їх не знає.
HTTP — через requests (без бібліотеки stripe): form-encoded REST API, підпис вебхуків — HMAC-SHA256.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Shop, ShopPayment

logger = logging.getLogger("shop")

WEBHOOK_TOLERANCE = 300          # с — допустима різниця часу підпису вебхука
MIN_AMOUNT = Decimal("0.50")     # мінімум Stripe для EUR
REUSE_MARGIN = timedelta(minutes=10)
UNPAID = ("", "unpaid", "pending", "failed")


class PaymentError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def _api_base() -> str:
    # у тестах / локально — імітація Stripe
    return getattr(settings, "STRIPE_API_BASE", "") or "https://api.stripe.com"


def key_mode(key: str) -> str:
    """test / live за префіксом ключа (sk_test_, rk_live_ …)."""
    key = key or ""
    if "_test_" in key:
        return "test"
    if "_live_" in key:
        return "live"
    return ""


def enabled(shop: Shop) -> bool:
    return bool(shop and shop.stripe_enabled and shop.stripe_secret_key)


def public_status(shop: Shop | None) -> dict:
    """Що бачить сайт: чи доступна оплата Stripe і в якому режимі."""
    return {"stripe": {"enabled": enabled(shop), "mode": key_mode(shop.stripe_secret_key) if shop else ""}}


# ── Stripe API ───────────────────────────────────────────────────────────────

def _flatten(data, prefix="") -> list[tuple[str, str]]:
    """{"a": {"b": 1}, "l": [{"x": 2}]} → [("a[b]", "1"), ("l[0][x]", "2")] (формат запитів Stripe)."""
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            out += _flatten(v, f"{prefix}[{k}]" if prefix else str(k))
    elif isinstance(data, (list, tuple)):
        for i, v in enumerate(data):
            out += _flatten(v, f"{prefix}[{i}]")
    elif data is not None:
        out.append((prefix, "true" if data is True else "false" if data is False else str(data)))
    return out


def _request(shop: Shop, method: str, path: str, data: dict | None = None, idempotency_key: str = "") -> dict:
    if not shop.stripe_secret_key:
        raise PaymentError(503, "stripe_not_configured", "У магазині не задано секретний ключ Stripe.")
    headers = {"Authorization": f"Bearer {shop.stripe_secret_key.strip()}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        resp = requests.request(method, _api_base() + path, data=_flatten(data or {}), headers=headers, timeout=20)
    except requests.RequestException as e:
        raise PaymentError(502, "stripe_unreachable", f"Stripe недоступний: {e}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        err = body.get("error") or {}
        message = err.get("message") or f"HTTP {resp.status_code}"
        logger.warning("Stripe %s %s → %s %s", method, path, resp.status_code, message)
        raise PaymentError(502 if resp.status_code >= 500 else 400, "stripe_error", f"Stripe: {message}")
    return body


def check_key(shop: Shop) -> str:
    """Для адмінки: перевірити ключ (назва акаунта Stripe і режим)."""
    try:
        acc = _request(shop, "GET", "/v1/account")
        name = ((acc.get("settings") or {}).get("dashboard") or {}).get("display_name") \
            or (acc.get("business_profile") or {}).get("name") or acc.get("id", "")
    except PaymentError as e:
        if "permission" not in e.message.lower():  # обмежений ключ без права читати акаунт — перевіримо Checkout
            raise
        _request(shop, "GET", "/v1/checkout/sessions", {"limit": 1})
        name = "обмежений ключ"
    mode = {"test": "тестовий режим", "live": "бойовий режим"}.get(key_mode(shop.stripe_secret_key), "?")
    return f"✅ Stripe: {name} · {mode}"


# ── Сума замовлення ──────────────────────────────────────────────────────────

def _money(v) -> Decimal:
    return Decimal(v or 0).quantize(Decimal("0.01"), ROUND_HALF_UP)


def order_amount(order) -> tuple[Decimal, str, str]:
    """(сума до оплати брутто, валюта, опис позицій). Так само, як сайт рахує суми (net + доставка + ПДВ)."""
    info = getattr(order, "shop_info", None)
    if info is None or info.vat_rate is None:
        raise PaymentError(409, "vat_unknown", "Для замовлення невідома ставка ПДВ — онлайн-оплату не створено.")
    lines = list(order.lines.select_related("product"))
    net = _money(order.total_price if order.total_price is not None else sum((l.total_price or 0) for l in lines))
    shipping = _money(order.shipping_cost)
    vat = _money((net + shipping) * Decimal(info.vat_rate) / 100)
    gross = net + shipping + vat
    items = ", ".join(f"{format(Decimal(l.qty).normalize(), 'f')} × {l.product.sku}" for l in lines[:20])
    return gross, (order.currency or "EUR").upper(), items[:450]


# ── Сторінка оплати ──────────────────────────────────────────────────────────

def check_return_url(shop: Shop, url) -> str:
    """Адреси повернення з Stripe — лише https на домен сайту магазину (http — тільки localhost)."""
    url = str(url or "").strip()
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if not parts.hostname or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local) \
            or len(url) > 1000:
        raise PaymentError(400, "invalid_return_url", "Некоректна адреса повернення.")
    allowed = urlsplit(shop.url).hostname if shop.url else None
    if allowed and parts.hostname != allowed and not local:
        raise PaymentError(400, "return_url_not_allowed",
                           f"Адреса повернення ({parts.hostname}) не збігається з «Адреса сайту» магазину ({allowed}).")
    return url


def find_order(shop: Shop, number):
    from sales.models import SalesOrder
    order = (SalesOrder.objects.filter(source=shop.slug, order_number=str(number or "")[:64])
             .select_related("shop_info").first())
    if order is None:
        raise PaymentError(404, "not_found", "Замовлення не знайдено.")
    return order


def start_checkout(shop: Shop, order, success_url, cancel_url, lang: str = "de") -> ShopPayment:
    """Сторінка оплати Stripe для замовлення: відкрита сесія використовується повторно (без подвійної оплати)."""
    from sales.models import SalesOrder
    if not enabled(shop):
        raise PaymentError(403, "stripe_disabled", "Оплату Stripe у магазині вимкнено.")
    success_url, cancel_url = check_return_url(shop, success_url), check_return_url(shop, cancel_url)
    if order.document_type == "QUOTE" or order.status == "cancelled":
        raise PaymentError(409, "not_payable", "Це замовлення не оплачується онлайн.")
    if order.payment_status == "paid":
        raise PaymentError(409, "already_paid", "Замовлення вже оплачено.")
    gross, currency, items = order_amount(order)
    if gross < MIN_AMOUNT:
        raise PaymentError(409, "amount_too_small", "Сума замовлення замала для онлайн-оплати.")
    lang = lang if lang in ("de", "en") else "de"
    with transaction.atomic():
        SalesOrder.objects.select_for_update().filter(pk=order.pk).first()  # одночасні кліки — по черзі
        now = timezone.now()
        reuse = (order.shop_payments.filter(status=ShopPayment.OPEN, amount=gross, currency=currency,
                                            expires_at__gt=now + REUSE_MARGIN).order_by("-created_at").first())
        if reuse:
            return reuse
        for old in order.shop_payments.filter(status=ShopPayment.OPEN):  # інша сума / майже прострочена
            try:
                _request(shop, "POST", f"/v1/checkout/sessions/{old.session_id}/expire")
            except PaymentError as e:
                logger.info("Stripe expire %s: %s", old.session_id, e.message)
            old.status = ShopPayment.EXPIRED
            old.save(update_fields=["status", "updated_at"])
        meta = {"order_id": order.pk, "order_number": order.order_number, "shop": shop.slug}
        title = f"{'Bestellung' if lang == 'de' else 'Order'} {order.order_number} – {shop.name}"
        data = {
            "mode": "payment",
            "line_items": [{"quantity": 1, "price_data": {
                "currency": currency.lower(), "unit_amount": int(gross * 100),
                "product_data": {"name": title[:250], "description": items or None}}}],
            "success_url": success_url, "cancel_url": cancel_url,
            "client_reference_id": order.order_number, "locale": lang,
            "customer_email": order.email or None,
            "metadata": meta,
            "payment_intent_data": {"metadata": meta, "description": title[:250]},
        }
        attempt = order.shop_payments.count() + 1
        session = _request(shop, "POST", "/v1/checkout/sessions", data,
                           idempotency_key=f"minerva-{shop.slug}-{order.pk}-{attempt}")
        payment = ShopPayment.objects.create(
            order=order, shop=shop, session_id=session["id"], url=session.get("url") or "",
            amount=gross, currency=currency, livemode=bool(session.get("livemode")),
            expires_at=_ts(session.get("expires_at")) or now + timedelta(hours=24))
        if order.payment_method != "stripe" or order.payment_status not in ("pending",):
            order.payment_method, order.payment_status = "stripe", "pending"
            order.status_source = "Stripe"
            order.save(update_fields=["payment_method", "payment_status", "status_source"])
    return payment


def _ts(value):
    return datetime.fromtimestamp(int(value), tz=dt_timezone.utc) if value else None


# ── Стан оплати ──────────────────────────────────────────────────────────────

def _set_order_payment(order, status: str, reference: str = ""):
    fields = {"payment_status": status, "status_source": "Stripe"}
    if reference:
        fields["payment_reference"] = reference[:128]
    changed = [k for k, v in fields.items() if getattr(order, k) != v]
    if changed:
        for k, v in fields.items():
            setattr(order, k, v)
        order.save(update_fields=list(fields))


def apply_session(payment: ShopPayment, session: dict, event: str = "") -> ShopPayment:
    """Стан Checkout Session зі Stripe → оплата і замовлення."""
    if payment.status in (ShopPayment.PAID, ShopPayment.REFUNDED) and session.get("payment_status") == "paid":
        return payment  # уже враховано (вебхук може прийти двічі)
    intent = session.get("payment_intent") or ""
    if isinstance(intent, dict):
        intent = intent.get("id", "")
    payment.payment_intent = intent or payment.payment_intent
    payment.last_event = event or payment.last_event
    order = payment.order
    if session.get("payment_status") == "paid":
        payment.status, payment.paid_at = ShopPayment.PAID, payment.paid_at or timezone.now()
        _set_order_payment(order, "paid", payment.payment_intent or payment.session_id)
    elif session.get("status") == "complete":           # SEPA тощо: гроші ще йдуть
        payment.status = ShopPayment.PROCESSING
        _set_order_payment(order, "pending")
    elif session.get("status") == "expired" and payment.status == ShopPayment.OPEN:
        payment.status = ShopPayment.EXPIRED
    payment.save()
    return payment


def sync(payment: ShopPayment) -> ShopPayment:
    """Запасний шлях без вебхука: запитати Stripe про сесію."""
    session = _request(payment.shop, "GET", f"/v1/checkout/sessions/{payment.session_id}")
    return apply_session(payment, session, "sync")


def sync_order(shop: Shop, order) -> str:
    """Оновити оплату замовлення зі Stripe (остання сесія) і повернути payment_status замовлення."""
    payment = order.shop_payments.filter(shop=shop).order_by("-created_at").first()
    if payment is not None and payment.status in (ShopPayment.OPEN, ShopPayment.PROCESSING):
        try:
            sync(payment)
        except PaymentError as e:
            logger.warning("Stripe sync %s: %s", payment.session_id, e.message)
        order.refresh_from_db(fields=["payment_status", "payment_reference"])
    return order.payment_status


# ── Вебхук Stripe ────────────────────────────────────────────────────────────

def verify_signature(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """Заголовок Stripe-Signature: t=<час>,v1=<HMAC-SHA256(secret, "t.payload")>[,v1=…]."""
    if not secret or not header:
        return False
    items = [p.split("=", 1) for p in header.split(",") if "=" in p]
    stamp = next((v for k, v in items if k.strip() == "t"), "")
    signatures = [v for k, v in items if k.strip() == "v1"]
    if not stamp.isdigit() or not signatures:
        return False
    if abs((now or time.time()) - int(stamp)) > WEBHOOK_TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s.strip()) for s in signatures)


def handle_webhook(shop: Shop, payload: bytes, signature: str) -> str:
    if not verify_signature(payload, signature, (shop.stripe_webhook_secret or "").strip()):
        raise PaymentError(400, "bad_signature", "Невірний підпис вебхука Stripe.")
    try:
        event = json.loads(payload.decode("utf-8"))
        kind, obj = event["type"], event["data"]["object"]
    except (ValueError, KeyError, TypeError):
        raise PaymentError(400, "bad_payload", "Некоректна подія Stripe.")

    if kind.startswith("checkout.session."):
        payment = ShopPayment.objects.filter(shop=shop, session_id=obj.get("id", "")).select_related("order").first()
        if payment is None:
            return "ignored"  # сесія не з Minerva (інший сайт на тому ж акаунті Stripe)
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.expired"):
            apply_session(payment, obj, kind)
        elif kind == "checkout.session.async_payment_failed":
            payment.status, payment.last_event = ShopPayment.FAILED, kind
            payment.save(update_fields=["status", "last_event", "updated_at"])
            _set_order_payment(payment.order, "failed")
        return kind

    if kind == "charge.refunded":
        intent = obj.get("payment_intent") or ""
        payment = ShopPayment.objects.filter(shop=shop, payment_intent=intent).select_related("order").first() \
            if intent else None
        if payment is None:
            return "ignored"
        payment.last_event = kind
        if obj.get("refunded"):  # повне повернення; часткове — лише позначка події
            payment.status = ShopPayment.REFUNDED
            _set_order_payment(payment.order, "refunded")
        payment.save(update_fields=["status", "last_event", "updated_at"])
        return kind
    return "ignored"


def dashboard_url(payment: ShopPayment) -> str:
    base = "https://dashboard.stripe.com/" + ("" if payment.livemode else "test/")
    if payment.payment_intent:
        return f"{base}payments/{payment.payment_intent}"
    return f"{base}checkout/sessions/{payment.session_id}"


def webhook_path(shop: Shop) -> str:
    return f"/api/v1/payments/stripe/webhook/{shop.slug}/"
