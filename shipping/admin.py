"""
shipping/admin.py — Адмін-панель модуля доставки
"""
import logging

from django.contrib import admin, messages
from core.mixins import AuditableMixin
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone

logger = logging.getLogger(__name__)

from .models import Carrier, Shipment, ShipmentPackage, PackagingMaterial, OrderPackaging, ProductPackaging, ShippingSettings
from .services.registry import get_service


# ── PackagingMaterial Admin ───────────────────────────────────────────────────

@admin.register(PackagingMaterial)
class PackagingMaterialAdmin(admin.ModelAdmin):
    list_display  = ('type_badge', 'name', 'dimensions_col', 'tare_weight_kg',
                     'max_weight_kg', 'volume_col', 'cost', 'is_active')
    list_filter   = ('box_type', 'is_active')
    list_editable = ('is_active',)
    search_fields = ('name',)

    fieldsets = (
        ('📦 Упаковка', {
            'fields': ('box_type', 'name', 'is_active', 'notes'),
        }),
        ('📐 Розміри', {
            'fields': (('length_cm', 'width_cm', 'height_cm'),),
            'description': 'Внутрішні розміри коробки в сантиметрах',
        }),
        ('⚖️ Вага', {
            'fields': (('tare_weight_kg', 'max_weight_kg'),),
        }),
        ('💰 Вартість', {
            'fields': ('cost',),
        }),
    )

    def type_badge(self, obj):
        colors = {
            'box':      '#1565c0', 'envelope': '#6a1b9a',
            'tube':     '#4e342e', 'bag':      '#2e7d32', 'custom': '#455a64',
        }
        bg = colors.get(obj.box_type, '#455a64')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px">{}</span>',
            bg, obj.get_box_type_display(),
        )
    type_badge.short_description = 'Тип'

    def dimensions_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;color:#80cbc4">'
            '{}×{}×{} см</span>',
            obj.length_cm, obj.width_cm, obj.height_cm,
        )
    dimensions_col.short_description = 'Розміри (Д×Ш×В)'

    def volume_col(self, obj):
        return format_html('<span style="color:#607d8b">{} см³</span>', obj.volume_cm3)
    volume_col.short_description = 'Об\'єм'


# ── Carrier Admin ─────────────────────────────────────────────────────────────

@admin.register(Carrier)
class CarrierAdmin(admin.ModelAdmin):
    list_display  = ("name", "carrier_type_badge", "is_active", "is_default",
                     "sender_name", "sender_country", "has_credentials")
    list_filter   = ("carrier_type", "is_active", "is_default")
    list_editable = ("is_active", "is_default")

    fieldsets = (
        ("🚚 Перевізник", {
            "fields": ("name", "carrier_type", "is_active", "is_default", "notes")
        }),
        ("🔑 API налаштування", {
            "fields": ("api_key", "api_secret", "api_url", "connection_uuid", "track_api_key"),
            "description": (
                "<b>Jumingo:</b> api_key = X-AUTH-TOKEN, connection_uuid = UUID інтеграції.<br>"
                "<b>DHL:</b> api_key = API Key, api_secret = API Secret, connection_uuid = Account Number, api_url = <code>test</code> для sandbox.<br>"
                "<b>UPS:</b> api_key = Client ID, api_secret = Client Secret, connection_uuid = Account Number, "
                "api_url = <code>sandbox</code> для тестів (порожньо = production).<br>"
                "<b>DHL Tracking:</b> track_api_key = окремий Tracking API ключ."
            ),
        }),
        ("📤 Дані відправника", {
            "fields": (
                ("sender_name", "sender_company"),
                "sender_street",
                ("sender_city", "sender_zip", "sender_country"),
                ("sender_phone", "sender_email"),
            ),
            "description": "Ці дані підставляються автоматично при кожному відправленні."
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        for field_name in ('api_key', 'api_secret', 'track_api_key'):
            if field_name in form.base_fields:
                form.base_fields[field_name].widget = PasswordInput(render_value=True)
        return form

    def carrier_type_badge(self, obj):
        colors = {
            "jumingo": "#e91e63", "dhl": "#ffcc00",
            "ups": "#351c75", "fedex": "#4d148c", "other": "#607d8b",
        }
        color = colors.get(obj.carrier_type, "#607d8b")
        text_color = "#000" if obj.carrier_type == "dhl" else "#fff"
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;'
            'border-radius:10px;font-size:11px;font-weight:700">{}</span>',
            color, text_color, obj.get_carrier_type_display()
        )
    carrier_type_badge.short_description = "Тип"

    def has_credentials(self, obj):
        if obj.api_key:
            return format_html('<span style="color:#4caf50;font-weight:bold">✅ Так</span>')
        return format_html('<span style="color:#f44336">❌ Немає</span>')
    has_credentials.short_description = "API ключ"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('<int:pk>/test-ups-token/',
                 self.admin_site.admin_view(self.test_ups_token_view),
                 name='carrier_test_ups_token'),
            path('<int:pk>/test-ups-rates/',
                 self.admin_site.admin_view(self.test_ups_rates_view),
                 name='carrier_test_ups_rates'),
            path('<int:pk>/ups-debug/',
                 self.admin_site.admin_view(self.ups_debug_view),
                 name='carrier_ups_debug'),
        ]
        return custom + urls

    def test_ups_token_view(self, request, pk):
        from .ups_client import UPSClient, UPSError
        carrier = get_object_or_404(Carrier, pk=pk)
        try:
            client = UPSClient(carrier=carrier)
            token = client.get_token()
            return JsonResponse({
                'ok': True,
                'message': f'Токен отримано ({len(token)} симв.)',
                'mode': 'Sandbox' if (carrier.api_url or '').lower() == 'sandbox' else 'Production',
            })
        except Exception as e:
            return JsonResponse({'ok': False, 'error': str(e)})

    def test_ups_rates_view(self, request, pk):
        from .ups_client import UPSClient, UPSError
        carrier = get_object_or_404(Carrier, pk=pk)
        # Use sender country to pick a domestic/nearby test destination
        origin = (carrier.sender_country or 'DE').upper()
        _test_dest = {
            'DE': {'name': 'Test', 'address_line': 'Marienplatz 1',   'city': 'Munich',   'postal': '80331',   'country': 'DE'},
            'AT': {'name': 'Test', 'address_line': 'Stephansplatz 1', 'city': 'Vienna',   'postal': '1010',    'country': 'AT'},
            'CH': {'name': 'Test', 'address_line': 'Bahnhofstr. 1',   'city': 'Zurich',   'postal': '8001',    'country': 'CH'},
            'PL': {'name': 'Test', 'address_line': 'Rynek 1',         'city': 'Warsaw',   'postal': '00-272',  'country': 'PL'},
            'GB': {'name': 'Test', 'address_line': '1 The Strand',    'city': 'London',   'postal': 'WC2N 5HR','country': 'GB'},
            'US': {'name': 'Test', 'address_line': '100 Main St',     'city': 'New York', 'postal': '10001',   'country': 'US', 'state': 'NY'},
        }
        to_addr = _test_dest.get(origin, {
            'name': 'Test', 'address_line': 'Test Street 1',
            'city': carrier.sender_city or 'Berlin',
            'postal': carrier.sender_zip or '10115',
            'country': origin,
        })
        try:
            client = UPSClient(carrier=carrier)
            rates = client.get_rates(
                to_address=to_addr,
                packages=[{'weight_kg': 1.0, 'length_cm': 20, 'width_cm': 15, 'height_cm': 10}],
            )
            return JsonResponse({
                'ok': True,
                'route': f"{origin} → {to_addr['country']} ({to_addr['city']})",
                'rates': [
                    {'name': r['name'], 'price': str(r['price']), 'currency': r['currency']}
                    for r in rates[:8]
                ],
            })
        except Exception as e:
            return JsonResponse({'ok': False, 'error': str(e)})

    def ups_debug_view(self, request, pk):
        """Покроковий debug UPS API — показує точний запит і відповідь кожного кроку."""
        import base64, uuid, time
        import requests as req_lib
        from django.core.cache import cache

        carrier = get_object_or_404(Carrier, pk=pk)
        steps = []

        def _mask(s):
            s = str(s or '')
            return s[:4] + '****' + s[-4:] if len(s) > 8 else '****'

        def _step(name, method, url, headers, body=None, params=None):
            t0 = time.time()
            display_headers = {k: (_mask(v) if k.lower() in ('authorization', 'x-merchant-id') else v)
                               for k, v in headers.items()}
            step = {
                'step': name, 'method': method, 'url': url,
                'request_headers': display_headers,
                'request_body': body,
                'request_params': params,
            }
            try:
                if method == 'POST':
                    r = req_lib.post(url, headers=headers, json=body, timeout=30)
                else:
                    r = req_lib.get(url, headers=headers, params=params, timeout=30)
                step['status'] = r.status_code
                step['ok'] = r.ok
                try:
                    step['response'] = r.json()
                except Exception:
                    step['response'] = r.text[:1000]
                step['ms'] = int((time.time() - t0) * 1000)
            except Exception as e:
                step['status'] = None
                step['ok'] = False
                step['response'] = str(e)
                step['ms'] = int((time.time() - t0) * 1000)
            return step

        # ── Step 1: OAuth Token ──────────────────────────────────────────────
        is_sandbox = (carrier.api_url or '').lower() in ('sandbox', 'test', 'staging')
        base_url = 'https://wwwcie.ups.com' if is_sandbox else 'https://onlinetools.ups.com'
        creds_b64 = base64.b64encode(f'{carrier.api_key}:{carrier.api_secret}'.encode()).decode()

        s1 = _step(
            '1. OAuth Token',
            'POST',
            f'{base_url}/security/v1/oauth/token',
            headers={
                'Authorization': f'Basic {creds_b64}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'x-merchant-id': carrier.connection_uuid,
            },
            body='grant_type=client_credentials',
        )
        # Override: POST with data= not json=
        t0 = time.time()
        try:
            r1 = req_lib.post(
                f'{base_url}/security/v1/oauth/token',
                headers={
                    'Authorization': f'Basic {creds_b64}',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'x-merchant-id': carrier.connection_uuid,
                },
                data='grant_type=client_credentials',
                timeout=30,
            )
            s1['status'] = r1.status_code
            s1['ok'] = r1.ok
            try:
                rj = r1.json()
                token = rj.get('access_token', '')
                s1['response'] = {**rj, 'access_token': _mask(token)} if token else rj
                s1['token_len'] = len(token)
                s1['expires_in'] = rj.get('expires_in')
            except Exception:
                s1['response'] = r1.text[:500]
                token = ''
        except Exception as e:
            s1['ok'] = False
            s1['response'] = str(e)
            token = ''
        s1['ms'] = int((time.time() - t0) * 1000)
        steps.append(s1)

        if not token:
            return JsonResponse({'steps': steps, 'summary': '❌ Зупинено: токен не отримано'})

        auth_headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'transId': str(uuid.uuid4())[:32],
            'transactionSrc': 'minerva-bi-debug',
        }

        origin = (carrier.sender_country or 'DE').upper()

        shipper_addr = {
            'name': carrier.sender_name or carrier.sender_company or 'Sender',
            'address_line': carrier.sender_street or '', 'city': carrier.sender_city or '',
            'postal': carrier.sender_zip or '', 'country': origin,
        }

        # International test destinations (probe all 3 to find which routes are enabled)
        INTL_DESTS = [
            {'label': f'{origin}→NL', 'name': 'Test NL',
             'address_line': 'Dam 1', 'city': 'Amsterdam', 'postal': '1012 JS', 'country': 'NL'},
            {'label': f'{origin}→CN', 'name': 'Test CN',
             'address_line': 'Shilong Industry Zone', 'city': 'Dongguan', 'postal': '523000', 'country': 'CN'},
            {'label': f'{origin}→US', 'name': 'Test US',
             'address_line': '100 Main St', 'city': 'Los Angeles', 'postal': '90001', 'country': 'US', 'state': 'CA'},
        ]

        rate_url        = f'{base_url}/api/rating/v2409/Shop'
        rate_url_single = f'{base_url}/api/rating/v2409/Rate'

        def fmt_addr(a):
            r = {'AddressLine': [a.get('address_line', '')], 'City': a.get('city', ''),
                 'PostalCode': a.get('postal', ''), 'CountryCode': (a.get('country') or 'DE').upper()}
            if a.get('state'):
                r['StateProvinceCode'] = a['state'].upper()
            return r

        def pkg(code, weight='1'):
            return {'PackagingType': {'Code': code},
                    'Dimensions': {'UnitOfMeasurement': {'Code': 'CM'},
                                   'Length': '20', 'Width': '15', 'Height': '10'},
                    'PackageWeight': {'UnitOfMeasurement': {'Code': 'KGS'}, 'Weight': weight}}

        def shipment_obj(dest, pkg_obj, svc=None):
            s = {
                'Shipper':  {'Name': shipper_addr['name'], 'ShipperNumber': carrier.connection_uuid,
                             'Address': fmt_addr(shipper_addr)},
                'ShipTo':   {'Name': dest['name'], 'Address': fmt_addr(dest)},
                'ShipFrom': {'Name': shipper_addr['name'], 'Address': fmt_addr(shipper_addr)},
                'PaymentDetails': {
                    'ShipmentCharge': {
                        'Type': '01',
                        'BillShipper': {'AccountNumber': carrier.connection_uuid},
                    },
                },
                'ShipmentRatingOptions': {'NegotiatedRatesIndicator': ''},
                'Package':  [pkg_obj],
            }
            if svc:
                s['Service'] = {'Code': svc}
            return s

        def rate_payload(option, dest, pkg_obj, svc=None):
            return {'RateRequest': {
                'Request': {'RequestOption': option, 'TransactionReference': {'CustomerContext': 'debug'}},
                'Shipment': shipment_obj(dest, pkg_obj, svc),
            }}

        def _err_msg(s):
            resp = s.get('response') or {}
            errs = resp.get('response', {}).get('errors') or []
            return errs[0].get('message', '?') if errs else str(resp)[:120]

        # ── Step 2: Shop each international destination with pkg=00 ─────────
        first_ok_dest = None
        for i, dest in enumerate(INTL_DESTS, start=2):
            s = _step(f'{i}. Shop {dest["label"]} pkg=00',
                      'POST', rate_url, auth_headers,
                      rate_payload('Shop', dest, pkg('00')))
            steps.append(s)
            if s.get('ok') and first_ok_dest is None:
                first_ok_dest = dest

        if first_ok_dest is None:
            fail_steps = [f"{s['step']}: {_err_msg(s)}" for s in steps[1:] if not s.get('ok')]
            return JsonResponse({
                'steps': steps,
                'summary': f'❌ Жоден маршрут не відповів. Можливо акаунт не активований. FAIL: {fail_steps}',
            })

        dest = first_ok_dest
        # ── Step 5: Probe all packaging codes for working destination ────────
        PKG_LABELS = {'02': 'My Packaging', '00': 'Unknown', '21': 'Express Box',
                      '2a': 'Small Exp Box', '2b': 'Med Exp Box', '2c': 'Large Exp Box'}
        first_ok_pkg = None
        step_n = len(INTL_DESTS) + 2
        for pkg_code, pkg_label in PKG_LABELS.items():
            s = _step(f'{step_n}. Shop {dest["label"]} pkg={pkg_code} ({pkg_label})',
                      'POST', rate_url, auth_headers,
                      rate_payload('Shop', dest, pkg(pkg_code)))
            steps.append(s)
            step_n += 1
            if s.get('ok') and first_ok_pkg is None:
                first_ok_pkg = pkg_code

        # ── Final steps: Rate individual services ────────────────────────────
        test_pkg = first_ok_pkg or '00'
        for svc_code, svc_label in [('07', 'Worldwide Express'), ('65', 'Worldwide Saver'), ('11', 'Standard')]:
            s = _step(f'{step_n}. Rate svc={svc_code} ({svc_label}) {dest["label"]} pkg={test_pkg}',
                      'POST', rate_url_single, auth_headers,
                      rate_payload('Rate', dest, pkg(test_pkg), svc=svc_code))
            steps.append(s)
            step_n += 1

        ok_steps   = [s['step'] for s in steps if s.get('ok')]
        fail_steps = [f"{s['step']}: {_err_msg(s)}" for s in steps if not s.get('ok')]
        working_pkg = f'✅ Working pkg: {first_ok_pkg}' if first_ok_pkg else '⚠️ pkg=00 used as fallback'
        return JsonResponse({
            'steps': steps,
            'working_route': dest['label'],
            'working_packaging_code': first_ok_pkg,
            'summary': f'{working_pkg} | Route: {dest["label"]} | OK: {ok_steps} | FAIL: {fail_steps}',
        })


# ── ShipmentPackage Inline ────────────────────────────────────────────────────

class ShipmentPackageInline(admin.TabularInline):
    model               = ShipmentPackage
    extra               = 0
    min_num             = 0
    can_delete          = True
    verbose_name        = "Коробка"
    verbose_name_plural = "📦 Коробки (multi-package)"
    fields              = ("weight_kg", "length_cm", "width_cm", "height_cm", "quantity")


# ── Shipment Admin ────────────────────────────────────────────────────────────

