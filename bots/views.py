"""
bots/views.py — OAuth2 callback + Webhook для DigiKey Marketplace
"""
import hashlib
import hmac
import json
import logging

from django.shortcuts import redirect
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


def digikey_oauth_callback(request):
    """
    Обробляє redirect від DigiKey після авторизації користувача.
    URL: /bots/digikey/oauth-callback/
    Параметри: ?code=... або ?error=...
    """
    error = request.GET.get("error")
    code  = request.GET.get("code")

    if error:
        request.session["digikey_oauth_error"] = (
            f"DigiKey повернув помилку: {error} — {request.GET.get('error_description', '')}"
        )
        return redirect("/admin/bots/digikeyconfig/1/change/")

    if not code:
        return HttpResponse("Missing code parameter", status=400)

    from bots.models import DigiKeyConfig
    from bots.services.digikey import exchange_code_for_tokens, DigiKeyAPIError

    config       = DigiKeyConfig.get()
    redirect_uri = request.session.pop("digikey_oauth_redirect_uri", None)

    if not redirect_uri:
        # Fallback: побудувати URI знову
        redirect_uri = request.build_absolute_uri("/bots/digikey/oauth-callback/")

    try:
        data       = exchange_code_for_tokens(config, code, redirect_uri)
        token      = data["access_token"]
        expires_in = int(data.get("expires_in", 600))
        refresh    = data.get("refresh_token", "")

        DigiKeyConfig.objects.filter(pk=1).update(
            marketplace_access_token=token,
            marketplace_refresh_token=refresh,
            marketplace_token_expires_at=timezone.now() + timedelta(seconds=expires_in - 30),
        )
        request.session["digikey_oauth_success"] = True
    except DigiKeyAPIError as e:
        request.session["digikey_oauth_error"] = str(e)
    except Exception as e:
        request.session["digikey_oauth_error"] = f"{type(e).__name__}: {e}"

    return redirect("/admin/bots/digikeyconfig/1/change/")


