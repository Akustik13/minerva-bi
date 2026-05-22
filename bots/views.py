"""
bots/views.py вЂ” OAuth2 callback + Webhook РґР»СЏ DigiKey Marketplace
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


def _match_carrier_id(shipping_courier, carriers):
    """Return the DigiKey carrier UUID that best matches order.shipping_courier."""
    if not shipping_courier or not carriers:
        return None
    sc = shipping_courier.lower().strip()
    for c in carriers:
        label = (c.get("label") or "").lower()
        code  = (c.get("code") or "").lower()
        if sc and (sc in label or label.startswith(sc) or (code and sc in code)):
            return c.get("id") or c.get("carrierId")
    return None


def digikey_oauth_callback(request):
    """
    РћР±СЂРѕР±Р»СЏС” redirect РІС–Рґ DigiKey РїС–СЃР»СЏ Р°РІС‚РѕСЂРёР·Р°С†С–С— РєРѕСЂРёСЃС‚СѓРІР°С‡Р°.
    URL: /bots/digikey/oauth-callback/
    РџР°СЂР°РјРµС‚СЂРё: ?code=... Р°Р±Рѕ ?error=...
    """
    error = request.GET.get("error")
    code  = request.GET.get("code")

    if error:
        request.session["digikey_oauth_error"] = (
            f"DigiKey РїРѕРІРµСЂРЅСѓРІ РїРѕРјРёР»РєСѓ: {error} вЂ” {request.GET.get('error_description', '')}"
        )
        return redirect("/admin/bots/digikeyconfig/1/change/")

    if not code:
        return HttpResponse("Missing code parameter", status=400)

    from bots.models import DigiKeyConfig
    from bots.services.digikey import exchange_code_for_tokens, DigiKeyAPIError

    config       = DigiKeyConfig.get()
    redirect_uri = request.session.pop("digikey_oauth_redirect_uri", None)

    if not redirect_uri:
        # Fallback: use public_base_url from config (reliable behind reverse proxy)
        base = (config.public_base_url or "").rstrip("/")
        if base:
            redirect_uri = f"{base}/bots/digikey/oauth-callback/"
        else:
            from django.conf import settings as _s
            redirect_uri = (
                getattr(_s, "DIGIKEY_OAUTH_REDIRECT_URI", "")
                or request.build_absolute_uri("/bots/digikey/oauth-callback/")
            )

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


# в”Ђв”Ђ DigiKey Webhook в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

@csrf_exempt
@require_http_methods(["GET", "POST"])
def digikey_webhook(request):
    """
    /bots/digikey/webhook/

    GET  вЂ” РІРµСЂРёС„С–РєР°С†С–Р№РЅРёР№ challenge РІС–Рґ DigiKey РїСЂРё СЂРµС”СЃС‚СЂР°С†С–С—:
           ?challenge=<string> в†’ РїРѕРІРµСЂС‚Р°С”РјРѕ С‚РѕР№ СЃР°РјРёР№ СЂСЏРґРѕРє.

    POST вЂ” РїРѕРґС–СЏ РїСЂРѕ РЅРѕРІРµ/Р·РјС–РЅРµРЅРµ Р·Р°РјРѕРІР»РµРЅРЅСЏ.
           РџС–РґРїРёСЃ: Р·Р°РіРѕР»РѕРІРѕРє X-DigiKey-Signature (HMAC-SHA256 hex).
           РЎРёРЅС…СЂРѕРЅС–Р·Р°С†С–СЏ Р·Р°РїСѓСЃРєР°С”С‚СЊСЃСЏ Сѓ С„РѕРЅРѕРІРѕРјСѓ РїРѕС‚РѕС†С– вЂ” РІС–РґРїРѕРІС–РґР°С”РјРѕ 200 РѕРґСЂР°Р·Сѓ.
    """
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()

    # в”Ђв”Ђ GET verification challenge в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    if request.method == "GET":
        challenge = request.GET.get("challenge", "")
        if challenge:
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse("DigiKey webhook endpoint OK", content_type="text/plain")

    # в”Ђв”Ђ POST: validate HMAC signature в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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

    # в”Ђв”Ђ Parse payload в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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

    # в”Ђв”Ђ Sync Сѓ С„РѕРЅРѕРІРѕРјСѓ РїРѕС‚РѕС†С– вЂ” РЅРµ Р±Р»РѕРєСѓС”РјРѕ DigiKey в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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
    Р“РµРЅРµСЂСѓС” PDF РїР°РєСѓРІР°Р»СЊРЅРѕРіРѕ Р»РёСЃС‚Р° Сѓ С„РѕСЂРјР°С‚С– DigiKey, РІРёРєРѕСЂРёСЃС‚РѕРІСѓСЋС‡Рё
    Р¶РёРІС– РґР°РЅС– Р· Marketplace API (РЅРµ Р· Р»РѕРєР°Р»СЊРЅРѕС— Р‘Р”).
    """
    from sales.models import SalesOrder
    from bots.models import DigiKeyConfig

    order  = get_object_or_404(SalesOrder, pk=order_pk, source="digikey")
    config = DigiKeyConfig.get()

    # в”Ђв”Ђ Fetch live order data from DigiKey Marketplace API в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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
            f"<h3>РќРµ РІРґР°Р»РѕСЃСЏ РѕС‚СЂРёРјР°С‚Рё РґР°РЅС– Р·Р°РјРѕРІР»РµРЅРЅСЏ Р· DigiKey API</h3>"
            f"<p>Order: <b>{order.order_number}</b></p>"
            f"<pre>{fetch_error or 'РџРѕСЂРѕР¶РЅСЏ РІС–РґРїРѕРІС–РґСЊ API'}</pre>"
            f"<p>РџРµСЂРµРєРѕРЅР°Р№СЃСЏ С‰Рѕ Marketplace Р°РІС‚РѕСЂРёР·РѕРІР°РЅРёР№ "
            f"(<a href='/admin/bots/digikeyconfig/1/change/'>DigiKey Config</a>).</p>",
            content_type="text/html; charset=utf-8",
            status=502,
        )

    # в”Ђв”Ђ Supplier address from AccountingSettings (our registered data) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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

    # в”Ђв”Ђ Generate PDF в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    try:
        from bots.services.digikey_pdf import generate_digikey_packing_list
        buf       = generate_digikey_packing_list(api_order, supplier)
        pdf_bytes = buf.getvalue()
    except Exception as e:
        return HttpResponse(
            f"<h3>РџРѕРјРёР»РєР° РіРµРЅРµСЂР°С†С–С— PDF</h3><pre>{type(e).__name__}: {e}</pre>",
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
    GET  /bots/digikey/ship/<order_pk>/  вЂ” С„РѕСЂРјР° РїС–РґС‚РІРµСЂРґР¶РµРЅРЅСЏ РІС–РґРїСЂР°РІР»РµРЅРЅСЏ
    POST                                 вЂ” РІРёРєР»РёРє ShipOrder API в†’ РѕРЅРѕРІР»РµРЅРЅСЏ СЃС‚Р°С‚СѓСЃСѓ
    """
    from django.contrib import messages as msg
    from django.shortcuts import render, redirect
    from sales.models import SalesOrder
    from bots.models import DigiKeyConfig
    from bots.services.digikey import (
        ship_marketplace_order, upload_vat_invoice,
        fetch_marketplace_order_data, link_vat_to_order, DigiKeyAPIError,
    )

    order  = get_object_or_404(SalesOrder, pk=order_pk, source="digikey")
    config = DigiKeyConfig.get()

    if request.method == "POST":
        tracking        = request.POST.get("tracking_number", "").strip()
        carrier         = request.POST.get("carrier_id", "").strip() or None
        invoice         = request.POST.get("invoice_number", "").strip() or None
        net_vat_raw     = request.POST.get("net_vat_invoice_amount", "").strip()
        net_vat         = float(net_vat_raw.replace(",", ".")) if net_vat_raw else None

        # Per-line shipped quantities: POST keys like "qty_<orderDetailId>"
        shipped_quantities = {}
        for key, val in request.POST.items():
            if key.startswith("qty_") and val.strip():
                detail_id = key[4:]
                try:
                    shipped_quantities[detail_id] = int(val)
                except ValueError:
                    pass

        if not tracking:
            msg.error(request, "Р’РєР°Р¶С–С‚СЊ С‚СЂРµРє-РЅРѕРјРµСЂ РІС–РґРїСЂР°РІР»РµРЅРЅСЏ.")
        else:
            try:
                # Optional VAT invoice file upload before shipping
                vat_file_id = None
                vat_file    = request.FILES.get("vat_invoice_file")
                supplier_id = request.POST.get("supplier_id", "").strip() or None
                if vat_file:
                    up = upload_vat_invoice(config, vat_file.read(), vat_file.name,
                                            supplier_id=supplier_id)
                    if up["ok"]:
                        vat_file_id = up.get("file_id")
                    else:
                        msg.warning(request, f"Р¤Р°Р№Р» VAT РЅРµ Р·Р°РІР°РЅС‚Р°Р¶РµРЅРѕ: {up['message']}")

                result = ship_marketplace_order(
                    config, order.order_number,
                    tracking_number=tracking,
                    carrier_id=carrier,
                    invoice_number=invoice,
                    net_vat_invoice_amount=net_vat,
                    shipped_quantities=shipped_quantities or None,
                    vat_file_id=vat_file_id,
                )
                if result["ok"]:
                    update_fields = ["status", "status_source"]
                    order.status        = "shipped"
                    order.status_source = "DigiKey Marketplace"
                    if tracking and not order.tracking_number:
                        order.tracking_number = tracking
                        update_fields.append("tracking_number")
                    order.save(update_fields=update_fields)
                    msg.success(request, result["message"])

                    # РЎРїСЂРѕР±Р° 3: РїСЂРёРІ'СЏР·Р°С‚Рё VAT С„Р°Р№Р» С‡РµСЂРµР· additionalFields (undocumented)
                    if vat_file_id:
                        lnk = link_vat_to_order(config, order.order_number, vat_file_id)
                        if lnk["ok"]:
                            msg.success(request, f"рџ“Ћ {lnk['message']}")
                        else:
                            msg.warning(
                                request,
                                f"вљ пёЏ Р¤Р°Р№Р» VAT Р·Р°РІР°РЅС‚Р°Р¶РµРЅРѕ РЅР° DigiKey (ID: {vat_file_id}), "
                                f"Р°Р»Рµ Р°РІС‚РѕРјР°С‚РёС‡РЅРѕ РїСЂРёРІ'СЏР·Р°С‚Рё РЅРµ РІРґР°Р»РѕСЃСЏ вЂ” РґРѕРґР°Р№ РІСЂСѓС‡РЅСѓ РЅР° СЃР°Р№С‚С– DigiKey. "
                                f"({lnk['message']})"
                            )
                else:
                    msg.error(request, result["message"])
            except DigiKeyAPIError as e:
                msg.error(request, f"DigiKey API РїРѕРјРёР»РєР°: {e}")
            except Exception as e:
                msg.error(request, f"{type(e).__name__}: {e}")

        return redirect(f"/admin/sales/salesorder/{order_pk}/change/")

    # GET вЂ” РїРѕРєР°Р·Р°С‚Рё С„РѕСЂРјСѓ; РїС–РґС‚СЏРіСѓС”РјРѕ СЃРїРёСЃРѕРє carriers С– РґРµС‚Р°Р»С– Р·Р°РјРѕРІР»РµРЅРЅСЏ Р· DigiKey API
    carriers      = []
    order_details = []
    has_token = bool(config.marketplace_access_token)
    if has_token:
        try:
            from bots.services.digikey import get_shipping_carriers
            carriers = get_shipping_carriers(config)
        except Exception:
            carriers = []
        try:
            dk_order      = fetch_marketplace_order_data(config, order.order_number)
            order_details = dk_order.get("orderDetails") or []
            supplier_id   = dk_order.get("supplierId", "")
        except Exception:
            order_details = []
            supplier_id   = ""

    # Auto-match order.shipping_courier against DigiKey carrier list
    preset_carrier_id = _match_carrier_id(order.shipping_courier, carriers)

    EU_COUNTRIES = {
        "AT","BE","BG","CY","CZ","DE","DK","EE","ES","FI",
        "FR","GR","HR","HU","IE","IT","LT","LU","LV","MT",
        "NL","PL","PT","RO","SE","SI","SK",
    }
    is_eu = (order.addr_country or "").upper() in EU_COUNTRIES

    from django.template.response import TemplateResponse
    return TemplateResponse(request, "admin/bots/digikey_ship_order.html", {
        "title":         f"Р’С–РґРїСЂР°РІРёС‚Рё #{order.order_number} РЅР° DigiKey",
        "order":         order,
        "config":        config,
        "has_token":     has_token,
        "carriers":      carriers,
        "order_details": order_details,
        "supplier_id":   supplier_id,
        "is_eu":            is_eu,
        "preset_carrier_id": preset_carrier_id,
    })


def push_tracking_bulk_view(request):
    """GET — показати preview; POST — перенести трекінги недоставлених замовлень у Shipment."""
    from django.contrib.admin.views.decorators import staff_member_required
    from django.template.response import TemplateResponse
    from django.http import HttpResponseForbidden
    from sales.models import SalesOrder

    if not request.user.is_staff:
        return HttpResponseForbidden()

    # замовлення з трекінгом, статус shipped, без Shipment або з порожнім tracking
    candidates = (
        SalesOrder.objects.filter(
            status="shipped",
            tracking_number__gt="",
        )
        .exclude(status="delivered")
        .order_by("-order_date")
    )

    results = []
    errors  = []

    if request.method == "POST":
        from shipping.services.import_tracking import ensure_shipment_for_order
        for order in candidates:
            try:
                shipment, created = ensure_shipment_for_order(
                    order,
                    order.tracking_number,
                    order.shipping_courier or "",
                )
                results.append({
                    "order":    order.order_number,
                    "tracking": order.tracking_number,
                    "created":  created,
                    "shipment_pk": shipment.pk,
                })
            except Exception as e:
                errors.append(f"{order.order_number}: {e}")

    return TemplateResponse(request, "admin/bots/push_tracking_bulk.html", {
        "title":      "Перенести трекінги у відправлення",
        "candidates": candidates,
        "results":    results,
        "errors":     errors,
        "is_post":    request.method == "POST",
    })