@admin.register(Shipment)
class ShipmentAdmin(AuditableMixin, admin.ModelAdmin):
    inlines = [ShipmentPackageInline]
    list_display  = (
        "id_badge", "order_link", "carrier_badge", "status_badge",
        "recipient_name", "recipient_country", "weight_kg",
        "tracking_badge", "label_badge", "created_at_fmt",
    )
    list_filter   = ("status", "carrier", "carrier__carrier_type", "recipient_country")
    search_fields = ("order__order_number", "recipient_name", "tracking_number",
                     "carrier_shipment_id")
    readonly_fields = (
        "carrier_shipment_id", "tracking_number", "label_url",
        "carrier_price", "carrier_currency", "carrier_service",
        "selected_tariff_id", "jumingo_order_number",
        "raw_request", "raw_response", "error_message",
        "submitted_at", "created_at",
        "order_detail_panel", "action_buttons",
        "customs_articles_panel",
    )
    ordering = ["-created_at"]

    fieldsets = (
        ("📦 Замовлення", {
            "fields": ("order", "carrier", "status", "order_detail_panel", "action_buttons")
        }),
        ("📬 Отримувач", {
            "fields": (
                ("recipient_company", "recipient_name"),
                "recipient_street",
                ("recipient_city", "recipient_zip", "recipient_state", "recipient_country"),
                ("recipient_phone", "recipient_email"),
            ),
            "description": "<b>Компанія</b> — юридична назва (DRONISOS). "
                           "<b>Контактна особа</b> — ім'я людини (FABIEN SANTOS).",
        }),
        ("📐 Параметри посилки", {
            "fields": (
                "weight_kg",
                ("length_cm", "width_cm", "height_cm"),
                "description",
                "export_reason",
                ("declared_value", "declared_currency"),
                "insurance_type",
                "reference",
            )
        }),
        ("🚚 Результат від перевізника", {
            "fields": (
                "carrier_shipment_id", "tracking_number",
                "label_url", "carrier_service",
                ("carrier_price", "carrier_currency"),
                ("selected_tariff_id", "jumingo_order_number"),
                "error_message",
            )
        }),
        ("🛃 Митна декларація", {
            "fields": ("customs_articles_panel",),
            "classes": ("collapse",),
            "description": "Артикули що передаються до Jumingo для позаєвропейських відправлень.",
        }),
        ("📋 Технічні деталі", {
            "fields": ("raw_request", "raw_response", "submitted_at", "created_at"),
            "classes": ("collapse",),
        }),
    )

    change_list_template = "admin/shipping/shipment_changelist.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "compare-rates/",
                self.admin_site.admin_view(self.compare_rates_view),
                name="shipping_compare_rates",
            ),
            path(
                "create/<int:order_id>/",
                self.admin_site.admin_view(self.create_from_order_view),
                name="shipping_shipment_create",
            ),
            path(
                "<int:shipment_id>/submit/",
                self.admin_site.admin_view(self.submit_view),
                name="shipping_shipment_submit",
            ),
            path(
                "<int:shipment_id>/rates/",
                self.admin_site.admin_view(self.rates_view),
                name="shipping_shipment_rates",
            ),
            path(
                "<int:shipment_id>/track/",
                self.admin_site.admin_view(self.track_view),
                name="shipping_shipment_track",
            ),
            path(
                "<int:shipment_id>/select-tariff/",
                self.admin_site.admin_view(self.select_tariff_view),
                name="shipping_shipment_select_tariff",
            ),
            path(
                "<int:shipment_id>/book/",
                self.admin_site.admin_view(self.book_view),
                name="shipping_shipment_book",
            ),
            path(
                "<int:shipment_id>/dhl-rates/",
                self.admin_site.admin_view(self.dhl_rates_view),
                name="shipping_shipment_dhl_rates",
            ),
            path(
                "<int:shipment_id>/dhl-track/",
                self.admin_site.admin_view(self.dhl_track_view),
                name="shipping_shipment_dhl_track",
            ),
            path(
                "<int:shipment_id>/dhl-book/",
                self.admin_site.admin_view(self.dhl_book_view),
                name="shipping_shipment_dhl_book",
            ),
            path(
                "<int:shipment_id>/edit-draft/",
                self.admin_site.admin_view(self.edit_draft_view),
                name="shipping_shipment_edit_draft",
            ),
            path(
                "<int:shipment_id>/detail/",
                self.admin_site.admin_view(self.shipment_detail_view),
                name="shipping_shipment_detail",
            ),
            path(
                "<int:shipment_id>/set-status/",
                self.admin_site.admin_view(self.set_status_view),
                name="shipping_shipment_set_status",
            ),
            path(
                "dhl-track-lookup/",
                self.admin_site.admin_view(self.dhl_track_lookup_view),
                name="shipping_dhl_track_lookup",
            ),
            path(
                "ups-track-lookup/",
                self.admin_site.admin_view(self.ups_track_lookup_view),
                name="shipping_ups_track_lookup",
            ),
            path(
                "<int:shipment_id>/dhl-cancel/",
                self.admin_site.admin_view(self.dhl_cancel_view),
                name="shipping_shipment_dhl_cancel",
            ),
            path(
                "<int:shipment_id>/jumingo-cancel/",
                self.admin_site.admin_view(self.jumingo_cancel_view),
                name="shipping_shipment_jumingo_cancel",
            ),
            path(
                "<int:shipment_id>/jumingo-confirm/",
                self.admin_site.admin_view(self.jumingo_confirm_view),
                name="shipping_shipment_jumingo_confirm",
            ),
            path(
                "<int:shipment_id>/set-tracking/",
                self.admin_site.admin_view(self.set_tracking_view),
                name="shipping_shipment_set_tracking",
            ),
            path(
                "<int:shipment_id>/set-jumingo-id/",
                self.admin_site.admin_view(self.set_jumingo_id_view),
                name="shipping_shipment_set_jumingo_id",
            ),
            path(
                "<int:shipment_id>/clone/",
                self.admin_site.admin_view(self.clone_view),
                name="shipping_shipment_clone",
            ),
            path(
                "order-tracking/",
                self.admin_site.admin_view(self.order_tracking_view),
                name="shipping_order_tracking",
            ),
            path(
                "order-tracking/refresh/<int:order_id>/",
                self.admin_site.admin_view(self.refresh_order_tracking_view),
                name="shipping_order_tracking_refresh",
            ),
            # UPS
            path(
                "<int:shipment_id>/ups-rates/",
                self.admin_site.admin_view(self.ups_rates_view),
                name="shipping_shipment_ups_rates",
            ),
            path(
                "<int:shipment_id>/ups-book/",
                self.admin_site.admin_view(self.ups_book_view),
                name="shipping_shipment_ups_book",
            ),
            path(
                "<int:shipment_id>/ups-confirm/",
                self.admin_site.admin_view(self.ups_confirm_view),
                name="shipping_shipment_ups_confirm",
            ),
            path(
                "<int:shipment_id>/ups-track/",
                self.admin_site.admin_view(self.ups_track_view),
                name="shipping_shipment_ups_track",
            ),
            path(
                "<int:shipment_id>/ups-void/",
                self.admin_site.admin_view(self.ups_void_view),
                name="shipping_shipment_ups_void",
            ),
        ]
        return custom + urls

    def change_view(self, request, object_id, form_url='', extra_context=None):
        """Route shipment change requests to custom views based on status."""
        if request.method == 'GET':
            try:
                shipment = Shipment.objects.get(pk=object_id)
                if shipment.status in (Shipment.Status.DRAFT, Shipment.Status.ERROR):
                    return redirect(
                        reverse("admin:shipping_shipment_edit_draft", args=[shipment.pk])
                    )
                return redirect(
                    reverse("admin:shipping_shipment_detail", args=[shipment.pk])
                )
            except Shipment.DoesNotExist:
                pass
        return super().change_view(request, object_id, form_url, extra_context)

    # ── Форма створення ───────────────────────────────────────────────────────

    def create_from_order_view(self, request, order_id):
        from sales.models import SalesOrder
        from .services.jumingo import build_customs_articles, JumingoService

        order = get_object_or_404(SalesOrder, pk=order_id)

        carrier = (
            Carrier.objects.filter(is_active=True, carrier_type="jumingo").first()
            or Carrier.objects.filter(is_active=True, is_default=True).first()
            or Carrier.objects.filter(is_active=True).first()
        )

        shipment = Shipment(order=order, carrier=carrier)
        shipment.copy_from_order()
        packaging_hint = self._fill_packaging_from_order(shipment)
        if not shipment.description:
            shipment.description = self._build_description_from_order(order)
        # Авто-заповнення задекларованої вартості з рядків замовлення
        if not shipment.declared_value:
            from decimal import Decimal, InvalidOperation
            try:
                total = order.order_total()
                if total:
                    shipment.declared_value    = Decimal(str(total)).quantize(Decimal("0.01"))
                    shipment.declared_currency = (order.currency or "EUR").upper()[:3]
            except (InvalidOperation, TypeError, ValueError):
                pass

        # Попереднє заповнення митної декларації
        sender_country = carrier.sender_country if carrier else "DE"
        default_currency = shipment.declared_currency or order.currency or "EUR"
        customs_articles = build_customs_articles(order, sender_country, default_currency)

        eu_countries = JumingoService._EU_COUNTRIES
        needs_customs = bool(
            shipment.recipient_country
            and shipment.recipient_country.upper() not in eu_countries
        )

        from django.db.models import Case, When, IntegerField
        carriers = Carrier.objects.filter(is_active=True).order_by(
            Case(When(carrier_type="jumingo", then=0), default=1, output_field=IntegerField()),
            "-is_default", "name",
        )

        if request.method == "POST":
            return self._handle_create_post(request, order, carriers)

        n_articles = len(customs_articles) or 1
        default_ca_weight = round(float(shipment.weight_kg or 1) / n_articles, 3)

        import json as _json
        pkg_rows = (packaging_hint or {}).get("pkg_rows") or []
        # Авто-активувати multi-package: якщо є кілька типів коробок АБО qty > 1
        pkg_auto = len(pkg_rows) > 1 or (len(pkg_rows) == 1 and pkg_rows[0]["quantity"] > 1)

        carriers_sender = {
            str(c.pk): {
                "name":    c.sender_name or c.sender_company or "",
                "company": c.sender_company or "",
                "street":  c.sender_street or "",
                "city":    c.sender_city or "",
                "zip":     c.sender_zip or "",
                "country": c.sender_country or "DE",
                "phone":   c.sender_phone or "",
                "email":   c.sender_email or "",
            }
            for c in carriers
        } if carriers else {}

        return render(request, "admin/shipping/create_shipment.html", {
            **self.admin_site.each_context(request),
            "order":               order,
            "shipment":            shipment,
            "carriers":            carriers,
            "carriers_sender_json": _json.dumps(carriers_sender),
            "packaging_hint":      packaging_hint,
            "customs_articles":    customs_articles,
            "needs_customs":       needs_customs,
            "eu_countries_js":     ",".join(sorted(eu_countries)),
            "default_ca_weight":   default_ca_weight,
            "title":               f"Нове відправлення — {order.order_number}",
            "pkg_rows_json":       _json.dumps(pkg_rows),
            "pkg_auto":            pkg_auto,
        })

    def _fill_packaging_from_order(self, shipment):
        from decimal import Decimal

        MIN_WEIGHT_KG = Decimal("0.1")
        order = shipment.order

        # ── Пріоритет 1: OrderPackaging (фактична упаковка вже вказана) ─────────
        ops = list(
            OrderPackaging.objects
            .filter(order=order)
            .select_related('packaging')
            .order_by('created_at')
        )
        if ops and ops[0].packaging:
            op = ops[0]
            raw_g     = op.actual_weight_g or 0
            raw_kg    = Decimal(raw_g) / 1000 if raw_g else Decimal(0)
            clamped   = raw_kg > 0 and raw_kg < MIN_WEIGHT_KG
            weight_kg = max(MIN_WEIGHT_KG, round(raw_kg, 3)) if raw_kg > 0 else MIN_WEIGHT_KG
            shipment.weight_kg = weight_kg
            shipment.length_cm = op.packaging.length_cm
            shipment.width_cm  = op.packaging.width_cm
            shipment.height_cm = op.packaging.height_cm

            # Будуємо список рядків для multi-package
            pkg_rows = []
            for o in ops:
                if not o.packaging:
                    continue
                qty = max(1, o.qty_boxes or 1)
                # Вага на коробку: actual_weight_g / qty_boxes або tare
                if o.actual_weight_g and qty:
                    per_box_kg = max(MIN_WEIGHT_KG,
                                     Decimal(str(round(o.actual_weight_g / qty / 1000, 3))))
                else:
                    tare = Decimal(str(o.packaging.tare_weight_kg or 0))
                    per_box_kg = max(MIN_WEIGHT_KG, tare) if tare > 0 else MIN_WEIGHT_KG
                pkg_rows.append({
                    "weight_kg": float(per_box_kg),
                    "length_cm": float(o.packaging.length_cm),
                    "width_cm":  float(o.packaging.width_cm),
                    "height_cm": float(o.packaging.height_cm),
                    "quantity":  qty,
                })

            return {
                "box":            op.packaging,
                "total_boxes":    op.qty_boxes,
                "weight_g":       raw_g,
                "weight_kg":      weight_kg,
                "missing_weight": raw_g == 0,
                "no_packaging":   False,
                "clamped":        clamped,
                "source":         "order_packaging",
                "pkg_rows":       pkg_rows,
            }

        # ── Пріоритет 2: ProductPackaging + net_weight_g (з інвентарю) ──────────
        lines = list(order.lines.select_related('product').all())
        if not lines:
            return None

        total_weight_g = 0
        total_boxes    = 0
        best_box       = None
        missing_weight = False
        no_packaging   = False

        for line in lines:
            product = line.product
            if not product:
                continue

            rec = (
                ProductPackaging.objects
                .filter(product=product, is_default=True)
                .select_related('packaging')
                .first()
            )

            if rec:
                boxes_needed = max(1, -(-line.qty // rec.qty_per_box))
                total_boxes += boxes_needed
                if rec.estimated_weight_g:
                    total_weight_g += rec.estimated_weight_g * boxes_needed
                elif getattr(product, 'net_weight_g', None):
                    total_weight_g += (
                        product.net_weight_g * line.qty
                        + int(rec.packaging.tare_weight_kg * 1000) * boxes_needed
                    )
                else:
                    missing_weight = True
                if best_box is None:
                    best_box = rec.packaging
            else:
                no_packaging = True
                nw = getattr(product, 'net_weight_g', None)
                if nw:
                    total_weight_g += nw * line.qty
                else:
                    missing_weight = True

        if total_weight_g > 0 or best_box:
            raw_kg    = Decimal(total_weight_g) / 1000 if total_weight_g > 0 else Decimal(0)
            clamped   = raw_kg > 0 and raw_kg < MIN_WEIGHT_KG
            weight_kg = max(MIN_WEIGHT_KG, round(raw_kg, 3)) if raw_kg > 0 else None
            if weight_kg:
                shipment.weight_kg = weight_kg
            if best_box:
                shipment.length_cm = best_box.length_cm
                shipment.width_cm  = best_box.width_cm
                shipment.height_cm = best_box.height_cm

            return {
                "box":            best_box,
                "total_boxes":    max(1, total_boxes) if total_boxes else 1,
                "weight_g":       total_weight_g,
                "weight_kg":      weight_kg,
                "missing_weight": missing_weight,
                "no_packaging":   no_packaging,
                "clamped":        clamped,
                "source":         "product_packaging",
            }
        return None

    def _build_description_from_order(self, order):
        from inventory.models import ProductCategory
        slugs = set()
        for line in order.lines.select_related('product').all():
            if line.product and line.product.category:
                slugs.add(line.product.category)
        if not slugs:
            return ""
        cat_map = dict(ProductCategory.objects.filter(slug__in=slugs).values_list('slug', 'name'))
        return ", ".join(cat_map.get(s, s) for s in sorted(slugs))

    def _handle_create_post(self, request, order, carriers):
        from decimal import Decimal, InvalidOperation

        MIN_WEIGHT = Decimal("0.1")

        carrier_id = request.POST.get("carrier")
        carrier = get_object_or_404(Carrier, pk=carrier_id) if carrier_id else None

        # Вага — мінімум 0.1 кг
        try:
            weight_kg = max(MIN_WEIGHT, Decimal(request.POST.get("weight_kg") or 1))
        except (InvalidOperation, TypeError):
            weight_kg = MIN_WEIGHT

        shipment = Shipment(
            order=order,
            carrier=carrier,
            status=Shipment.Status.DRAFT,
            sender_name    = request.POST.get("sender_name", ""),
            sender_company = request.POST.get("sender_company", ""),
            sender_street  = request.POST.get("sender_street", ""),
            sender_city    = request.POST.get("sender_city", ""),
            sender_zip     = request.POST.get("sender_zip", ""),
            sender_country = request.POST.get("sender_country", "").upper()[:2],
            sender_phone   = request.POST.get("sender_phone", ""),
            sender_state   = request.POST.get("sender_state", ""),
            sender_email   = request.POST.get("sender_email", ""),
            recipient_name    = request.POST.get("recipient_name", ""),
            recipient_company = request.POST.get("recipient_company", ""),
            recipient_street  = request.POST.get("recipient_street", ""),
            recipient_city    = request.POST.get("recipient_city", ""),
            recipient_zip     = request.POST.get("recipient_zip", ""),
            recipient_state   = request.POST.get("recipient_state", ""),
            recipient_country = request.POST.get("recipient_country", ""),
            recipient_phone   = request.POST.get("recipient_phone", ""),
            recipient_email   = request.POST.get("recipient_email", ""),
            weight_kg         = weight_kg,
            length_cm         = request.POST.get("length_cm") or None,
            width_cm          = request.POST.get("width_cm") or None,
            height_cm         = request.POST.get("height_cm") or None,
            description       = request.POST.get("description", ""),
            export_reason     = request.POST.get("export_reason", "Commercial"),
            declared_value    = request.POST.get("declared_value") or None,
            declared_currency = request.POST.get("declared_currency", "EUR"),
            insurance_type    = request.POST.get("insurance_type", "none"),
            reference         = request.POST.get("reference", order.order_number),
            created_by        = request.user,
        )
        # Парсимо митну декларацію з форми
        ca_descs  = request.POST.getlist("ca_desc")
        if ca_descs:
            ca_qtys     = request.POST.getlist("ca_qty")
            ca_hscodes  = request.POST.getlist("ca_hs")
            ca_origins  = request.POST.getlist("ca_origin")
            ca_weights  = request.POST.getlist("ca_weight")
            ca_values   = request.POST.getlist("ca_value")
            ca_curs     = request.POST.getlist("ca_currency")

            customs_items = []
            for i, desc in enumerate(ca_descs):
                if not desc.strip():
                    continue
                def _get(lst, idx, default=""):
                    return lst[idx] if idx < len(lst) else default
                item = {
                    "description":    desc.strip()[:35],
                    "quantity":       max(1, int(float(_get(ca_qtys, i, "1") or 1))),
                    "value":          round(float(_get(ca_values, i, "0") or 0), 2),
                    "currency":       (_get(ca_curs, i, "EUR") or "EUR").upper()[:3],
                    "origin_country": (_get(ca_origins, i, "DE") or "DE").upper()[:2],
                    "customs_number": _get(ca_hscodes, i, "").strip(),
                }
                w = _get(ca_weights, i, "")
                if w:
                    try:
                        item["weight"] = round(float(w), 3)
                    except ValueError:
                        pass
                customs_items.append(item)

            if customs_items:
                shipment.customs_articles = {
                    "type":               request.POST.get("customs_invoice_type", "commercial"),
                    "customs_line_items": customs_items,
                }

        shipment.save()

        # ── Multi-package: зберігаємо ShipmentPackage рядки якщо є ──────────
        pkg_weights = request.POST.getlist("pkg_weight[]")
        if pkg_weights:
            from decimal import Decimal as _D, InvalidOperation as _IE
            pkg_lengths = request.POST.getlist("pkg_length[]")
            pkg_widths  = request.POST.getlist("pkg_width[]")
            pkg_heights = request.POST.getlist("pkg_height[]")
            pkg_qtys    = request.POST.getlist("pkg_qty[]")
            def _dec(lst, i, default):
                try:
                    return max(_D(default), _D(str(lst[i]).strip()))
                except (IndexError, _IE, ValueError):
                    return _D(default)
            def _int(lst, i):
                try:
                    return max(1, int(lst[i]))
                except (IndexError, ValueError, TypeError):
                    return 1
            for i, w_raw in enumerate(pkg_weights):
                try:
                    w = max(_D("0.1"), _D(str(w_raw).strip()))
                except (_IE, ValueError):
                    continue
                ShipmentPackage.objects.create(
                    shipment  = shipment,
                    weight_kg = w,
                    length_cm = _dec(pkg_lengths, i, "30"),
                    width_cm  = _dec(pkg_widths,  i, "20"),
                    height_cm = _dec(pkg_heights, i, "15"),
                    quantity  = _int(pkg_qtys, i),
                )

        action = request.POST.get("action_btn", "save")
        if action == "submit" and carrier:
            return redirect(reverse("admin:shipping_shipment_submit", args=[shipment.pk]))

        if action == "jumingo_tariff" and carrier:
            from urllib.parse import urlencode
            preview_id   = request.POST.get("jumingo_preview_id", "").strip()
            tariff_id    = request.POST.get("jumingo_tariff_id", "").strip()
            tariff_name  = request.POST.get("jumingo_tariff_name", "").strip()
            tariff_price = request.POST.get("jumingo_tariff_price", "0").strip()
            shipper_name = request.POST.get("jumingo_shipper", "").strip()
            if preview_id and tariff_id:
                shipment.carrier_shipment_id = preview_id
                shipment.status              = Shipment.Status.SUBMITTED
                shipment.submitted_at        = timezone.now()
                shipment.save(update_fields=[
                    "carrier_shipment_id", "status", "submitted_at"
                ])
                qs = urlencode({"tariff_id": tariff_id, "name": tariff_name,
                                "price": tariff_price, "shipper": shipper_name})
                messages.success(request,
                    f"✅ Відправлення #{shipment.pk} збережено (Jumingo ID: {preview_id[:12]}…)")
                return redirect(
                    reverse("admin:shipping_shipment_select_tariff", args=[shipment.pk])
                    + f"?{qs}"
                )

        if action == "dhl_book" and carrier:
            from urllib.parse import urlencode
            dhl_code  = request.POST.get("dhl_product_code", "").strip()
            dhl_name  = request.POST.get("dhl_product_name", dhl_code).strip()
            dhl_price = request.POST.get("dhl_price", "0").strip()
            if dhl_code:
                qs = urlencode({"product_code": dhl_code,
                                "product_name": dhl_name,
                                "price":        dhl_price})
                messages.success(request, f"✅ Відправлення #{shipment.pk} збережено. Оформлення DHL…")
                return redirect(
                    reverse("admin:shipping_shipment_dhl_book", args=[shipment.pk]) + f"?{qs}"
                )

        if action == "ups_book" and carrier:
            ups_code = request.POST.get("ups_service_code", "11").strip() or "11"
            messages.success(request, f"✅ Відправлення #{shipment.pk} збережено. Перевірте дані перед відправкою…")
            return redirect(
                reverse("admin:shipping_shipment_ups_confirm", args=[shipment.pk])
                + f"?service_code={ups_code}"
            )

        messages.success(request, f"✅ Відправлення #{shipment.pk} збережено як чернетку.")
        return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

    # ── Редагування чернетки ─────────────────────────────────────────────────

    def edit_draft_view(self, request, shipment_id):
        from .services.jumingo import build_customs_articles, JumingoService

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        order    = shipment.order
        carriers = Carrier.objects.filter(is_active=True)

        if request.method == "POST":
            return self._handle_edit_post(request, shipment, carriers)

        # Авто-заповнення задекларованої вартості якщо поле порожнє
        if not shipment.declared_value:
            from decimal import Decimal, InvalidOperation
            try:
                total = order.order_total()
                if total:
                    shipment.declared_value    = Decimal(str(total)).quantize(Decimal("0.01"))
                    shipment.declared_currency = (order.currency or shipment.declared_currency or "EUR").upper()[:3]
            except (InvalidOperation, TypeError, ValueError):
                pass

        # Митна декларація — з наявних даних або будуємо з замовлення
        existing_ca   = (shipment.customs_articles or {}).get("customs_line_items") or []
        inv_type      = (shipment.customs_articles or {}).get("type", "commercial")
        if existing_ca:
            customs_articles = existing_ca
        else:
            sender_country   = shipment.carrier.sender_country if shipment.carrier else "DE"
            default_currency = shipment.declared_currency or getattr(order, "currency", None) or "EUR"
            customs_articles = build_customs_articles(order, sender_country, default_currency)

        eu_countries  = JumingoService._EU_COUNTRIES
        needs_customs = bool(
            shipment.recipient_country
            and shipment.recipient_country.upper() not in eu_countries
        )

        is_error = shipment.status == Shipment.Status.ERROR

        n_articles = len(customs_articles) or 1
        default_ca_weight = round(float(shipment.weight_kg or 1) / n_articles, 3)

        import json as _json2
        carriers_sender_edit = {
            str(c.pk): {
                "name":    c.sender_name or c.sender_company or "",
                "company": c.sender_company or "",
                "street":  c.sender_street or "",
                "city":    c.sender_city or "",
                "zip":     c.sender_zip or "",
                "country": c.sender_country or "DE",
                "phone":   c.sender_phone or "",
                "email":   c.sender_email or "",
            }
            for c in carriers
        }

        return render(request, "admin/shipping/create_shipment.html", {
            **self.admin_site.each_context(request),
            "order":                order,
            "shipment":             shipment,
            "carriers":             carriers,
            "carriers_sender_json": _json2.dumps(carriers_sender_edit),
            "packaging_hint":       None,
            "customs_articles":     customs_articles,
            "customs_invoice_type": inv_type,
            "needs_customs":        needs_customs,
            "eu_countries_js":      ",".join(sorted(eu_countries)),
            "default_ca_weight":    default_ca_weight,
            "is_edit":              True,
            "is_error":             is_error,
            "error_message":        shipment.error_message if is_error else "",
            "form_action":          reverse("admin:shipping_shipment_edit_draft", args=[shipment.pk]),
            "cancel_url":           reverse("admin:shipping_shipment_changelist"),
            "title":                f"{'Виправити помилку' if is_error else 'Редагувати чернетку'} #{shipment.pk}",
        })

    def _handle_edit_post(self, request, shipment, carriers):
        from decimal import Decimal, InvalidOperation

        MIN_WEIGHT = Decimal("0.1")

        carrier_id = request.POST.get("carrier")
        carrier    = get_object_or_404(Carrier, pk=carrier_id) if carrier_id else None

        try:
            weight_kg = max(MIN_WEIGHT, Decimal(request.POST.get("weight_kg") or 1))
        except (InvalidOperation, TypeError):
            weight_kg = MIN_WEIGHT

        # Скидаємо статус ERROR → DRAFT щоб дозволити повторне відправлення
        if shipment.status == Shipment.Status.ERROR:
            shipment.status        = Shipment.Status.DRAFT
            shipment.error_message = ""

        shipment.carrier       = carrier
        shipment.sender_name    = request.POST.get("sender_name", "")
        shipment.sender_company = request.POST.get("sender_company", "")
        shipment.sender_street  = request.POST.get("sender_street", "")
        shipment.sender_city    = request.POST.get("sender_city", "")
        shipment.sender_zip     = request.POST.get("sender_zip", "")
        shipment.sender_country = request.POST.get("sender_country", "").upper()[:2]
        shipment.sender_phone   = request.POST.get("sender_phone", "")
        shipment.sender_state   = request.POST.get("sender_state", "")
        shipment.sender_email   = request.POST.get("sender_email", "")
        shipment.recipient_name    = request.POST.get("recipient_name", "")
        shipment.recipient_company = request.POST.get("recipient_company", "")
        shipment.recipient_street  = request.POST.get("recipient_street", "")
        shipment.recipient_city    = request.POST.get("recipient_city", "")
        shipment.recipient_zip     = request.POST.get("recipient_zip", "")
        shipment.recipient_state   = request.POST.get("recipient_state", "")
        shipment.recipient_country = request.POST.get("recipient_country", "")
        shipment.recipient_phone   = request.POST.get("recipient_phone", "")
        shipment.recipient_email   = request.POST.get("recipient_email", "")
        shipment.weight_kg         = weight_kg
        shipment.length_cm         = request.POST.get("length_cm") or None
        shipment.width_cm          = request.POST.get("width_cm") or None
        shipment.height_cm         = request.POST.get("height_cm") or None
        shipment.description       = request.POST.get("description", "")
        shipment.export_reason     = request.POST.get("export_reason", "Commercial")
        shipment.declared_value    = request.POST.get("declared_value") or None
        shipment.declared_currency = request.POST.get("declared_currency", "EUR")
        shipment.insurance_type    = request.POST.get("insurance_type", "none")
        shipment.reference         = request.POST.get("reference", shipment.order.order_number)

        ca_descs = request.POST.getlist("ca_desc")
        if ca_descs:
            ca_qtys    = request.POST.getlist("ca_qty")
            ca_hscodes = request.POST.getlist("ca_hs")
            ca_origins = request.POST.getlist("ca_origin")
            ca_weights = request.POST.getlist("ca_weight")
            ca_values  = request.POST.getlist("ca_value")
            ca_curs    = request.POST.getlist("ca_currency")

            customs_items = []
            for i, desc in enumerate(ca_descs):
                if not desc.strip():
                    continue
                def _get(lst, idx, default=""):
                    return lst[idx] if idx < len(lst) else default
                item = {
                    "description":    desc.strip()[:35],
                    "quantity":       max(1, int(float(_get(ca_qtys, i, "1") or 1))),
                    "value":          round(float(_get(ca_values, i, "0") or 0), 2),
                    "currency":       (_get(ca_curs, i, "EUR") or "EUR").upper()[:3],
                    "origin_country": (_get(ca_origins, i, "DE") or "DE").upper()[:2],
                    "customs_number": _get(ca_hscodes, i, "").strip(),
                }
                w = _get(ca_weights, i, "")
                if w:
                    try:
                        item["weight"] = round(float(w), 3)
                    except ValueError:
                        pass
                customs_items.append(item)

            if customs_items:
                shipment.customs_articles = {
                    "type":               request.POST.get("customs_invoice_type", "commercial"),
                    "customs_line_items": customs_items,
                }

        shipment.save()

        action = request.POST.get("action_btn", "save")

        if action == "submit" and carrier:
            return redirect(reverse("admin:shipping_shipment_submit", args=[shipment.pk]))

        if action == "jumingo_tariff" and carrier:
            from urllib.parse import urlencode
            preview_id   = request.POST.get("jumingo_preview_id", "").strip()
            tariff_id    = request.POST.get("jumingo_tariff_id", "").strip()
            tariff_name  = request.POST.get("jumingo_tariff_name", "").strip()
            tariff_price = request.POST.get("jumingo_tariff_price", "0").strip()
            shipper_name = request.POST.get("jumingo_shipper", "").strip()
            if preview_id and tariff_id:
                shipment.carrier_shipment_id = preview_id
                shipment.status              = Shipment.Status.SUBMITTED
                shipment.submitted_at        = timezone.now()
                shipment.save(update_fields=["carrier_shipment_id", "status", "submitted_at"])
                qs = urlencode({"tariff_id": tariff_id, "name": tariff_name,
                                "price": tariff_price, "shipper": shipper_name})
                messages.success(request,
                    f"✅ Відправлення #{shipment.pk} оновлено (Jumingo ID: {preview_id[:12]}…)")
                return redirect(
                    reverse("admin:shipping_shipment_select_tariff", args=[shipment.pk])
                    + f"?{qs}"
                )

        if action == "dhl_book" and carrier:
            from urllib.parse import urlencode
            dhl_code  = request.POST.get("dhl_product_code", "").strip()
            dhl_name  = request.POST.get("dhl_product_name", dhl_code).strip()
            dhl_price = request.POST.get("dhl_price", "0").strip()
            if dhl_code:
                qs = urlencode({"product_code": dhl_code,
                                "product_name": dhl_name,
                                "price":        dhl_price})
                messages.success(request, f"✅ Відправлення #{shipment.pk} оновлено. Оформлення DHL…")
                return redirect(
                    reverse("admin:shipping_shipment_dhl_book", args=[shipment.pk]) + f"?{qs}"
                )

        if action == "ups_book" and carrier:
            ups_code = request.POST.get("ups_service_code", "11").strip() or "11"
            messages.success(request, f"✅ Відправлення #{shipment.pk} оновлено. Перевірте дані перед відправкою…")
            return redirect(
                reverse("admin:shipping_shipment_ups_confirm", args=[shipment.pk])
                + f"?service_code={ups_code}"
            )

        messages.success(request, f"✅ Відправлення #{shipment.pk} збережено.")
        return redirect(
            reverse("admin:shipping_shipment_edit_draft", args=[shipment.pk])
        )

    # ── Деталі відправлення (read-only) ──────────────────────────────────────

    def shipment_detail_view(self, request, shipment_id):
        import json as _json
        from .services.jumingo import JUMINGO_APP_URL

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        order    = shipment.order

        # ── Таймлайн ──────────────────────────────────────────────────────────
        STEPS = [
            ("draft",       "Чернетка"),
            ("submitted",   "Надіслано"),
            ("label_ready", "Етикетка"),
            ("in_transit",  "В дорозі"),
            ("delivered",   "Доставлено"),
        ]
        idx_map     = {s: i for i, (s, _) in enumerate(STEPS)}
        current_idx = idx_map.get(shipment.status, -1)
        timeline    = []
        for i, (st, lbl) in enumerate(STEPS):
            if i < current_idx:
                state = "done"
            elif i == current_idx:
                state = "active"
            else:
                state = "pending"
            timeline.append({"label": lbl, "state": state, "is_last": i == len(STEPS) - 1})

        show_timeline = shipment.status not in ("error", "cancelled")

        # ── Кнопки дій залежно від статусу ────────────────────────────────────
        has_dhl = Carrier.objects.filter(carrier_type="dhl", is_active=True).exists()
        actions = []
        st = shipment.status

        if st == "submitted":
            if shipment.carrier_shipment_id:
                actions += [
                    {"label": "💰 Тарифи Jumingo",
                     "url": reverse("admin:shipping_shipment_rates", args=[shipment.pk]),
                     "cls": "blue"},
                    {"label": "🔄 Оновити статус",
                     "url": reverse("admin:shipping_shipment_track", args=[shipment.pk]),
                     "cls": "orange"},
                    {"label": "🔗 Відкрити Jumingo",
                     "url": f"{JUMINGO_APP_URL}/de-de/shipments/",
                     "cls": "pink", "external": True},
                    {"label": "🗑️ Скасувати відправлення",
                     "url": reverse("admin:shipping_shipment_jumingo_cancel", args=[shipment.pk]),
                     "cls": "red", "confirm": "Скасувати це відправлення на Jumingo і видалити з системи?"},
                ]
            if has_dhl:
                actions.append({"label": "🟡 DHL Тарифи",
                                 "url": reverse("admin:shipping_shipment_dhl_rates", args=[shipment.pk]),
                                 "cls": "yellow"})

        elif st in ("label_ready", "in_transit"):
            if shipment.carrier_shipment_id:
                actions.append({"label": "🔄 Оновити трекінг",
                                 "url": reverse("admin:shipping_shipment_track", args=[shipment.pk]),
                                 "cls": "orange"})
            if shipment.label_url:
                actions.append({"label": "📄 Етикетка PDF",
                                 "url": shipment.label_url,
                                 "cls": "teal", "external": True})
            if shipment.tracking_number and has_dhl:
                actions.append({"label": "📡 DHL Трекінг",
                                 "url": reverse("admin:shipping_shipment_dhl_track", args=[shipment.pk]),
                                 "cls": "yellow"})

        elif st == "delivered":
            if shipment.label_url:
                actions.append({"label": "📄 Етикетка PDF",
                                 "url": shipment.label_url,
                                 "cls": "teal", "external": True})
            if shipment.tracking_number and has_dhl:
                actions.append({"label": "📡 DHL Трекінг",
                                 "url": reverse("admin:shipping_shipment_dhl_track", args=[shipment.pk]),
                                 "cls": "yellow"})

        # Скасовано / Помилка — повторна відправка (клонування в новий DRAFT)
        if st in ("cancelled", "error"):
            actions.append({
                "label":   "🔁 Повторити відправлення",
                "url":     reverse("admin:shipping_shipment_clone", args=[shipment.pk]),
                "cls":     "green",
                "confirm": (
                    f"Створити нове відправлення на основі #{shipment.pk}?\n\n"
                    "Всі дані отримувача, параметри посилки і митна декларація будуть скопійовані. "
                    "Ви зможете відредагувати їх перед відправкою."
                ),
            })

        # Завжди — назад до замовлення
        actions.append({"label": "← Замовлення",
                         "url": reverse("admin:sales_salesorder_change", args=[order.pk]),
                         "cls": "ghost"})

        # ── Статус від перевізника (з raw_response) ───────────────────────────
        carrier_tracking = None
        if shipment.raw_response and isinstance(shipment.raw_response, dict):
            rv           = shipment.raw_response
            tracking_obj = rv.get("tracking") or {}
            progress     = tracking_obj.get("progress") or {}
            prog_class   = progress.get("class", "")
            prog_label   = (progress.get("label") or progress.get("text")
                            or progress.get("description") or "")
            jumingo_status = rv.get("status", "")

            PROGRESS_DISPLAY = {
                "in_system":   ("🏷️", "У системі",     "#00bcd4"),
                "in_transit":  ("🚚", "В дорозі",       "#ff9800"),
                "in_delivery": ("📦", "Доставляється",  "#ff9800"),
                "completed":   ("✅", "Доставлено",      "#4caf50"),
                "exception":   ("⚠️", "Виняток",         "#f44336"),
                "undelivered": ("❌", "Не доставлено",   "#f44336"),
            }
            if prog_class or jumingo_status:
                icon, display, color = PROGRESS_DISPLAY.get(
                    prog_class, ("📦", prog_class or jumingo_status, "#607d8b")
                )
                carrier_tracking = {
                    "class":          prog_class,
                    "icon":           icon,
                    "display":        display,
                    "color":          color,
                    "label":          prog_label,
                    "jumingo_status": jumingo_status,
                }

        # ── Raw API data ───────────────────────────────────────────────────────
        def _to_str(val, limit=4000):
            if val is None:
                return ""
            if isinstance(val, (dict, list)):
                return _json.dumps(val, ensure_ascii=False, indent=2)[:limit]
            return str(val)[:limit]

        # Статуси для ручної зміни (виключаємо DRAFT — для нього є окрема форма)
        status_choices = [
            (v, l) for v, l in Shipment.Status.choices
            if v != Shipment.Status.DRAFT
        ]

        return render(request, "admin/shipping/shipment_detail.html", {
            **self.admin_site.each_context(request),
            "shipment":         shipment,
            "order":            order,
            "timeline":         timeline,
            "show_timeline":    show_timeline,
            "actions":          actions,
            "status_choices":   status_choices,
            "carrier_tracking": carrier_tracking,
            "raw_request":      _to_str(shipment.raw_request),
            "raw_response":     _to_str(shipment.raw_response),
            "title":            f"Відправлення #{shipment.pk} — {shipment.recipient_name}",
        })

    # ── Ручна зміна статусу ───────────────────────────────────────────────────

    def set_status_view(self, request, shipment_id):
        if request.method != "POST":
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment_id]))

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        valid = {v for v, _ in Shipment.Status.choices if v != Shipment.Status.DRAFT}
        new_status = request.POST.get("new_status", "").strip()

        if new_status not in valid:
            messages.error(request, "❌ Невірний статус.")
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

        old_label = shipment.get_status_display()
        shipment.status = new_status
        shipment.save(update_fields=["status"])

        # Синхронізуємо SalesOrder
        order = shipment.order
        order_fields = []
        if new_status == "delivered" and order.status != "delivered":
            order.status = "delivered"
            order_fields.append("status")
            if not order.delivered_at:
                order.delivered_at = timezone.now()
                order_fields.append("delivered_at")
        elif new_status == "in_transit" and order.status in ("received", "processing"):
            order.status = "shipped"
            order_fields.append("status")
            if not order.shipped_at:
                order.shipped_at = timezone.now().date()
                order_fields.append("shipped_at")
        elif new_status == "label_ready" and order.status in ("received", "processing"):
            order.status = "shipped"
            order_fields.append("status")
            if not order.shipped_at:
                order.shipped_at = timezone.now().date()
                order_fields.append("shipped_at")
        if order_fields:
            order.save(update_fields=order_fields)

        new_label = shipment.get_status_display()
        messages.success(
            request,
            f"✅ Статус змінено вручну: {old_label} → {new_label}"
            + (f" · Замовлення {order.order_number} теж оновлено." if order_fields else ""),
        )
        return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

    # ── DHL Cancel ───────────────────────────────────────────────────────────

    def dhl_cancel_view(self, request, shipment_id):
        """DELETE /shipments/{trackingNumber} — скасування відправлення DHL."""
        if request.method != "POST":
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment_id]))

        from .services.dhl import cancel_shipment as dhl_cancel

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        if not shipment.tracking_number:
            messages.error(request, "❌ Трекінг-номер відсутній — нічого скасовувати в DHL.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        dhl_carrier = (
            Carrier.objects
            .filter(carrier_type="dhl", is_active=True)
            .exclude(api_key="")
            .order_by("-is_default")
            .first()
        )
        if not dhl_carrier:
            messages.error(request, "❌ Немає активного DHL перевізника з API ключем.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        result = dhl_cancel(dhl_carrier, shipment.tracking_number)

        if result["success"]:
            shipment.status        = Shipment.Status.CANCELLED
            shipment.error_message = f"Скасовано через DHL API. Трекінг: {shipment.tracking_number}"
            shipment.save(update_fields=["status", "error_message"])
            messages.success(request, f"✅ {result['message']} Відправлення #{shipment.pk} → Скасовано.")
        else:
            messages.error(request, f"❌ DHL API: {result['message']}")
            if result.get("url"):
                messages.info(request, f"URL запиту: {result['url']}")

        return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

    # ── Jumingo Cancel ────────────────────────────────────────────────────────

    def jumingo_cancel_view(self, request, shipment_id):
        """DELETE /v1/shipments/{id} — скасування відправлення Jumingo + видалення з Minerva."""
        if request.method != "POST":
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment_id]))

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        if not shipment.carrier_shipment_id:
            messages.error(request, "❌ Jumingo Shipment ID відсутній — нічого скасовувати.")
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

        from .services.jumingo import JumingoService
        service = JumingoService(shipment.carrier)
        ok = service.delete_shipment(shipment.carrier_shipment_id)

        order = shipment.order
        if ok:
            shipment.status = Shipment.Status.CANCELLED
            shipment.error_message = f"Скасовано вручну. Jumingo ID: {shipment.carrier_shipment_id}"
            shipment.save(update_fields=["status", "error_message"])
            messages.success(
                request,
                f"✅ Відправлення #{shipment.pk} скасовано на Jumingo і позначено як Скасоване."
            )
        else:
            messages.error(
                request,
                f"❌ Jumingo API повернуло помилку для відправлення {shipment.carrier_shipment_id}. "
                f"Оплачені відправлення не можна видалити через API — скасуй вручну на сайті Jumingo."
            )

        if order:
            return redirect(reverse("admin:sales_salesorder_change", args=[order.pk]))
        return redirect(reverse("admin:shipping_shipment_changelist"))

    # ── Set tracking number manually ─────────────────────────────────────────

    def set_tracking_view(self, request, shipment_id):
        """POST — зберігає трекінг-номер вручну і оновлює статус відправлення."""
        if request.method != "POST":
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment_id]))

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        tn = (request.POST.get("tracking_number") or "").strip()

        if not tn:
            messages.error(request, "❌ Трекінг-номер не може бути порожнім.")
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

        update_fields = ["tracking_number"]
        shipment.tracking_number = tn

        # Якщо статус draft/submitted — переводимо в label_ready
        if shipment.status in (Shipment.Status.DRAFT, Shipment.Status.SUBMITTED):
            shipment.status = Shipment.Status.LABEL_READY
            update_fields.append("status")

        shipment.save(update_fields=update_fields)

        # Копіюємо TN на SalesOrder якщо там ще немає
        if shipment.order and not shipment.order.tracking_number:
            shipment.order.tracking_number = tn
            shipment.order.save(update_fields=["tracking_number"])

        messages.success(request, f"✅ Трекінг-номер збережено: {tn}")
        return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

    # ── Set Jumingo shipment ID manually ─────────────────────────────────────

    def set_jumingo_id_view(self, request, shipment_id):
        """POST — зберігає carrier_shipment_id вручну для підключення до Jumingo API."""
        if request.method != "POST":
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment_id]))

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        jumingo_id = (request.POST.get("carrier_shipment_id") or "").strip()

        if not jumingo_id:
            messages.error(request, "❌ Jumingo Shipment ID не може бути порожнім.")
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

        shipment.carrier_shipment_id = jumingo_id
        shipment.save(update_fields=["carrier_shipment_id"])
        messages.success(request, f"✅ Jumingo ID підключено: {jumingo_id}")
        return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

    # ── Клонування відправлення (для повторної відправки після скасування) ────

    def clone_view(self, request, shipment_id):
        """POST — клонує відправлення в новий DRAFT, зберігаючи дані одержувача,
        параметри посилки і митну декларацію. Перенаправляє на редагування нового чернетки."""
        if request.method != "POST":
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment_id]))

        orig = get_object_or_404(Shipment, pk=shipment_id)

        clone = Shipment(
            order             = orig.order,
            carrier           = orig.carrier,
            status            = Shipment.Status.DRAFT,
            # Отримувач
            recipient_name    = orig.recipient_name,
            recipient_company = orig.recipient_company,
            recipient_street  = orig.recipient_street,
            recipient_city    = orig.recipient_city,
            recipient_zip     = orig.recipient_zip,
            recipient_state   = orig.recipient_state,
            recipient_country = orig.recipient_country,
            recipient_phone   = orig.recipient_phone,
            recipient_email   = orig.recipient_email,
            # Параметри посилки
            weight_kg         = orig.weight_kg,
            length_cm         = orig.length_cm,
            width_cm          = orig.width_cm,
            height_cm         = orig.height_cm,
            description       = orig.description,
            export_reason     = orig.export_reason,
            declared_value    = orig.declared_value,
            declared_currency = orig.declared_currency,
            insurance_type    = orig.insurance_type,
            reference         = orig.reference,
            # Митна декларація
            customs_articles  = orig.customs_articles,
            # Автор
            created_by        = request.user,
        )
        clone.save()

        messages.success(
            request,
            f"✅ Створено нове відправлення #{clone.pk} на основі #{orig.pk}. "
            f"Перевірте дані та виправте митну декларацію за потреби."
        )
        return redirect(reverse("admin:shipping_shipment_edit_draft", args=[clone.pk]))

    # ── Jumingo Confirm (preview before API call) ─────────────────────────────

    def jumingo_confirm_view(self, request, shipment_id):
        """GET — показує preview даних перед відправкою на Jumingo API.
           POST — виконує create_shipment() + patch_tariff()."""
        from datetime import date, timedelta
        from .services.jumingo import JumingoService

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        if not shipment.selected_tariff_id:
            messages.error(request, "❌ Спочатку оберіть тариф.")
            return redirect(reverse("admin:shipping_shipment_rates", args=[shipment.pk]))

        service = JumingoService(shipment.carrier)

        # ── POST: виконати відправку ──────────────────────────────────────────
        if request.method == "POST":
            old_preview_id = shipment.carrier_shipment_id
            full_result = service.create_shipment(shipment)

            if full_result.success and full_result.carrier_shipment_id:
                shipment.carrier_shipment_id = full_result.carrier_shipment_id
                shipment.save(update_fields=["carrier_shipment_id"])
                if old_preview_id and old_preview_id != full_result.carrier_shipment_id:
                    service.delete_shipment(old_preview_id)
                if shipment.carrier:
                    request.session.pop(f"jumingo_preview_{shipment.carrier.pk}", None)
            else:
                messages.error(
                    request,
                    f"❌ Jumingo API: {full_result.error_message or 'невідома помилка'}"
                )
                return redirect(reverse("admin:shipping_shipment_jumingo_confirm", args=[shipment.pk]))

            # PATCH тариф — дата/час з форми або наступний робочий день
            pickup_date_str = request.POST.get("pickup_date", "").strip()
            pickup_min_time = request.POST.get("pickup_min_time", "09:00:00").strip() or "09:00:00"
            pickup_max_time = request.POST.get("pickup_max_time", "18:00:00").strip() or "18:00:00"
            # Validate/fallback pickup_date
            try:
                pickup_date = date.fromisoformat(pickup_date_str)
                if pickup_date < date.today():
                    raise ValueError("past date")
            except (ValueError, TypeError):
                pickup_date = date.today() + timedelta(days=1)
                while pickup_date.weekday() >= 5:
                    pickup_date += timedelta(days=1)
            result = service.patch_tariff(
                shipment.carrier_shipment_id,
                shipment.selected_tariff_id,
                pickup_date.strftime("%Y-%m-%d"),
                pickup_min_time=pickup_min_time,
                pickup_max_time=pickup_max_time,
            )
            if not result.get("success"):
                messages.warning(
                    request,
                    f"⚠️ Тариф збережено в Minerva, але PATCH до Jumingo не вдався: "
                    f"{result.get('error', '')}. Оберіть тариф вручну на Jumingo."
                )
            else:
                messages.success(
                    request,
                    f"✅ Відправлення створено на Jumingo: {shipment.carrier_service} "
                    f"— {shipment.carrier_price} EUR"
                )
            return redirect(reverse("admin:shipping_shipment_detail", args=[shipment.pk]))

        # ── GET: показати preview ─────────────────────────────────────────────
        payload = service._build_payload(shipment)
        customs = payload.get("customs_invoice")

        # ── Перевірка відповідності ваги ─────────────────────────────────────
        weight_warn = None
        articles = (shipment.customs_articles or {}).get("customs_line_items") or []
        articles_weight = sum(float(a.get("weight") or 0) for a in articles)
        ship_kg = float(shipment.weight_kg or 0)
        if articles_weight > 0 and ship_kg > 0:
            diff_abs = abs(ship_kg - articles_weight)
            diff_pct = diff_abs / ship_kg * 100
            if diff_abs >= 0.5 or diff_pct >= 20:
                weight_warn = {
                    "shipment_kg":  ship_kg,
                    "articles_kg":  round(articles_weight, 3),
                    "diff_kg":      round(diff_abs, 3),
                    "diff_pct":     round(diff_pct, 1),
                }

        # ── Тип тарифу: shop (везеш сам) vs pickup (кур'єр забирає) ─────────────
        is_shop_tariff = str(shipment.selected_tariff_id or "").startswith("s-")
        default_pickup = date.today() + timedelta(days=1)
        while default_pickup.weekday() >= 5:
            default_pickup += timedelta(days=1)

        return render(request, "admin/shipping/jumingo_confirm.html", {
            **self.admin_site.each_context(request),
            "title":    f"Підтвердити відправлення #{shipment.pk}",
            "shipment": shipment,
            "payload":  payload,
            "from_address":    payload.get("from_address", {}),
            "to_address":      payload.get("to_address", {}),
            "packages":        payload.get("packages", []),
            "details":         payload.get("details", {}),
            "customs":         customs,
            "weight_warn":     weight_warn,
            "is_shop_tariff":  is_shop_tariff,
            "default_pickup":  default_pickup.strftime("%Y-%m-%d"),
            "confirm_url":     reverse("admin:shipping_shipment_jumingo_confirm", args=[shipment.pk]),
            "back_url":        reverse("admin:shipping_shipment_rates", args=[shipment.pk]),
        })

    # ── UPS Views ─────────────────────────────────────────────────────────────

    def ups_rates_view(self, request, shipment_id):
        """Тарифи UPS для конкретного відправлення."""
        from .ups_client import UPSClient, UPSError

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        order    = shipment.order

        try:
            client   = UPSClient()
            to_addr  = self._ups_extract_address(shipment)
            packages = self._ups_extract_packages(shipment)
            rates    = client.get_rates(to_addr, packages)
        except UPSError as e:
            messages.error(request, f'❌ UPS: {e}')
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

        weight = float(shipment.weight_kg or 1)
        return render(request, 'admin/shipping/ups_rates.html', {
            **self.admin_site.each_context(request),
            'shipment': shipment,
            'order':    order,
            'rates':    rates,
            'weight':   weight,
            'title':    f'UPS Тарифи — #{shipment.pk} → {shipment.recipient_name}',
        })

    def ups_confirm_view(self, request, shipment_id):
        """GET — Preview даних перед відправкою через UPS API.
           POST — redirect to ups_book_view (actual API call)."""
        from .ups_client import UPSClient, UPSError, UPS_SERVICES

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        service_code = (
            request.POST.get('service_code') or
            request.GET.get('service_code', '11')
        ).strip() or '11'
        service_name = UPS_SERVICES.get(service_code, f'UPS {service_code}')

        # POST — підтверджено, передаємо на ups_book_view
        if request.method == 'POST':
            return redirect(
                reverse('admin:shipping_shipment_ups_book', args=[shipment.pk])
                + f'?service_code={service_code}'
            )

        # GET — будуємо превью
        try:
            client  = UPSClient()
            to_addr = self._ups_extract_address(shipment)
            packages = self._ups_extract_packages(shipment)
            customs  = self._ups_extract_customs(shipment)
            shipper  = self._ups_extract_shipper(shipment)
        except UPSError as e:
            messages.error(request, f'❌ UPS налаштування: {e}')
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

        # Перевірка розбіжності ваги (митниця vs посилка)
        weight_warn = None
        if customs and customs.get('items'):
            customs_kg = sum(float(i.get('weight_kg', 0)) for i in customs['items'])
            ship_kg    = float(shipment.weight_kg or 0)
            if customs_kg > 0 and ship_kg > 0:
                diff = abs(ship_kg - customs_kg)
                pct  = diff / ship_kg * 100
                if diff >= 0.5 or pct >= 20:
                    weight_warn = {
                        'shipment_kg': ship_kg,
                        'customs_kg':  round(customs_kg, 3),
                        'diff_kg':     round(diff, 3),
                        'diff_pct':    round(pct, 1),
                    }

        back_url = (
            reverse('admin:shipping_shipment_edit_draft', args=[shipment.pk])
            if shipment.status == 'draft'
            else reverse('admin:shipping_shipment_change', args=[shipment.pk])
        )

        return render(request, 'admin/shipping/ups_confirm.html', {
            **self.admin_site.each_context(request),
            'title':        f'Підтвердити UPS відправлення #{shipment.pk}',
            'shipment':     shipment,
            'service_code': service_code,
            'service_name': service_name,
            'to_addr':      to_addr,
            'shipper':      shipper,
            'packages':     packages,
            'customs':      customs,
            'weight_warn':  weight_warn,
            'is_intl':      (to_addr.get('country', 'DE').upper() !=
                             (shipper.get('country') or 'DE').upper()),
            'confirm_url':  reverse('admin:shipping_shipment_ups_confirm', args=[shipment.pk]),
            'back_url':     back_url,
        })

    def ups_book_view(self, request, shipment_id):
        """Оформити відправлення через UPS."""
        import base64 as _b64
        import os
        from django.conf import settings as django_settings
        from .ups_client import UPSClient, UPSError

        shipment     = get_object_or_404(Shipment, pk=shipment_id)
        service_code = request.GET.get('service_code', '11')

        try:
            client   = UPSClient()
            to_addr  = self._ups_extract_address(shipment)
            packages = self._ups_extract_packages(shipment)
            customs  = self._ups_extract_customs(shipment)

            shipper = self._ups_extract_shipper(shipment)
            result = client.create_shipment(
                to_address=to_addr,
                packages=packages,
                service_code=service_code,
                from_address=shipper,
                customs_info=customs or None,
                reference=shipment.reference or str(shipment.pk),
            )
        except UPSError as e:
            messages.error(request, f'❌ UPS: {e}')
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

        # Зберегти трекінг + сервіс
        update_fields = []
        if result['tracking_number']:
            shipment.tracking_number = result['tracking_number']
            update_fields.append('tracking_number')
        if result['service_name']:
            shipment.carrier_service = result['service_name']
            update_fields.append('carrier_service')
        if result['total_charge']:
            shipment.carrier_price    = result['total_charge']
            shipment.carrier_currency = result['currency']
            update_fields += ['carrier_price', 'carrier_currency']
        if result.get('shipment_id'):
            shipment.carrier_shipment_id = result['shipment_id']
            update_fields.append('carrier_shipment_id')
        shipment.status = Shipment.Status.LABEL_READY
        update_fields.append('status')
        shipment.save(update_fields=update_fields)

        # Зберегти мітку
        if result.get('label_base64'):
            label_fmt = result['label_format'].lower()
            label_dir = os.path.join(django_settings.MEDIA_ROOT, 'shipping', 'labels')
            os.makedirs(label_dir, exist_ok=True)
            fname    = f'ups_{shipment.pk}_{result["tracking_number"]}.{label_fmt}'
            fpath    = os.path.join(label_dir, fname)
            with open(fpath, 'wb') as f:
                f.write(_b64.b64decode(result['label_base64']))
            rel = f'shipping/labels/{fname}'
            shipment.label_url = f'/media/{rel}'
            shipment.save(update_fields=['label_url'])
            logger.info('UPS label saved: %s', fpath)

        # Синхронізуємо SalesOrder
        from datetime import date as _date
        order        = shipment.order
        order_fields = []
        if result['tracking_number'] and not order.tracking_number:
            order.tracking_number = result['tracking_number']
            order_fields.append('tracking_number')
        if not order.shipping_courier:
            order.shipping_courier = 'UPS'
            order_fields.append('shipping_courier')
        if order.status in ('received', 'processing'):
            order.status = 'shipped'
            order_fields.append('status')
        if not order.shipped_at:
            order.shipped_at = _date.today()
            order_fields.append('shipped_at')
        if order_fields:
            order.save(update_fields=order_fields)

        messages.success(
            request,
            f'✅ UPS відправлення створено! Трекінг: {result["tracking_number"]} | '
            f'{result["service_name"]} | {result["total_charge"]} {result["currency"]}',
        )
        return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

    def ups_track_view(self, request, shipment_id):
        """Трекінг UPS для відправлення."""
        from .ups_client import UPSClient, UPSError

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        tracking = (shipment.tracking_number or '').strip()

        if not tracking:
            messages.error(request, '❌ Трекінг-номер не вказано.')
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

        try:
            client = UPSClient()
            result = client.track(tracking)
        except UPSError as e:
            messages.error(request, f'❌ UPS: {e}')
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

        # Оновити статус відправлення
        if result.get('delivered'):
            shipment.status = Shipment.Status.DELIVERED
            shipment.save(update_fields=['status'])
        elif result.get('status') not in ('', 'UNKNOWN'):
            if shipment.status not in (Shipment.Status.DELIVERED, Shipment.Status.CANCELLED):
                shipment.carrier_status_label = result.get('status_description', '')
                shipment.status = Shipment.Status.IN_TRANSIT
                shipment.save(update_fields=['status', 'carrier_status_label'])

        return render(request, 'admin/shipping/ups_tracking.html', {
            **self.admin_site.each_context(request),
            'shipment':        shipment,
            'tracking_number': tracking,
            'result':          result,
            'events':          result.get('events', []),
            'title':           f'UPS Трекінг — {tracking}',
        })

    def ups_void_view(self, request, shipment_id):
        """Анулювати відправлення UPS."""
        from .ups_client import UPSClient, UPSError

        if request.method != 'POST':
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment_id]))

        shipment    = get_object_or_404(Shipment, pk=shipment_id)
        shipment_id_ups = shipment.carrier_shipment_id or shipment.tracking_number

        if not shipment_id_ups:
            messages.error(request, '❌ Carrier Shipment ID не вказано.')
            return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

        try:
            client = UPSClient()
            client.void_shipment(shipment_id_ups)
            shipment.status = Shipment.Status.CANCELLED
            shipment.save(update_fields=['status'])
            messages.success(request, f'✅ UPS відправлення анульовано. #{shipment.pk} → Скасовано.')
        except UPSError as e:
            messages.error(request, f'❌ UPS Void: {e}')

        return redirect(reverse('admin:shipping_shipment_change', args=[shipment.pk]))

    def _ups_extract_shipper(self, shipment) -> dict:
        """Адреса відправника: shipment.sender_* → fallback carrier.*"""
        c = shipment.carrier
        return {
            'name':         shipment.sender_name    or c.sender_name or c.sender_company or 'Sender',
            'company':      shipment.sender_company or c.sender_company or '',
            'address_line': shipment.sender_street  or c.sender_street or '',
            'city':         shipment.sender_city    or c.sender_city   or '',
            'state':        shipment.sender_state   or '',
            'postal':       shipment.sender_zip     or c.sender_zip    or '',
            'country':      shipment.sender_country or c.sender_country or 'DE',
            'phone':        shipment.sender_phone   or c.sender_phone  or '',
            'email':        shipment.sender_email   or c.sender_email  or '',
        }

    def _ups_extract_address(self, shipment) -> dict:
        return {
            'name':         shipment.recipient_name or shipment.recipient_company or 'Recipient',
            'company':      shipment.recipient_company or '',
            'address_line': shipment.recipient_street or '',
            'city':         shipment.recipient_city or '',
            'state':        shipment.recipient_state or '',
            'postal':       shipment.recipient_zip or '',
            'country':      shipment.recipient_country or 'DE',
            'phone':        shipment.recipient_phone or '',
        }

    def _ups_extract_packages(self, shipment) -> list:
        # Якщо є окремі ShipmentPackage — використати їх
        pkgs = list(shipment.packages.all())
        if pkgs:
            result = []
            for p in pkgs:
                for _ in range(p.quantity or 1):
                    result.append({
                        'weight_kg': float(p.weight_kg or 1),
                        'length_cm': float(p.length_cm or 20),
                        'width_cm':  float(p.width_cm or 15),
                        'height_cm': float(p.height_cm or 10),
                    })
            return result
        return [{
            'weight_kg': float(shipment.weight_kg or 1),
            'length_cm': float(shipment.length_cm or 20),
            'width_cm':  float(shipment.width_cm or 15),
            'height_cm': float(shipment.height_cm or 10),
        }]

    def _ups_extract_customs(self, shipment) -> dict:
        order = shipment.order
        # Пропустити якщо внутрішнє відправлення
        dest = (shipment.recipient_country or '').upper()
        shipper_country = (shipment.sender_country or '').upper()
        if not shipper_country:
            from .ups_client import UPSClient
            try:
                shipper_country = UPSClient().carrier.sender_country or 'DE'
            except Exception:
                shipper_country = 'DE'
        shipper_country = shipper_country.upper()
        if dest == shipper_country:
            return None

        reason_map = {
            'Commercial': 'SALE', 'Gift': 'GIFT', 'Personal': 'GIFT',
            'Return': 'RETURN', 'Claim': 'OTHER',
        }
        currency = shipment.declared_currency or 'EUR'
        contents_type = reason_map.get(shipment.export_reason, 'SALE')

        # ── Пріоритет: customs_articles збережені з форми ──────────────────
        saved = shipment.customs_articles or {}
        saved_lines = saved.get('customs_line_items') or []
        if saved_lines:
            items = []
            for li in saved_lines:
                if not li.get('description', '').strip():
                    continue
                items.append({
                    'description': li.get('description', '')[:35],
                    'quantity':    int(li.get('quantity') or 1),
                    'value':       float(li.get('value') or 0),
                    'weight_kg':   float(li.get('weight') or 0.1),
                    'hs_code':     li.get('customs_number', '') or '',
                    'country':     (li.get('origin_country') or shipper_country).upper(),
                })
            if items:
                return {
                    'description':   f'Order #{order.order_number}',
                    'value_usd':     sum(i['value'] for i in items),
                    'currency':      (saved_lines[0].get('currency') or currency).upper(),
                    'contents_type': contents_type,
                    'items':         items,
                }

        # ── Fallback: будуємо з рядків замовлення ──────────────────────────
        items = []
        for line in order.lines.select_related('product').all():
            product = getattr(line, 'product', None)
            items.append({
                'description': str(product)[:35] if product else (shipment.description or 'Goods'),
                'quantity':    int(getattr(line, 'quantity', 1) or 1),
                'value':       float(getattr(line, 'total_price', 0) or 0),
                'weight_kg':   float(getattr(product, 'weight', 0.1) or 0.1) if product else 0.1,
                'hs_code':     getattr(product, 'hs_code', '') or '',
                'country':     shipper_country,
            })

        if not items:
            return {
                'description':   shipment.description or f'Order #{order.order_number}',
                'value_usd':     float(shipment.declared_value or 0),
                'currency':      currency,
                'contents_type': contents_type,
            }

        return {
            'description':   f'Order #{order.order_number}',
            'value_usd':     sum(i['value'] for i in items),
            'currency':      currency,
            'contents_type': contents_type,
            'items':         items,
        }

    # ── DHL Track Lookup (standalone) ────────────────────────────────────────

    def dhl_track_lookup_view(self, request):
        from .services.dhl import get_tracking as dhl_get_tracking

        # Будь-який активний DHL carrier з api_key — той самий що й для тарифів
        dhl_carrier = (
            Carrier.objects
            .filter(carrier_type="dhl", is_active=True)
            .exclude(api_key="")
            .order_by("-is_default", "pk")
            .first()
        )

        tracking_number = request.GET.get("tn", "").strip()
        result = None
        error  = None
        no_key = not dhl_carrier

        if tracking_number and dhl_carrier:
            result = dhl_get_tracking(dhl_carrier, tracking_number)
            if result.get("error"):
                error  = result["error"]
                result = None

        return render(request, "admin/shipping/dhl_track_lookup.html", {
            **self.admin_site.each_context(request),
            "title":           "🔍 DHL Трекінг",
            "tracking_number": tracking_number,
            "result":          result,
            "error":           error,
            "no_key":          no_key,
            "dhl_carrier":     dhl_carrier,
        })

    # ── UPS Track Lookup (standalone) ────────────────────────────────────────

    def ups_track_lookup_view(self, request):
        from .ups_client import UPSClient, UPSError

        ups_carrier = (
            Carrier.objects
            .filter(carrier_type="ups", is_active=True)
            .exclude(api_key="")
            .order_by("-is_default", "pk")
            .first()
        )

        tracking_number = request.GET.get("tn", "").strip()
        result = None
        error  = None
        no_key = not ups_carrier

        if tracking_number and ups_carrier:
            try:
                client = UPSClient(ups_carrier)
                result = client.track(tracking_number)
                if result.get("status") == "UNKNOWN" and not result.get("events"):
                    error  = result.get("status_description") or "Не вдалося отримати статус"
                    result = None
            except UPSError as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"

        return render(request, "admin/shipping/ups_track_lookup.html", {
            **self.admin_site.each_context(request),
            "title":       "🔍 UPS Трекінг",
            "tracking_number": tracking_number,
            "result":      result,
            "error":       error,
            "no_key":      no_key,
            "ups_carrier": ups_carrier,
        })

    # ── Compare rates (AJAX) ──────────────────────────────────────────────────

    def compare_rates_view(self, request):
        """AJAX POST — повертає тарифи усіх активних перевізників у JSON."""
        import json

        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)

        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        # Multi-package: якщо є масив коробок — агрегуємо
        packages_raw = body.get("packages") or []
        if packages_raw:
            try:
                total_weight = 0.0
                first = packages_raw[0]
                length = int(float(first.get("length_cm") or 20))
                width  = int(float(first.get("width_cm")  or 15))
                height = int(float(first.get("height_cm") or 10))
                for p in packages_raw:
                    qty = max(1, int(p.get("quantity") or 1))
                    total_weight += float(p.get("weight_kg") or 1) * qty
                weight = round(max(0.1, total_weight), 3)
            except (ValueError, TypeError, IndexError):
                weight, length, width, height = 1.0, 20, 15, 10
                packages_raw = []
        else:
            try:
                weight = float(body.get("weight_kg") or 1)
                length = int(float(body.get("length_cm") or 20))
                width  = int(float(body.get("width_cm")  or 15))
                height = int(float(body.get("height_cm") or 10))
            except (ValueError, TypeError):
                weight, length, width, height = 1.0, 20, 15, 10

        dest_country   = (body.get("recipient_country") or "").upper().strip()
        dest_postal    = (body.get("recipient_zip")     or "").strip()
        dest_city      = (body.get("recipient_city")    or "").strip()
        insurance_type = (body.get("insurance_type")   or "none").strip()
        try:
            declared_value = float(body.get("declared_value") or 1)
        except (ValueError, TypeError):
            declared_value = 1.0

        # Sender override from form (user can edit sender fields)
        sender_override = {
            "name":         (body.get("sender_name")    or "").strip(),
            "address_line": (body.get("sender_street")  or "").strip(),
            "city":         (body.get("sender_city")    or "").strip(),
            "postal":       (body.get("sender_zip")     or "").strip(),
            "country":      (body.get("sender_country") or "").upper().strip(),
        }

        if not dest_country:
            return JsonResponse({"error": "Вкажіть країну отримувача"}, status=400)

        from .services.dhl import _EU_COUNTRIES
        is_customs = dest_country not in _EU_COUNTRIES

        carriers = Carrier.objects.filter(is_active=True).order_by("-is_default", "name")
        results  = []

        for carrier in carriers:
            entry = {
                "carrier_id":   carrier.pk,
                "carrier_name": carrier.name,
                "carrier_type": carrier.carrier_type,
                "products":     [],
                "error":        None,
                "note":         None,
            }

            if carrier.carrier_type == "dhl":
                if carrier.api_key and carrier.api_secret:
                    from .services.dhl import get_rates as _dhl_rates
                    r = _dhl_rates(carrier, dest_country, dest_postal, dest_city,
                                   weight, length, width, height,
                                   is_customs_declarable=is_customs)
                    entry["products"] = r.get("products") or []
                    entry["error"]    = r.get("error")
                else:
                    entry["error"] = "API ключ не налаштовано"

            elif carrier.carrier_type == "jumingo":
                if carrier.api_key:
                    service = get_service(carrier)
                    # Видаляємо попередній preview щоб не накопичувались orphan-відправки
                    prev_key = f"jumingo_preview_{carrier.pk}"
                    old_preview = request.session.get(prev_key)
                    if old_preview:
                        service.delete_shipment(old_preview)
                        del request.session[prev_key]
                    r = service.get_rates_preview(
                        dest_country, dest_postal, dest_city,
                        weight, length, width, height,
                        insurance_type=insurance_type,
                        declared_value=declared_value,
                    )
                    preview_id = r.get("preview_id", "")
                    if preview_id:
                        request.session[prev_key] = preview_id
                    products = []
                    for t in (r.get("tariffs") or []):
                        shipper = (t.get("shipper") or {}).get("name", "")
                        svc     = t.get("name", "")
                        transit = None
                        try:
                            transit = ((t.get("dates") or {})
                                       .get("transit_time_range", {})
                                       .get("days"))
                        except Exception:
                            pass
                        products.append({
                            "name":         f"{shipper} {svc}".strip(),
                            "code":         str(t.get("id", "")),
                            "price":        float(t.get("price_brutto") or 0),
                            "currency":     t.get("currency", "EUR"),
                            "transit_days": transit,
                            "delivery_date": "",
                            "shipper":      shipper,
                            "tariff_id":    str(t.get("id", "")),
                        })
                    entry["products"]   = products
                    entry["preview_id"] = preview_id
                    entry["error"]      = r.get("error")
                else:
                    entry["error"] = "API ключ не налаштовано"

            elif carrier.carrier_type == "ups":
                if carrier.api_key and carrier.api_secret and carrier.connection_uuid:
                    from .ups_client import UPSClient, UPSError
                    try:
                        client = UPSClient(carrier)
                        to_addr = {
                            'name':         'Recipient',
                            'address_line': '',
                            'city':         dest_city,
                            'postal':       dest_postal,
                            'country':      dest_country,
                        }
                        from_addr_ups = {
                            'name':         sender_override.get('name') or carrier.sender_name or carrier.sender_company or '',
                            'address_line': sender_override.get('address_line') or carrier.sender_street or '',
                            'city':         sender_override.get('city') or carrier.sender_city or '',
                            'postal':       sender_override.get('postal') or carrier.sender_zip or '',
                            'country':      sender_override.get('country') or carrier.sender_country or 'DE',
                        }
                        pkgs = [{'weight_kg': weight, 'length_cm': length,
                                 'width_cm': width, 'height_cm': height}]
                        rates = client.get_rates(to_addr, pkgs, from_address=from_addr_ups)
                        entry["products"] = [
                            {
                                "name":          r['name'],
                                "code":          r['code'],
                                "price":         float(r['price']),
                                "currency":      r['currency'],
                                "transit_days":  r.get('transit_days'),
                                "delivery_date": r.get('delivery_date') or '',
                                "guaranteed":    r.get('guaranteed', False),
                            }
                            for r in rates
                        ]
                    except UPSError as e:
                        entry["error"] = str(e)
                    except Exception as e:
                        entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                else:
                    entry["error"] = "UPS: заповніть Client ID, Client Secret і Account Number"

            else:
                entry["note"] = "Rate shopping не підтримується"

            results.append(entry)

        # Sender address: prefer form-submitted values, fall back to first carrier
        first_carrier = carriers.first()
        from_address = {
            "name":    sender_override.get("name")    or (first_carrier.sender_name or first_carrier.sender_company if first_carrier else ""),
            "street":  sender_override.get("address_line") or (first_carrier.sender_street if first_carrier else ""),
            "city":    sender_override.get("city")    or (first_carrier.sender_city    if first_carrier else ""),
            "zip":     sender_override.get("postal")  or (first_carrier.sender_zip     if first_carrier else ""),
            "country": sender_override.get("country") or (first_carrier.sender_country if first_carrier else ""),
        }

        # Normalize packages for display
        display_packages = []
        if packages_raw:
            for p in packages_raw:
                display_packages.append({
                    "weight_kg": round(float(p.get("weight_kg") or 1), 3),
                    "length_cm": int(float(p.get("length_cm") or 20)),
                    "width_cm":  int(float(p.get("width_cm")  or 15)),
                    "height_cm": int(float(p.get("height_cm") or 10)),
                    "quantity":  max(1, int(p.get("quantity") or 1)),
                })

        return JsonResponse({
            "results":          results,
            "weight":           weight,
            "total_weight_kg":  round(weight, 3),
            "dims":             f"{length}×{width}×{height}",
            "packages":         display_packages,
            "from_address":     from_address,
        })

    # ── Submit → Jumingo API ──────────────────────────────────────────────────

    def submit_view(self, request, shipment_id):
        shipment = get_object_or_404(Shipment, pk=shipment_id)

        allowed = (Shipment.Status.DRAFT, Shipment.Status.ERROR, Shipment.Status.SUBMITTED)
        if shipment.status not in allowed:
            messages.warning(request, "Неможливо перенадіслати відправлення в поточному статусі.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        # DHL — автоматично відкриваємо тарифи
        if shipment.carrier and shipment.carrier.carrier_type == "dhl":
            return redirect(reverse("admin:shipping_shipment_dhl_rates", args=[shipment.pk]))

        SUBMIT_SUPPORTED = ("jumingo",)
        if shipment.carrier and shipment.carrier.carrier_type not in SUBMIT_SUPPORTED:
            messages.error(
                request,
                f"❌ Перевізник «{shipment.carrier.name}» ({shipment.carrier.get_carrier_type_display()}) "
                f"не підтримує створення відправлень через Minerva.",
            )
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        service = get_service(shipment.carrier)
        result  = service.create_shipment(shipment)

        shipment.raw_request  = result.raw_request
        shipment.raw_response = result.raw_response

        if result.success:
            shipment.status              = Shipment.Status.SUBMITTED
            shipment.carrier_shipment_id = result.carrier_shipment_id
            shipment.submitted_at        = timezone.now()
            shipment.error_message       = ""
            shipment.save()
            messages.success(
                request,
                f"✅ Відправлення #{shipment.pk} створено в Jumingo! "
                f"ID: {result.carrier_shipment_id}. Оберіть тариф і оплатіть на Jumingo."
            )
            # Перенаправляємо на сторінку тарифів
            return redirect(reverse("admin:shipping_shipment_rates", args=[shipment.pk]))
        else:
            shipment.status        = Shipment.Status.ERROR
            shipment.error_message = result.error_message
            shipment.save()
            messages.error(request, f"❌ Помилка Jumingo API: {result.error_message}")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

    # ── Тарифи ───────────────────────────────────────────────────────────────

    def rates_view(self, request, shipment_id):
        """Показує доступні тарифи для відправлення."""
        import json
        shipment = get_object_or_404(Shipment, pk=shipment_id)

        rates_data = {}
        if shipment.carrier_shipment_id:
            service    = get_service(shipment.carrier)
            rates_data = service.get_rates(shipment.carrier_shipment_id)

        from .services.jumingo import JUMINGO_APP_URL
        return render(request, "admin/shipping/shipment_rates.html", {
            **self.admin_site.each_context(request),
            "shipment":      shipment,
            "tariffs":       rates_data.get("tariffs", []),
            "rates_error":   rates_data.get("error", ""),
            "rates_raw":     json.dumps(rates_data, ensure_ascii=False, indent=2)[:8000],
            "jumingo_url":   f"{JUMINGO_APP_URL}/de-de/shipments/",
            "resubmit_url":  reverse("admin:shipping_shipment_submit", args=[shipment.pk]),
            "title":         f"Тарифи — #{shipment.pk} → {shipment.recipient_name}",
        })

    # ── Оновлення трекінгу ────────────────────────────────────────────────────

    def track_view(self, request, shipment_id):
        """Вручну оновлює статус і трекінг через Jumingo API."""
        shipment = get_object_or_404(Shipment, pk=shipment_id)

        if not shipment.carrier_shipment_id:
            messages.warning(request, "Відправлення не має Jumingo ID — спочатку надішліть.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        service = get_service(shipment.carrier)
        data    = service.track(shipment.carrier_shipment_id)

        if not data:
            messages.error(request, "❌ Не вдалося отримати дані від Jumingo API.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        # Зберігаємо raw_response від track
        shipment.raw_response = data
        shipment.save(update_fields=["raw_response"])

        # Перезавантажуємо об'єкт з БД щоб мати актуальний стан
        shipment.refresh_from_db()

        changed = _apply_tracking_update(shipment, data)

        # ── Діагностика митної декларації ─────────────────────────────────────
        shipment.refresh_from_db()
        customs_inv   = data.get("customs_invoice") or {}
        line_items_in = customs_inv.get("lineItems") or []
        customs_saved = (shipment.customs_articles or {}).get("customs_line_items") or []
        if line_items_in and not customs_saved:
            messages.warning(
                request,
                f"⚠️ DEBUG: customs_invoice має {len(line_items_in)} рядків у API-відповіді, "
                f"але customs_articles в БД порожнє! "
                f"exportReason={customs_inv.get('exportReason')!r}, "
                f"першийLI={line_items_in[0] if line_items_in else 'N/A'!r}"
            )
        elif customs_saved:
            messages.success(request, f"🛃 Митна декларація: {len(customs_saved)} рядків збережено.")

        if changed:
            messages.success(request, f"🔄 Оновлено: {shipment.get_status_display()}")
        else:
            messages.info(request, "ℹ️ Дані актуальні.")

        return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

    # ── Вибір тарифу (Variant 2) ─────────────────────────────────────────────

    def select_tariff_view(self, request, shipment_id):
        """Зберігає обраний тариф в Minerva + PATCH до Jumingo → редірект на Jumingo."""
        from datetime import date, timedelta
        shipment = get_object_or_404(Shipment, pk=shipment_id)

        tariff_id    = request.GET.get("tariff_id", "")
        tariff_name  = request.GET.get("name", "")
        tariff_price = request.GET.get("price", "")
        shipper_name = request.GET.get("shipper", "")

        if not tariff_id:
            messages.error(request, "❌ Не передано tariff_id.")
            return redirect(reverse("admin:shipping_shipment_rates", args=[shipment.pk]))

        # Зберегти тариф в Minerva
        from decimal import Decimal, InvalidOperation
        try:
            shipment.carrier_price = Decimal(tariff_price)
        except (InvalidOperation, TypeError):
            pass
        shipment.carrier_service    = f"{shipper_name} — {tariff_name}" if shipper_name else tariff_name
        shipment.selected_tariff_id = tariff_id
        shipment.carrier_currency   = "EUR"
        shipment.save(update_fields=[
            "carrier_price", "carrier_service", "selected_tariff_id", "carrier_currency"
        ])

        # Тариф збережено → показуємо preview перед відправкою на Jumingo API
        return redirect(reverse("admin:shipping_shipment_jumingo_confirm", args=[shipment.pk]))

    # ── Бронювання через API (Variant 3) ─────────────────────────────────────

    def book_view(self, request, shipment_id):
        """POST /v1/orders → отримує label → оновлює Shipment."""
        shipment = get_object_or_404(Shipment, pk=shipment_id)

        if not shipment.selected_tariff_id:
            messages.error(request, "❌ Спочатку оберіть тариф.")
            return redirect(reverse("admin:shipping_shipment_rates", args=[shipment.pk]))

        service = get_service(shipment.carrier)

        # Отримати доступні методи оплати
        cart = service.cart_total(shipment.carrier_shipment_id)

        # Jumingo може повертати camelCase або snake_case
        raw_methods = (
            cart.get("paymentMethods")
            or cart.get("payment_methods")
            or []
        )
        # Елемент може бути рядком або dict
        payment_methods = []
        for m in raw_methods:
            if isinstance(m, str):
                payment_methods.append(m)
            elif isinstance(m, dict):
                payment_methods.append(m.get("name") or m.get("id") or "")

        # Вибрати метод оплати (пріоритет: bill → invoice → prepayment → перший)
        method = None
        for preferred in ("bill", "invoice", "prepayment", "balance"):
            if preferred in payment_methods:
                method = preferred
                break
        if not method and payment_methods:
            method = payment_methods[0]
        if not method:
            err_detail = cart.get("detail") or cart.get("message") or ""
            if "payment" in err_detail.lower():
                messages.error(
                    request,
                    "❌ На Jumingo акаунті не налаштовано метод оплати. "
                    "Зайдіть на jumingo.com → Аккаунт → Zahlungsmethoden → "
                    "додайте картку або SEPA. Поки що використовуйте «✅ Обрати» для ручної оплати.",
                )
            else:
                import json as _json
                messages.error(
                    request,
                    f"❌ Немає доступних методів оплати. Оплатіть вручну на Jumingo. "
                    f"({err_detail or _json.dumps(cart, ensure_ascii=False)[:200]})",
                )
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        # Оформити замовлення
        order_data = service.book_order(shipment.carrier_shipment_id, method)

        if not order_data.get("success", True) or order_data.get("error"):
            messages.error(request, f"❌ Помилка бронювання: {order_data.get('error', order_data)}")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        order_number = order_data.get("orderNumber", "")
        return_url   = order_data.get("returnUrl", "")

        if order_number:
            shipment.jumingo_order_number = order_number
            shipment.status = Shipment.Status.LABEL_READY
            shipment.save(update_fields=["jumingo_order_number", "status"])

            # Спробувати отримати label
            docs = service.get_order_documents(order_number)
            labels = docs.get("labels", [])
            if labels:
                label_url = labels[0].get("url") or labels[0].get("link") or ""
                if label_url:
                    shipment.label_url = label_url
                    shipment.save(update_fields=["label_url"])
                    messages.success(
                        request,
                        f"✅ Замовлення оформлено! #{order_number}. Етикетка готова."
                    )
                else:
                    messages.success(request, f"✅ Замовлення #{order_number} оформлено! Етикетка з'явиться незабаром.")
            else:
                messages.success(request, f"✅ Замовлення #{order_number} оформлено!")

        elif return_url:
            # Потрібна зовнішня оплата (PayPal тощо)
            messages.info(request, f"ℹ️ Перейдіть за посиланням для оплати: {return_url}")

        return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

    # ── DHL Rate Comparison ────────────────────────────────────────────────────

    def dhl_rates_view(self, request, shipment_id):
        """Показує тарифи DHL Express для даного відправлення."""
        import json as _json
        from .services.dhl import get_rates as dhl_get_rates

        shipment = get_object_or_404(Shipment, pk=shipment_id)
        order    = shipment.order

        # Знаходимо DHL carrier
        dhl_carrier = Carrier.objects.filter(carrier_type="dhl", is_active=True).first()
        if not dhl_carrier:
            messages.error(request, "❌ Немає активного DHL перевізника. Додайте Carrier з типом DHL.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        if not dhl_carrier.api_key or not dhl_carrier.api_secret:
            messages.error(request, "❌ DHL Carrier не має API ключа/секрету. Заповніть поля.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        # Параметри відправлення
        weight  = float(shipment.weight_kg or 1)
        length  = int(shipment.length_cm  or 20)
        width   = int(shipment.width_cm   or 15)
        height  = int(shipment.height_cm  or 10)

        dest_country = order.addr_country or ""
        dest_postal  = order.addr_zip     or ""
        dest_city    = order.addr_city    or ""

        is_customs = dest_country not in getattr(__import__('shipping.services.jumingo', fromlist=['JumingoService']), 'JumingoService', type('', (), {'_EU_COUNTRIES': set()}))._EU_COUNTRIES

        result = dhl_get_rates(
            carrier=dhl_carrier,
            destination_country=dest_country,
            destination_postal=dest_postal,
            destination_city=dest_city,
            weight_kg=weight,
            length_cm=length,
            width_cm=width,
            height_cm=height,
            is_customs_declarable=is_customs,
        )

        return render(request, "admin/shipping/dhl_rates.html", {
            **self.admin_site.each_context(request),
            "shipment":  shipment,
            "order":     order,
            "products":  result.get("products", []),
            "dhl_error": result.get("error"),
            "weight":    weight,
            "dims":      f"{length}×{width}×{height} см",
            "title":     f"DHL Тарифи — #{shipment.pk} → {shipment.recipient_name}",
        })

    # ── DHL Tracking ──────────────────────────────────────────────────────────

    def dhl_track_view(self, request, shipment_id):
        """Трекінг посилки через DHL Express API."""
        from .services.dhl import get_tracking as dhl_get_tracking

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        dhl_carrier = Carrier.objects.filter(carrier_type="dhl", is_active=True).first()
        if not dhl_carrier:
            messages.error(request, "❌ Немає активного DHL перевізника.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        tracking_number = (shipment.tracking_number or "").strip()
        if not tracking_number:
            messages.error(request, "❌ Трекінг-номер не вказано у відправленні.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        result = dhl_get_tracking(dhl_carrier, tracking_number)

        return render(request, "admin/shipping/dhl_tracking.html", {
            **self.admin_site.each_context(request),
            "shipment":        shipment,
            "tracking_number": tracking_number,
            "result":          result,
            "dhl_error":       result.get("error"),
            "events":          result.get("events", []),
            "title":           f"DHL Трекінг — {tracking_number}",
        })

    # ── DHL Book (POST /shipments) ────────────────────────────────────────────

    def dhl_book_view(self, request, shipment_id):
        """Оформлює відправлення через DHL POST /shipments → зберігає трекінг і label."""
        import os
        from django.conf import settings
        from .services.dhl import create_shipment as dhl_create

        shipment = get_object_or_404(Shipment, pk=shipment_id)

        dhl_carrier = Carrier.objects.filter(carrier_type="dhl", is_active=True).first()
        if not dhl_carrier:
            messages.error(request, "❌ Немає активного DHL перевізника.")
            return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

        product_code    = request.GET.get("product_code", "").strip()
        product_name    = request.GET.get("product_name", product_code).strip()
        price_str       = request.GET.get("price", "0")
        request_pickup  = request.GET.get("pickup", "0") == "1"
        pickup_close    = request.GET.get("pickup_close", "18:00").strip() or "18:00"
        pickup_location = request.GET.get("pickup_location", "reception").strip() or "reception"
        customs_param   = request.GET.get("customs")
        include_customs = None if customs_param is None else (customs_param == "1")

        if not product_code:
            messages.error(request, "❌ Не передано product_code.")
            return redirect(reverse("admin:shipping_shipment_dhl_rates", args=[shipment.pk]))

        try:
            price_f = float(price_str)
        except (ValueError, TypeError):
            price_f = 0.0

        result = dhl_create(dhl_carrier, shipment, product_code, product_name, price_f,
                            request_pickup=request_pickup,
                            pickup_close_time=pickup_close,
                            pickup_location=pickup_location,
                            include_customs=include_customs)

        shipment.raw_request  = result.get("raw_request")
        shipment.raw_response = result.get("raw_response")

        if not result.get("success"):
            shipment.status        = Shipment.Status.ERROR
            shipment.error_message = result.get("error", "")
            shipment.save(update_fields=[
                "raw_request", "raw_response", "status", "error_message"
            ])
            # Показуємо кожен рядок помилки окремим повідомленням
            error_text = result.get("error", "")
            for line in error_text.split("\n"):
                if line.strip():
                    messages.error(request, line.strip())
            return redirect(reverse("admin:shipping_shipment_dhl_rates", args=[shipment.pk]))

        tracking_number = result.get("tracking_number", "")
        label_bytes     = result.get("label_bytes")

        # Зберегти label PDF до media/labels/dhl/
        label_url = ""
        if label_bytes and tracking_number:
            try:
                labels_dir = os.path.join(settings.MEDIA_ROOT, "labels", "dhl")
                os.makedirs(labels_dir, exist_ok=True)
                label_filename = f"{tracking_number}.pdf"
                with open(os.path.join(labels_dir, label_filename), "wb") as fh:
                    fh.write(label_bytes)
                label_url = f"{settings.MEDIA_URL}labels/dhl/{label_filename}"
            except Exception as exc:
                logger.warning("DHL label save failed: %s", exc)

        # Оновлюємо Shipment
        update_fields = [
            "tracking_number", "carrier_service", "carrier_price",
            "carrier_currency", "status", "submitted_at",
            "error_message", "raw_request", "raw_response",
        ]
        shipment.tracking_number = tracking_number
        shipment.carrier_service = result.get("carrier_service", product_code)
        shipment.carrier_currency = "EUR"
        shipment.status           = Shipment.Status.LABEL_READY
        shipment.submitted_at     = timezone.now()
        shipment.error_message    = ""
        if result.get("carrier_price"):
            from decimal import Decimal
            shipment.carrier_price = Decimal(str(result["carrier_price"]))
        if label_url:
            shipment.label_url = label_url
            update_fields.append("label_url")
        shipment.save(update_fields=update_fields)

        # Синхронізуємо SalesOrder
        from datetime import date as _date
        order         = shipment.order
        order_fields  = []
        if tracking_number and not order.tracking_number:
            order.tracking_number = tracking_number
            order_fields.append("tracking_number")
        if not order.shipping_courier:
            order.shipping_courier = "DHL"
            order_fields.append("shipping_courier")
        if order.status in ("received", "processing"):
            order.status = "shipped"
            order_fields.append("status")
        if not order.shipped_at:
            order.shipped_at = _date.today()
            order_fields.append("shipped_at")
        if order_fields:
            order.save(update_fields=order_fields)

        msg = f"✅ DHL відправлення створено! Трекінг: {tracking_number}."
        if label_url:
            msg += " Етикетка збережена."
        messages.success(request, msg)
        return redirect(reverse("admin:shipping_shipment_change", args=[shipment.pk]))

    # ── Readonly panels ───────────────────────────────────────────────────────

    def action_buttons(self, obj):
        if not obj.pk:
            return "—"
        btns = []
        carrier_type = obj.carrier.carrier_type if obj.carrier else ""
        if obj.status in (Shipment.Status.DRAFT, Shipment.Status.ERROR) and carrier_type == "jumingo":
            url = reverse("admin:shipping_shipment_submit", args=[obj.pk])
            btns.append(
                f'<a href="{url}" style="background:#4caf50;color:#fff;padding:8px 18px;'
                f'border-radius:6px;text-decoration:none;font-weight:700;display:inline-block;margin-right:8px">'
                f'🚀 Надіслати до Jumingo</a>'
            )
        if obj.carrier_shipment_id:
            rates_url = reverse("admin:shipping_shipment_rates", args=[obj.pk])
            track_url = reverse("admin:shipping_shipment_track", args=[obj.pk])
            btns.append(
                f'<a href="{rates_url}" style="background:#2196f3;color:#fff;padding:8px 14px;'
                f'border-radius:6px;text-decoration:none;font-weight:600;margin-right:8px">'
                f'💰 Тарифи</a>'
            )
            if obj.selected_tariff_id and not obj.jumingo_order_number:
                book_url = reverse("admin:shipping_shipment_book", args=[obj.pk])
                btns.append(
                    f'<a href="{book_url}" style="background:#9c27b0;color:#fff;padding:8px 14px;'
                    f'border-radius:6px;text-decoration:none;font-weight:600;margin-right:8px">'
                    f'⚡ Замовити через API</a>'
                )
            btns.append(
                f'<a href="{track_url}" style="background:#ff9800;color:#fff;padding:8px 14px;'
                f'border-radius:6px;text-decoration:none;font-weight:600">'
                f'🔄 Оновити трекінг</a>'
            )
        # DHL — показуємо якщо є DHL carrier
        from .models import Carrier as _Carrier
        if _Carrier.objects.filter(carrier_type="dhl", is_active=True).exists() and obj.pk:
            dhl_rates_url = reverse("admin:shipping_shipment_dhl_rates", args=[obj.pk])
            btns.append(
                f'<a href="{dhl_rates_url}" style="background:#ffcc00;color:#000;padding:8px 14px;'
                f'border-radius:6px;text-decoration:none;font-weight:600;margin-right:8px">'
                f'🟡 DHL Тарифи</a>'
            )
            if obj.tracking_number:
                dhl_track_url = reverse("admin:shipping_shipment_dhl_track", args=[obj.pk])
                btns.append(
                    f'<a href="{dhl_track_url}" style="background:#f9a825;color:#000;padding:8px 14px;'
                    f'border-radius:6px;text-decoration:none;font-weight:600;margin-right:8px">'
                    f'📡 DHL Трекінг</a>'
                )
        return format_html("".join(btns) if btns else '<span style="color:#607d8b">—</span>')
    action_buttons.short_description = "Дії"

    def order_detail_panel(self, obj):
        if not obj.order_id:
            return "—"
        o = obj.order
        return format_html(
            '<div style="background:#111c26;border:1px solid #2a3f52;'
            'border-radius:6px;padding:12px 16px;font-size:12px">'
            '<b>#{}</b> · {} · {} · {}<br>'
            '<span style="color:#607d8b">{}</span>'
            '</div>',
            o.order_number, o.source, o.client or "—",
            o.shipping_region or "—",
            o.shipping_address or "немає адреси",
        )
    order_detail_panel.short_description = "Дані замовлення"

    def customs_articles_panel(self, obj):
        import json as _json
        if not obj.customs_articles:
            return format_html('<span style="color:#607d8b">—</span>')
        data = obj.customs_articles
        inv_type = data.get("type", "—")
        items    = data.get("customs_line_items") or data.get("articles") or []
        if not items:
            return format_html('<span style="color:#607d8b">Порожньо</span>')
        rows = ""
        total = 0.0
        for it in items:
            val = it.get("value", 0) or 0
            total += float(val)
            rows += (
                f'<tr style="border-bottom:1px solid #1e2d3e">'
                f'<td style="padding:5px 8px;color:#c9d8e4">{it.get("description","")}</td>'
                f'<td style="padding:5px 8px;color:#9aafbe;text-align:center">{it.get("quantity","")}</td>'
                f'<td style="padding:5px 8px;color:#80cbc4;font-family:monospace">{it.get("customs_number","")}</td>'
                f'<td style="padding:5px 8px;color:#9aafbe;text-align:center">{it.get("origin_country","")}</td>'
                f'<td style="padding:5px 8px;color:#9aafbe;text-align:right">{it.get("weight","")}</td>'
                f'<td style="padding:5px 8px;color:#4caf50;text-align:right;font-weight:700">'
                f'{val} {it.get("currency","")}</td>'
                f'</tr>'
            )
        return format_html(
            '<div style="font-size:12px">'
            '<div style="color:#607d8b;margin-bottom:6px">Тип: <b style="color:#c9d8e4">{}</b></div>'
            '<table style="width:100%;border-collapse:collapse">'
            '<thead><tr style="background:#162030">'
            '<th style="padding:5px 8px;color:#9aafbe;text-align:left">Опис</th>'
            '<th style="padding:5px 8px;color:#9aafbe">К-ть</th>'
            '<th style="padding:5px 8px;color:#9aafbe;text-align:left">HS-код</th>'
            '<th style="padding:5px 8px;color:#9aafbe">Країна</th>'
            '<th style="padding:5px 8px;color:#9aafbe;text-align:right">Вага кг</th>'
            '<th style="padding:5px 8px;color:#9aafbe;text-align:right">Вартість</th>'
            '</tr></thead><tbody>{}</tbody></table>'
            '<div style="text-align:right;color:#4caf50;font-weight:700;margin-top:6px">Всього: {:.2f}</div>'
            '</div>',
            inv_type, format_html(rows), total,
        )
    customs_articles_panel.short_description = "Митні артикули"

    # ── List columns ──────────────────────────────────────────────────────────

    def id_badge(self, obj):
        return format_html('<b style="color:#9aafbe">#{}</b>', obj.pk)
    id_badge.short_description = "#"

    def order_link(self, obj):
        url = reverse("admin:sales_salesorder_change", args=[obj.order_id])
        return format_html('<a href="{}">{}</a>', url, obj.order)
    order_link.short_description = "Замовлення"

    def carrier_badge(self, obj):
        colors = {
            "jumingo": "#e91e63", "dhl": "#ffcc00",
            "ups": "#351c75", "fedex": "#4d148c", "other": "#607d8b",
        }
        color = colors.get(obj.carrier.carrier_type, "#607d8b")
        text_color = "#000" if obj.carrier.carrier_type == "dhl" else "#fff"
        return format_html(
            '<span style="background:{};color:{};padding:2px 8px;'
            'border-radius:8px;font-size:11px;font-weight:700">{}</span>',
            color, text_color, obj.carrier.name
        )
    carrier_badge.short_description = "Перевізник"

    def status_badge(self, obj):
        colors = {
            "draft":       ("#607d8b", "⬜"), "submitted":   ("#2196f3", "📤"),
            "label_ready": ("#00bcd4", "🏷️"), "in_transit":  ("#ff9800", "🚚"),
            "delivered":   ("#4caf50", "✅"), "error":       ("#f44336", "❌"),
            "cancelled":   ("#9e9e9e", "🚫"),
        }
        color, icon = colors.get(obj.status, ("#607d8b", "❓"))
        return format_html(
            '<span style="color:{};font-weight:600;white-space:nowrap">{} {}</span>',
            color, icon, obj.get_status_display()
        )
    status_badge.short_description = "Статус"

    def tracking_badge(self, obj):
        if obj.tracking_number:
            return format_html('<code style="font-size:11px">{}</code>', obj.tracking_number)
        return format_html('<span style="color:#607d8b">—</span>')
    tracking_badge.short_description = "Трекінг"

    def label_badge(self, obj):
        if obj.label_url:
            return format_html(
                '<a href="{}" target="_blank" style="background:#2196f3;color:#fff;'
                'padding:3px 8px;border-radius:5px;font-size:11px;text-decoration:none">'
                '📄 PDF</a>', obj.label_url
            )
        return format_html('<span style="color:#607d8b">—</span>')
    label_badge.short_description = "Етикетка"

    def created_at_fmt(self, obj):
        return obj.created_at.strftime("%d.%m.%Y %H:%M")
    created_at_fmt.short_description = "Створено"

    # ── Order Tracking overview ───────────────────────────────────────────────

    def order_tracking_view(self, request):
        """Список замовлень з трекінгом — автовизначення перевізника."""
        from sales.models import SalesOrder

        rows = []
        covered_order_ids = set()

        # 1. Shipments created via Minerva that have a tracking number
        # Exclude cancelled — вони не потребують трекінгу
        minerva_shipments = (
            Shipment.objects
            .exclude(tracking_number="")
            .filter(tracking_number__isnull=False)
            .exclude(status=Shipment.Status.CANCELLED)
            .select_related("carrier", "order")
            .order_by("-created_at")
        )
        for shipment in minerva_shipments:
            tn = shipment.tracking_number or ""
            # ERROR shipment: prefer manually-set TN on the SalesOrder
            if shipment.status == Shipment.Status.ERROR:
                order_tn = (shipment.order.tracking_number or "").strip()
                if order_tn:
                    tn = order_tn
            info = _detect_carrier(tn)
            # If carrier is known, override auto flag based on carrier type
            if shipment.carrier:
                ct = shipment.carrier.carrier_type
                if ct == "dhl":
                    info["auto"] = bool(tn)
                elif ct == "jumingo":
                    info["auto"] = bool(shipment.carrier_shipment_id)
            rows.append({
                "order":       shipment.order,
                "tn":          tn,
                "carrier":     info,
                "via_minerva": True,
                "shipment":    shipment,
                "part_label":  "",
                "total_parts": 1,
            })
            covered_order_ids.add(shipment.order_id)

        # 2. SalesOrders with manual tracking numbers not covered by a Shipment
        manual_orders = (
            SalesOrder.objects
            .exclude(tracking_number="")
            .filter(tracking_number__isnull=False)
            .exclude(id__in=covered_order_ids)
            .order_by("-order_date", "-id")
        )
        for order in manual_orders:
            tns = _parse_tracking_numbers(order.tracking_number or "")
            if not tns:
                tns = [order.tracking_number or ""]
            for idx, tn in enumerate(tns):
                info = _detect_carrier(tn)
                rows.append({
                    "order":       order,
                    "tn":          tn,
                    "carrier":     info,
                    "via_minerva": False,
                    "shipment":    None,
                    "part_label":  f"Part {idx + 1}" if len(tns) > 1 else "",
                    "total_parts": len(tns),
                })

        return render(request, "admin/shipping/order_tracking.html", {
            **self.admin_site.each_context(request),
            "rows":  rows,
            "title": "📡 Відслідковування замовлень",
        })

    def refresh_order_tracking_view(self, request, order_id):
        """Оновлює статус трекінгу для одного замовлення (Jumingo або DHL API)."""
        from sales.models import SalesOrder

        if request.method != "POST":
            return redirect(reverse("admin:shipping_order_tracking"))

        order = get_object_or_404(SalesOrder, pk=order_id)

        # Знаходимо АКТИВНИЙ пов'язаний Shipment (не cancelled)
        minerva_ship = (
            Shipment.objects
            .filter(order=order)
            .exclude(status=Shipment.Status.CANCELLED)
            .select_related("carrier")
            .order_by("-created_at")
            .first()
        )

        # TN: з Shipment → з SalesOrder
        # ERROR shipment: prefer manually-set TN on SalesOrder (user corrected it)
        tn = ""
        if minerva_ship and minerva_ship.status == Shipment.Status.ERROR:
            tn = (order.tracking_number or "").strip()
        if not tn and minerva_ship and minerva_ship.tracking_number:
            tn = minerva_ship.tracking_number.strip()
        if not tn:
            tn = (order.tracking_number or "").strip()

        carrier_type = (
            minerva_ship.carrier.carrier_type
            if minerva_ship and minerva_ship.carrier else ""
        )

        # ── Jumingo: трекінг через Jumingo API ───────────────────────────────
        if carrier_type == "jumingo":
            if not minerva_ship.carrier_shipment_id:
                messages.error(
                    request,
                    f"❌ {order.order_number}: немає Jumingo Shipment ID для трекінгу."
                )
                return redirect(reverse("admin:shipping_order_tracking"))

            from .services.jumingo import JumingoService
            service = JumingoService(minerva_ship.carrier)
            data = service.track(minerva_ship.carrier_shipment_id)

            if not data:
                messages.error(
                    request,
                    f"❌ {order.order_number}: Jumingo не повернув дані "
                    f"(ID: {minerva_ship.carrier_shipment_id})."
                )
                return redirect(reverse("admin:shipping_order_tracking"))

            changed = _apply_tracking_update(minerva_ship, data)

            progress = (
                (data.get("tracking") or {})
                .get("progress", {})
                .get("class", "")
            )
            new_tn = (
                (data.get("tracking") or {})
                .get("data", {})
                .get("tracking_number", "")
            ) or tn or minerva_ship.carrier_shipment_id

            PROGRESS_LABEL = {
                "in_system":   "📦 В системі",
                "in_transit":  "🚚 В дорозі",
                "in_delivery": "🚚 Доставляється",
                "completed":   "✅ Доставлено",
                "exception":   "⚠️ Виняток",
                "undelivered": "↩️ Не доставлено",
            }
            label = PROGRESS_LABEL.get(progress, f"📦 {progress or 'ok'}")

            if changed:
                messages.success(
                    request,
                    f"✅ {order.order_number} [{new_tn}]: Jumingo — {label}"
                )
            else:
                messages.info(
                    request,
                    f"📦 {order.order_number} [{new_tn}]: Jumingo — {label} (без змін)"
                )
            return redirect(reverse("admin:shipping_order_tracking"))

        # ── DHL Express: трекінг через DHL API ───────────────────────────────
        if not tn:
            messages.error(request, "❌ Замовлення не має трекінг-номера.")
            return redirect(reverse("admin:shipping_order_tracking"))

        from .services.dhl import get_tracking as dhl_get_tracking
        info = _detect_carrier(tn)

        if info["carrier"] != "dhl_express" and carrier_type != "dhl":
            messages.warning(
                request,
                f"⚠️ {order.order_number}: автоматичне відслідковування недоступне для {info['label']}. "
                f"Перевір вручну: {info['url'] or '—'}"
            )
            return redirect(reverse("admin:shipping_order_tracking"))

        dhl_carrier = (
            Carrier.objects
            .filter(carrier_type="dhl", is_active=True)
            .exclude(api_key="")
            .order_by("-is_default")
            .first()
        )
        if not dhl_carrier:
            messages.error(request, "❌ Немає активного DHL перевізника з API ключем.")
            return redirect(reverse("admin:shipping_order_tracking"))

        result = dhl_get_tracking(dhl_carrier, tn)

        if result.get("error"):
            messages.error(request, f"❌ {order.order_number}: {result['error']}")
            return redirect(reverse("admin:shipping_order_tracking"))

        # Оновлюємо SalesOrder на основі DHL статусу
        status_str    = result.get("status", "")
        description   = result.get("description", "")
        update_fields = []

        STATUS_MAP = {
            "transit":   "shipped",
            "delivered": "delivered",
            "failure":   "shipped",
            "unknown":   None,
        }
        new_order_status = STATUS_MAP.get(status_str)

        if new_order_status == "delivered" and order.status != "delivered":
            order.status = "delivered"
            update_fields.append("status")
            if not order.delivered_at:
                order.delivered_at = timezone.now()
                update_fields.append("delivered_at")
        elif new_order_status == "shipped" and order.status in ("received", "processing"):
            order.status = "shipped"
            update_fields.append("status")
            if not order.shipped_at:
                order.shipped_at = timezone.now().date()
                update_fields.append("shipped_at")

        if update_fields:
            order.save(update_fields=update_fields)
            messages.success(
                request,
                f"✅ {order.order_number} [{tn}]: статус оновлено → {order.get_status_display()}"
            )
        else:
            icon = {"transit": "🚚", "delivered": "✅"}.get(status_str, "📦")
            messages.info(
                request,
                f"{icon} {order.order_number} [{tn}]: {description or status_str} (без змін)"
            )

        return redirect(reverse("admin:shipping_order_tracking"))


# ── Utility: apply tracking update ───────────────────────────────────────────

def _apply_tracking_update(shipment, data: dict) -> bool:
    """Оновлює Shipment та SalesOrder з відповіді GET /v1/shipments/{id}.
    Повертає True якщо були зміни.

    Структура Jumingo API:
      data["tracking"]["data"]["tracking_number"]  ← трекінг-номер UPS/DHL
      data["tracking"]["progress"]["class"]         ← in_system / in_transit / completed
      data["tracking"]["carrierTrackingPage"]       ← посилання на трекінг перевізника
      data["order"]["number"]                       ← Jumingo order number
    """
    from .services.jumingo import SHIPMENT_STATUS_MAP

    # Статуси Jumingo tracking.progress.class → Minerva
    PROGRESS_CLASS_MAP = {
        "in_system":   "label_ready",
        "in_transit":  "in_transit",
        "in_delivery": "in_transit",
        "completed":   "delivered",
        "exception":   "error",
        "undelivered": "in_transit",
    }

    changed = False

    # ── Трекінг-номер: tracking.data.tracking_number ─────────────────────────
    tracking_obj  = data.get("tracking") or {}
    tracking_data = tracking_obj.get("data") or {}
    new_tn        = tracking_data.get("tracking_number", "")
    carrier_page  = tracking_obj.get("carrierTrackingPage", "")

    if new_tn and new_tn != shipment.tracking_number:
        shipment.tracking_number = new_tn
        changed = True
    if carrier_page and not shipment.label_url:
        shipment.label_url = carrier_page
        changed = True

    # ── Jumingo order number: order.number ────────────────────────────────────
    order_num = (data.get("order") or {}).get("number", "")
    if order_num and not shipment.jumingo_order_number:
        shipment.jumingo_order_number = order_num
        changed = True

    # ── Вартість доставки: rate.price_total ───────────────────────────────────
    rate = data.get("rate") or {}
    price_total = rate.get("price_total") or rate.get("price_net")
    if price_total and not shipment.carrier_price:
        from decimal import Decimal, InvalidOperation
        try:
            shipment.carrier_price    = Decimal(str(price_total))
            shipment.carrier_currency = rate.get("price_total_currency", "EUR")
            changed = True
        except InvalidOperation:
            pass

    # ── Назва сервісу: UPS EXPEDITED ® ────────────────────────────────────────
    if not shipment.carrier_service:
        carrier_name  = (rate.get("carrier") or {}).get("shipper_group_name", "")
        service_name  = (rate.get("service") or {}).get("name", "")
        service_str   = f"{carrier_name} {service_name}".strip()
        if service_str:
            shipment.carrier_service = service_str
            changed = True

    # ── Митна декларація: customs_invoice.lineItems → customs_articles ────────
    customs_inv    = data.get("customs_invoice") or {}
    line_items     = customs_inv.get("lineItems") or []
    existing_items = ((shipment.customs_articles or {}).get("customs_line_items") or [])
    if line_items:
        inv_currency = customs_inv.get("currency", "EUR")
        export_reason = customs_inv.get("exportReason", "Commercial")
        type_map = {
            "Commercial": "commercial", "Gift": "gift",
            "Sample": "sample", "Return": "return", "Private": "private",
        }
        items = []
        for li in line_items:
            item = {
                "description":    (li.get("content") or "")[:35],
                "quantity":       li.get("quantity") or 1,
                "value":          li.get("value") or 0.0,
                "currency":       inv_currency,
                "origin_country": li.get("manufacturingCountry") or "",
                "customs_number": li.get("hsTariffNumber") or "",
            }
            nw = li.get("netWeight") or 0
            if nw:
                item["weight"] = round(float(nw), 3)
            items.append(item)
        if items:
            shipment.customs_articles = {
                "type":               type_map.get(export_reason, "commercial"),
                "currency":           inv_currency,   # для _build_customs_invoice
                "customs_line_items": items,
            }
            shipment.save(update_fields=["customs_articles"])
            changed = True

    # ── Carrier status label та delayed flag ──────────────────────────────────
    progress     = tracking_obj.get("progress") or {}
    prog_label   = (
        progress.get("label") or
        progress.get("status_label") or
        progress.get("statusLabel") or
        progress.get("description") or
        ""
    )
    prog_delayed = bool(
        progress.get("delayed") or
        progress.get("isDelayed") or
        progress.get("is_delayed") or
        False
    )

    if prog_label and prog_label != shipment.carrier_status_label:
        shipment.carrier_status_label = prog_label[:200]
        changed = True

    old_delayed = shipment.carrier_delayed
    if prog_delayed != shipment.carrier_delayed:
        shipment.carrier_delayed = prog_delayed
        changed = True

    # ── ETA дати (очікувана доставка) ─────────────────────────────────────────
    from datetime import date as _date

    def _parse_date(s):
        if not s:
            return None
        try:
            if isinstance(s, str):
                return _date.fromisoformat(s[:10])
        except ValueError:
            pass
        return None

    # Пробуємо кілька можливих шляхів у відповіді Jumingo
    tracking_dates = tracking_obj.get("dates") or {}
    # dates.delivery.* from Jumingo detail response
    dates_block    = data.get("dates") or {}
    delivery_block = dates_block.get("delivery") or {}

    eta_f = (
        _parse_date(tracking_data.get("estimated_delivery_from"))
        or _parse_date(tracking_data.get("estimatedDeliveryFrom"))
        or _parse_date(tracking_data.get("delivery_date_from"))
        or _parse_date(tracking_dates.get("eta_from"))
        or _parse_date(tracking_dates.get("deliveryDateFrom"))
        or _parse_date(progress.get("delivery_date_from"))
        # Jumingo detail: rate.delivery_date_min / dates.delivery.min_delivery_date
        or _parse_date(rate.get("delivery_date_min"))
        or _parse_date(delivery_block.get("min_delivery_date"))
    )
    eta_t = (
        _parse_date(tracking_data.get("estimated_delivery_to"))
        or _parse_date(tracking_data.get("estimatedDeliveryTo"))
        or _parse_date(tracking_data.get("delivery_date_to"))
        or _parse_date(tracking_dates.get("eta_to"))
        or _parse_date(tracking_dates.get("deliveryDateTo"))
        or _parse_date(progress.get("delivery_date_to"))
        # fallback: single date → use as eta_to
        or _parse_date(tracking_data.get("estimated_delivery"))
        or _parse_date(tracking_data.get("estimatedDelivery"))
        or _parse_date(tracking_data.get("promised_delivery"))
        # Jumingo detail: rate.delivery_date_max / dates.delivery.max_delivery_date
        or _parse_date(rate.get("delivery_date_max"))
        or _parse_date(delivery_block.get("max_delivery_date"))
    )

    if eta_f and eta_f != shipment.eta_from:
        shipment.eta_from = eta_f
        changed = True
    if eta_t and eta_t != shipment.eta_to:
        shipment.eta_to = eta_t
        changed = True

    # ── Статус: progress.class → Minerva статус ───────────────────────────────
    progress_class  = progress.get("class", "")
    shipment_status = data.get("status", "")
    new_status_str  = (
        PROGRESS_CLASS_MAP.get(progress_class)
        or SHIPMENT_STATUS_MAP.get(shipment_status)
    )
    if new_status_str and new_status_str != shipment.status:
        shipment.status = new_status_str
        changed = True

    if changed:
        shipment.save()

    # ── Нотифікація при появі затримки ────────────────────────────────────────
    if not old_delayed and prog_delayed:
        try:
            from config.models import NotificationSettings
            ns = NotificationSettings.get()
            order_num = shipment.order.order_number if shipment.order else f"#{shipment.pk}"
            tn = shipment.tracking_number or shipment.carrier_shipment_id or "—"
            eta_str = ""
            if eta_f or eta_t:
                parts = []
                if eta_f:
                    parts.append(eta_f.strftime("%d.%m.%y"))
                if eta_t and eta_t != eta_f:
                    parts.append(eta_t.strftime("%d.%m.%y"))
                eta_str = f" · Доставка: {' – '.join(parts)}"
            msg = (
                f"🚨 <b>Затримка відправлення</b>\n"
                f"Замовлення: <b>{order_num}</b>\n"
                f"Трекінг: <code>{tn}</code>\n"
                f"Статус: {prog_label or 'Verspätet'}{eta_str}"
            )
            if ns.telegram_enabled and ns.telegram_bot_token and ns.telegram_chat_id:
                from dashboard.notifications import _send_telegram
                _send_telegram(ns, msg)
        except Exception:
            pass

    # ── Синхронізація SalesOrder (незалежно від змін у відправленні) ──────────
    order         = shipment.order
    order_changed = False
    update_fields = []

    carrier_name = (rate.get("carrier") or {}).get("shipper_group_name", "")

    # Трекінг-номер
    if new_tn and not order.tracking_number:
        order.tracking_number = new_tn
        order_changed = True
        update_fields.append("tracking_number")

    # Перевізник (UPS / DHL тощо)
    if carrier_name and not order.shipping_courier:
        order.shipping_courier = carrier_name
        order_changed = True
        update_fields.append("shipping_courier")

    # Вартість доставки на замовлення
    if price_total and not order.shipping_cost:
        from decimal import Decimal, InvalidOperation
        try:
            order.shipping_cost     = Decimal(str(price_total))
            order.shipping_currency = rate.get("price_total_currency", "EUR")
            order_changed = True
            update_fields += ["shipping_cost", "shipping_currency"]
        except InvalidOperation:
            pass

    # Статус замовлення + shipped_at
    if shipment.status == "label_ready":
        if order.status in ("received", "processing"):
            order.status = "shipped"
            order_changed = True
            update_fields.append("status")
        if not order.shipped_at:
            order.shipped_at = timezone.now().date()
            order_changed = True
            update_fields.append("shipped_at")
    elif shipment.status == "in_transit" and order.status != "shipped":
        order.status = "shipped"
        if not order.shipped_at:
            order.shipped_at = timezone.now().date()
            update_fields.append("shipped_at")
        order_changed = True
        update_fields.append("status")
    elif shipment.status == "delivered" and order.status != "delivered":
        order.status = "delivered"
        order_changed = True
        update_fields.append("status")
        if not order.delivered_at:
            order.delivered_at = timezone.now()
            order_changed = True
            update_fields.append("delivered_at")

    if order_changed:
        order.save(update_fields=update_fields)

    return changed


# ── Parse multiple tracking numbers from a single field ───────────────────────

def _parse_tracking_numbers(tn_str: str) -> list:
    """Extract individual TNs from a field that may contain multiple numbers.

    Handles:
      'JD123, JD456'
      'JD123\nJD456'
      'Part1: JD123 Part2: JD456'   ← DigiKey multi-package format
      'JD123 JD456'
    """
    import re
    if not tn_str:
        return []
    s = re.sub(r'\bPart\s*\d+\s*:', ' ', tn_str, flags=re.IGNORECASE)
    parts = re.split(r'[,;\s]+', s)
    return [p.strip() for p in parts if p.strip()]


# ── Carrier detection by tracking number ─────────────────────────────────────

def _detect_carrier(tn: str) -> dict:
    """Визначає перевізника за форматом трекінг-номера."""
    import re
    tn = (tn or "").strip()
    if re.match(r'^\d{10}$', tn):
        return {"carrier": "dhl_express", "label": "DHL Express",
                "color": "#ffcc00", "text": "#000", "auto": True,
                "url": f"https://www.dhl.com/en/express/tracking.html?AWB={tn}&brand=DHL"}
    if re.match(r'^1Z[0-9A-Z]{16}$', tn.upper()):
        return {"carrier": "ups", "label": "UPS",
                "color": "#351c75", "text": "#fff", "auto": False,
                "url": f"https://www.ups.com/track?tracknum={tn}"}
    if re.match(r'^\d{14}$', tn) and (tn.startswith("20") or tn.startswith("59")):
        return {"carrier": "nova_poshta", "label": "Нова Пошта",
                "color": "#e53935", "text": "#fff", "auto": False,
                "url": f"https://novaposhta.ua/tracking/?cargo_number={tn}"}
    if re.match(r'^JD\d{8,}$', tn.upper()):
        return {"carrier": "dhl_paket", "label": "DHL Paket",
                "color": "#ffcc00", "text": "#000", "auto": False,
                "url": f"https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?lang=de&idc={tn}"}
    if re.match(r'^\d{12,22}$', tn):
        return {"carrier": "dhl_paket", "label": "DHL Paket",
                "color": "#ffcc00", "text": "#000", "auto": False,
                "url": f"https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?lang=de&idc={tn}"}
    if re.match(r'^[A-Z]{2}\d{9}[A-Z]{2}$', tn.upper()):
        return {"carrier": "post", "label": "Post",
                "color": "#607d8b", "text": "#fff", "auto": False,
                "url": f"https://track24.net/?lang=en&number={tn}"}
    return {"carrier": "unknown", "label": "Невідомий",
            "color": "#37474f", "text": "#fff", "auto": False, "url": ""}


# ── Inject shipping stats into shipping app_index context ─────────────────────

def _get_shipping_stats():
    try:
        from django.db.models import Count
        from datetime import date
        month_start = date.today().replace(day=1)

        qs = Shipment.objects.values("status").annotate(n=Count("pk"))
        by_status = {row["status"]: row["n"] for row in qs}

        delivered_month = Shipment.objects.filter(
            status="delivered",
            created_at__date__gte=month_start,
        ).count()

        return {
            "draft":           by_status.get("draft", 0),
            "submitted":       by_status.get("submitted", 0),
            "label_ready":     by_status.get("label_ready", 0),
            "in_transit":      by_status.get("in_transit", 0),
            "delivered":       by_status.get("delivered", 0),
            "error":           by_status.get("error", 0),
            "delivered_month": delivered_month,
            "active":          (by_status.get("submitted", 0)
                                + by_status.get("label_ready", 0)
                                + by_status.get("in_transit", 0)),
        }
    except Exception:
        empty = {"draft": "—", "submitted": "—", "label_ready": "—",
                 "in_transit": "—", "delivered": "—", "error": "—",
                 "delivered_month": "—", "active": "—"}
        return empty


@admin.register(ShippingSettings)
class ShippingSettingsAdmin(admin.ModelAdmin):
    """Singleton — налаштування доставки."""

    readonly_fields = ("last_tracking_run", "tracking_actions")

    fieldsets = [
        ("🔄 Автоматичний трекінг", {
            "fields": (
                "auto_tracking_enabled",
                "tracking_interval_minutes",
                "last_tracking_run",
                "tracking_actions",
            ),
            "description": (
                "Додайте cron для автоматичного запуску (рекомендується кожні 5 хвилин — "
                "команда сама пропустить запуск якщо інтервал ще не вийшов):<br>"
                "<code>*/5 * * * * docker-compose exec -T web python manage.py track_shipments</code>"
            ),
        }),
    ]

    def tracking_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="../run-tracking/" style="'
            'display:inline-block;padding:8px 18px;'
            'background:#1976d2;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '🔄 Запустити оновлення зараз</a>'
        )
    tracking_actions.short_description = "Дії"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "",
                self.admin_site.admin_view(self._redirect_singleton),
                name="shipping_shippingsettings_changelist",
            ),
            path(
                "<int:pk>/run-tracking/",
                self.admin_site.admin_view(self._run_tracking),
                name="shipping_shippingsettings_run_tracking",
            ),
        ]
        return custom + urls

    def _redirect_singleton(self, request):
        obj = ShippingSettings.get()
        return redirect(reverse("admin:shipping_shippingsettings_change", args=[obj.pk]))

    def _run_tracking(self, request, pk):
        from django.core.management import call_command
        import io
        out = io.StringIO()
        try:
            call_command("track_shipments", "--force", stdout=out)
            messages.success(request, f"✅ Трекінг оновлено. {out.getvalue().strip().splitlines()[-1]}")
        except Exception as e:
            messages.error(request, f"❌ Помилка: {e}")
        return redirect(reverse("admin:shipping_shippingsettings_change", args=[1]))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


_orig_shipping_app_index = admin.site.app_index


def _shipping_app_index(request, app_label, extra_context=None):
    if app_label == "shipping":
        extra_context = extra_context or {}
        extra_context["shipping_stats"] = _get_shipping_stats()
    return _orig_shipping_app_index(request, app_label, extra_context)


admin.site.app_index = _shipping_app_index
