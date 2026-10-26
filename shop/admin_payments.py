"""Адмінка онлайн-оплат (Stripe): налаштування в картці магазину, список оплат (логіка — shop/payments.py)."""
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from . import payments
from .models import Shop, ShopPayment

STRIPE_EVENTS = ["checkout.session.completed", "checkout.session.async_payment_succeeded",
                 "checkout.session.async_payment_failed", "checkout.session.expired", "charge.refunded"]


def _valid(validator, value) -> bool:
    try:
        validator(value)
        return True
    except forms.ValidationError:
        return False


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
    assistant_key = forms.CharField(
        label="Ключ помічника", required=False, max_length=255,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 60}),
        help_text="Ключ сайту для RAG (X-Minerva-Bridge). Порожньо — залишити збережений; без збереженого — ключ "
                  "з private/minerva-config.php сайту. Передається лише серверу сайту ключем, прив'язаним до магазину.")
    assistant_clear = forms.BooleanField(label="Видалити збережений ключ помічника", required=False)
    payment_methods = forms.MultipleChoiceField(
        label="Показувати в касі", required=False, choices=Shop.PAYMENT_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="Що покупець бачить на сайті. Stripe — лише з ключем (блок «💳 Оплата Stripe» нижче). "
                  "Банківські реквізити для передоплати й строк оплати рахунку — у shop-config.php сайту.")
    payment_default = forms.ChoiceField(
        label="Вибрано за замовчуванням", required=False, choices=[("", "— перший зі списку —")] + Shop.PAYMENT_CHOICES)

    class Meta:
        model = Shop
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.secrets import placeholder
        for name in ("stripe_secret_key", "stripe_webhook_secret", "assistant_key"):   # що збережено — замасковано
            if name in self.fields:
                self.fields[name].widget.attrs["placeholder"] = placeholder(getattr(self.instance, name, ""))
        methods = list(getattr(self.instance, "payment_methods", None) or [])
        # self.initial, а не field.initial: значення з бази ([] — ще не налаштовано) мають пріоритет над field.initial
        if "payment_methods" in self.fields:
            # ще не налаштовано: як зараз на сайті (передоплата, рахунок + Stripe, якщо був увімкнений)
            self.initial["payment_methods"] = methods or (
                ["prepayment", "invoice"] + (["stripe"] if getattr(self.instance, "stripe_enabled", False) else []))
        if "payment_default" in self.fields and methods:
            self.initial["payment_default"] = methods[0]

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

    def clean_assistant_key(self):
        value = (self.cleaned_data.get("assistant_key") or "").strip()   # порожньо — залишити збережений
        return value or (self.instance.assistant_key if self.instance and self.instance.pk else "")

    def _clean_addresses(self, name):
        from django.core.validators import validate_email
        from .mail import addresses
        items = addresses(self.cleaned_data.get(name))
        bad = [a for a in items if not _valid(validate_email, a)]
        if bad:
            raise forms.ValidationError(f"Некоректні адреси: {', '.join(bad)}")
        return ", ".join(items)

    def clean_mail_bcc(self):
        return self._clean_addresses("mail_bcc")

    def clean_team_emails(self):
        return self._clean_addresses("team_emails")

    def clean_assistant_url(self):
        from .assistant import check_url
        url = (self.cleaned_data.get("assistant_url") or "").strip().rstrip("/")
        error = check_url(url)
        if error:
            raise forms.ValidationError(error)
        return url

    def clean(self):
        d = super().clean()
        if "payment_methods" not in self.fields:
            return d
        if d.get("stripe_clear"):
            d["stripe_secret_key"] = d["stripe_webhook_secret"] = ""
        if d.get("assistant_clear"):
            d["assistant_key"] = ""
        order = [m for m, _ in Shop.PAYMENT_CHOICES]
        methods = sorted(d.get("payment_methods") or [], key=order.index)
        default = d.get("payment_default") or ""
        if default and default not in methods:
            self.add_error("payment_default", "Цей спосіб не відмічено в «Показувати в касі».")
        elif default:
            methods = [default] + [m for m in methods if m != default]
        if not methods:
            self.add_error("payment_methods", "Оберіть хоча б один спосіб оплати.")
        if "stripe" in methods and not d.get("stripe_secret_key"):
            self.add_error("payment_methods", "Для Stripe потрібен секретний ключ (блок «💳 Оплата Stripe»).")
        d["payment_methods"] = methods
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
        '<div style="margin-top:6px">Перевірка оплат: {}</div>'
        '<div style="margin-top:8px"><button type="submit" class="button" formaction="{}" formnovalidate>'
        '🔌 Перевірити ключ Stripe</button> '
        '<button type="submit" class="button" formaction="{}" formnovalidate>🔄 Перевірити незавершені оплати зараз</button> '
        '<span class="help">незбережені зміни на сторінці не зберігаються</span></div>',
        tail(shop.stripe_secret_key), tail(shop.stripe_webhook_secret), mode_html,
        shop.pk, path, shop.pk, ", ".join(STRIPE_EVENTS),
        format_html('<a href="{}">{} · {} {} · {}</a>', reverse("admin:shop_shoppayment_change", args=[last.pk]),
                    timezone.localtime(last.created_at).strftime("%d.%m.%Y %H:%M"), last.amount, last.currency,
                    last.get_status_display()) if last else "ще немає",
        payments.poll_state(shop),
        reverse("admin:shop_shop_stripe_check", args=[shop.pk]),
        reverse("admin:shop_shoppayment_check_open") + f"?shop={shop.pk}")


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

    # ── Перевірка незавершених оплат: усі / магазин / клієнт CRM / акаунт покупця ──
    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("check-open/", view(self._check_open_view), name="shop_shoppayment_check_open"),
            path("check-crm/<int:pk>/", view(self._check_crm_view), name="shop_shoppayment_check_crm"),
            path("check-account/<int:pk>/", view(self._check_account_view), name="shop_shoppayment_check_account"),
        ] + super().get_urls()

    def _guard(self, request):
        if request.method != "POST" or not self.has_view_permission(request):
            raise PermissionDenied

    def _check_open_view(self, request):
        self._guard(request)
        qs = ShopPayment.objects.all()
        if request.GET.get("shop"):
            qs = qs.filter(shop_id=request.GET["shop"])
        self.message_user(request, payments.stats_text(payments.check_payments(qs)))
        return redirect(request.META.get("HTTP_REFERER") or reverse("admin:shop_shoppayment_changelist"))

    def _check_crm_view(self, request, pk):
        from crm.models import Customer
        self._guard(request)
        customer = get_object_or_404(Customer, pk=pk)
        self.message_user(request, f"{customer}: " + payments.stats_text(
            payments.check_orders(payments.customer_orders(customer))))
        return redirect(reverse("admin:crm_customer_change", args=[pk]))

    def _check_account_view(self, request, pk):
        from .models import ShopCustomer
        self._guard(request)
        account = get_object_or_404(ShopCustomer, pk=pk)
        self.message_user(request, f"{account.email}: " + payments.stats_text(
            payments.check_orders(account.orders.values_list("order_id", flat=True))))
        return redirect(reverse("admin:shop_shopcustomer_change", args=[pk]))

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["poll_states"] = [(s, payments.poll_state(s)) for s in Shop.objects.exclude(stripe_secret_key="")]
        extra_context["open_count"] = payments.unfinished().count()
        return super().changelist_view(request, extra_context)

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
