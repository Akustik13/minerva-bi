"""
Онлайн-оплата Stripe — API для сайту і вебхук Stripe (логіка: shop/payments.py).

  GET  shop/payments/                       {stripe: {enabled, mode}}                       (orders:read)
  POST shop/payments/stripe/checkout/       {order_number, success_url, cancel_url, lang}
                                            → {url, session_id, amount, currency, expires_at} (orders:write)
  POST shop/payments/stripe/sync/           {order_number} → {payment_status, paid}           (orders:write)
  POST payments/stripe/webhook/<код магазину>/   — для Stripe (без ключа API, підпис Stripe-Signature)

Сайт сам вирішує, кому дозволено платити за замовлення (гість — підписане посилання, покупець — свій кабінет):
ключ API сайту тут довірений. Сума завжди з Minerva.
"""
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from shop import payments
from shop.payments import PaymentError

from .models import APIKey


def _shop(request):
    from shop.services import shop_for_key
    key = request.auth if isinstance(request.auth, APIKey) else None
    return shop_for_key(key) if key else None


def _error(e: PaymentError):
    return Response({"detail": e.message, "code": e.code}, status=e.status)


def checkout_payload(payment) -> dict:
    return {"url": payment.url, "session_id": payment.session_id, "amount": payment.amount,
            "currency": payment.currency, "expires_at": payment.expires_at, "mode": "live" if payment.livemode else "test"}


class PaymentsStatusView(APIView):
    resource_scope = "orders"

    def get(self, request):
        return Response(payments.public_status(_shop(request)))


class StripeCheckoutView(APIView):
    resource_scope = "orders"

    def post(self, request):
        shop = _shop(request)
        if shop is None:
            return Response({"detail": "Для ключа не налаштовано магазин.", "code": "no_shop"}, status=400)
        d = request.data if isinstance(request.data, dict) else {}
        try:
            order = payments.find_order(shop, d.get("order_number"))
            payment = payments.start_checkout(shop, order, d.get("success_url"), d.get("cancel_url"),
                                              d.get("lang", "de"))
        except PaymentError as e:
            return _error(e)
        return Response(checkout_payload(payment), status=status.HTTP_201_CREATED)


class StripeSyncView(APIView):
    resource_scope = "orders"

    def post(self, request):
        shop = _shop(request)
        if shop is None:
            return Response({"detail": "Для ключа не налаштовано магазин.", "code": "no_shop"}, status=400)
        d = request.data if isinstance(request.data, dict) else {}
        try:
            order = payments.find_order(shop, d.get("order_number"))
        except PaymentError as e:
            return _error(e)
        state = payments.sync_order(shop, order)
        return Response({"payment_status": state, "paid": state == "paid"})


class StripeWebhookView(APIView):
    """Stripe → Minerva. Без ключа API: справжність перевіряється підписом (секрет вебхука магазину)."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request, slug):
        from shop.models import Shop
        shop = Shop.objects.filter(slug=slug).first()
        if shop is None or not shop.stripe_webhook_secret:
            return Response({"detail": "unknown endpoint"}, status=404)
        try:
            result = payments.handle_webhook(shop, request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""))
        except PaymentError as e:
            return _error(e)
        return Response({"received": True, "result": result})
