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
from .models import ShippingZone, Shop, ShopListing, ShopPriceTier, ShopProduct, ShopSettings
from .shipping import format_countries, parse_countries


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

class ShippingZoneForm(forms.ModelForm):
    countries = forms.CharField(
        label="Країни", widget=forms.TextInput(attrs={"size": 46}),
        help_text="Коди ISO через кому: DE, AT, CH. «EU» — усі країни ЄС, «*» — решта світу.",
    )

    class Meta:
        model = ShippingZone
        fields = ["name", "countries", "price", "free_shipping", "is_active", "sort_order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial["countries"] = format_countries(self.instance.countries)

    def clean_countries(self):
        try:
            codes = parse_countries(self.cleaned_data["countries"])
        except ValueError as e:
            raise forms.ValidationError(str(e))
        if not codes:
            raise forms.ValidationError("Вкажіть хоча б одну країну.")
        return codes


class ShippingZoneFormSet(forms.BaseInlineFormSet):
    """Країна може бути лише в одному активному регіоні магазину."""

    def clean(self):
        super().clean()
        seen = {}
        for f in self.forms:
            d = getattr(f, "cleaned_data", None) or {}
            if not d or d.get("DELETE") or not d.get("is_active"):
                continue
            for c in d.get("countries") or []:
                if c in seen:
                    label = "«решта світу» (*)" if c == "*" else c
                    raise forms.ValidationError(f"{label} є і в «{seen[c]}», і в «{d.get('name')}».")
                seen[c] = d.get("name")


class ShippingZoneInline(admin.TabularInline):
    model = ShippingZone
    form = ShippingZoneForm
    formset = ShippingZoneFormSet
    extra = 0
    fields = ["name", "countries", "price", "free_shipping", "is_active", "sort_order", "dk_code"]
    readonly_fields = ["dk_code"]
    verbose_name = "Регіон доставки"
    verbose_name_plural = "🚚 Регіони доставки — куди доставляємо і скільки коштує (нетто)"


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_default", "is_active", "currency", "listings_col", "shipping_col",
                    "orders_col", "keys_col"]
    list_editable = ["is_active"]
    ordering = ["-is_default", "name"]  # з annotate(Count) Meta.ordering не діє
    readonly_fields = ["created_at", "keys_col", "zones_tools"]
    inlines = [ShippingZoneInline]
    actions = ["action_default_zones", "action_import_dk_zones"]
    fieldsets = [
        (None, {"fields": ["name", "slug", "url", "currency", "is_active", "is_default", "notes"]}),
        ("🚚 Доставка", {
            "fields": ["free_shipping_enabled", "free_shipping_threshold", "zones_tools"],
            "description": "Куди доставляємо — регіони внизу сторінки. Замовити можуть лише покупці з країн цих "
                           "регіонів; без регіонів сайт використовує власні налаштування доставки. "
                           "Швидкий старт: у списку магазинів позначте магазин → дія «🌍 Створити базові регіони "
                           "доставки» (Німеччина, Європа, США, світ), або «⬇️ Регіони доставки з DigiKey».",
        }),
        ("Підключення", {"fields": ["keys_col"],
                         "description": "Сайт підключається ключем API, прив'язаним до цього магазину "
                                        "(Адмін → REST API → API Токени → поле «Магазин»)."}),
    ]

    @admin.display(description="Доставка")
    def shipping_col(self, obj):
        zones = [z for z in obj.shipping_zones.all() if z.is_active]
        if not zones:
            return format_html('<span style="color:#9e9e9e">{}</span>', "не задано")
        free = (f", безкоштовно від {obj.free_shipping_threshold:.2f}"
                if obj.free_shipping_enabled and obj.free_shipping_threshold is not None else "")
        return f"{len(zones)} регіонів{free}"

    @admin.action(description="🌍 Створити базові регіони доставки")
    def action_default_zones(self, request, queryset):
        from .shipping import create_default_zones
        for shop in queryset:
            n = create_default_zones(shop)
            self.message_user(request, f"«{shop}»: створено регіонів: {n}. Ціни можна змінити в таблиці регіонів; "
                                       "безкоштовна доставка — галочка в блоці «🚚 Доставка» (поріг уже вписано).")

    @admin.action(description="⬇️ Регіони доставки з DigiKey")
    def action_import_dk_zones(self, request, queryset):
        from bots.services.dk_marketplace import fetch_shipping_rates
        from .shipping import import_digikey_zones
        try:
            rates = fetch_shipping_rates()
        except Exception as e:
            self.message_user(request, f"DigiKey: {e}", messages.ERROR)
            return
        if not rates:
            self.message_user(request, "DigiKey не повернув регіонів доставки (в оферах немає shippingRates).",
                              messages.WARNING)
            return
        for shop in queryset:
            created, updated = import_digikey_zones(shop, rates)
            self.message_user(request, f"«{shop}»: регіонів з DigiKey створено {created}, оновлено {updated}. "
                                       "Перевірте ціни — DigiKey дає лише мінімальну ціну доставки регіону.")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("shipping_zones").annotate(
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
        keys = services.keys_for_shop(obj)
        if not keys:
            return format_html('немає — <a href="{}">створити ключ</a>', reverse("admin:api_apikey_add"))

        def how(k):
            if k.shop_id == obj.pk:
                return "прив'язаний"
            if k.default_source == obj.slug:
                return f"за «Джерелом замовлень» {obj.slug}"
            return "магазин за замовчуванням"
        return format_html_join(", ", '<a href="{}">{}</a> <span style="opacity:.7">({})</span>',
                                ((reverse("admin:api_apikey_change", args=[k.pk]), k.name, how(k)) for k in keys))

    # ── Кнопки регіонів доставки в картці магазину ───────────────────────────
    @admin.display(description="Регіони")
    def zones_tools(self, obj):
        if not obj or not obj.pk:
            return "Спершу збережіть магазин."
        n = obj.shipping_zones.count()
        # formaction: кнопка надсилає основну форму (з CSRF-токеном) на окрему адресу
        return format_html(
            '<span style="margin-right:10px">{}</span>'
            '<button type="submit" class="button" formaction="{}" formnovalidate>🌍 Створити базові регіони</button> '
            '<button type="submit" class="button" formaction="{}" formnovalidate>⬇️ Імпортувати з DigiKey</button>'
            '<div class="help" style="margin-top:6px">Базові: Deutschland 6,90 · Europa 17,00 · USA 25,00 · Welt 42,00 € '
            '(нетто, за зразком правил на DigiKey). Наявні регіони не змінюються — лише додаються відсутні. '
            'Незбережені зміни на цій сторінці не зберігаються.</div>',
            f"Регіонів: {n}." if n else "Регіонів ще немає.",
            reverse("admin:shop_shop_default_zones", args=[obj.pk]),
            reverse("admin:shop_shop_dk_zones", args=[obj.pk]))

    def get_urls(self):
        from django.urls import path
        return [
            path("<int:pk>/default-zones/", self.admin_site.admin_view(self._default_zones_view),
                 name="shop_shop_default_zones"),
            path("<int:pk>/dk-zones/", self.admin_site.admin_view(self._dk_zones_view),
                 name="shop_shop_dk_zones"),
        ] + super().get_urls()

    def _zones_view(self, request, pk, run):
        from django.core.exceptions import PermissionDenied
        from django.shortcuts import get_object_or_404
        shop = get_object_or_404(Shop, pk=pk)
        if request.method != "POST" or not self.has_change_permission(request, shop):
            raise PermissionDenied
        run(request, Shop.objects.filter(pk=shop.pk))
        return redirect(reverse("admin:shop_shop_change", args=[shop.pk]) + "#shipping_zones-group")

    def _default_zones_view(self, request, pk):
        return self._zones_view(request, pk, self.action_default_zones)

    def _dk_zones_view(self, request, pk):
        return self._zones_view(request, pk, self.action_import_dk_zones)


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


class DigiKeyPriceForm(forms.Form):
    factor = forms.DecimalField(label="% від ціни DigiKey", max_digits=7, decimal_places=2, initial=Decimal("100"),
                                min_value=Decimal("1"),
                                help_text="100 = як на DigiKey, 95 = на 5 % дешевше, 110 = на 10 % дорожче")


class AddToShopForm(forms.Form):
    shop = forms.ModelChoiceField(label="Магазин", queryset=Shop.objects.all())
    visible = forms.BooleanField(label="Одразу показувати", required=False, initial=True)
    price_source = forms.ChoiceField(label="Ціна", choices=[
        ("sale", "«Ціна продажу» товару (можна змінити пізніше)"),
        ("markup", "Закупівля + націнка з налаштувань"),
        ("digikey", "Ціни DigiKey зі ступенями (оновлюються автоматично)"),
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


class ShopFilter(admin.SimpleListFilter):
    """Фільтр за магазином — видно завжди (стандартний ховається, коли магазин лише один)."""
    title = "Магазин"
    parameter_name = "shop__id__exact"

    def lookups(self, request, model_admin):
        return [(str(s.pk), s.name) for s in Shop.objects.all()]

    def queryset(self, request, qs):
        return qs.filter(shop_id=self.value()) if self.value() else qs


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
    list_display = ["sku_col", "name_col", "shop", "is_visible", "price", "source_col", "sale_price_col",
                    "purchase_col", "margin_col", "tiers_col", "available_col"]
    list_display_links = ["sku_col"]
    list_editable = ["is_visible", "price"]
    list_filter = [ShopFilter, "is_visible", "price_source", PriceStateFilter, StockFilter, "product__category"]
    search_fields = ["product__sku", "product__name", "product__name_export", "product__manufacturer"]
    list_select_related = ["shop", "product"]
    list_per_page = 100
    ordering = ["shop", "product__sku"]
    raw_id_fields = ["product"]
    inlines = [ShopPriceTierInline]
    fields = ["shop", "product", "is_visible", "price", "price_source", "price_factor", "breaks_preview",
              "dk_prices_preview"]
    readonly_fields = ["breaks_preview", "dk_prices_preview"]
    actions = ["action_show", "action_hide", "action_set_price", "action_adjust", "action_markup",
               "action_digikey", "action_manual",
               "action_tiers_default", "action_tiers_custom", "action_tiers_clear",
               "action_reset_to_sale", "action_round", "action_copy", "delete_selected"]

    def save_model(self, request, obj, form, change):
        """Ручна зміна ціни від'єднує позицію від DigiKey; % або перемикання на DigiKey — перерахунок."""
        changed = set(form.changed_data) if form else set()
        if (change and obj.price_source == ShopListing.PRICE_DIGIKEY and "price" in changed
                and "price_source" not in changed):
            obj.price_source = ShopListing.PRICE_MANUAL
            self.message_user(request, f"{obj.product.sku}: ціну змінено вручну — позицію від'єднано від DigiKey.",
                              messages.INFO)
        super().save_model(request, obj, form, change)
        if obj.price_source == ShopListing.PRICE_DIGIKEY and changed & {"price_source", "price_factor"}:
            if not services.apply_digikey_prices(obj):
                self.message_user(request, f"{obj.product.sku}: у DigiKey немає цін для цього товару.",
                                  messages.WARNING)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        if obj.price_source == ShopListing.PRICE_DIGIKEY and any(fs.has_changed() for fs in formsets):
            services.use_manual_prices([obj])  # ступені змінено вручну
            self.message_user(request, f"{obj.product.sku}: ступені змінено вручну — позицію від'єднано від DigiKey.",
                              messages.INFO)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("tiers")

    def changelist_view(self, request, extra_context=None):
        self._stock = None  # залишки рахуються один раз на сторінку
        # Вкладки магазинів над списком: видно, з яким магазином зараз працюємо
        current = request.GET.get("shop__id__exact", "")
        base = reverse("admin:shop_shoplisting_changelist")
        shops = Shop.objects.annotate(_n=Count("listings")).order_by("-is_default", "name")
        tabs = [{"label": "Усі магазини", "url": base, "count": sum(s._n for s in shops), "active": not current}]
        tabs += [{"label": s.name + (" ★" if s.is_default else ""), "url": f"{base}?shop__id__exact={s.pk}",
                  "count": s._n, "active": current == str(s.pk)} for s in shops]
        extra_context = {**(extra_context or {}), "shop_tabs": tabs}
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

    @admin.display(description="Джерело", ordering="price_source")
    def source_col(self, obj):
        if obj.price_source == ShopListing.PRICE_DIGIKEY:
            pct = "" if obj.price_factor == 100 else f" {obj.price_factor.normalize():f} %"
            return format_html('<span title="Ціна і ступені з DigiKey" style="padding:1px 6px;border-radius:8px;'
                               'background:#c62828;color:#fff">DigiKey{}</span>', pct)
        return "вручну"

    @admin.display(description="Ціни DigiKey (офер)")
    def dk_prices_preview(self, obj):
        if not obj or not obj.pk:
            return "—"
        tiers = services.dk_price_tiers(obj.product)
        if not tiers:
            return "Немає — товар не пов'язаний з офером DigiKey або ціни ще не стягнуто."
        body = format_html_join("", "<tr><td>від {} шт.</td><td>{}</td></tr>", ((q, f"{p:.2f}") for q, p in tiers))
        return format_html("<table><tr><th>Кількість</th><th>Ціна/шт.</th></tr>{}</table>", body)

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

    @admin.action(description="🔗 Ціни з DigiKey (зі ступенями, автоматично)…")
    def action_digikey(self, request, queryset):
        def apply(qs, d):
            n, missing = services.use_digikey_prices(qs.select_related("product"), d["factor"])
            if missing:
                messages.warning(request, "Без цін DigiKey (не змінено): " + ", ".join(missing[:20]) +
                                 (" …" if len(missing) > 20 else ""))
            return f"Ціни з DigiKey ({d['factor']:g} %): {n} позицій. Далі оновлюються разом з цінами DigiKey."
        return self._form_action(request, queryset, DigiKeyPriceForm, "Ціни з DigiKey", apply)

    @admin.action(description="✋ Ціни вручну (від'єднати від DigiKey)")
    def action_manual(self, request, queryset):
        self.message_user(request, f"Від'єднано від DigiKey: {services.use_manual_prices(queryset)} "
                                   "(поточні ціни залишились)")

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
            dk = d["price_source"] == "digikey"
            added, existed = services.add_products(d["shop"], qs, d["visible"], markup,
                                                   d["with_tiers"] and not dk, digikey=dk)
            return f"Додано в «{d['shop']}»: {added}, вже були в магазині: {existed}"
        default = Shop.objects.filter(is_default=True).first()
        return self._form_action(request, queryset, AddToShopForm, "Додати товари в магазин", apply,
                                 initial={"shop": default.pk if default else None})
