"""Адмінка шаблонів листів магазину: власні тексти листів покупцям і сповіщення команді (shop/mail.py)."""
from string import Formatter

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from . import mail
from .models import Shop, ShopMailTemplate

TEXT_FIELDS = ("subject", "greeting", "intro", "button", "outro")


class ShopMailTemplateForm(forms.ModelForm):
    class Meta:
        model = ShopMailTemplate
        fields = ["shop", "kind", "lang", "is_active", "subject", "greeting", "intro", "button", "outro"]
        widgets = {"subject": forms.TextInput(attrs={"size": 90}), "greeting": forms.TextInput(attrs={"size": 60}),
                   "intro": forms.Textarea(attrs={"rows": 6, "cols": 90}),
                   "outro": forms.Textarea(attrs={"rows": 3, "cols": 90})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("subject", "intro"):  # порожньо при створенні → стандартний текст
            self.fields[name].required = False

    def clean(self):
        d = super().clean()
        kind, lang = d.get("kind"), d.get("lang")
        if not kind or not lang:
            return d
        default = mail.defaults(kind, lang)
        for name in ("subject", "intro"):
            if not (d.get(name) or "").strip():
                d[name] = default[name]
        allowed = set(mail.PLACEHOLDERS.get(kind, []))
        for name in TEXT_FIELDS:
            try:
                used = {f for _, f, _, _ in Formatter().parse(d.get(name) or "") if f is not None}
            except ValueError:
                self.add_error(name, "Перевірте фігурні дужки: поле пишеться як {name}.")
                continue
            unknown = sorted(used - allowed)
            if unknown:
                self.add_error(name, "Невідомі поля: " + ", ".join("{" + u + "}" for u in unknown)
                               + ". Доступні: " + ", ".join("{" + a + "}" for a in sorted(allowed)))
        return d


@admin.register(ShopMailTemplate)
class ShopMailTemplateAdmin(admin.ModelAdmin):
    form = ShopMailTemplateForm
    list_display = ["kind", "lang", "shop", "subject", "is_active", "updated_at", "updated_by"]
    list_filter = ["shop", "kind", "lang", "is_active"]
    list_editable = ["is_active"]
    search_fields = ["subject", "intro"]
    readonly_fields = ["placeholders_col", "preview_col"]
    fieldsets = [
        (None, {"fields": ["shop", "kind", "lang", "is_active"]}),
        ("Текст листа", {"fields": ["subject", "greeting", "intro", "button", "outro", "placeholders_col"],
                         "description": "Порожні «Тема» і «Текст» при створенні — заповняться стандартним текстом. "
                                        "Посилання (підтвердження, новий пароль, замовлення) Minerva додає кнопкою сама. "
                                        "Підпис внизу — «Підпис листів» у картці магазину."}),
        ("Попередній перегляд", {"fields": ["preview_col"]}),
    ]

    @admin.display(description="Поля для вставки")
    def placeholders_col(self, obj):
        kind = obj.kind if obj else ""
        names = mail.PLACEHOLDERS.get(kind, [])
        if not names:
            return "Оберіть лист і збережіть — тут з'являться доступні поля."
        labels = {"shop": "назва для покупців", "name": "ім'я покупця", "number": "номер замовлення",
                  "amount": "сума з валютою", "hours": "годин дії посилання", "minutes": "хвилин дії посилання",
                  "days": "днів дії посилання", "customer": "компанія / email покупця", "reference": "номер платежу Stripe"}
        return format_html_join(" · ", "<code>{{{}}}</code> — {}", ((n, labels.get(n, n)) for n in names))

    @admin.display(description="Так виглядатиме лист")
    def preview_col(self, obj):
        if not obj or not obj.pk:
            return "Збережіть шаблон, щоб побачити попередній перегляд."
        tpl = {f: getattr(obj, f) for f in TEXT_FIELDS}
        if obj.kind in mail.TEAM_KINDS:
            subject = mail._fill(obj.subject, {"shop": mail.public_name(obj.shop), **mail.SAMPLE})
            body = mail._fill(obj.intro, {"shop": mail.public_name(obj.shop), **mail.SAMPLE})
            return format_html('<div><b>{}</b></div><div style="white-space:pre-line;margin-top:6px">{}</div>', subject, body)
        link = "https://example.com/de/shop/konto/index.html#…" if obj.kind not in ("exists", "deleted", "test") else ""
        subject, _text, body = mail.compose(obj.shop, obj.kind, obj.lang, name=mail.SAMPLE["name"], link=link, tpl=tpl,
                                            **{k: v for k, v in mail.SAMPLE.items() if k != "name"})
        test_url = reverse("admin:shop_shopmailtemplate_test", args=[obj.pk])
        # body — уже екранований HTML з compose(): весь текст шаблону проходить через html.escape
        return format_html('<div style="margin-bottom:6px">Тема: <b>{}</b></div>'
                           '<div style="background:#fff;color:#222;padding:16px;border-radius:6px;max-width:640px">{}</div>'
                           '<div style="margin-top:8px"><button type="submit" class="button" formaction="{}" '
                           'formnovalidate>✉️ Надіслати тестовий лист собі</button> '
                           '<span class="help">з прикладом даних; незбережені зміни не враховуються</span></div>',
                           subject, mark_safe(body), test_url)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        default = Shop.objects.filter(is_active=True).order_by("-is_default", "name").first()
        if default and "shop" not in initial:
            initial["shop"] = default.pk
        kind, lang = request.GET.get("kind"), request.GET.get("lang")
        if kind in mail.PLACEHOLDERS and lang in ("de", "en"):
            initial.update(mail.defaults(kind, lang))
        return initial

    # ── Кнопки: створити всі шаблони зі стандартних текстів, тестовий лист ──
    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("create-defaults/", view(self._create_defaults_view), name="shop_shopmailtemplate_defaults"),
            path("<int:pk>/test/", view(self._test_view), name="shop_shopmailtemplate_test"),
        ] + super().get_urls()

    def _create_defaults_view(self, request):
        if request.method != "POST" or not self.has_add_permission(request):
            raise PermissionDenied
        created = 0
        for shop in Shop.objects.filter(is_active=True):
            for kind, _ in ShopMailTemplate.KINDS:
                for lang in ("de", "en"):
                    _, new = ShopMailTemplate.objects.get_or_create(
                        shop=shop, kind=kind, lang=lang, defaults={**mail.defaults(kind, lang), "updated_by": request.user})
                    created += new
        self.message_user(request, f"Створено шаблонів зі стандартних текстів: {created} (наявні не змінено). "
                                   "Тепер їх можна редагувати.")
        return redirect(reverse("admin:shop_shopmailtemplate_changelist"))

    def _test_view(self, request, pk):
        t = get_object_or_404(ShopMailTemplate, pk=pk)
        if request.method != "POST" or not self.has_change_permission(request, t):
            raise PermissionDenied
        to = (request.user.email or "").strip()
        back = redirect(reverse("admin:shop_shopmailtemplate_change", args=[pk]))
        if not to:
            self.message_user(request, "У вашого користувача Minerva не задано email.", messages.ERROR)
            return back
        try:
            if t.kind in mail.TEAM_KINDS:
                subject = mail._fill(t.subject, {"shop": mail.public_name(t.shop), **mail.SAMPLE})
                body = mail._fill(t.intro, {"shop": mail.public_name(t.shop), **mail.SAMPLE})
                from django.core.mail import EmailMultiAlternatives
                conn, from_addr = mail.sender(t.shop)
                EmailMultiAlternatives(f"[Test] {subject}", body, from_addr, [to], connection=conn).send()
            else:
                link = "https://example.com/#test" if t.kind not in ("exists", "deleted", "test") else ""
                mail.send(t.shop, to, t.kind, t.lang, name=mail.SAMPLE["name"], link=link,
                          **{k: v for k, v in mail.SAMPLE.items() if k != "name"})
            self.message_user(request, f"Тестовий лист «{t}» надіслано на {to}.")
        except Exception as e:
            self.message_user(request, f"Лист не надіслано: {e}", messages.ERROR)
        return back

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["shops_info"] = [(s, mail.public_name(s)) for s in Shop.objects.filter(is_active=True)]
        extra_context["kinds"] = ShopMailTemplate.KINDS
        return super().changelist_view(request, extra_context)
