"""Адмінка особистого кабінету покупців: акаунти, їхні замовлення, листи (логіка — shop/accounts.py)."""
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import accounts
from .models import ShopCustomer


class VerifiedFilter(admin.SimpleListFilter):
    title = "Стан"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [("verified", "✅ Підтверджені"), ("pending", "⏳ Очікують підтвердження"),
                ("locked", "🔒 Вхід заблоковано"), ("disabled", "⛔ Вимкнені")]

    def queryset(self, request, qs):
        v = self.value()
        if v == "verified":
            return qs.filter(email_verified_at__isnull=False, is_active=True)
        if v == "pending":
            return qs.filter(email_verified_at__isnull=True)
        if v == "locked":
            return qs.filter(locked_until__gt=timezone.now())
        if v == "disabled":
            return qs.filter(is_active=False)
        return qs


@admin.register(ShopCustomer)
class ShopCustomerAdmin(admin.ModelAdmin):
    list_display = ["email", "company", "name", "shop", "state_col", "orders_col", "last_login_at", "created_at"]
    list_filter = ["shop", VerifiedFilter, "lang"]
    search_fields = ["email", "company", "name", "city"]
    list_select_related = ["shop"]
    raw_id_fields = ["crm_customer"]
    readonly_fields = ["state_col", "email_verified_at", "last_login_at", "created_at", "updated_at",
                       "failed_logins", "locked_until", "sessions_col", "last_mail_at", "last_mail_error",
                       "cart_col", "orders_list"]
    actions = ["action_send_reset", "action_resend_verify", "action_unlock", "action_logout_all",
               "action_disable", "action_enable"]
    fieldsets = [
        ("Акаунт", {"fields": ["shop", "email", "state_col", "is_active", "email_verified_at", "lang",
                               "crm_customer"],
                    "description": "Пароль покупця не видно нікому (зберігається лише хеш). Щоб допомогти покупцю — "
                                   "дія «✉️ Надіслати посилання для нового пароля» у списку акаунтів."}),
        ("Дані покупця", {"fields": ["company", "name", "phone", "vat_id", "street", "zip", "city", "country"]}),
        ("Адреса доставки", {"classes": ["collapse"],
                             "fields": ["ship_separate", "ship_company", "ship_name", "ship_street", "ship_zip",
                                        "ship_city", "ship_country", "ship_phone"]}),
        ("Замовлення і кошик", {"fields": ["orders_list", "cart_col"]}),
        ("Безпека і листи", {"fields": ["last_login_at", "sessions_col", "failed_logins", "locked_until",
                                        "last_mail_at", "last_mail_error", "created_at", "updated_at"]}),
    ]

    def has_add_permission(self, request):
        return False  # акаунт створює покупець на сайті (з підтвердженням email)

    def get_queryset(self, request):
        from django.db.models import Count
        return super().get_queryset(request).annotate(_orders=Count("orders", distinct=True))

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        return ro + ["shop", "email"] if obj else ro

    @admin.display(description="Стан")
    def state_col(self, obj):
        if not obj.is_active:
            return format_html('<span style="color:#e57373">{}</span>', "⛔ вимкнено")
        if obj.locked_until and obj.locked_until > timezone.now():
            return format_html('<span style="color:#ffb74d">🔒 вхід заблоковано до {}</span>',
                               timezone.localtime(obj.locked_until).strftime("%H:%M"))
        if not obj.is_verified:
            return format_html('<span style="color:#ffb74d">{}</span>', "⏳ email не підтверджено")
        return format_html('<span style="color:#81c784">{}</span>', "✅ активний")

    @admin.display(description="Замовлень", ordering="_orders")
    def orders_col(self, obj):
        return getattr(obj, "_orders", None) or "—"

    @admin.display(description="Активні входи")
    def sessions_col(self, obj):
        now = timezone.now()
        rows = [s for s in obj.sessions.all() if s.expires_at > now] if obj and obj.pk else []
        if not rows:
            return "немає"
        return format_html_join("", "<div>{} · до {}{}</div>", (
            (timezone.localtime(s.last_used_at).strftime("%d.%m.%Y %H:%M"),
             timezone.localtime(s.expires_at).strftime("%d.%m.%Y"), " · запам'ятати" if s.remember else "")
            for s in rows))

    @admin.display(description="Кошик")
    def cart_col(self, obj):
        items = (obj.cart or {}) if obj else {}
        if not items:
            return "порожній"
        when = timezone.localtime(obj.cart_updated_at).strftime("%d.%m.%Y %H:%M") if obj.cart_updated_at else ""
        return format_html('{}<div class="help">змінено {}</div>', format_html_join(
            "", "<div>{} × {}</div>", ((qty, sku[3:] + " (запит ціни)" if sku.startswith("rq:") else sku)
                                      for sku, qty in items.items())), when)

    @admin.display(description="Замовлення з кабінету")
    def orders_list(self, obj):
        if not obj or not obj.pk:
            return "—"
        infos = list(obj.orders.select_related("order").order_by("-order__order_date", "-pk")[:30])
        if not infos:
            return "ще немає"
        return format_html_join("", '<div><a href="{}">{}</a> · {} · {} · {}</div>', (
            (reverse("admin:sales_salesorder_change", args=[i.order_id]), i.order.order_number,
             i.order.order_date.strftime("%d.%m.%Y") if i.order.order_date else "—",
             "запит пропозиції" if i.order.document_type == "QUOTE" else i.order.get_status_display(),
             f"{i.order.total_price:.2f} {i.order.currency}" if i.order.total_price is not None else "")
            for i in infos))

    # ── Дії ─────────────────────────────────────────────────────────────────
    def _link_or_warn(self, request, customer):
        link = accounts.admin_link(customer)
        if not link:
            self.message_user(request, f"{customer.email}: сайт ще не повідомив адресу сторінки кабінету — "
                                       "лист можна надіслати після першого звернення сайту (реєстрація / вхід).",
                              messages.WARNING)
        return link

    @admin.action(description="✉️ Надіслати посилання для нового пароля")
    def action_send_reset(self, request, queryset):
        sent = 0
        for c in queryset.select_related("shop"):
            link = self._link_or_warn(request, c)
            if link and accounts.send_reset(c, link, throttle=False):
                sent += 1
            elif link:
                self.message_user(request, f"{c.email}: лист не надіслано — {c.last_mail_error or 'див. журнал'}",
                                  messages.ERROR)
        if sent:
            self.message_user(request, f"Надіслано листів для нового пароля: {sent}. Посилання діє 1 годину.")

    @admin.action(description="✉️ Повторно надіслати підтвердження email")
    def action_resend_verify(self, request, queryset):
        sent = 0
        for c in queryset.select_related("shop").filter(email_verified_at__isnull=True):
            link = self._link_or_warn(request, c)
            if link and accounts.send_verification(c, link, throttle=False):
                sent += 1
        self.message_user(request, f"Надіслано листів підтвердження: {sent} (підтверджені акаунти пропущено).")

    @admin.action(description="🔓 Зняти блокування входу")
    def action_unlock(self, request, queryset):
        n = queryset.update(failed_logins=0, locked_until=None)
        self.message_user(request, f"Розблоковано: {n}.")

    @admin.action(description="🚪 Завершити всі входи (на всіх пристроях)")
    def action_logout_all(self, request, queryset):
        from .models import ShopCustomerSession
        n = ShopCustomerSession.objects.filter(customer__in=queryset).delete()[0]
        self.message_user(request, f"Завершено сесій: {n}.")

    @admin.action(description="⛔ Вимкнути акаунт")
    def action_disable(self, request, queryset):
        from .models import ShopCustomerSession
        n = queryset.update(is_active=False)
        ShopCustomerSession.objects.filter(customer__in=queryset).delete()
        self.message_user(request, f"Вимкнено: {n}. Замовлення залишились; покупець не зможе увійти.")

    @admin.action(description="✅ Увімкнути акаунт")
    def action_enable(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"Увімкнено: {n}.")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data and not obj.is_active:
            obj.sessions.all().delete()
