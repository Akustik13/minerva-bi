"""
bots/admin.py — Адміністрування ботів і DigiKey конфігурації
"""
from django.contrib import admin
from django.urls import reverse, path
from django.shortcuts import redirect, render
from django.contrib import messages
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from .models import Bot, BotLog, DigiKeyConfig, DigiKeyListing, BotTask


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
        "task_status_panel",
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
            "fields": ("client_id", "client_secret", "account_id", "marketplace_supplier_id"),
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
                "sync_order_status",
                "auto_confirm_mode",
                "last_synced_at",
            ),
            "description": (
                "<b>Оновлювати статус замовлення:</b> вимкніть якщо статус "
                "керується трекінгом перевізника (UPS/DHL) або виставляється вручну. "
                "Зазвичай вмикають поки трекінг-номер ще не отримано."
            ),
        }),
        ("🔌 Дії", {
            "fields": ("action_buttons", "task_status_panel"),
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
            path(
                "fetch-supplier-uuid/",
                self.admin_site.admin_view(self.fetch_supplier_uuid_view),
                name="bots_digikeyconfig_fetch_supplier_uuid",
            ),
            path(
                "custom-fields/",
                self.admin_site.admin_view(self.custom_fields_view),
                name="bots_digikeyconfig_custom_fields",
            ),
            path(
                "import-offers/",
                self.admin_site.admin_view(self.import_offers_view),
                name="bots_digikeyconfig_import_offers",
            ),
            path(
                "create-listings/",
                self.admin_site.admin_view(self.create_listings_view),
                name="bots_digikeyconfig_create_listings",
            ),
            path(
                "task-status/",
                self.admin_site.admin_view(self.task_status_view),
                name="bots_digikeyconfig_task_status",
            ),
            path(
                "task-cancel/",
                self.admin_site.admin_view(self.task_cancel_view),
                name="bots_digikeyconfig_task_cancel",
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

        order_pk_map = {}  # businessId → Minerva SalesOrder.pk

        if config.marketplace_refresh_token or config.marketplace_access_token:
            try:
                orders_data = get_marketplace_orders(config, offset=offset, max_results=20)
                raw_json = json.dumps(orders_data, indent=2, ensure_ascii=False)

                # Build businessId → Minerva SalesOrder.pk mapping for action buttons
                biz_ids = [
                    str(o.get("businessId") or o.get("id") or "")
                    for o in (orders_data.get("orders") or [])
                    if o.get("businessId") or o.get("id")
                ]
                if biz_ids:
                    from sales.models import SalesOrder as _SO
                    for so in _SO.objects.filter(source="digikey", order_number__in=biz_ids).values("order_number", "pk"):
                        order_pk_map[so["order_number"]] = so["pk"]
                # Attach minerva_pk directly to each order dict for easy template access
                for o in orders_data.get("orders") or []:
                    biz_id = str(o.get("businessId") or o.get("id") or "")
                    o["minerva_pk"] = order_pk_map.get(biz_id)
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
            order_pk_map=order_pk_map,
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

        config       = DigiKeyConfig.get()
        no_credentials = not (config.client_id and config.client_secret)
        order_number   = request.GET.get("order", "").strip()
        calls          = []
        db_record      = None
        db_lines       = []

        if not no_credentials:
            # ── Step 1: Token (always) ─────────────────────────────────────────
            t0 = time.time()
            token_call = {"label": "POST /v1/oauth2/token", "ok": False, "method": "POST"}
            try:
                from bots.services.digikey import TOKEN_PATH
                base      = _base_url(config)
                token_url = f"{base}{TOKEN_PATH}"
                token = get_token(config)
                token_call.update({
                    "ok":          True,
                    "url":         token_url,
                    "status":      200,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "request_body":  "client_id=*** client_secret=*** grant_type=client_credentials",
                    "response_body": json.dumps({
                        "access_token": token[:20] + "…",
                        "token_type":   "Bearer",
                        "expires_in":   600,
                    }, indent=2),
                })
            except Exception as e:
                token_call.update({"ok": False, "error": str(e), "duration_ms": int((time.time() - t0) * 1000)})
                calls.append(token_call)
                ctx = dict(self.admin_site.each_context(request), title="DigiKey Debug",
                           config=config, no_credentials=False, calls=calls,
                           order_number=order_number, db_record=None, db_lines=[])
                return render(request, "admin/bots/digikey_debug.html", ctx)
            calls.append(token_call)

            base         = _base_url(config)
            hdrs         = _headers(config, token)
            display_hdrs = {k: (v[:20] + "…" if k == "Authorization" else v) for k, v in hdrs.items()}

            if order_number:
                # ── Step 2a: GET /orderstatus/v4/orders/{id} ──────────────────
                t0  = time.time()
                url = f"{base}{ORDERS_PATH}/{order_number}"
                api_call = {
                    "label":           f"GET /orderstatus/v4/orders/{order_number}",
                    "url":             url,
                    "method":          "GET",
                    "request_headers": display_hdrs,
                }
                try:
                    resp        = req.get(url, headers=hdrs, timeout=30)
                    duration_ms = int((time.time() - t0) * 1000)
                    try:
                        body_parsed = resp.json()
                        body_str    = json.dumps(body_parsed, ensure_ascii=False, indent=2)
                    except Exception:
                        body_str = resp.text[:15000]
                    api_call.update({
                        "ok":               resp.ok,
                        "status":           resp.status_code,
                        "status_text":      resp.reason,
                        "duration_ms":      duration_ms,
                        "response_headers": {k: v for k, v in resp.headers.items()
                                             if k.lower() in ("content-type", "x-request-id",
                                                               "x-correlationid", "x-ratelimit-remaining")},
                        "response_body":    body_str,
                    })
                except Exception as e:
                    api_call.update({"ok": False, "error": str(e),
                                     "duration_ms": int((time.time() - t0) * 1000)})
                calls.append(api_call)

                # ── Step 2b: search Marketplace by businessId / order number ──
                try:
                    from bots.services.digikey import get_marketplace_token, get_marketplace_orders
                    mk_token = get_marketplace_token(config)
                    if mk_token:
                        mk_hdrs = {**hdrs, "Authorization": f"Bearer {mk_token}"}
                        mk_disp = {k: (v[:20] + "…" if k == "Authorization" else v)
                                   for k, v in mk_hdrs.items()}
                        t0 = time.time()
                        mk_url = (f"{base}/Sales/Marketplace2/Orders/v1/orders"
                                  f"?status=All&offset=0&limit=5&orderNumber={order_number}")
                        mk_call = {
                            "label":           f"GET /Sales/Marketplace2/Orders (order={order_number})",
                            "url":             mk_url,
                            "method":          "GET",
                            "request_headers": mk_disp,
                        }
                        resp2 = req.get(mk_url, headers=mk_hdrs, timeout=30)
                        duration_ms2 = int((time.time() - t0) * 1000)
                        try:
                            mk_body = json.dumps(resp2.json(), ensure_ascii=False, indent=2)
                        except Exception:
                            mk_body = resp2.text[:15000]
                        mk_call.update({
                            "ok":               resp2.ok,
                            "status":           resp2.status_code,
                            "status_text":      resp2.reason,
                            "duration_ms":      duration_ms2,
                            "response_headers": {k: v for k, v in resp2.headers.items()
                                                 if k.lower() in ("content-type", "x-request-id",
                                                                   "x-correlationid")},
                            "response_body":    mk_body,
                        })
                        calls.append(mk_call)
                except Exception:
                    pass  # marketplace token not available — skip silently

                # ── Step 3: Minerva DB record ──────────────────────────────────
                try:
                    from sales.models import SalesOrder, SalesOrderLine
                    db_record = SalesOrder.objects.filter(order_number=order_number).first()
                    if db_record:
                        db_lines = list(SalesOrderLine.objects.filter(order=db_record).select_related("product"))
                except Exception:
                    pass

            else:
                # ── Default: list last 25 orders ──────────────────────────────
                t0     = time.time()
                url    = f"{base}{ORDERS_PATH}"
                params = {"Shared": False, "PageNumber": 1, "PageSize": 25}
                api_call = {
                    "label":           "GET /orderstatus/v4/orders (last 25)",
                    "url":             url,
                    "method":          "GET",
                    "request_headers": display_hdrs,
                    "request_params":  params,
                }
                try:
                    resp        = req.get(url, headers=hdrs, params=params, timeout=30)
                    duration_ms = int((time.time() - t0) * 1000)
                    try:
                        body_str = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                    except Exception:
                        body_str = resp.text[:15000]
                    api_call.update({
                        "ok":               resp.ok,
                        "status":           resp.status_code,
                        "status_text":      resp.reason,
                        "duration_ms":      duration_ms,
                        "response_headers": {k: v for k, v in resp.headers.items()
                                             if k.lower() in ("content-type", "x-request-id")},
                        "response_body":    body_str,
                    })
                except Exception as e:
                    api_call.update({"ok": False, "error": str(e),
                                     "duration_ms": int((time.time() - t0) * 1000)})
                calls.append(api_call)

        ctx = dict(
            self.admin_site.each_context(request),
            title="DigiKey Debug",
            config=config,
            no_credentials=no_credentials,
            calls=calls,
            order_number=order_number,
            db_record=db_record,
            db_lines=db_lines,
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

    def fetch_supplier_uuid_view(self, request):
        """Decode marketplace JWT token and show all claims to find supplier UUID."""
        from bots.models import DigiKeyConfig
        from bots.services.dk_marketplace import fetch_supplier_uuid, DKMarketplaceError
        from django.utils.html import format_html
        config = DigiKeyConfig.get()
        try:
            uuids = fetch_supplier_uuid(config)
            if isinstance(uuids, dict):
                for uid, name in uuids.items():
                    self.message_user(request,
                        f'✅ Supplier UUID: {uid}  (назва: {name or "—"}) — '
                        f'скопіюй в поле "Marketplace Vendor ID" вище.',
                        level='success')
        except DKMarketplaceError as e:
            self.message_user(request, f'❌ {e}', level='error')
        except Exception as e:
            self.message_user(request, f'❌ Помилка: {e}', level='error')
        return redirect(reverse('admin:bots_digikeyconfig_change', args=[1]))

    def custom_fields_view(self, request):
        """Fetch Product custom field definitions from DigiKey Custom API."""
        from bots.models import DigiKeyConfig
        from bots.services.dk_marketplace import fetch_custom_fields, DKMarketplaceError
        config = DigiKeyConfig.get()
        try:
            fields = fetch_custom_fields(config)
            if not fields:
                self.message_user(request, '⚠️ Поля не знайдено (порожня відповідь)', level='warning')
            else:
                for f in fields:
                    req_mark = '✅ required' if f.get('required') else '—'
                    vals = ', '.join(f.get('fieldValues', []) or []) or ''
                    self.message_user(
                        request,
                        f'📋 code={f.get("code","?")} | {f.get("name","?")} | '
                        f'{f.get("fieldType","?")} | {req_mark}'
                        + (f' | values: {vals}' if vals else ''),
                        level='info',
                    )
        except DKMarketplaceError as e:
            self.message_user(request, f'❌ {e}', level='error')
        except Exception as e:
            self.message_user(request, f'❌ Помилка: {e}', level='error')
        return redirect(reverse('admin:bots_digikeyconfig_change', args=[1]))

    def import_offers_view(self, request):
        """Start import_offers in a background thread, track via BotTask."""
        import threading
        from bots.services.dk_marketplace import import_offers_from_dk

        task = BotTask.start('import_offers')

        def _run():
            try:
                result = import_offers_from_dk(task=task)
                task.finish(
                    f"✅ Оновлено {result['updated']} лістингів. "
                    + (f"Не знайдено SKU: {', '.join(result['not_found'][:20])}" if result['not_found'] else "")
                )
            except InterruptedError as e:
                task.finish(f"⛔ {e}")
            except Exception as exc:
                task.finish(f"❌ {exc}", error=True)
                logger.error("DK import_offers FAILED: %s", exc, exc_info=True)
            finally:
                from django.db import connection
                connection.close()

        threading.Thread(target=_run, daemon=True).start()
        self.message_user(request, "⏳ Імпорт офферів запущено у фоні.", messages.INFO)
        return redirect(reverse('admin:bots_digikeyconfig_change', args=[1]))

    def create_listings_view(self, request):
        """Start create_listings in a background thread, track via BotTask."""
        import threading
        from bots.services.dk_marketplace import create_listings_from_offers

        task = BotTask.start('create_listings')

        def _run():
            try:
                result = create_listings_from_offers(task=task)
                task.finish(
                    f"✅ Створено {result['created']} лістингів. "
                    f"Вже існувало: {result['already_exists']}. "
                    + (f"SKU без товару: {', '.join(result['no_product'][:20])}" if result['no_product'] else "")
                )
            except InterruptedError as e:
                task.finish(f"⛔ {e}")
            except Exception as exc:
                task.finish(f"❌ {exc}", error=True)
                logger.error("DK create_listings FAILED: %s", exc, exc_info=True)
            finally:
                from django.db import connection
                connection.close()

        threading.Thread(target=_run, daemon=True).start()
        self.message_user(request, "⏳ Створення лістингів запущено у фоні.", messages.INFO)
        return redirect(reverse('admin:bots_digikeyconfig_change', args=[1]))

    def task_status_view(self, request):
        from django.http import JsonResponse
        name = request.GET.get('name', '')
        try:
            t = BotTask.objects.get(name=name)
            return JsonResponse({
                'status':   t.status,
                'progress': t.progress,
                'message':  t.message,
                'started':  t.started_at.strftime('%H:%M:%S') if t.started_at else '',
                'finished': t.finished_at.strftime('%H:%M:%S') if t.finished_at else '',
            })
        except BotTask.DoesNotExist:
            return JsonResponse({'status': 'idle', 'progress': '', 'message': '', 'started': '', 'finished': ''})

    def task_cancel_view(self, request):
        from django.http import JsonResponse
        name = request.GET.get('name', '')
        updated = BotTask.objects.filter(name=name, status='running').update(cancel_requested=True)
        return JsonResponse({'ok': bool(updated)})

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
        supplier_uuid_url  = reverse("admin:bots_digikeyconfig_fetch_supplier_uuid")
        custom_fields_url  = reverse("admin:bots_digikeyconfig_custom_fields")
        import_offers_url    = reverse("admin:bots_digikeyconfig_import_offers")
        create_listings_url  = reverse("admin:bots_digikeyconfig_create_listings")

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
            '<a href="{}" style="background:#00838f;{}">🪪 Отримати Supplier UUID</a>'
            '<a href="{}" style="background:#4527a0;{}">📋 Custom Fields</a>'
            '<a href="{}" style="background:#00695c;{}">📥 Імпорт офферів</a>'
            '<a href="{}" style="background:#00838f;{}">🆕 Створити лістинги з DigiKey</a>'
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
            supplier_uuid_url, s,
            custom_fields_url, s,
            import_offers_url, s,
            create_listings_url, s,
        )
    action_buttons.short_description = "Дії"

    def task_status_panel(self, obj):
        status_url = reverse('admin:bots_digikeyconfig_task_status')
        cancel_url = reverse('admin:bots_digikeyconfig_task_cancel')
        tasks_html = ''
        for name, label in [('import_offers', '📥 Імпорт офферів'), ('create_listings', '🆕 Створити лістинги')]:
            try:
                t = BotTask.objects.get(name=name)
            except BotTask.DoesNotExist:
                t = None
            status  = t.status if t else 'idle'
            progress = t.progress if t else ''
            message  = t.message if t else ''
            started  = t.started_at.strftime('%H:%M:%S') if (t and t.started_at) else ''
            finished = t.finished_at.strftime('%H:%M:%S') if (t and t.finished_at) else ''

            if status == 'running':
                icon = '⏳'; color = '#ff9800'
                info = f'Запущено о {started} · {progress}'
                cancel_btn = (
                    f'<button onclick="dkCancelTask(\'{name}\')" '
                    f'style="margin-left:10px;background:#c62828;color:#fff;border:none;'
                    f'padding:3px 10px;border-radius:4px;cursor:pointer;font-size:12px">'
                    f'⛔ Зупинити</button>'
                )
            elif status == 'done':
                icon = '✅'; color = '#4caf50'
                info = f'Завершено о {finished} · {message}'
                cancel_btn = ''
            elif status == 'error':
                icon = '❌'; color = '#e53935'
                info = f'Помилка о {finished} · {message}'
                cancel_btn = ''
            else:
                icon = '💤'; color = '#9aafbe'
                info = 'Не запускалось'
                cancel_btn = ''

            tasks_html += (
                f'<div id="dkTask_{name}" style="display:flex;align-items:center;gap:8px;'
                f'padding:6px 10px;margin:4px 0;border-radius:6px;background:rgba(0,0,0,.15);'
                f'font-size:13px;color:#cfd8dc">'
                f'<span style="min-width:160px;font-weight:bold">{label}</span>'
                f'<span style="color:{color}">{icon}</span>'
                f'<span id="dkTaskInfo_{name}" style="color:#b0bec5;flex:1">{info}</span>'
                f'{cancel_btn}'
                f'</div>'
            )

        script = f"""
<script>
(function(){{
  var STATUS_URL = '{status_url}';
  var CANCEL_URL = '{cancel_url}';
  var TASKS = ['import_offers','create_listings'];
  var _timer = null;

  function pollAll() {{
    TASKS.forEach(function(name) {{
      fetch(STATUS_URL + '?name=' + name, {{credentials:'same-origin'}})
        .then(function(r){{return r.json();}})
        .then(function(d){{updateTask(name, d);}})
        .catch(function(){{}});
    }});
  }}

  function updateTask(name, d) {{
    var infoEl = document.getElementById('dkTaskInfo_' + name);
    var taskEl = document.getElementById('dkTask_' + name);
    if (!infoEl) return;
    var icons   = {{idle:'💤', running:'⏳', done:'✅', error:'❌'}};
    var colors  = {{idle:'#9aafbe', running:'#ff9800', done:'#4caf50', error:'#e53935'}};
    var icon    = icons[d.status] || '❓';
    var color   = colors[d.status] || '#ccc';
    var iconEl  = taskEl.querySelector('span[style*="color"]');
    if (iconEl) {{ iconEl.textContent = icon; iconEl.style.color = color; }}

    if (d.status === 'running') {{
      infoEl.textContent = 'Запущено о ' + d.started + ' · ' + (d.progress || '...');
      ensureCancelBtn(name, taskEl);
      if (!_timer) _timer = setInterval(pollAll, 2000);
    }} else {{
      removeCancelBtn(taskEl);
      if (d.status === 'done' || d.status === 'error') {{
        infoEl.textContent = (d.status==='done'?'Завершено':'Помилка') + ' о ' + d.finished + ' · ' + d.message;
      }} else {{
        infoEl.textContent = 'Не запускалось';
      }}
      var stillRunning = TASKS.some(function(n){{
        var el = document.getElementById('dkTaskInfo_' + n);
        return el && el.closest('[id^=dkTask_]') && el.textContent.indexOf('Запущено') === 0;
      }});
      if (!stillRunning && _timer) {{ clearInterval(_timer); _timer = null; }}
    }}
  }}

  function ensureCancelBtn(name, taskEl) {{
    if (taskEl.querySelector('.dk-cancel-btn')) return;
    var btn = document.createElement('button');
    btn.className = 'dk-cancel-btn';
    btn.textContent = '⛔ Зупинити';
    btn.style.cssText = 'margin-left:10px;background:#c62828;color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:12px';
    btn.onclick = function(){{ dkCancelTask(name); }};
    taskEl.appendChild(btn);
  }}

  function removeCancelBtn(taskEl) {{
    var btn = taskEl.querySelector('.dk-cancel-btn');
    if (btn) btn.remove();
  }}

  window.dkCancelTask = function(name) {{
    if (!confirm('Зупинити операцію? Поточний SKU завершить обробку.')) return;
    fetch(CANCEL_URL + '?name=' + name, {{credentials:'same-origin'}})
      .then(function(r){{return r.json();}})
      .then(function(d){{
        var infoEl = document.getElementById('dkTaskInfo_' + name);
        if (infoEl) infoEl.textContent = '⛔ Запит на скасування відправлено…';
      }});
  }};

  /* Auto-start polling if any task is running */
  var anyRunning = {str(BotTask.objects.filter(status='running').exists()).lower()};
  if (anyRunning) {{ _timer = setInterval(pollAll, 2000); }}

  /* Poll once immediately to refresh stale initial state */
  pollAll();
}})();
</script>"""

        return mark_safe(tasks_html + script)
    task_status_panel.short_description = "📊 Фонові завдання"

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


# ── DigiKey Marketplace Listings ──────────────────────────────────────────────

@admin.register(DigiKeyListing)
class DigiKeyListingAdmin(admin.ModelAdmin):
    change_form_template = 'admin/bots/digikeylisting/change_form.html'

    list_display   = ('product_sku_link', 'dk_title', 'category_type',
                      'stock_qty_display', 'sync_status_badge',
                      'last_synced_at', 'publish_btn')
    list_filter    = ('sync_status', 'category_type', 'dk_is_active')
    search_fields  = ('product__sku', 'dk_title', 'dk_supplier_sku')
    autocomplete_fields = ('product',)
    actions        = [
        'action_sync_qty',
        'action_bulk_publish',
        'action_bulk_activate',
        'action_bulk_deactivate',
        'action_bulk_prices',
    ]
    save_as        = True

    readonly_fields = (
        'dk_product_id', 'dk_offer_id',
        'sync_status', 'last_synced_at', 'last_error',
        'created_at', 'updated_at',
        'stock_qty_readonly', 'dk_attributes_table',
    )

    fieldsets = (
        ('📦 Товар', {
            'fields': ('product', 'category_type', 'dk_is_active'),
        }),
        ('🏷️ DigiKey Каталог', {
            'fields': (
                'dk_category_id', 'dk_category_name',
                'dk_title', 'dk_description',
                'dk_manufacturer', 'dk_image_url', 'dk_datasheet_url',
            ),
            'description': (
                'Натисни <b>📥 Стягнути поля з DigiKey</b> вище — всі поля заповнюються автоматично.'
            ),
        }),
        ('🛒 Offer (Пропозиція)', {
            'fields': (
                'dk_supplier_sku', 'dk_min_order_qty',
                'dk_lead_time_days', 'dk_qty_alert',
                'stock_qty_readonly',
            ),
        }),
        ('💰 Цінові тири (JSON)', {
            'fields': ('dk_prices',),
            'description': 'Керуй цінами через візуальний редактор нижче. JSON оновлюється автоматично.',
            'classes': ('collapse',),
        }),
        ('📋 Обов\'язкові атрибути DigiKey', {
            'fields': ('dk_packaging', 'dk_lifecycle_status'),
            'description': (
                'Заповнюються автоматично при натисканні «📥 Стягнути поля з DigiKey».'
            ),
        }),
        ('📡 Технічні атрибути DigiKey', {
            'fields': ('dk_attributes',),
            'description': (
                'Всі технічні атрибути товару з DigiKey (Antenna Type, Gain, Frequency Range, VSWR тощо). '
                'Редагуються через таблицю нижче. Заповнюються кнопкою «📥 Стягнути поля з DigiKey».'
            ),
        }),
        ('🔧 RF Filter — спеціальні поля', {
            'fields': (
                'fa_frequency', 'fa_bandwidth', 'fa_filter_type',
                'fa_ripple', 'fa_insertion_loss', 'fa_mounting_type',
                'fa_package_case', 'fa_size_dimension', 'fa_height_max',
            ),
            'classes': ('collapse',),
            'description': (
                'Тільки для категорії RF Filter — передаються в DigiKey API як additionalFields. '
                'Для антен, кабелів, конекторів — використовуй таблицю вище (Технічні атрибути DigiKey).'
            ),
        }),
        ('📊 Статус синхронізації', {
            'fields': (
                'dk_product_id', 'dk_offer_id',
                'sync_status', 'last_synced_at', 'last_error',
                'created_at', 'updated_at',
            ),
            'classes': ('collapse',),
        }),
    )

    # ── Save / copy ───────────────────────────────────────────────────────────

    def save_model(self, request, obj, form, change):
        if '_saveasnew' in request.POST:
            obj.dk_offer_id    = ''
            obj.dk_product_id  = ''
            obj.sync_status    = DigiKeyListing.SYNC_DRAFT
            obj.last_error     = ''
            obj.last_synced_at = None
        super().save_model(request, obj, form, change)

    # ── change_view: inject buttons ───────────────────────────────────────────

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            extra_context['publish_url'] = reverse(
                'admin:bots_digikeylisting_publish', args=[object_id]
            )
            extra_context['sync_qty_url'] = reverse(
                'admin:bots_digikeylisting_sync_qty', args=[object_id]
            )
            extra_context['create_offer_url'] = reverse(
                'admin:bots_digikeylisting_create_offer', args=[object_id]
            )
            extra_context['pull_product_url'] = reverse(
                'admin:bots_digikeylisting_pull_product', args=[object_id]
            )
            # For list of existing sync status choices — used in template
            extra_context['sync_choices'] = DigiKeyListing.SYNC_CHOICES
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('<int:pk>/publish/',
                 self.admin_site.admin_view(self.publish_view),
                 name='bots_digikeylisting_publish'),
            path('<int:pk>/sync-qty/',
                 self.admin_site.admin_view(self.sync_qty_view),
                 name='bots_digikeylisting_sync_qty'),
            path('<int:pk>/create-offer/',
                 self.admin_site.admin_view(self.create_offer_view),
                 name='bots_digikeylisting_create_offer'),
            path('<int:pk>/pull-product/',
                 self.admin_site.admin_view(self.pull_product_view),
                 name='bots_digikeylisting_pull_product'),
        ]
        return custom + urls

    def publish_view(self, request, pk):
        from bots.services.dk_marketplace import publish_listing, DKMarketplaceError
        listing = DigiKeyListing.objects.select_related('product').get(pk=pk)
        try:
            publish_listing(listing)
            messages.success(
                request,
                f"✅ «{listing.product.sku}» успішно опубліковано на DigiKey. "
                f"Product ID: {listing.dk_product_id} | Offer ID: {listing.dk_offer_id}"
            )
        except DKMarketplaceError as exc:
            messages.error(request, f"❌ Помилка публікації: {exc}")
        except Exception as exc:
            messages.error(request, f"❌ Помилка: {exc}")
        return redirect(
            reverse('admin:bots_digikeylisting_change', args=[pk])
        )

    def sync_qty_view(self, request, pk):
        from bots.services.dk_marketplace import sync_quantity, DKMarketplaceError
        listing = DigiKeyListing.objects.select_related('product').get(pk=pk)
        try:
            stock = listing.get_stock_qty()
            sync_quantity(listing)
            messages.success(
                request,
                f"✅ Залишок оновлено: {listing.product.sku} → {stock} шт."
            )
        except DKMarketplaceError as exc:
            messages.error(request, f"❌ Помилка оновлення залишку: {exc}")
        except Exception as exc:
            messages.error(request, f"❌ Помилка: {exc}")
        return redirect(
            reverse('admin:bots_digikeylisting_change', args=[pk])
        )

    def create_offer_view(self, request, pk):
        from bots.services.dk_marketplace import create_offer_for_listing, DKMarketplaceError
        listing = DigiKeyListing.objects.select_related('product').get(pk=pk)
        try:
            create_offer_for_listing(listing)
            messages.success(
                request,
                f"✅ Offer створено: {listing.product.sku} | Offer ID: {listing.dk_offer_id}"
            )
        except DKMarketplaceError as exc:
            messages.error(request, f"❌ Помилка створення Offer: {exc}")
        except Exception as exc:
            messages.error(request, f"❌ Помилка: {exc}")
        return redirect(reverse('admin:bots_digikeylisting_change', args=[pk]))

    def pull_product_view(self, request, pk):
        """Pull all fields from DigiKey. Supports both regular redirect and AJAX JSON."""
        from bots.services.dk_marketplace import pull_product_fields, DKMarketplaceError
        from django.http import JsonResponse

        is_ajax = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.GET.get('ajax') == '1'
        )
        listing = DigiKeyListing.objects.select_related('product').get(pk=pk)

        try:
            result  = pull_product_fields(listing)
            changed = result['changed']
            prod    = result.get('raw_product') or {}
            offer   = result.get('raw_offer')

            if is_ajax:
                return JsonResponse({
                    'ok': True,
                    'changed': changed,
                    'data': {
                        'dk_title':            listing.dk_title,
                        'dk_description':      listing.dk_description,
                        'dk_manufacturer':     listing.dk_manufacturer,
                        'dk_image_url':        listing.dk_image_url,
                        'dk_datasheet_url':    listing.dk_datasheet_url,
                        'dk_category_id':      listing.dk_category_id,
                        'dk_category_name':    getattr(listing, 'dk_category_name', ''),
                        'dk_packaging':        listing.dk_packaging,
                        'dk_lifecycle_status': listing.dk_lifecycle_status,
                        'category_type':       listing.category_type,
                        'dk_attributes':       listing.dk_attributes or {},
                        'dk_prices':           listing.dk_prices or [],
                        'fa_frequency':        listing.fa_frequency,
                        'fa_bandwidth':        listing.fa_bandwidth,
                        'fa_filter_type':      listing.fa_filter_type,
                        'fa_ripple':           listing.fa_ripple,
                        'fa_insertion_loss':   listing.fa_insertion_loss,
                        'fa_mounting_type':    listing.fa_mounting_type,
                        'fa_package_case':     listing.fa_package_case,
                        'fa_size_dimension':   listing.fa_size_dimension,
                        'fa_height_max':       listing.fa_height_max,
                    },
                    'raw_product': prod,
                    'raw_offer':   offer,
                })

            # ── Non-AJAX: redirect with messages ──────────────────────────
            if changed:
                messages.success(request, f"✅ Оновлено {len(changed)} полів: {', '.join(changed)}")
            else:
                messages.info(request, "ℹ️ Всі поля вже актуальні.")

            add_fields = prod.get('additionalFields', [])
            messages.info(
                request,
                f"🔍 PRODUCT | id={prod.get('_id')} | title={prod.get('title')!r} | "
                f"categoryId={prod.get('categoryId')!r} | attrs={len(add_fields)}"
            )
            if offer:
                messages.info(
                    request,
                    f"🔍 OFFER | id={offer.get('id')} | qty={offer.get('quantityAvailable')} | "
                    f"prices={len(offer.get('prices', []))}"
                )
            else:
                messages.warning(request, "⚠️ Offer не знайдено — SKU не збігається або offer ще не створено.")

        except DKMarketplaceError as exc:
            if is_ajax:
                return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
            messages.error(request, f"❌ {exc}")
        except Exception as exc:
            import traceback
            err_msg = f"❌ {exc}"
            if is_ajax:
                return JsonResponse({'ok': False, 'error': err_msg}, status=500)
            messages.error(request, f"{err_msg} | {traceback.format_exc()[-300:]}")

        return redirect(reverse('admin:bots_digikeylisting_change', args=[pk]))

    # ── Bulk action: sync quantities ──────────────────────────────────────────

    @admin.action(description='🔄 Оновити залишки на DigiKey')
    def action_sync_qty(self, request, queryset):
        from bots.services.dk_marketplace import sync_quantity, DKMarketplaceError
        ok = err = 0
        for listing in queryset.filter(dk_offer_id__gt='').select_related('product'):
            try:
                sync_quantity(listing)
                ok += 1
            except Exception as exc:
                logger.warning("sync_qty failed %s: %s", listing.product.sku, exc)
                err += 1
        if ok:
            self.message_user(request, f"✅ Оновлено залишки для {ok} лістингів.", messages.SUCCESS)
        if err:
            self.message_user(request, f"⚠️ Помилки для {err} лістингів (перевір Last Error).", messages.WARNING)

    @admin.action(description='🚀 Опублікувати / Оновити на DigiKey')
    def action_bulk_publish(self, request, queryset):
        from bots.services.dk_marketplace import publish_listing, DKMarketplaceError
        ok = err = 0
        for listing in queryset.select_related('product'):
            try:
                publish_listing(listing)
                ok += 1
            except Exception as exc:
                listing.sync_status = DigiKeyListing.SYNC_ERROR
                listing.last_error  = str(exc)
                listing.save(update_fields=['sync_status', 'last_error'])
                logger.warning("bulk_publish failed %s: %s", listing.product.sku, exc)
                err += 1
        if ok:
            self.message_user(request, f"✅ Опубліковано / оновлено {ok} лістингів на DigiKey.", messages.SUCCESS)
        if err:
            self.message_user(request, f"⚠️ Помилки для {err} лістингів — перевір Last Error.", messages.WARNING)

    @admin.action(description='✅ Активувати на DigiKey (dk_is_active=True)')
    def action_bulk_activate(self, request, queryset):
        self._bulk_set_active(request, queryset, active=True)

    @admin.action(description='❌ Деактивувати на DigiKey (dk_is_active=False)')
    def action_bulk_deactivate(self, request, queryset):
        self._bulk_set_active(request, queryset, active=False)

    def _bulk_set_active(self, request, queryset, active: bool):
        from bots.services.dk_marketplace import update_offer, DKMarketplaceError
        from bots.models import DigiKeyConfig
        label = "активовано" if active else "деактивовано"
        queryset.update(dk_is_active=active)
        pushed = err = 0
        config = DigiKeyConfig.get()
        for listing in queryset.filter(dk_offer_id__gt='').select_related('product'):
            try:
                update_offer(config, listing)
                pushed += 1
            except Exception as exc:
                logger.warning("bulk_active failed %s: %s", listing.product.sku, exc)
                err += 1
        msg = f"✅ {queryset.count()} лістингів {label} локально"
        if pushed:
            msg += f", {pushed} оновлено на DigiKey"
        self.message_user(request, msg + ".", messages.SUCCESS)
        if err:
            self.message_user(request, f"⚠️ Не вдалося оновити на DigiKey: {err} лістингів.", messages.WARNING)

    @admin.action(description='💰 Змінити ціни (масово)')
    def action_bulk_prices(self, request, queryset):
        import json
        from django.template.response import TemplateResponse
        from bots.services.dk_marketplace import update_offer, DKMarketplaceError
        from bots.models import DigiKeyConfig

        if 'apply' in request.POST:
            raw = request.POST.get('bulk_prices_json', '[]')
            try:
                tiers = json.loads(raw)
                if not tiers:
                    raise ValueError("empty")
            except (ValueError, TypeError):
                self.message_user(request, "❌ Невалідний JSON цін.", messages.ERROR)
                return

            prices_json = json.dumps(tiers)
            push_to_dk  = request.POST.get('push_to_dk') == '1'
            updated = pushed = err = 0

            for listing in queryset.select_related('product'):
                listing.dk_prices = prices_json
                listing.save(update_fields=['dk_prices'])
                updated += 1
                if push_to_dk and listing.dk_offer_id:
                    try:
                        config = DigiKeyConfig.get()
                        update_offer(config, listing)
                        pushed += 1
                    except Exception as exc:
                        logger.warning("bulk_prices push failed %s: %s", listing.product.sku, exc)
                        err += 1

            msg = f"✅ Ціни оновлено для {updated} лістингів"
            if pushed:
                msg += f", {pushed} оновлено на DigiKey"
            self.message_user(request, msg + ".", messages.SUCCESS)
            if err:
                self.message_user(request, f"⚠️ Не вдалося оновити на DigiKey: {err} лістингів.", messages.WARNING)
            return

        # Render intermediate form
        skus = ", ".join(
            queryset.select_related('product').values_list('product__sku', flat=True)[:20]
        )
        if queryset.count() > 20:
            skus += f" … і ще {queryset.count() - 20}"

        return TemplateResponse(request, 'admin/bots/digikeylisting/bulk_prices.html', {
            'title':   'Масова зміна цін',
            'count':   queryset.count(),
            'skus':    skus,
            'pks':     list(queryset.values_list('pk', flat=True)),
            'opts':    self.model._meta,
        })

    # ── Display helpers ───────────────────────────────────────────────────────

    def product_sku_link(self, obj):
        listing_url  = reverse('admin:bots_digikeylisting_change', args=[obj.pk])
        inventory_url = reverse('admin:inventory_product_change', args=[obj.product_id])
        return format_html(
            '<a href="{}">{}</a>'
            '&nbsp;<a href="{}" title="Картка складу" style="color:var(--text-muted);font-size:11px">🏭</a>',
            listing_url, obj.product.sku, inventory_url,
        )
    product_sku_link.short_description = 'Товар (SKU)'
    product_sku_link.admin_order_field = 'product__sku'

    def stock_qty_display(self, obj):
        qty = obj.get_stock_qty()
        color = 'var(--ok)' if qty > 0 else 'var(--err)'
        return format_html('<span style="color:{};font-weight:600">{} шт.</span>', color, qty)
    stock_qty_display.short_description = 'Залишок'

    def stock_qty_readonly(self, obj):
        if obj and obj.pk:
            qty = obj.get_stock_qty()
            wh_color = 'var(--ok)' if qty > 0 else 'var(--err)'

            dk_qty = obj.dk_quantity_available
            if dk_qty is not None:
                dk_color  = '#4caf50' if dk_qty > 0 else '#e53935'
                dk_part   = format_html(
                    ' &nbsp;·&nbsp; '
                    '<span style="color:{};font-weight:600">{} шт.</span>'
                    '<span style="color:var(--text-muted);margin-left:6px;font-size:12px">'
                    '(показано на DigiKey)</span>',
                    dk_color, dk_qty,
                )
            else:
                dk_part = format_html(
                    ' &nbsp;·&nbsp; '
                    '<span style="color:var(--text-muted);font-size:12px">'
                    'DigiKey: не синхронізовано</span>'
                )

            return format_html(
                '<strong style="color:{};font-size:15px">{} шт.</strong>'
                '<span style="color:var(--text-muted);margin-left:8px;font-size:12px">'
                '(наш склад)</span>'
                '{}',
                wh_color, qty, dk_part,
            )
        return '—'
    stock_qty_readonly.short_description = 'Поточний залишок'

    def dk_attributes_table(self, obj):
        if not obj or not obj.dk_attributes:
            return format_html(
                '<span style="color:var(--text-muted)">Порожньо — натисни '
                '📥 Стягнути поля з DigiKey щоб заповнити.</span>'
            )
        rows = ''
        for code, val in sorted(obj.dk_attributes.items()):
            rows += (
                f'<tr>'
                f'<td style="padding:3px 10px;color:var(--text-muted);font-family:monospace;font-size:12px">'
                f'{code}</td>'
                f'<td style="padding:3px 10px;font-size:13px">{val}</td>'
                f'</tr>'
            )
        return format_html(
            '<table style="border-collapse:collapse;font-size:13px;max-width:700px">'
            '<thead><tr>'
            '<th style="padding:4px 10px;text-align:left;font-size:11px;color:var(--text-muted);'
            'border-bottom:1px solid var(--border-strong);text-transform:uppercase">Code</th>'
            '<th style="padding:4px 10px;text-align:left;font-size:11px;color:var(--text-muted);'
            'border-bottom:1px solid var(--border-strong);text-transform:uppercase">Value</th>'
            '</tr></thead>'
            '<tbody>{}</tbody></table>',
            format_html(rows)
        )
    dk_attributes_table.short_description = 'Атрибути DigiKey (raw)'

    _STATUS_COLORS = {
        'draft':     ('#607d8b', '⬜'),
        'published': ('#4caf50', '✅'),
        'error':     ('#e53935', '❌'),
    }

    def sync_status_badge(self, obj):
        color, icon = self._STATUS_COLORS.get(obj.sync_status, ('#607d8b', '?'))
        label = obj.get_sync_status_display()
        return format_html(
            '<span style="color:{};font-weight:600">{} {}</span>', color, icon, label
        )
    sync_status_badge.short_description = 'Статус'
    sync_status_badge.admin_order_field = 'sync_status'

    def publish_btn(self, obj):
        if not obj.pk:
            return '—'
        url = reverse('admin:bots_digikeylisting_publish', args=[obj.pk])
        label = '🔄 Оновити' if obj.dk_offer_id else '🚀 Опублікувати'
        return format_html(
            '<a class="button" href="{}" style="padding:3px 8px;font-size:12px">{}</a>',
            url, label
        )
    publish_btn.short_description = 'Дія'

    def prices_widget(self, obj):
        """Read-only preview of pricing tiers in a table."""
        if not obj or not obj.dk_prices:
            return format_html(
                '<span style="color:var(--text-muted)">Заповни поле «Цінові тири» нижче у форматі JSON</span>'
            )
        rows = ''
        for i, tier in enumerate(obj.dk_prices, 1):
            qty   = tier.get('qty', '?')
            price = tier.get('price', '?')
            bg    = 'var(--bg-hover)' if i % 2 == 0 else 'transparent'
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:3px 10px">{i}</td>'
                f'<td style="padding:3px 10px;font-weight:600">{qty}</td>'
                f'<td style="padding:3px 10px;color:var(--ok)">€&nbsp;{price}</td>'
                f'</tr>'
            )
        return format_html(
            '<table style="border-collapse:collapse;font-size:13px;min-width:200px">'
            '<thead><tr style="color:var(--text-muted);font-size:11px">'
            '<th style="padding:3px 10px">#</th>'
            '<th style="padding:3px 10px">Qty Break</th>'
            '<th style="padding:3px 10px">Ціна (EUR)</th>'
            '</tr></thead><tbody>{}</tbody></table>',
            format_html(''.join(rows)),  # mark_safe-equivalent via format_html
        )
    prices_widget.short_description = 'Перегляд тирів'


import logging as _logging
logger = _logging.getLogger(__name__)
