"""Розділ «Інтернет-магазин»: магазини, асортимент і ціни кожного магазину, масові дії."""
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from inventory.services.stock import annotate_stock

from . import services
from .models import Shop, ShopListing, ShopPriceTier, ShopProduct, ShopSettings


# ── Налаштування цін (синглтон) ──────────────────────────────────────────────

@admin.register(ShopSettings)
class ShopSettingsAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Ціноутворення", {"fields": ["default_markup", "rounding"]}),
        ("Шаблон ступенів цін (як на DigiKey)", {
            "fields": ["price_breaks", "price_breaks_preview"],
            "description": "Знижка у % від ціни за 1 шт. Застосовується дією «Згенерувати ступені цін» "
                           "в «Асортимент і ціни». Спільний для всіх магазинів.",
        }),
    ]
    readonly_fields = ["price_breaks_preview"]

    def has_add_permission(self, request):
        return not ShopSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:shop_shopsettings_change", args=[ShopSettings.get().pk]))

    @admin.display(description="Приклад для ціни 100,00")
    def price_breaks_preview(self, obj):
        rows = [(1, "100,00", "—")]
        for step in sorted(obj.price_breaks or [], key=lambda s: int(s["min_qty"])):
            price = services.round_price(Decimal(100) * (100 - Decimal(str(step["discount"]))) / 100, obj.rounding)
            rows.append((step["min_qty"], f"{price:.2f}".replace(".", ","), f'−{step["discount"]} %'))
        body = format_html_join("", "<tr><td>{} шт.</td><td>{}</td><td>{}</td></tr>", rows)
        return format_html('<table style="min-width:280px"><tr><th>Кількість</th><th>Ціна/шт.</th>'
                           '<th>Знижка</th></tr>{}</table>', body)


# ── Магазини ─────────────────────────────────────────────────────────────────

