"""Розділ «Інтернет-магазин»: асортимент, ціни, ступені цін, масові дії."""
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from inventory.services.stock import annotate_stock

from . import services
from .models import ShopPriceTier, ShopProduct, ShopSettings


# ── Налаштування (синглтон) ──────────────────────────────────────────────────

@admin.register(ShopSettings)
class ShopSettingsAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Ціноутворення", {"fields": ["default_markup", "rounding"]}),
        ("Шаблон ступенів цін (як на DigiKey)", {
            "fields": ["price_breaks", "price_breaks_preview"],
            "description": "Знижка у % від ціни за 1 шт. Застосовується дією «Згенерувати ступені цін» "
                           "у списку товарів магазину.",
        }),
    ]
    readonly_fields = ["price_breaks_preview"]

    def has_add_permission(self, request):
        return not ShopSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = ShopSettings.get()
        return redirect(reverse("admin:shop_shopsettings_change", args=[obj.pk]))

    @admin.display(description="Приклад для ціни 100,00")
    def price_breaks_preview(self, obj):
        rows = [(1, "100,00", "—")]
        for step in sorted(obj.price_breaks or [], key=lambda s: int(s["min_qty"])):
            price = services.round_price(Decimal(100) * (100 - Decimal(str(step["discount"]))) / 100, obj.rounding)
            rows.append((step["min_qty"], f"{price:.2f}".replace(".", ","), f'−{step["discount"]} %'))
        body = format_html_join("", "<tr><td>{} шт.</td><td>{}</td><td>{}</td></tr>", rows)
        return format_html('<table style="min-width:280px"><tr><th>Кількість</th><th>Ціна/шт.</th>'
                           '<th>Знижка</th></tr>{}</table>', body)


# ── Товари магазину ──────────────────────────────────────────────────────────

class ShopPriceTierInline(admin.TabularInline):
    model = ShopPriceTier
    extra = 1
    fields = ["min_qty", "unit_price"]
    verbose_name = "Ступінь ціни"
    verbose_name_plural = "💶 Ступені цін за кількістю (1 шт. = ціна магазину вище)"


class InShopFilter(admin.SimpleListFilter):
    title = "Ціна"
    parameter_name = "price_state"

    def lookups(self, request, model_admin):
        return [("missing", "Без ціни (за запитом)"), ("own", "Окрема ціна магазину"),
                ("sale", "Ціна продажу"), ("tiers", "Є ступені цін"), ("notiers", "Без ступенів")]

    def queryset(self, request, qs):
        v = self.value()
        if v == "missing":
            return qs.filter(shop_price__isnull=True, sale_price__isnull=True)
        if v == "own":
            return qs.filter(shop_price__isnull=False)
        if v == "sale":
            return qs.filter(shop_price__isnull=True, sale_price__isnull=False)
        if v == "tiers":
            return qs.annotate(_n=Count("shop_tiers")).filter(_n__gt=0)
        if v == "notiers":
            return qs.annotate(_n=Count("shop_tiers")).filter(_n=0)
        return qs


class StockFilter(admin.SimpleListFilter):
    title = "Наявність"
    parameter_name = "stock"

    def lookups(self, request, model_admin):
        return [("in", "Є на складі"), ("out", "Немає на складі")]

    def queryset(self, request, qs):
        if self.value() == "in":
            return qs.filter(_available__gt=0)
        if self.value() == "out":
            return qs.filter(_available__lte=0)
        return qs


class PercentForm(forms.Form):
    percent = forms.DecimalField(label="Зміна ціни, %", max_digits=7, decimal_places=2,
                                 help_text="Напр. 5 = +5 %, −10 = знижка 10 %")
    scale_tiers = forms.BooleanField(label="Змінити й ступені цін на той самий %", required=False, initial=True)


class MarkupForm(forms.Form):
    markup = forms.DecimalField(label="Націнка на закупівельну ціну, %", max_digits=7, decimal_places=2,
                                help_text="100 % = закупівля × 2")
    regenerate = forms.BooleanField(label="Одразу перегенерувати ступені цін за шаблоном", required=False,
                                    initial=True)


