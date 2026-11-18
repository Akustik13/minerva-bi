"""Адмінка лоту (shop/lots.py): поле «Лише ці кількості», форма масової дії, фільтр, колонка й пояснення в картках
позиції магазину й товару (з підказкою MOQ DigiKey — нічого на DigiKey не надсилається)."""
from django import forms
from django.contrib import admin
from django.db.models import Q
from django.utils.html import format_html, format_html_join

from . import lots
from .models import ShopListing


class ChoicesField(forms.CharField):
    """«1000; 5000; 10000» ↔ [1000, 5000, 10000]."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.TextInput(attrs={"size": 30, "placeholder": "1000; 5000; 10000"}))
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return lots.format_choices(value)
        return value

    def to_python(self, value):
        try:
            return lots.parse_choices(value)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from None


def clean_rule(form, min_name: str, step_name: str, empty_min=None) -> None:
    """Крок без мінімуму (порожньо / 1) → мінімум = крок: «лише лотом 1000» одним полем."""
    data = form.cleaned_data
    lo, step = data.get(min_name), data.get(step_name)
    if step and step > 1 and (lo in (None, 1)):
        data[min_name] = step
    try:
        lots.check(data.get(min_name), data.get(step_name))
    except ValueError as e:
        form.add_error(min_name, str(e))


class ListingLotForm(forms.ModelForm):
    """Форма позиції магазину: «Лише ці кількості» текстом."""
    qty_choices = ChoicesField(label="Лише ці кількості",
                               help_text="Напр. «1000; 5000; 10000» — покупець вибирає одну з них "
                                         "(мінімум і крок тоді не діють). Порожньо — без списку.")

    class Meta:
        model = ShopListing
        fields = "__all__"

    def clean(self):
        data = super().clean()
        clean_rule(self, "min_qty", "qty_step")
        return data


class LotForm(forms.Form):
    """Масова дія «📦 Мін. замовлення / крок…» для вибраних позицій."""
    mode = forms.ChoiceField(label="Що зробити", widget=forms.RadioSelect, initial="set", choices=[
        ("set", "Задати свій лот у цьому магазині"),
        ("product", "Як у картці товару (прибрати свій лот)"),
        ("any", "Будь-яка кількість у цьому магазині (навіть якщо в товарі лот)"),
    ])
    min_qty = forms.IntegerField(label="Мін. замовлення, шт.", required=False, min_value=1, max_value=lots.MAX_QTY)
    qty_step = forms.IntegerField(label="Крок, шт.", required=False, min_value=1, max_value=lots.MAX_QTY,
                                  help_text="Від мінімуму кроками: мін. 1000 і крок 1000 → 1000, 2000, 3000…")
    qty_choices = ChoicesField(label="Або лише ці кількості",
                               help_text="Напр. «1000; 5000; 10000» — тоді мінімум і крок не діють.")

    def clean(self):
        data = super().clean()
        if data.get("mode") == "set":
            clean_rule(self, "min_qty", "qty_step")
            if not (data.get("min_qty") or data.get("qty_step") or data.get("qty_choices")):
                raise forms.ValidationError("Вкажіть мінімальне замовлення, крок або кількості.")
        return data


def apply_lot_form(listings, data) -> int:
    from .services import set_lot
    mode = data.get("mode")
    if mode == "product":
        return set_lot(listings)
    if mode == "any":
        return set_lot(listings, min_qty=1)
    return set_lot(listings, data.get("min_qty"), data.get("qty_step"), data.get("qty_choices"))


_OWN_ANY = Q(min_qty__isnull=False) | Q(qty_step__isnull=False) | Q(qty_choices__0__isnull=False)
_OWN_LOT = Q(min_qty__gt=1) | Q(qty_step__gt=1) | Q(qty_choices__0__isnull=False)
_PRODUCT_LOT = (Q(product__shop_min_qty__gt=1) | Q(product__shop_qty_step__gt=1)
                | Q(product__shop_qty_choices__0__isnull=False))


class LotFilter(admin.SimpleListFilter):
    title = "Лот"
    parameter_name = "lot"

    def lookups(self, request, model_admin):
        return [("yes", "Продається лотом"), ("own", "Свій лот у магазині"), ("product", "Лот з картки товару"),
                ("no", "Будь-яка кількість")]

    def queryset(self, request, qs):
        v = self.value()
        has_lot = _OWN_LOT | (~_OWN_ANY & _PRODUCT_LOT)
        if v == "yes":
            return qs.filter(has_lot)
        if v == "own":
            return qs.filter(_OWN_LOT)
        if v == "product":
            return qs.filter(~_OWN_ANY & _PRODUCT_LOT)
        if v == "no":
            return qs.exclude(has_lot)
        return qs


def lot_cell(listing):
    """Колонка списку: «≥1000 ×1000», «1000 / 5000», «≥500 · товар»; «—» без лоту."""
    rule = lots.rule_for(listing)
    if rule.any:
        return "—"
    tip = rule.text() + (" · з картки товару" if rule.source == lots.SOURCE_PRODUCT else " · лише в цьому магазині")
    return format_html('<span title="{}" style="white-space:nowrap">📦 {}{}</span>', tip, rule.short(),
                       format_html(' <span style="opacity:.6">· товар</span>')
                       if rule.source == lots.SOURCE_PRODUCT else "")


def _examples(rule) -> str:
    if not rule.choices and rule.step <= 1:      # без кроку — не «1 000, 1 001, 1 002 …»
        return f"{lots._n(rule.min_qty)} шт. або більше"
    vals = rule.examples(lots.EXAMPLES if not rule.choices else lots.MAX_CHOICES)
    more = "" if rule.choices else " …"
    return ", ".join(lots._n(v) for v in vals) + more


def _dk_hint(product, rule, field_id: str):
    """«DigiKey: MOQ 1000 шт. [⇢ у поле]» — лише підказка, на DigiKey нічого не надсилається."""
    from .badges import dk_listing
    dk = dk_listing(product)
    if dk is None:
        return ""
    moq = max(1, int(dk.dk_min_order_qty or 1))
    button = ""
    if moq > 1 and moq != rule.first:
        button = format_html(
            ' <button type="button" class="button" style="padding:1px 8px;font-size:11px" '
            'onclick="var f=document.getElementById(\'{}\');if(f){{f.value=\'{}\';f.focus();}}">⇢ у поле</button>',
            field_id, moq)
    return format_html('<div style="margin-top:4px;font-size:12px;opacity:.85">DigiKey: MOQ {} шт. '
                       '(мінімальне замовлення офера; лот магазину від нього не залежить){}</div>',
                       lots._n(moq), button)


def listing_panel(listing):
    """Картка позиції магазину: яке правило діє, що може замовити покупець, які ціни сховано."""
    if not listing or not listing.pk:
        return "—"
    from .services import all_price_breaks
    rule = lots.rule_for(listing)
    if rule.any:
        product = lots.product_rule(listing.product)
        if rule.source == lots.SOURCE_LISTING and not product.any:
            body = format_html("Будь-яка кількість — у цьому магазині (у картці товару лот: {}).", product.text())
        else:
            body = format_html("{}", "Будь-яка кількість.")
    else:
        where = ("з картки товару (для всіх магазинів)" if rule.source == lots.SOURCE_PRODUCT
                 else "своє правило цього магазину")
        hidden = lots.hidden_rows(all_price_breaks(listing), rule)
        hidden_html = ""
        if hidden:
            hidden_html = format_html(
                '<div style="margin-top:4px;font-size:12px;opacity:.85">Покупець не бачить: {}</div>',
                format_html_join(", ", "від {} шт. — {}", ((lots._n(r["min_qty"]), f'{r["unit_price"]:.2f}')
                                                          for r in hidden)))
        body = format_html("<b>📦 {}</b> — {}.<div style=\"margin-top:4px\">Покупець може замовити: {}</div>{}",
                           rule.text(), where, _examples(rule), hidden_html)
    return format_html("{}{}", body, _dk_hint(listing.product, rule, "id_min_qty"))


def product_panel(product):
    """Картка товару: лот за замовчуванням і де магазин має свій."""
    if not product or not product.pk:
        return "Спершу збережіть товар."
    rule = lots.product_rule(product)
    head = (format_html("<b>📦 {}</b> — у всіх магазинах. Покупець може замовити: {}", rule.text(), _examples(rule))
            if not rule.any else format_html("{}", "Будь-яка кількість (лоту немає)."))
    own = []
    for l in product.shop_listings.select_related("shop"):
        if lots.has_own(l):
            r = lots.rule_for(l)
            own.append((l.shop.name, r.text() or "будь-яка кількість"))
    own_html = ""
    if own:
        own_html = format_html('<div style="margin-top:4px;font-size:12px">Свій лот у магазині: {}</div>',
                               format_html_join("; ", "{} — {}", own))
    return format_html("{}{}{}", head, own_html, _dk_hint(product, rule, "id_shop_min_qty"))