@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_default", "is_active", "currency", "listings_col", "orders_col", "keys_col"]
    list_editable = ["is_active"]
    readonly_fields = ["created_at", "keys_col"]
    fieldsets = [
        (None, {"fields": ["name", "slug", "url", "currency", "is_active", "is_default", "notes"]}),
        ("Підключення", {"fields": ["keys_col"],
                         "description": "Сайт підключається ключем API, прив'язаним до цього магазину "
                                        "(Адмін → REST API → API Токени → поле «Магазин»)."}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _n=Count("listings", distinct=True),
            _vis=Count("listings", filter=Q(listings__is_visible=True), distinct=True))

    @admin.display(description="Асортимент")
    def listings_col(self, obj):
        url = reverse("admin:shop_shoplisting_changelist") + f"?shop__id__exact={obj.pk}"
        return format_html('<a href="{}">{} показується / {} усього</a>', url, obj._vis, obj._n)

    @admin.display(description="Замовлення")
    def orders_col(self, obj):
        return format_html('<a href="{}?source={}">замовлення «{}» →</a>',
                           reverse("admin:sales_salesorder_changelist"), obj.slug, obj.slug)

    @admin.display(description="Ключі API")
    def keys_col(self, obj):
        if not obj or not obj.pk:
            return "—"
        keys = list(obj.api_keys.all())
        if not keys:
            return format_html('немає — <a href="{}">створити ключ</a>', reverse("admin:api_apikey_add"))
        return format_html_join(", ", '<a href="{}">{}</a>',
                                ((reverse("admin:api_apikey_change", args=[k.pk]), k.name) for k in keys))


# ── Масові дії: форми ────────────────────────────────────────────────────────

class PercentForm(forms.Form):
    percent = forms.DecimalField(label="Зміна ціни, %", max_digits=7, decimal_places=2,
                                 help_text="Напр. 5 = +5 %, −10 = знижка 10 %")
    scale_tiers = forms.BooleanField(label="Змінити й ступені цін на той самий %", required=False, initial=True)


class SetPriceForm(forms.Form):
    price = forms.DecimalField(label="Нова ціна (нетто), за 1 шт.", max_digits=18, decimal_places=4,
                               min_value=Decimal("0"))
    regenerate = forms.BooleanField(label="Перегенерувати ступені цін за шаблоном", required=False, initial=True)


class MarkupForm(forms.Form):
    markup = forms.DecimalField(label="Націнка на закупівельну ціну, %", max_digits=7, decimal_places=2,
                                help_text="100 % = закупівля × 2")
    regenerate = forms.BooleanField(label="Одразу перегенерувати ступені цін за шаблоном", required=False,
                                    initial=True)


class TiersForm(forms.Form):
    schedule = forms.CharField(
        label="Ступені (кількість: знижка %)", widget=forms.Textarea(attrs={"rows": 7, "cols": 40}),
        help_text="По одному на рядок, напр. «10: 5». Порожньо — лише ціна за 1 шт.", required=False,
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


class CopyForm(forms.Form):
    target = forms.ModelChoiceField(label="У магазин", queryset=Shop.objects.all())
    factor = forms.DecimalField(label="Коефіцієнт цін", max_digits=7, decimal_places=4, initial=Decimal("1"),
                                help_text="1 = ті самі ціни, 1.15 = на 15 % дорожче, 0.9 = на 10 % дешевше")
    with_tiers = forms.BooleanField(label="Копіювати ступені цін", required=False, initial=True)
    overwrite = forms.BooleanField(label="Перезаписати позиції, що вже є в цільовому магазині", required=False)


class AddToShopForm(forms.Form):
    shop = forms.ModelChoiceField(label="Магазин", queryset=Shop.objects.all())
    visible = forms.BooleanField(label="Одразу показувати", required=False, initial=True)
    price_source = forms.ChoiceField(label="Ціна", choices=[
        ("sale", "«Ціна продажу» товару (можна змінити пізніше)"),
        ("markup", "Закупівля + націнка з налаштувань"),
    ], initial="sale", widget=forms.RadioSelect)
    with_tiers = forms.BooleanField(label="Згенерувати ступені цін за шаблоном", required=False, initial=True)


class FormActionMixin:
    def _form_action(self, request, queryset, form_class, title, apply, initial=None):
        """Проміжна сторінка з формою для масової дії."""
        if "apply" in request.POST:
            form = form_class(request.POST)
            if form.is_valid():
                self.message_user(request, apply(queryset, form.cleaned_data))
                return None
        else:
            form = form_class(initial=initial)
        meta = self.model._meta
        return render(request, "admin/shop/action_form.html", {
            **self.admin_site.each_context(request),
            "title": title, "form": form, "queryset": queryset[:30], "count": queryset.count(),
            "action": request.POST.get("action"), "opts": meta,
            "selected": request.POST.getlist(admin.helpers.ACTION_CHECKBOX_NAME),
            "back_url": reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist"),
            "back_label": meta.verbose_name_plural,
        })


# ── Асортимент і ціни ────────────────────────────────────────────────────────

class PriceStateFilter(admin.SimpleListFilter):
    title = "Ціна"
    parameter_name = "price_state"

    def lookups(self, request, model_admin):
        return [("missing", "Без ціни (за запитом)"), ("own", "Власна ціна магазину"),
                ("sale", "Ціна продажу товару"), ("tiers", "Є ступені цін"), ("notiers", "Без ступенів")]

    def queryset(self, request, qs):
        v = self.value()
        if v == "missing":
            return qs.filter(price__isnull=True, product__sale_price__isnull=True)
        if v == "own":
            return qs.filter(price__isnull=False)
        if v == "sale":
            return qs.filter(price__isnull=True, product__sale_price__isnull=False)
        if v == "tiers":
            return qs.annotate(_n=Count("tiers")).filter(_n__gt=0)
        if v == "notiers":
            return qs.annotate(_n=Count("tiers")).filter(_n=0)
        return qs


class StockFilter(admin.SimpleListFilter):
    title = "Наявність"
    parameter_name = "stock"

    def lookups(self, request, model_admin):
        return [("in", "Є на складі"), ("out", "Немає на складі")]

    def queryset(self, request, qs):
        if self.value() in ("in", "out"):
            from inventory.models import Product
            cond = {"_available__gt": 0} if self.value() == "in" else {"_available__lte": 0}
            ids = annotate_stock(Product.objects.all()).filter(**cond).values("pk")
            return qs.filter(product__in=ids)
        return qs


class ShopPriceTierInline(admin.TabularInline):
    model = ShopPriceTier
    extra = 1
    fields = ["min_qty", "unit_price"]
    verbose_name = "Ступінь ціни"
    verbose_name_plural = "💶 Ступені цін за кількістю (1 шт. = ціна позиції)"


@admin.register(ShopListing)
class ShopListingAdmin(FormActionMixin, admin.ModelAdmin):
    list_display = ["sku_col", "name_col", "shop", "is_visible", "price", "sale_price_col", "purchase_col",
                    "margin_col", "tiers_col", "available_col"]
    list_display_links = ["sku_col"]
    list_editable = ["is_visible", "price"]
    list_filter = ["shop", "is_visible", PriceStateFilter, StockFilter, "product__category"]
    search_fields = ["product__sku", "product__name", "product__name_export", "product__manufacturer"]
    list_select_related = ["shop", "product"]
    list_per_page = 100
    ordering = ["shop", "product__sku"]
    raw_id_fields = ["product"]
    inlines = [ShopPriceTierInline]
    fields = ["shop", "product", "is_visible", "price", "breaks_preview"]
    readonly_fields = ["breaks_preview"]
    actions = ["action_show", "action_hide", "action_set_price", "action_adjust", "action_markup",
               "action_tiers_default", "action_tiers_custom", "action_tiers_clear",
               "action_reset_to_sale", "action_round", "action_copy", "delete_selected"]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("tiers")

    def changelist_view(self, request, extra_context=None):
        self._stock = None  # залишки рахуються один раз на сторінку
        return super().changelist_view(request, extra_context)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (["shop", "product"] if obj else [])

    # ── Колонки ──────────────────────────────────────────────────────────────
    @admin.display(description="SKU", ordering="product__sku")
    def sku_col(self, obj):
        return obj.product.sku

    @admin.display(description="Назва", ordering="product__name")
    def name_col(self, obj):
        return (obj.product.name or "")[:45]

    @admin.display(description="Ціна продажу")
    def sale_price_col(self, obj):
        return f"{obj.product.sale_price:.2f}" if obj.product.sale_price is not None else "—"

    @admin.display(description="Закупівля")
    def purchase_col(self, obj):
        return f"{obj.product.purchase_price:.2f}" if obj.product.purchase_price else "—"

    @admin.display(description="Маржа")
    def margin_col(self, obj):
        price, cost = obj.effective_price, obj.product.purchase_price
        if not price or not cost:
            return "—"
        pct = (Decimal(price) - Decimal(cost)) / Decimal(price) * 100
        color = "#43a047" if pct >= 30 else "#fb8c00" if pct >= 10 else "#e53935"
        return format_html('<span style="color:{}">{} %</span>', color, f"{pct:.0f}")

    @admin.display(description="Ступені цін")
    def tiers_col(self, obj):
        tiers = sorted(obj.tiers.all(), key=lambda t: t.min_qty)
        if not tiers:
            return "—"
        return ", ".join(f"{t.min_qty}+: {t.unit_price:.2f}" for t in tiers[:4]) + (" …" if len(tiers) > 4 else "")

    @admin.display(description="На складі")
    def available_col(self, obj):
        if getattr(self, "_stock", None) is None:
            from inventory.models import Product
            self._stock = dict(annotate_stock(Product.objects.all()).values_list("pk", "_available"))
        v = self._stock.get(obj.product_id) or 0
        return format_html('<span style="color:{}">{}</span>', "#43a047" if v > 0 else "#9e9e9e", f"{v:g}")

    @admin.display(description="Ціни, як бачить покупець")
    def breaks_preview(self, obj):
        if not obj or not obj.pk:
            return "—"
        rows = services.price_breaks(obj)
        if not rows:
            return "Ціна не задана — на сайті «Ціна за запитом»"
        body = format_html_join("", "<tr><td>від {} шт.</td><td>{}</td></tr>",
                                ((r["min_qty"], f'{r["unit_price"]:.2f}') for r in rows))
        return format_html("<table><tr><th>Кількість</th><th>Ціна/шт. нетто</th></tr>{}</table>", body)

    # ── Дії ──────────────────────────────────────────────────────────────────
    @admin.action(description="🏪 Показувати в магазині")
    def action_show(self, request, queryset):
        self.message_user(request, f"Показується: {services.set_visibility(queryset, True)}")

    @admin.action(description="🚫 Сховати в магазині")
    def action_hide(self, request, queryset):
        self.message_user(request, f"Сховано: {services.set_visibility(queryset, False)}")

    @admin.action(description="💶 Встановити однакову ціну…")
    def action_set_price(self, request, queryset):
        return self._form_action(request, queryset, SetPriceForm, "Встановити ціну для вибраних позицій",
            lambda qs, d: f"Ціну {d['price']:.2f} встановлено для "
                          f"{services.set_price(qs, d['price'], regenerate_tiers=d['regenerate'])} позицій")

    @admin.action(description="📈 Змінити ціни на ± %%")
    def action_adjust(self, request, queryset):
        return self._form_action(request, queryset, PercentForm, "Змінити ціни на відсоток",
            lambda qs, d: f"Ціни змінено на {d['percent']} %: "
                          f"{services.adjust_prices(qs, d['percent'], scale_tiers=d['scale_tiers'])} позицій")

    @admin.action(description="🧮 Ціна = закупівля + націнка…")
    def action_markup(self, request, queryset):
        def apply(qs, d):
            n, skipped = services.price_from_purchase(qs, d["markup"])
            if d["regenerate"]:
                services.generate_tiers(qs.filter(product__purchase_price__isnull=False))
            if skipped:
                messages.warning(request, "Без закупівельної ціни (не змінено): " + ", ".join(skipped[:20]))
            return f"Ціну розраховано від закупівлі (+{d['markup']} %): {n} позицій"
        return self._form_action(request, queryset, MarkupForm, "Ціна від закупівельної ціни", apply,
                                 initial={"markup": ShopSettings.get().default_markup})

    @admin.action(description="💶 Згенерувати ступені цін (шаблон з налаштувань)")
    def action_tiers_default(self, request, queryset):
        self.message_user(request, f"Ступені цін створено для {services.generate_tiers(queryset)} позицій")

    @admin.action(description="💶 Згенерувати ступені цін (свій шаблон)…")
    def action_tiers_custom(self, request, queryset):
        current = "\n".join(f'{s["min_qty"]}: {s["discount"]:g}' for s in ShopSettings.get().price_breaks or [])
        return self._form_action(request, queryset, TiersForm, "Ступені цін за кількістю",
            lambda qs, d: f"Ступені цін створено для {services.generate_tiers(qs, d['schedule'])} позицій",
            initial={"schedule": current})

    @admin.action(description="🧹 Видалити ступені цін")
    def action_tiers_clear(self, request, queryset):
        self.message_user(request, f"Видалено ступенів: {services.clear_tiers(queryset)}")

    @admin.action(description="↩️ Ціна = ціна продажу товару")
    def action_reset_to_sale(self, request, queryset):
        self.message_user(request, f"Власну ціну скинуто: {services.reset_to_sale_price(queryset)}")

    @admin.action(description="🔢 Округлити ціни (правило з налаштувань)")
    def action_round(self, request, queryset):
        self.message_user(request, f"Округлено: {services.round_all(queryset)} позицій")

    @admin.action(description="📋 Скопіювати в інший магазин…")
    def action_copy(self, request, queryset):
        def apply(qs, d):
            n, skipped = services.copy_listings(qs.select_related("product"), d["target"], d["factor"],
                                                d["with_tiers"], d["overwrite"])
            return f"Скопійовано в «{d['target']}»: {n}, пропущено (вже є): {skipped}"
        return self._form_action(request, queryset, CopyForm, "Скопіювати позиції в інший магазин", apply)


# ── Каталог → додати в магазин ───────────────────────────────────────────────

@admin.register(ShopProduct)
class ShopProductAdmin(FormActionMixin, admin.ModelAdmin):
    list_display = ["sku", "name_col", "category", "sale_price", "purchase_price", "shops_col"]
    list_display_links = None
    list_filter = ["category", "kind", "is_active"]
    search_fields = ["sku", "sku_short", "name", "name_export", "manufacturer"]
    list_per_page = 100
    ordering = ["category", "sku"]
    actions = ["action_add_to_shop"]

    def has_add_permission(self, request):
        return False  # товари створюються у «Складі»

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("shop_listings__shop")

    @admin.display(description="Назва", ordering="name")
    def name_col(self, obj):
        return (obj.name or "")[:50]

    @admin.display(description="У магазинах")
    def shops_col(self, obj):
        rows = list(obj.shop_listings.all())
        if not rows:
            return "—"
        return format_html_join(
            " ", '<span title="{}" style="padding:1px 6px;border-radius:8px;background:{};color:#fff">{}</span>',
            (("показується" if l.is_visible else "сховано", "#2e7d32" if l.is_visible else "#757575", l.shop.slug)
             for l in rows))

    @admin.action(description="➕ Додати в магазин…")
    def action_add_to_shop(self, request, queryset):
        def apply(qs, d):
            markup = ShopSettings.get().default_markup if d["price_source"] == "markup" else None
            added, existed = services.add_products(d["shop"], qs, d["visible"], markup, d["with_tiers"])
            return f"Додано в «{d['shop']}»: {added}, вже були в магазині: {existed}"
        default = Shop.objects.filter(is_default=True).first()
        return self._form_action(request, queryset, AddToShopForm, "Додати товари в магазин", apply,
                                 initial={"shop": default.pk if default else None})