class TiersForm(forms.Form):
    schedule = forms.CharField(
        label="Ступені (кількість: знижка %)", widget=forms.Textarea(attrs={"rows": 7, "cols": 40}),
        help_text="По одному на рядок, напр. «10: 5». Порожньо — товар лише з ціною за 1 шт.",
        required=False,
    )

    def clean_schedule(self):
        rows = []
        for line in self.cleaned_data["schedule"].splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                qty, disc = [x.strip().replace(",", ".").rstrip("%") for x in line.split(":", 1)]
                qty, disc = int(qty), Decimal(disc)
            except (ValueError, InvalidOperation):
                raise forms.ValidationError(f"Не розумію рядок «{line}». Формат: 100: 12")
            if qty < 2 or not (0 <= disc < 100):
                raise forms.ValidationError(f"«{line}»: кількість ≥ 2, знижка 0–99 %")
            rows.append({"min_qty": qty, "discount": float(disc)})
        return rows


@admin.register(ShopProduct)
class ShopProductAdmin(admin.ModelAdmin):
    list_display = ["sku", "name_col", "category", "shop_visible", "shop_price", "sale_price",
                    "purchase_price", "margin_col", "tiers_col", "available_col"]
    list_display_links = ["sku"]
    list_editable = ["shop_visible", "shop_price"]
    list_filter = ["shop_visible", InShopFilter, StockFilter, "category", "kind", "is_active"]
    search_fields = ["sku", "sku_short", "name", "name_export", "manufacturer"]
    list_per_page = 100
    ordering = ["-shop_visible", "category", "sku"]
    inlines = [ShopPriceTierInline]
    fieldsets = [
        (None, {"fields": ["sku", "name", "category", "is_active"]}),
        ("🏪 Магазин", {"fields": ["shop_visible", ("shop_price", "sale_price", "purchase_price"),
                                   "margin_col", "breaks_preview"]}),
    ]
    readonly_fields = ["sku", "name", "category", "is_active", "purchase_price", "margin_col", "breaks_preview"]
    actions = ["action_publish", "action_unpublish", "action_adjust", "action_markup",
               "action_tiers_default", "action_tiers_custom", "action_tiers_clear",
               "action_reset_to_sale", "action_round"]

    def has_add_permission(self, request):
        return False  # товари створюються у «Складі»

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return annotate_stock(super().get_queryset(request)).prefetch_related("shop_tiers")

    # ── Колонки ──────────────────────────────────────────────────────────────
    @admin.display(description="Назва", ordering="name")
    def name_col(self, obj):
        return (obj.name or "")[:50]

    @admin.display(description="Маржа")
    def margin_col(self, obj):
        price, cost = obj.shop_effective_price, obj.purchase_price
        if not price or not cost:
            return "—"
        pct = (Decimal(price) - Decimal(cost)) / Decimal(price) * 100
        color = "#43a047" if pct >= 30 else "#fb8c00" if pct >= 10 else "#e53935"
        return format_html('<span style="color:{}">{} %</span>', color, f"{pct:.0f}")

    @admin.display(description="Ступені цін")
    def tiers_col(self, obj):
        tiers = sorted(obj.shop_tiers.all(), key=lambda t: t.min_qty)
        if not tiers:
            return "—"
        return ", ".join(f"{t.min_qty}+: {t.unit_price:.2f}" for t in tiers[:4]) + (" …" if len(tiers) > 4 else "")

    @admin.display(description="На складі", ordering="_available")
    def available_col(self, obj):
        v = getattr(obj, "_available", 0) or 0
        return format_html('<span style="color:{}">{}</span>', "#43a047" if v > 0 else "#9e9e9e", f"{v:g}")

    @admin.display(description="Ціни, як бачить покупець")
    def breaks_preview(self, obj):
        rows = services.price_breaks(obj)
        if not rows:
            return "Ціна не задана — на сайті «Ціна за запитом»"
        body = format_html_join("", "<tr><td>від {} шт.</td><td>{}</td></tr>",
                                ((r["min_qty"], f'{r["unit_price"]:.2f}') for r in rows))
        return format_html("<table><tr><th>Кількість</th><th>Ціна/шт. нетто</th></tr>{}</table>", body)

    # ── Дії ──────────────────────────────────────────────────────────────────
    def _form_action(self, request, queryset, form_class, title, apply, initial=None):
        """Проміжна сторінка з формою для масової дії."""
        if "apply" in request.POST:
            form = form_class(request.POST)
            if form.is_valid():
                msg = apply(queryset, form.cleaned_data)
                self.message_user(request, msg)
                return None
        else:
            form = form_class(initial=initial)
        return render(request, "admin/shop/action_form.html", {
            **self.admin_site.each_context(request),
            "title": title, "form": form, "queryset": queryset[:30], "count": queryset.count(),
            "action": request.POST.get("action"), "opts": self.model._meta,
            "selected": request.POST.getlist(admin.helpers.ACTION_CHECKBOX_NAME),
        })

    @admin.action(description="🏪 Опублікувати в магазині")
    def action_publish(self, request, queryset):
        n = services.set_visibility(queryset, True)
        self.message_user(request, f"Опубліковано: {n}")

    @admin.action(description="🚫 Зняти з магазину")
    def action_unpublish(self, request, queryset):
        n = services.set_visibility(queryset, False)
        self.message_user(request, f"Знято з магазину: {n}")

    @admin.action(description="📈 Змінити ціни на ± %%")
    def action_adjust(self, request, queryset):
        return self._form_action(request, queryset, PercentForm, "Змінити ціни магазину на відсоток",
            lambda qs, d: f"Ціни змінено на {d['percent']} %: "
                          f"{services.adjust_prices(qs, d['percent'], scale_tiers=d['scale_tiers'])} товарів")

    @admin.action(description="🧮 Ціна = закупівля + націнка")
    def action_markup(self, request, queryset):
        def apply(qs, d):
            n, skipped = services.price_from_purchase(qs, d["markup"])
            if d["regenerate"]:
                services.generate_tiers(qs.filter(purchase_price__isnull=False))
            msg = f"Ціну розраховано від закупівлі (+{d['markup']} %): {n} товарів"
            if skipped:
                messages.warning(request, "Без закупівельної ціни (не змінено): " + ", ".join(skipped[:20]))
            return msg
        return self._form_action(request, queryset, MarkupForm, "Ціна магазину від закупівельної ціни", apply,
                                 initial={"markup": ShopSettings.get().default_markup})

    @admin.action(description="💶 Згенерувати ступені цін (шаблон з налаштувань)")
    def action_tiers_default(self, request, queryset):
        n = services.generate_tiers(queryset)
        self.message_user(request, f"Ступені цін створено за шаблоном для {n} товарів")

    @admin.action(description="💶 Згенерувати ступені цін (свій шаблон)…")
    def action_tiers_custom(self, request, queryset):
        current = "\n".join(f'{s["min_qty"]}: {s["discount"]:g}' for s in ShopSettings.get().price_breaks or [])
        return self._form_action(request, queryset, TiersForm, "Ступені цін за кількістю",
            lambda qs, d: f"Ступені цін створено для {services.generate_tiers(qs, d['schedule'])} товарів",
            initial={"schedule": current})

    @admin.action(description="🧹 Видалити ступені цін")
    def action_tiers_clear(self, request, queryset):
        self.message_user(request, f"Видалено ступенів: {services.clear_tiers(queryset)}")

    @admin.action(description="↩️ Ціна магазину = ціна продажу")
    def action_reset_to_sale(self, request, queryset):
        self.message_user(request, f"Окрему ціну магазину скинуто: {services.copy_sale_price(queryset)}")

    @admin.action(description="🔢 Округлити ціни (правило з налаштувань)")
    def action_round(self, request, queryset):
        self.message_user(request, f"Округлено: {services.round_all(queryset)} товарів")
