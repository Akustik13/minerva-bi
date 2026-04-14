"""
bots/admin.py — Адміністрування ботів і DigiKey конфігурації
"""
from django.contrib import admin
from django.urls import reverse, path
from django.shortcuts import redirect, render
from django.contrib import messages
from django.utils.html import format_html
from .models import Bot, BotLog, DigiKeyConfig


@admin.register(Bot)
class BotAdmin(admin.ModelAdmin):
    list_display = ("name", "bot_type", "status", "last_run_at", "total_runs", "success_runs")
    list_filter = ("bot_type", "status", "is_active", "schedule_enabled")
    search_fields = ("name", "description", "login")
    
    readonly_fields = (
        "status", "last_run_at", "last_run_status", "last_run_duration",
        "next_run_at", "total_runs", "success_runs", "error_runs",
        "created_at", "updated_at"
    )
    
    fieldsets = (
        ("🤖 Основна інформація", {
            "fields": ("name", "bot_type", "description", "is_active", "status")
        }),
        ("🔐 Credentials", {
            "fields": ("login", "password", "api_key"),
            "classes": ("collapse",),
        }),
        ("⏰ Розклад", {
            "fields": ("schedule_enabled", "schedule_cron", "schedule_interval_minutes"),
        }),
        ("📊 Статистика", {
            "fields": (
                "last_run_at", "last_run_status", "last_run_duration",
                "next_run_at", "total_runs", "success_runs", "error_runs"
            ),
        }),
    )
    
    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        for field_name in ('password', 'api_key'):
            if field_name in form.base_fields:
                form.base_fields[field_name].widget = PasswordInput(render_value=True)
        return form

    def get_urls(self):
        from django.shortcuts import render as _render
        urls = super().get_urls()
        custom_urls = [
            path('<int:bot_id>/run/', self.admin_site.admin_view(self.run_bot_view),
                 name='bots_bot_run'),
            path('ai-info/', self.admin_site.admin_view(self.ai_info_view),
                 name='bots_ai_info'),
        ]
        return custom_urls + urls

    def ai_info_view(self, request):
        from django.shortcuts import render as _render
        ctx = dict(
            self.admin_site.each_context(request),
            title="🧠 AI — Штучний інтелект",
        )
        return _render(request, "admin/ai/info.html", ctx)
    
    def run_bot_view(self, request, bot_id):
        """View для запуску бота."""
        bot = Bot.objects.get(pk=bot_id)
        
        if not bot.can_run():
            messages.error(request, f"❌ Не можна запустити: {bot.status}")
            return redirect('admin:bots_bot_change', bot_id)
        
        try:
            if bot.bot_type == Bot.BotType.DIGIKEY:
                from bots.runners import run_digikey_bot
                result = run_digikey_bot(bot)
            else:
                messages.warning(request, f"⚠️ Бот {bot.bot_type} ще не реалізований")
                return redirect('admin:bots_bot_change', bot_id)
            
            if result['success']:
                messages.success(request, f"✅ {result['message']}")
            else:
                messages.error(request, f"❌ {result['message']}")
        except Exception as e:
            messages.error(request, f"❌ Помилка: {str(e)}")
        
        return redirect('admin:bots_bot_change', bot_id)
    
    # Додаємо кнопки в change_form
    change_form_template = 'admin/bots/bot/change_form.html'


# ── DigiKey Configuration Admin ───────────────────────────────────────────────

