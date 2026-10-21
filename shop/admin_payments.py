"""Адмінка онлайн-оплат (Stripe): налаштування в картці магазину, список оплат (логіка — shop/payments.py)."""
from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from . import payments
from .models import Shop, ShopPayment

STRIPE_EVENTS = ["checkout.session.completed", "checkout.session.async_payment_succeeded",
                 "checkout.session.async_payment_failed", "checkout.session.expired", "charge.refunded"]


class ShopForm(forms.ModelForm):
    """Ключі Stripe — як паролі: збережене значення не показується, порожнє поле його не змінює."""
    stripe_secret_key = forms.CharField(
        label="Stripe: секретний ключ", required=False, max_length=255,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 60}),
        help_text="sk_test_… (тестовий режим) або sk_live_… / обмежений rk_live_…. Порожньо — залишити збережений. "
                  "Ключ зберігається лише в Minerva, сайт його не бачить.")
    stripe_webhook_secret = forms.CharField(
        label="Stripe: секрет вебхука", required=False, max_length=255,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 60}),
        help_text="whsec_… (Stripe → Developers → Webhooks → endpoint → Signing secret). Порожньо — залишити збережений.")
    stripe_clear = forms.BooleanField(label="Видалити збережені ключі Stripe", required=False)

    class Meta:
        model = Shop
        fields = "__all__"

    def _keep(self, name, prefixes, hint):
        value = (self.cleaned_data.get(name) or "").strip()
        if not value:
            return getattr(self.instance, name, "") if self.instance and self.instance.pk else ""
        if not value.startswith(prefixes):
            raise forms.ValidationError(hint)
        return value

    def clean_stripe_secret_key(self):
        return self._keep("stripe_secret_key", ("sk_", "rk_"),
                          "Потрібен секретний ключ sk_… або обмежений rk_… (pk_… — публічний, тут не підходить).")

    def clean_stripe_webhook_secret(self):
        return self._keep("stripe_webhook_secret", ("whsec_",), "Секрет вебхука починається з whsec_….")

    def clean(self):
        d = super().clean()
        if d.get("stripe_clear"):
            d["stripe_secret_key"] = d["stripe_webhook_secret"] = ""
            d["stripe_enabled"] = False
        if d.get("stripe_enabled") and not d.get("stripe_secret_key"):
            self.add_error("stripe_secret_key", "Щоб увімкнути оплату Stripe, вкажіть секретний ключ.")
        return d


def stripe_tools(shop: Shop) -> str:
    """Стан Stripe у картці магазину: ключі, режим, адреса вебхука, перевірка."""
    if not shop or not shop.pk:
        return "Спершу збережіть магазин."
    def tail(v):
        return f"задано (…{v[-4:]})" if v else format_html('<span style="color:#e57373">{}</span>', "не задано")
    mode = payments.key_mode(shop.stripe_secret_key)
    mode_html = (format_html('<span style="color:#ffb74d">тестовий режим</span> — гроші не списуються, '
                             'картка 4242 4242 4242 4242') if mode == "test"
                 else format_html('<span style="color:#81c784">бойовий режим</span>') if mode == "live" else "—")
    path = payments.webhook_path(shop)
    last = ShopPayment.objects.filter(shop=shop).order_by("-created_at").first()
    return format_html(
        '<div>Секретний ключ: {} · секрет вебхука: {} · {}</div>'
        '<div style="margin-top:6px">Адреса вебхука для Stripe (Developers → Webhooks → Add endpoint): '
        '<code id="stripe-hook-{}">{}</code>'
        '<script>(function(){{var c=document.getElementById("stripe-hook-{}");if(c)c.textContent=location.origin+c.textContent;}})();</script>'
        '<div class="help">Події: {}</div></div>'
        '<div style="margin-top:6px">Остання оплата: {}</div>'
        '<div style="margin-top:8px"><button type="submit" class="button" formaction="{}" formnovalidate>'
        '🔌 Перевірити ключ Stripe</button> <span class="help">перевіряє збережений ключ (спершу збережіть зміни)</span></div>',
        tail(shop.stripe_secret_key), tail(shop.stripe_webhook_secret), mode_html,
        shop.pk, path, shop.pk, ", ".join(STRIPE_EVENTS),
        format_html('<a href="{}">{} · {} {} · {}</a>', reverse("admin:shop_shoppayment_change", args=[last.pk]),
                    timezone.localtime(last.created_at).strftime("%d.%m.%Y %H:%M"), last.amount, last.currency,
                    last.get_status_display()) if last else "ще немає",
        reverse("admin:shop_shop_stripe_check", args=[shop.pk]))


@admin.register(ShopPayment)
class ShopPaymentAdmin(admin.ModelAdmin):
    list_display = ["created_at", "order_link", "shop", "amount_col", "status_col", "livemode", "paid_at", "stripe_link"]
    list_filter = ["status", "shop", "livemode"]
    search_fields = ["order__order_number", "session_id", "payment_intent", "order__email", "order__client"]
    list_select_related = ["order", "shop"]
    actions = ["action_sync"]
    readonly_fields = ["order_link", "shop", "provider", "session_id", "payment_intent", "url", "amount", "currency",
                       "status", "livemode", "last_event", "expires_at", "paid_at", "created_at", "updated_at",
                       "stripe_link"]
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # лише перегляд: стан оплати змінюють Stripe (вебхук) або дія «Оновити стан зі Stripe»

    @admin.display(description="Замовлення", ordering="order__order_number")
    def order_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("admin:sales_salesorder_change", args=[obj.order_id]),
                           obj.order.order_number)

    @admin.display(description="Сума", ordering="amount")
    def amount_col(self, obj):
        return f"{obj.amount:.2f} {obj.currency}"

    @admin.display(description="Статус", ordering="status")
    def status_col(self, obj):
        color = {"paid": "#81c784", "refunded": "#90a4ae", "failed": "#e57373", "expired": "#9e9e9e"}.get(obj.status, "#ffb74d")
        return format_html('<span style="color:{}">{}</span>', color, obj.get_status_display())

    @admin.display(description="Stripe")
    def stripe_link(self, obj):
        return format_html('<a href="{}" target="_blank" rel="noopener">відкрити ↗</a>', payments.dashboard_url(obj))

    @admin.action(description="🔄 Оновити стан зі Stripe")
    def action_sync(self, request, queryset):
        done = 0
        for p in queryset.select_related("shop", "order"):
            try:
                payments.sync(p)
                done += 1
            except payments.PaymentError as e:
                self.message_user(request, f"{p.order}: {e.message}", messages.ERROR)
        if done:
            self.message_user(request, f"Оновлено зі Stripe: {done}.")
