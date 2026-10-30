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


CONFIG_URL = "/admin/bots/digikeyconfig/1/change/"


def _oauth_refused(request, reason: str, status: int):
    """Сторінка «авторизацію не прийнято» — токени Marketplace не змінено."""
    from django.shortcuts import render
    return render(request, "bots/digikey_oauth_error.html", {
        "reason": reason, "config_url": CONFIG_URL,
        "login_url": f"/admin/login/?next={CONFIG_URL}",
        "anonymous": not request.user.is_authenticated,
    }, status=status)


@require_http_methods(["GET"])
def digikey_oauth_callback(request):
    """
    Повернення з DigiKey після «🔑 Авторизувати» (3-legged OAuth Marketplace).
    URL: /bots/digikey/oauth-callback/?code=…&state=… або ?error=…
    code обмінюється на токени лише для людини, що почала авторизацію в цьому ж браузері (state з сесії,
    bots/oauth.py) і може змінювати налаштування DigiKey; інакше — сторінка з поясненням, токени не змінюються.
    """
    from bots import oauth

    if not request.user.is_authenticated:
        return _oauth_refused(request, "Увійдіть у Minerva й натисніть «🔑 Авторизувати» на сторінці DigiKey ще раз.", 403)
    if not oauth.can_authorize(request.user):
        oauth.forget(request.session)
        return _oauth_refused(request, "Авторизувати DigiKey Marketplace може лише той, хто має право змінювати "
                                       "налаштування DigiKey (адміністратор).", 403)

    error = request.GET.get("error")
    if error:                     # DigiKey може не повертати state з помилкою; токени тут не змінюються
        oauth.forget(request.session)
        request.session["digikey_oauth_error"] = (
            f"DigiKey повернув помилку: {error} — {request.GET.get('error_description', '')}"[:300]
        )
        return redirect(CONFIG_URL)

    problem = oauth.check_state(request.session, request.GET.get("state", ""))
    if problem:
        logger.warning("DigiKey OAuth callback: state %s, user=%s — code не обмінюється",
                       problem, request.user.get_username())
        return _oauth_refused(request, oauth.REASONS[problem] + " Натисніть «🔑 Авторизувати» ще раз.", 400)

    code = request.GET.get("code")
    if not code:
        return _oauth_refused(request, "DigiKey не передав код авторизації. Натисніть «🔑 Авторизувати» ще раз.", 400)

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

    return redirect(CONFIG_URL)


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

    # ?action=save — зберегти в media/orders/digikey/{order_number}/ і повернути JSON
    if request.GET.get("action") == "save":
        from django.conf import settings
        from django.http import JsonResponse
        from datetime import date as _date
        dest_dir = settings.MEDIA_ROOT / 'orders' / 'digikey' / order.order_number
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / filename).write_bytes(pdf_bytes)
        rel_url = f"{settings.MEDIA_URL}orders/digikey/{order.order_number}/{filename}"
        return JsonResponse({
            'ok': True,
            'filename': filename,
            'url': rel_url,
            'order_number': order.order_number,
            'source_slug': 'digikey',
            'date_str': _date.today().strftime('%Y-%m-%d'),
            'local': {},
        })

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def confirm_digikey_shipment(request, order, config, tracking, carrier=None, invoice=None,
                              net_vat=None, shipped_quantities=None) -> bool:
    """Підтверджує відправлення на DigiKey Marketplace (ShipOrder API) і ставить замовлення в shipped.
    Використовується формою /bots/digikey/ship/<pk>/ і автопідтвердженням зі сторінки відправлення.
    Повідомлення — через django.contrib.messages. Повертає True при успіху."""
    from django.contrib import messages as msg
    from bots.services.digikey import ship_marketplace_order, upload_vat_invoice, DigiKeyAPIError

    ok = False
    _EU = {"AT","BE","BG","CY","CZ","DE","DK","EE","ES","FI","FR","GR","HR",
           "HU","IE","IT","LT","LU","LV","MT","NL","PL","PT","RO","SE","SI","SK"}
    is_eu_post = (order.addr_country or "").upper() in _EU

    if not tracking:
        msg.error(request, "Вкажіть трек-номер відправлення.")
    else:
        try:
            # ── STEP 1: AUTO-GENERATE INVOICE via InvoiceService (before ship API) ──
            # Uses same /invoices/ system (DOCX → PDF, Invoice model record)
            if config.auto_invoice_eu and is_eu_post:
                from django.db.models import Q as _Q
                from shipping.models import Invoice as _Inv
                _existing_inv = (
                    _Inv.objects.filter(
                        _Q(sales_order=order) | _Q(digikey_order_no=order.order_number)
                    ).first()
                )
                if not _existing_inv:
                    try:
                        from shipping.services.invoice_service import InvoiceService
                        _auto_inv = InvoiceService.generate_from_digikey_order(
                            order.order_number, request.user,
                            invoice_number=invoice or None,
                        )
                        # Feed invoice number and subtotal into ship API call
                        invoice = _auto_inv.invoice_number
                        if net_vat is None:
                            net_vat = float(_auto_inv.subtotal or 0) or None
                        msg.success(request, f"🧾 Інвойс #{invoice} згенеровано автоматично.")
                    except Exception as _inv_err:
                        msg.warning(request, f"Авто-генерація інвойсу не вдалась: {_inv_err}")
                else:
                    # Existing invoice — still fill fields for ship API
                    invoice = invoice or _existing_inv.invoice_number
                    if net_vat is None:
                        net_vat = float(_existing_inv.subtotal or 0) or None

            # ── STEP 2: Optional VAT invoice file upload before shipping ──────
            vat_file_id = None
            vat_file    = request.FILES.get("vat_invoice_file")
            supplier_id = request.POST.get("supplier_id", "").strip() or None
            if vat_file:
                vat_bytes = vat_file.read()

                # Save to order documents folder on server
                try:
                    from django.conf import settings as _s
                    import pathlib as _pl
                    docs_dir = _pl.Path(_s.MEDIA_ROOT) / 'orders' / order.source / order.order_number
                    docs_dir.mkdir(parents=True, exist_ok=True)
                    (docs_dir / vat_file.name).write_bytes(vat_bytes)
                    msg.success(request, f"📄 {vat_file.name} збережено в документи замовлення.")
                except Exception as _e:
                    msg.warning(request, f"Не вдалося зберегти файл в документи: {_e}")

                # Upload to DigiKey FileDocuments (best-effort)
                up = upload_vat_invoice(config, vat_bytes, vat_file.name,
                                        supplier_id=supplier_id)
                if up["ok"]:
                    vat_file_id = up.get("file_id")
                else:
                    msg.warning(request, f"Файл VAT не завантажено на DigiKey: {up['message']}")

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
                ok = True
                update_fields = ["status", "status_source"]
                order.status        = "shipped"
                order.status_source = "DigiKey Marketplace"
                if tracking and not order.tracking_number:
                    order.tracking_number = tracking
                    update_fields.append("tracking_number")
                order.save(update_fields=update_fields)
                msg.success(request, result["message"])

                if vat_file_id:
                    msg.info(request, f"\ud83d\udcce VAT \u0444\u0430\u0439\u043b \u0437\u0430\u0432\u0430\u043d\u0442\u0430\u0436\u0435\u043d\u043e \u043d\u0430 DigiKey (ID: {vat_file_id}). \u041f\u0440\u0438\u0432'\u044f\u0436\u0456\u0442\u044c \u0439\u043e\u0433\u043e \u0432\u0440\u0443\u0447\u043d\u0443 \u0443 DigiKey Marketplace portal.")
            else:
                msg.error(request, result["message"])
        except DigiKeyAPIError as e:
            msg.error(request, f"DigiKey API помилка: {e}")
        except Exception as e:
            msg.error(request, f"{type(e).__name__}: {e}")

    return ok


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
    from bots.services.digikey import (
        ship_marketplace_order, upload_vat_invoice,
        fetch_marketplace_order_data, DigiKeyAPIError,
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

        confirm_digikey_shipment(request, order, config, tracking, carrier, invoice, net_vat, shipped_quantities)

        return redirect(f"/admin/sales/salesorder/{order_pk}/change/")

    # GET — показати форму; підтягуємо список carriers і деталі замовлення з DigiKey API
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

    # Auto-fill invoice fields from existing Invoice record
    existing_invoice = None
    inv_net_amount = None
    try:
        from django.db.models import Q
        from shipping.models import Invoice as _Inv
        existing_invoice = (
            _Inv.objects.filter(
                Q(sales_order=order) | Q(digikey_order_no=order.order_number)
            )
            .order_by("-invoice_date")
            .first()
        )
        if existing_invoice:
            inv_net_amount = f"{existing_invoice.subtotal:.2f}"
    except Exception:
        pass

    # Invoice number info: compare DB vs DigiKey API, suggest next number
    inv_info = None
    try:
        from shipping.services.invoice_service import get_invoice_number_info
        inv_info = get_invoice_number_info(config if has_token else None)
    except Exception:
        pass

    # Auto-generate invoice immediately on GET if conditions are met:
    # auto_invoice_eu=True + EU order + no discrepancy + no existing invoice
    auto_generated_now = False
    if (
        config.auto_invoice_eu
        and is_eu
        and not existing_invoice
        and inv_info is not None
        and inv_info["match"]
    ):
        try:
            from shipping.services.invoice_service import InvoiceService
            existing_invoice = InvoiceService.generate_from_digikey_order(
                order.order_number, request.user,
                invoice_number=inv_info["suggested"],
            )
            inv_net_amount    = f"{existing_invoice.subtotal:.2f}"
            auto_generated_now = True
        except Exception as _e:
            # Non-fatal: show warning in template via flag
            inv_info["auto_gen_error"] = str(_e)

    from django.template.response import TemplateResponse
    return TemplateResponse(request, "admin/bots/digikey_ship_order.html", {
        "title":              f"Відправити #{order.order_number} на DigiKey",
        "order":              order,
        "config":             config,
        "has_token":          has_token,
        "carriers":           carriers,
        "order_details":      order_details,
        "supplier_id":        supplier_id,
        "is_eu":              is_eu,
        "preset_carrier_id":  preset_carrier_id,
        "existing_invoice":   existing_invoice,
        "inv_net_amount":     inv_net_amount,
        "inv_info":           inv_info,
        "auto_generated_now": auto_generated_now,
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