@admin.register(DigiKeyConfig)
class DigiKeyConfigAdmin(admin.ModelAdmin):
    """Singleton-адмін — завжди редагує запис pk=1."""

    readonly_fields = (
        "last_synced_at",
        "access_token_preview",
        "token_expires_at",
        "marketplace_auth_status",
        "action_buttons",
        "oauth_url_display",
        "webhook_url_display",
    )

    fieldsets = (
        ("🌐 Публічний URL", {
            "fields": ("public_base_url", "oauth_url_display", "webhook_url_display"),
            "description": (
                "Вкажи публічний URL сайту — решта URL генеруються автоматично. "
                "Скопіюй <b>OAuth Callback URL</b> в DigiKey dev portal → My Apps → OAuth Callback. "
                "Скопіюй <b>Webhook URL</b> в DigiKey dev portal → My Apps → Webhooks."
            ),
        }),
        ("🔑 DigiKey API Credentials", {
            "fields": ("client_id", "client_secret", "account_id"),
            "description": (
                "Отримати на <a href='https://developer.digikey.com/' target='_blank'>"
                "developer.digikey.com</a> → My Apps → Create App (тип: Marketplace Seller)."
            ),
        }),
        ("🌍 Локаль", {
            "fields": (("locale_site", "locale_language", "locale_currency"),),
            "description": "Ці значення передаються в кожен API-запит як заголовки.",
        }),
        ("⏰ Синхронізація", {
            "fields": (
                "sync_enabled", "sync_interval_minutes", "use_sandbox",
                "auto_confirm_mode",
                "last_synced_at",
            ),
        }),
        ("🔌 Дії", {
            "fields": ("action_buttons",),
        }),
        ("🔮 Webhook", {
            "fields": ("webhook_enabled", "webhook_secret"),
            "description": (
                "Webhook Secret — довільний рядок, вкажи той самий і в DigiKey dev portal → Webhooks."
            ),
        }),
        ("🛒 Marketplace API (3-legged OAuth)", {
            "fields": ("marketplace_auth_status",),
            "description": (
                "Marketplace API потребує авторизації користувача (3-legged OAuth). "
                "Натисніть <b>🔑 Авторизувати Marketplace</b> нижче."
            ),
        }),
        ("🔐 OAuth Token (технічний)", {
            "fields": ("access_token_preview", "token_expires_at"),
            "classes": ("collapse",),
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        for field_name in ('client_secret', 'webhook_secret'):
            if field_name in form.base_fields:
                form.base_fields[field_name].widget = PasswordInput(render_value=True)
        return form

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "test-connection/",
                self.admin_site.admin_view(self.test_connection_view),
                name="bots_digikeyconfig_test",
            ),
            path(
                "sync-now/",
                self.admin_site.admin_view(self.sync_now_view),
                name="bots_digikeyconfig_sync",
            ),
            path(
                "clear-token/",
                self.admin_site.admin_view(self.clear_token_view),
                name="bots_digikeyconfig_clear_token",
            ),
            path(
                "po-lookup/",
                self.admin_site.admin_view(self.po_lookup_view),
                name="bots_digikeyconfig_po_lookup",
            ),
            path(
                "debug/",
                self.admin_site.admin_view(self.debug_view),
                name="bots_digikeyconfig_debug",
            ),
            path(
                "products/",
                self.admin_site.admin_view(self.products_view),
                name="bots_digikeyconfig_products",
            ),
            path(
                "oauth-start/",
                self.admin_site.admin_view(self.oauth_start_view),
                name="bots_digikeyconfig_oauth_start",
            ),
            path(
                "marketplace-orders/",
                self.admin_site.admin_view(self.marketplace_orders_view),
                name="bots_digikeyconfig_marketplace_orders",
            ),
            path(
                "reconcile/",
                self.admin_site.admin_view(self.reconcile_view),
                name="bots_digikeyconfig_reconcile",
            ),
            path(
                "confirm-order/<str:order_number>/",
                self.admin_site.admin_view(self.confirm_order_view),
                name="bots_digikeyconfig_confirm_order",
            ),
            path(
                "api-log/",
                self.admin_site.admin_view(self.api_log_view),
                name="bots_digikeyconfig_api_log",
            ),
            path(
                "api-log/clear/",
                self.admin_site.admin_view(self.api_log_clear_view),
                name="bots_digikeyconfig_api_log_clear",
            ),
            path(
                "api-log/download/",
                self.admin_site.admin_view(self.api_log_download_view),
                name="bots_digikeyconfig_api_log_download",
            ),
        ]
        return custom + urls

    # ── Changelist → redirect to singleton ────────────────────────────────────

    def changelist_view(self, request, extra_context=None):
        obj, _ = DigiKeyConfig.objects.get_or_create(pk=1)
        return redirect(
            reverse("admin:bots_digikeyconfig_change", args=[obj.pk])
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """Показуємо OAuth success/error повідомлення після redirect з callback."""
        if request.session.pop("digikey_oauth_success", False):
            messages.success(request, "✅ DigiKey Marketplace авторизація успішна! Тепер можна отримувати замовлення.")
        oauth_err = request.session.pop("digikey_oauth_error", None)
        if oauth_err:
            messages.error(request, f"❌ OAuth помилка: {oauth_err}")
        return super().change_view(request, object_id, form_url, extra_context)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # ── Custom action views ───────────────────────────────────────────────────

    def test_connection_view(self, request):
        from bots.services.digikey import test_connection
        config = DigiKeyConfig.get()
        result = test_connection(config)
        if result["ok"]:
            messages.success(request, result["message"])
        else:
            messages.error(request, result["message"])
        return redirect(reverse("admin:bots_digikeyconfig_change", args=[1]))

    def clear_token_view(self, request):
        DigiKeyConfig.objects.filter(pk=1).update(
            access_token="", token_expires_at=None,
            marketplace_access_token="", marketplace_refresh_token="",
            marketplace_token_expires_at=None,
        )
        messages.success(request, "🗑️ Всі OAuth токени скинуто.")
        return redirect(reverse("admin:bots_digikeyconfig_change", args=[1]))

    def oauth_start_view(self, request):
        """Починає 3-legged OAuth flow — редіректить на DigiKey authorization URL."""
        from bots.services.digikey import build_authorize_url
        config = DigiKeyConfig.get()

        if not config.client_id:
            messages.error(request, "❌ Заповніть Client ID перед авторизацією.")
            return redirect(reverse("admin:bots_digikeyconfig_change", args=[1]))

        from django.conf import settings as dj_settings
        base = (config.public_base_url or "").rstrip("/")
        redirect_uri = (
            f"{base}/bots/digikey/oauth-callback/" if base
            else dj_settings.DIGIKEY_OAUTH_REDIRECT_URI
            or request.build_absolute_uri("/bots/digikey/oauth-callback/")
        )
        request.session["digikey_oauth_redirect_uri"] = redirect_uri

        auth_url = build_authorize_url(config, redirect_uri)
        return redirect(auth_url)

    def reconcile_view(self, request):
        """Звіряє всі DigiKey замовлення з Minerva і показує звіт."""
        from bots.services.digikey import reconcile_marketplace_orders, DigiKeyAPIError
        import json

        config = DigiKeyConfig.get()
        stats = None
        error = None

        if request.method == "POST":
            days     = int(request.POST.get("days_back", 365))
            dry_run  = request.POST.get("dry_run") == "1"
            try:
                stats = reconcile_marketplace_orders(config, days_back=days, dry_run=dry_run)
                stats["dry_run"]   = dry_run
                stats["days_back"] = days
            except DigiKeyAPIError as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"

        ctx = dict(
            self.admin_site.each_context(request),
            title="DigiKey — Звірка замовлень",
            config=config,
            stats=stats,
            error=error,
        )
        return render(request, "admin/bots/digikey_reconcile.html", ctx)

    def marketplace_orders_view(self, request):
        """Показує вхідні замовлення Marketplace."""
        from bots.services.digikey import get_marketplace_orders, DigiKeyAPIError
        import json

        config = DigiKeyConfig.get()

        # Показуємо success/error з OAuth callback
        if request.session.pop("digikey_oauth_success", False):
            messages.success(request, "✅ Marketplace авторизація успішна!")
        oauth_err = request.session.pop("digikey_oauth_error", None)
        if oauth_err:
            messages.error(request, f"❌ OAuth помилка: {oauth_err}")

        offset = int(request.GET.get("offset", 0))
        orders_data = None
        error = None
        raw_json = None

        if config.marketplace_refresh_token or config.marketplace_access_token:
            try:
                orders_data = get_marketplace_orders(config, offset=offset, max_results=20)
                raw_json = json.dumps(orders_data, indent=2, ensure_ascii=False)
            except DigiKeyAPIError as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"

        ctx = dict(
            self.admin_site.each_context(request),
            title="DigiKey — Marketplace замовлення",
            config=config,
            orders_data=orders_data,
            raw_json=raw_json,
            error=error,
            offset=offset,
            not_authorized=not (config.marketplace_refresh_token or config.marketplace_access_token),
            oauth_start_url=reverse("admin:bots_digikeyconfig_oauth_start"),
        )
        return render(request, "admin/bots/digikey_marketplace_orders.html", ctx)

    def sync_now_view(self, request):
        from bots.services.digikey import sync_marketplace_orders, sync_orders, DigiKeyAPIError
        config = DigiKeyConfig.get()

        if not config.client_id or not config.client_secret:
            messages.error(request, "❌ Заповніть Client ID та Client Secret.")
            return redirect(reverse("admin:bots_digikeyconfig_change", args=[1]))

        try:
            # Marketplace API (3-legged) — пріоритет якщо авторизовано
            if config.marketplace_refresh_token or config.marketplace_access_token:
                stats = sync_marketplace_orders(config)
            else:
                stats = sync_orders(config)
            unmatched = stats.get("unmatched_skus") or []
            errs      = stats.get("errors") or []

            messages.success(
                request,
                f"✅ Синхронізація завершена: "
                f"+{stats['created']} замовлень, "
                f"+{stats['lines_created']} рядків, "
                f"оновлено {stats['updated']}."
            )
            if unmatched:
                messages.warning(
                    request,
                    f"⚠️ Товари не знайдено в базі ({len(unmatched)} SKU): "
                    + ", ".join(unmatched[:10])
                )
            if errs:
                messages.error(request, f"❌ Помилки ({len(errs)}): " + "; ".join(errs[:5]))

        except DigiKeyAPIError as e:
            messages.error(request, f"❌ DigiKey API: {e}")
        except Exception as e:
            messages.error(request, f"❌ {type(e).__name__}: {e}")

        return redirect(reverse("admin:bots_digikeyconfig_change", args=[1]))

    def products_view(self, request):
        from bots.services.digikey import search_products, DigiKeyAPIError

        config   = DigiKeyConfig.get()
        keywords = request.GET.get("q", "Sevskiy").strip()
        page     = max(1, int(request.GET.get("page", 1)))
        limit    = 50
        offset   = (page - 1) * limit

        products   = []
        total      = 0
        error      = None
        no_credentials = not (config.client_id and config.client_secret)

        if keywords and not no_credentials:
            try:
                data     = search_products(config, keywords, limit=limit, offset=offset)
                products = data.get("Products") or []
                total    = data.get("ProductsCount") or len(products)
            except DigiKeyAPIError as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"

        ctx = dict(
            self.admin_site.each_context(request),
            title=f"DigiKey — Компоненти: {keywords}",
            config=config,
            keywords=keywords,
            products=products,
            total=total,
            page=page,
            limit=limit,
            has_next=offset + limit < total,
            has_prev=page > 1,
            error=error,
            no_credentials=no_credentials,
        )
        return render(request, "admin/bots/digikey_products.html", ctx)

    def debug_view(self, request):
        import time, json
        import requests as req
        from bots.services.digikey import get_token, _headers, _base_url, ORDERS_PATH, DigiKeyAPIError

        config = DigiKeyConfig.get()
        no_credentials = not (config.client_id and config.client_secret)

        endpoint = request.GET.get("endpoint", "orders")
        calls = []

        if not no_credentials:
            # ── Step 1: Token ──────────────────────────────────────────────────
            t0 = time.time()
            token_call = {"label": "POST /v1/oauth2/token", "ok": False}
            try:
                from bots.services.digikey import TOKEN_PATH, _SANDBOX_BASE, _PROD_BASE
                base = _base_url(config)
                token_url = f"{base}{TOKEN_PATH}"
                token_resp = req.post(
                    token_url,
                    data={
                        "client_id":     config.client_id,
                        "client_secret": "***",
                        "grant_type":    "client_credentials",
                    },
                    timeout=15,
                )
                # Real call for actual token
                token = get_token(config)
                token_call.update({
                    "ok":         True,
                    "url":        token_url,
                    "method":     "POST",
                    "status":     token_resp.status_code,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "request_body": "client_id=*** client_secret=*** grant_type=client_credentials",
                    "response_body": json.dumps({"access_token": token[:16] + "…", "token_type": "Bearer", "expires_in": 600}, indent=2),
                })
            except Exception as e:
                token_call.update({"ok": False, "error": str(e), "duration_ms": int((time.time() - t0) * 1000)})
                calls.append(token_call)
                ctx = dict(self.admin_site.each_context(request), title="DigiKey Debug",
                           config=config, no_credentials=False, calls=calls, endpoint=endpoint)
                return render(request, "admin/bots/digikey_debug.html", ctx)
            calls.append(token_call)

            # ── Step 2: API call ───────────────────────────────────────────────
            t0 = time.time()
            base = _base_url(config)
            hdrs = _headers(config, token)
            # Mask token in display
            display_hdrs = {k: (v[:20] + "…" if k == "Authorization" else v) for k, v in hdrs.items()}

            if endpoint == "orders":
                url    = f"{base}{ORDERS_PATH}"
                params = {"Shared": False, "PageNumber": 1, "PageSize": 25}
                label  = "GET /orderstatus/v4/orders"
            else:
                url    = f"{base}{ORDERS_PATH}"
                params = {"Shared": False, "PageNumber": 1, "PageSize": 25}
                label  = "GET /orderstatus/v4/orders"

            api_call = {"label": label, "url": url, "method": "GET",
                        "request_headers": display_hdrs, "request_params": params}
            try:
                resp = req.get(url, headers=hdrs, params=params, timeout=30)
                duration_ms = int((time.time() - t0) * 1000)
                try:
                    body_parsed = resp.json()
                    body_str = json.dumps(body_parsed, ensure_ascii=False, indent=2)
                except Exception:
                    body_str = resp.text[:10000]

                api_call.update({
                    "ok":           resp.ok,
                    "status":       resp.status_code,
                    "status_text":  resp.reason,
                    "duration_ms":  duration_ms,
                    "response_headers": dict(resp.headers),
                    "response_body":    body_str,
                })
            except Exception as e:
                api_call.update({"ok": False, "error": str(e), "duration_ms": int((time.time() - t0) * 1000)})
            calls.append(api_call)

        ctx = dict(
            self.admin_site.each_context(request),
            title="DigiKey Debug",
            config=config,
            no_credentials=no_credentials,
            calls=calls,
            endpoint=endpoint,
        )
        return render(request, "admin/bots/digikey_debug.html", ctx)

    def po_lookup_view(self, request):
        from bots.services.digikey import get_orders_by_po_number, DigiKeyAPIError
        config = DigiKeyConfig.get()

        po_number = request.GET.get("po", "").strip()
        results = None
        error = None
        no_credentials = not (config.client_id and config.client_secret)

        if po_number and not no_credentials:
            try:
                data = get_orders_by_po_number(config, po_number)
                results = data.get("SalesOrdersDetails") or []
            except DigiKeyAPIError as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"

        ctx = dict(
            self.admin_site.each_context(request),
            title="DigiKey — Пошук за PO номером",
            config=config,
            po_number=po_number,
            results=results,
            error=error,
            no_credentials=no_credentials,
        )
        return render(request, "admin/bots/digikey_po_lookup.html", ctx)

    def confirm_order_view(self, request, order_number):
        """Підтверджує Marketplace замовлення на DigiKey і повертає на форму замовлення."""
        from bots.services.digikey import confirm_marketplace_order, DigiKeyAPIError
        config = DigiKeyConfig.get()
        try:
            result = confirm_marketplace_order(config, order_number)
            if result["ok"]:
                messages.success(request, result["message"])
            else:
                messages.error(request, result["message"])
        except Exception as e:
            messages.error(request, f"❌ {type(e).__name__}: {e}")

        return redirect(reverse("admin:bots_digikeyconfig_marketplace_orders"))

    def api_log_view(self, request):
        """GET/POST — перегляд та налаштування DigiKey API логу."""
        import json as _json
        from django.shortcuts import render, redirect
        from django.urls import reverse
        from tabele.api_logger import get_log

        config = DigiKeyConfig.get()

        if request.method == 'POST':
            try:
                from shipping.models import ShippingSettings
                val = int(request.POST.get('max_entries', 20))
                val = max(1, min(val, 500))
                s = ShippingSettings.get()
                s.api_log_max_entries = val
                s.save(update_fields=['api_log_max_entries'])
                messages.success(request, f'✅ Ліміт логу збережено: {val} записів')
            except (ValueError, TypeError):
                messages.error(request, '❌ Невірне значення ліміту')
            except Exception as e:
                messages.error(request, f'❌ Помилка збереження: {e}')
            return redirect(reverse('admin:bots_digikeyconfig_api_log'))

        try:
            from shipping.models import ShippingSettings
            max_entries = ShippingSettings.get().api_log_max_entries
        except Exception:
            max_entries = 20

        raw_entries = get_log('digikey')
        entries = []
        for e in raw_entries:
            entries.append({
                'ts':          e.get('ts', ''),
                'action':      e.get('action', ''),
                'method':      e.get('method', ''),
                'url':         e.get('url', ''),
                'duration_ms': e.get('duration_ms'),
                'error':       e.get('error'),
                'req_headers': _json.dumps(e.get('request', {}).get('headers') or {}, ensure_ascii=False, indent=2),
                'req_body':    _json.dumps(e.get('request', {}).get('body'),    ensure_ascii=False, indent=2),
                'resp_status': e.get('response', {}).get('status'),
                'resp_body':   _json.dumps(e.get('response', {}).get('body'),   ensure_ascii=False, indent=2),
            })
        return render(request, 'admin/bots/digikey_api_log.html', {
            **self.admin_site.each_context(request),
            'config':      config,
            'service':     'digikey',
            'entries':     entries,
            'max_entries': max_entries,
            'log_data':    raw_entries,
            'title':       'DigiKey API Лог',
        })

    def api_log_clear_view(self, request):
        """POST — очистити DigiKey API лог."""
        from django.shortcuts import redirect
        from django.urls import reverse
        from tabele.api_logger import clear_log
        if request.method == 'POST':
            try:
                clear_log('digikey')
                messages.success(request, '🗑️ DigiKey API лог очищено')
            except Exception as e:
                messages.error(request, f'❌ Помилка: {e}')
        return redirect(reverse('admin:bots_digikeyconfig_api_log'))

    def api_log_download_view(self, request):
        """GET — завантажити DigiKey API лог як JSON."""
        import json as _json
        from django.http import HttpResponse
        from datetime import date as _date
        from tabele.api_logger import get_log
        data = get_log('digikey')
        filename = f'digikey_api_log_{_date.today().isoformat()}.json'
        response = HttpResponse(
            _json.dumps(data, ensure_ascii=False, indent=2),
            content_type='application/json; charset=utf-8',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # ── Readonly display fields ───────────────────────────────────────────────

    def action_buttons(self, obj):
        test_url       = reverse("admin:bots_digikeyconfig_test")
        sync_url       = reverse("admin:bots_digikeyconfig_sync")
        clear_url      = reverse("admin:bots_digikeyconfig_clear_token")
        po_url         = reverse("admin:bots_digikeyconfig_po_lookup")
        products_url   = reverse("admin:bots_digikeyconfig_products")
        debug_url      = reverse("admin:bots_digikeyconfig_debug")
        oauth_url      = reverse("admin:bots_digikeyconfig_oauth_start")
        mkorders_url   = reverse("admin:bots_digikeyconfig_marketplace_orders")
        reconcile_url  = reverse("admin:bots_digikeyconfig_reconcile")
        log_url        = reverse("admin:bots_digikeyconfig_api_log")

        authorized     = bool(obj and obj.marketplace_refresh_token)
        auth_label     = "✅ Marketplace авторизовано" if authorized else "🔑 Авторизувати Marketplace"
        auth_color     = "#2e7d32" if authorized else "#e65100"

        s = 'color:#fff;padding:6px 14px;border-radius:4px;text-decoration:none;display:inline-block;margin:3px 4px 3px 0;white-space:nowrap'
        return format_html(
            '<div style="line-height:2.4">'
            '<a href="{}" style="background:#1565c0;{}">🔌 Тест з\'єднання</a>'
            '<a href="{}" style="background:#455a64;{}">🗑️ Скинути токени</a>'
            '<a href="{}" style="background:#00695c;{}">📦 Компоненти</a>'
            '<a href="{}" style="background:#6a1b9a;{}">🔍 Пошук за PO</a>'
            '<a href="{}" style="background:#37474f;{}">🔬 Debug</a>'
            '<a href="{}" style="background:#4a148c;{}">📋 API Лог</a>'
            '<br>'
            '<a href="{}" style="background:{};{}">Marketplace: {}</a>'
            '<a href="{}" style="background:#1565c0;{}">📋 Marketplace замовлення</a>'
            '<a href="{}" style="background:#2e7d32;{}">🔄 Синхронізувати</a>'
            '<a href="{}" style="background:#f57c00;{}">🔍 Звірити з DigiKey</a>'
            '</div>',
            test_url, s,
            clear_url, s,
            products_url, s,
            po_url, s,
            debug_url, s,
            log_url, s,
            oauth_url, auth_color, s, auth_label,
            mkorders_url, s,
            sync_url, s,
            reconcile_url, s,
        )
    action_buttons.short_description = "Дії"

    def marketplace_auth_status(self, obj):
        from django.utils import timezone
        if not obj or not obj.marketplace_refresh_token:
            return format_html(
                '<span style="color:#ff9800;font-weight:bold">⚠️ Не авторизовано</span> — '
                'натисніть <b>🔑 Авторизувати Marketplace</b>'
            )
        exp = obj.marketplace_token_expires_at
        if exp and exp > timezone.now():
            from django.utils.timesince import timeuntil
            return format_html(
                '<span style="color:#4caf50;font-weight:bold">✅ Авторизовано</span> — '
                'access token дійсний ще ~{} · refresh token збережено',
                timeuntil(exp),
            )
        return format_html(
            '<span style="color:#4caf50;font-weight:bold">✅ Авторизовано</span> — '
            'access token закінчився, оновиться автоматично через refresh token'
        )
    marketplace_auth_status.short_description = "Статус Marketplace авторизації"

    def access_token_preview(self, obj):
        if obj.access_token:
            return format_html(
                '<span style="font-family:monospace;color:#4caf50">{}</span>',
                obj.access_token[:20] + "…",
            )
        return format_html('<span style="color:#607d8b">—</span>')
    access_token_preview.short_description = "Access Token"

    def _public_base(self, obj):
        """Повертає базовий URL без слеша: з поля моделі або з settings."""
        if obj and obj.public_base_url:
            return obj.public_base_url.rstrip("/")
        from django.conf import settings
        uri = getattr(settings, "DIGIKEY_OAUTH_REDIRECT_URI", "")
        if uri:
            # Витягуємо base з повного URL oauth-callback
            return uri.replace("/bots/digikey/oauth-callback/", "").rstrip("/")
        return ""

    def oauth_url_display(self, obj):
        base = self._public_base(obj)
        if not base:
            return format_html('<span style="color:#e3b341">⚠️ Вкажи Публічний URL сайту вище</span>')
        url = f"{base}/bots/digikey/oauth-callback/"
        return format_html(
            '<code style="user-select:all;font-size:13px">{}</code>'
            '<br><small style="color:var(--text-muted,#9aafbe)">Вкажи в DigiKey dev portal → My Apps → OAuth Callback</small>',
            url,
        )
    oauth_url_display.short_description = "OAuth Callback URL"

    def webhook_url_display(self, obj):
        base = self._public_base(obj)
        if not base:
            return format_html('<span style="color:#e3b341">⚠️ Вкажи Публічний URL сайту вище</span>')
        url = f"{base}/bots/digikey/webhook/"
        status_color = "#4caf50" if (obj and obj.webhook_enabled) else "#607d8b"
        status_label = "✅ увімкнено" if (obj and obj.webhook_enabled) else "⭕ вимкнено"
        return format_html(
            '<code style="user-select:all;font-size:13px">{}</code>'
            '&nbsp;&nbsp;<span style="color:{}">{}</span>'
            '<br><small style="color:var(--text-muted,#9aafbe)">Вкажи в DigiKey dev portal → My Apps → Webhooks</small>',
            url, status_color, status_label,
        )
    webhook_url_display.short_description = "Webhook URL"


@admin.register(BotLog)
class BotLogAdmin(admin.ModelAdmin):
    list_display = ("bot", "started_at", "level", "duration", "items_processed", "message_short")
    list_filter = ("bot", "level", "started_at")
    search_fields = ("message", "bot__name")
    readonly_fields = ("bot", "started_at", "finished_at", "duration", "level", 
                       "message", "details", "items_processed", "items_created",
                       "items_updated", "items_failed")
    
    def has_add_permission(self, request):
        return False
    
    def message_short(self, obj):
        return obj.message[:80] + "..." if len(obj.message) > 80 else obj.message
    message_short.short_description = "Повідомлення"
