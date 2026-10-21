"""Адмінка особистого кабінету покупців: акаунти, їхні замовлення, листи (логіка — shop/accounts.py)."""
from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import accounts
from .models import Shop, ShopCustomer


def account_status(shop: Shop) -> dict:
    """Готовність кабінету магазину: увімкнено, ключі без прав, відправник листів, сторінка кабінету."""
    from . import services
    from .mail import describe_sender
    keys = [k for k, m in services.keys_for_shop(shop) if m != services.KEY_DEFAULT]
    return {
        "shop": shop, "enabled": shop.accounts_enabled,
        "keys": [k.name for k in keys],
        "missing": [k.name for k in keys if not {"accounts:read", "accounts:write"} <= set(k.scopes or [])],
        "sender": describe_sender(shop),
        "links": shop.account_links or {},
        "url": reverse("admin:shop_shop_change", args=[shop.pk]),
        "link_error": _link_error(shop),
    }


def _link_error(shop: Shop) -> str:
    """Сайт надсилає адресу кабінету з іншого домену, ніж «Адреса сайту» → реєстрація не працює."""
    if not shop.account_link_error:
        return ""
    from urllib.parse import urlsplit
    date, time, link = (shop.account_link_error.split(" ", 2) + ["", "", ""])[:3]  # «дд.мм.рррр гг:хх адреса»
    parts = urlsplit(link)
    site = f"{parts.scheme}://{parts.hostname}" if parts.hostname else link
    return (f"Сайт надсилає адресу кабінету {link} ({date} {time}), а «Адреса сайту» магазину — "
            f"{shop.url or '—'}. Реєстрація й листи не працюють: впишіть у «Адреса сайту» {site}.")


class ShopCustomerAddForm(forms.ModelForm):
    """Акаунт створює менеджер: без пароля — покупець задає його сам за посиланням із запрошення."""
    send_invite = forms.BooleanField(
        label="Надіслати запрошення", required=False, initial=True,
        help_text="Лист покупцю з посиланням «Passwort festlegen» (діє 7 днів). Пароль задає сам покупець; "
                  "після цього email вважається підтвердженим і попередні замовлення з цією адресою з'являться в кабінеті.")

    class Meta:
        model = ShopCustomer
        fields = ["shop", "email", "lang", "company", "name", "phone", "vat_id", "street", "zip", "city", "country"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shop"].queryset = Shop.objects.filter(is_active=True)
        default = Shop.objects.filter(is_active=True).order_by("-is_default", "name").first()
        if default and not self.initial.get("shop"):
            self.initial["shop"] = default.pk
        for name in ("company", "name"):
            self.fields[name].required = True

    def clean_email(self):
        return accounts.normalize_email(self.cleaned_data.get("email"))

    def clean_country(self):
        value = (self.cleaned_data.get("country") or "").strip().upper()
        if value and len(value) != 2:
            raise forms.ValidationError("Код країни ISO з 2 літер, напр. DE.")
        return value

    def clean_vat_id(self):
        return (self.cleaned_data.get("vat_id") or "").upper().replace(" ", "")


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
    actions = ["action_send_reset", "action_send_invite", "action_resend_verify", "action_unlock",
               "action_logout_all", "action_disable", "action_enable"]
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

    add_fieldsets = [
        (None, {"fields": ["shop", "email", "lang", "send_invite"],
                "description": "Зазвичай акаунт створює сам покупець на сайті. Тут можна створити його за покупця "
                               "(напр. для постійного клієнта): він отримає лист-запрошення і сам задасть пароль."}),
        ("Дані покупця", {"fields": ["company", "name", "phone", "vat_id", "street", "zip", "city", "country"]}),
    ]

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else super().get_fieldsets(request, obj)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = ShopCustomerAddForm
        return super().get_form(request, obj, **kwargs)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["accounts_info"] = [account_status(s) for s in Shop.objects.filter(is_active=True)
                                          .order_by("-is_default", "name")]
        return super().changelist_view(request, extra_context)

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
            self.message_user(request, f"{customer.email}: сайт ще не повідомив адресу сторінки кабінету. Відкрийте "
                                       "будь-яку сторінку магазину на сайті (кабінет у магазині має бути ввімкнено) — "
                                       "адреса підтягнеться протягом хвилини — і повторіть дію «✉️ Надіслати запрошення».",
                              messages.WARNING)
        return link

    @admin.action(description="✉️ Надіслати запрошення (встановити пароль, 7 днів)")
    def action_send_invite(self, request, queryset):
        sent = 0
        for c in queryset.select_related("shop"):
            link = self._link_or_warn(request, c)
            if link and accounts.send_invite(c, link):
                sent += 1
            elif link:
                self.message_user(request, f"{c.email}: лист не надіслано — {c.last_mail_error or 'див. журнал'}",
                                  messages.ERROR)
        if sent:
            self.message_user(request, f"Надіслано запрошень: {sent}. Посилання діє 7 днів.")

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
            if not link:
                continue
            # акаунт створив менеджер (пароля ще немає): підтвердження з паролем неможливе — запрошення
            ok = (accounts.send_verification(c, link, throttle=False) if accounts.has_password(c)
                  else accounts.send_invite(c, link))
            sent += bool(ok)
        self.message_user(request, f"Надіслано листів підтвердження / запрошень: {sent} "
                                   "(підтверджені акаунти пропущено).")

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
        if not change:
            from django.contrib.auth.hashers import make_password
            obj.password = make_password(None)  # без пароля: покупець задає його сам за посиланням
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data and not obj.is_active:
            obj.sessions.all().delete()
        if not change and form.cleaned_data.get("send_invite"):
            link = self._link_or_warn(request, obj)
            if link and accounts.send_invite(obj, link):
                self.message_user(request, f"Запрошення надіслано на {obj.email} (посилання діє 7 днів).")
            elif link:
                self.message_user(request, f"Акаунт створено, але лист не надіслано: {obj.last_mail_error}",
                                  messages.ERROR)