# ── DigiKey Webhook ───────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "POST"])
def digikey_webhook(request):
    """
    /bots/digikey/webhook/

    GET  — верифікаційний challenge від DigiKey при реєстрації:
           ?challenge=<string> → повертаємо той самий рядок.

    POST — подія про нове/змінене замовлення.
           Підпис: заголовок X-DigiKey-Signature (HMAC-SHA256 hex).
           Синхронізація запускається у фоновому потоці — відповідаємо 200 одразу.
    """
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()

    # ── GET verification challenge ────────────────────────────────────────────
    if request.method == "GET":
        challenge = request.GET.get("challenge", "")
        if challenge:
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse("DigiKey webhook endpoint OK", content_type="text/plain")

    # ── POST: validate HMAC signature ─────────────────────────────────────────
    if config.webhook_secret:
        sig_header = request.headers.get("X-DigiKey-Signature", "")
        expected = hmac.new(
            config.webhook_secret.encode("utf-8"),
            request.body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            logger.warning("DigiKey webhook: invalid HMAC signature")
            return HttpResponse("Forbidden", status=403)

    # ── Parse payload ─────────────────────────────────────────────────────────
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse("Bad Request", status=400)

    event_type = (
        payload.get("EventType")
        or payload.get("eventType")
        or "unknown"
    )
    order_id = (
        payload.get("OrderId")
        or payload.get("orderId")
        or payload.get("businessId")
        or ""
    )
    logger.info("DigiKey webhook received: event=%s order=%s", event_type, order_id)

    if not config.webhook_enabled:
        return HttpResponse("OK", status=200)

    # ── Sync у фоновому потоці — не блокуємо DigiKey ─────────────────────────
    import threading

    def _sync():
        try:
            from bots.services.digikey import sync_marketplace_orders
            stats = sync_marketplace_orders(config)
            logger.info(
                "DigiKey webhook sync done: +%d orders, +%d lines",
                stats.get("created", 0),
                stats.get("lines_created", 0),
            )
        except Exception:
            logger.exception("DigiKey webhook sync failed")

    threading.Thread(target=_sync, daemon=True).start()
    return HttpResponse("OK", status=200)


from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404


@staff_member_required
def digikey_packlist(request, order_pk):
    """
    GET /bots/digikey/packlist/<order_pk>/
    Генерує PDF пакувального листа у форматі DigiKey, використовуючи
    живі дані з Marketplace API (не з локальної БД).
    """
    from sales.models import SalesOrder
    from bots.models import DigiKeyConfig

    order  = get_object_or_404(SalesOrder, pk=order_pk, source="digikey")
    config = DigiKeyConfig.get()

    # ── Fetch live order data from DigiKey Marketplace API ────────────────────
    api_order = {}
    fetch_error = None
    try:
        from bots.services.digikey import get_marketplace_token, _fetch_marketplace_order
        token     = get_marketplace_token(config)
        api_order = _fetch_marketplace_order(config, order.order_number, token)
    except Exception as e:
        fetch_error = str(e)

    if not api_order:
        return HttpResponse(
            f"<h3>Не вдалося отримати дані замовлення з DigiKey API</h3>"
            f"<p>Order: <b>{order.order_number}</b></p>"
            f"<pre>{fetch_error or 'Порожня відповідь API'}</pre>"
            f"<p>Переконайся що Marketplace авторизований "
            f"(<a href='/admin/bots/digikeyconfig/1/change/'>DigiKey Config</a>).</p>",
            content_type="text/html; charset=utf-8",
            status=502,
        )

    # ── Supplier address from AccountingSettings (our registered data) ─────────
    supplier = {"name": "Supplier", "street": "", "city_zip": "", "country": ""}
    try:
        from accounting.models import CompanySettings
        cs = CompanySettings.get()
        city_zip = " ".join(filter(None, [cs.addr_zip, cs.addr_city])).strip()
        supplier = {
            "name":     cs.name or "",
            "street":   cs.addr_street or "",
            "city_zip": city_zip,
            "country":  cs.addr_country or "",
        }
    except Exception:
        pass

    # ── Generate PDF ──────────────────────────────────────────────────────────
    try:
        from bots.services.digikey_pdf import generate_digikey_packing_list
        buf       = generate_digikey_packing_list(api_order, supplier)
        pdf_bytes = buf.getvalue()
    except Exception as e:
        return HttpResponse(
            f"<h3>Помилка генерації PDF</h3><pre>{type(e).__name__}: {e}</pre>",
            content_type="text/html; charset=utf-8",
            status=500,
        )

    filename = f"DigiKey_PackList_{order.order_number}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@staff_member_required
def digikey_ship_order(request, order_pk):
    """
    GET  /bots/digikey/ship/<order_pk>/  — форма підтвердження відправлення
    POST                                 — виклик ShipOrder API → оновлення статусу
    """
    from django.contrib import messages as msg
    from django.shortcuts import render, redirect
    from sales.models import SalesOrder
    from bots.models import DigiKeyConfig
    from bots.services.digikey import ship_marketplace_order, DigiKeyAPIError

    order  = get_object_or_404(SalesOrder, pk=order_pk, source="digikey")
    config = DigiKeyConfig.get()

    if request.method == "POST":
        tracking  = request.POST.get("tracking_number", "").strip()
        carrier   = request.POST.get("carrier_id", "").strip() or None
        invoice   = request.POST.get("invoice_number", "").strip() or None

        if not tracking:
            msg.error(request, "Вкажіть трек-номер відправлення.")
        else:
            try:
                result = ship_marketplace_order(config, order.order_number,
                                                tracking, carrier, invoice)
                if result["ok"]:
                    update_fields = ["status", "status_source"]
                    order.status        = "shipped"
                    order.status_source = "DigiKey Marketplace"
                    if tracking and not order.tracking_number:
                        order.tracking_number = tracking
                        update_fields.append("tracking_number")
                    order.save(update_fields=update_fields)
                    msg.success(request, result["message"])
                else:
                    msg.error(request, result["message"])
            except DigiKeyAPIError as e:
                msg.error(request, f"DigiKey API помилка: {e}")
            except Exception as e:
                msg.error(request, f"{type(e).__name__}: {e}")

        return redirect(f"/admin/sales/salesorder/{order_pk}/change/")

    # GET — показати форму; підтягуємо список carriers з DigiKey API
    carriers = []
    has_token = bool(config.marketplace_access_token)
    if has_token:
        try:
            from bots.services.digikey import get_shipping_carriers
            carriers = get_shipping_carriers(config)
        except Exception:
            carriers = []

    from django.template.response import TemplateResponse
    return TemplateResponse(request, "admin/bots/digikey_ship_order.html", {
        "title":     f"Відправити #{order.order_number} на DigiKey",
        "order":     order,
        "config":    config,
        "has_token": has_token,
        "carriers":  carriers,
    })
