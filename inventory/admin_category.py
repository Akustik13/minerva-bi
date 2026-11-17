"""🏷️ Картка категорії товару: значок і колір бейджа з палітр + «як виглядатиме» наживо.

Палітри, перевірка й вигляд бейджа — inventory/category_badges.py; тут лише форма й поля.
JS і стилі — admin/inventory/productcategory/change_form.html (той самий розрахунок кольору тексту, що text_color).
"""
from django import forms
from django.utils.html import format_html, format_html_join

from . import category_badges as cb
from .models import ProductCategory


class CategoryColorWidget(forms.TextInput):
    """HEX у полі + готові кольори кліком + «🎨 Свій колір…» (вікно кольору браузера)."""

    def __init__(self, attrs=None):
        super().__init__(attrs={"maxlength": 16, "spellcheck": "false", "autocomplete": "off",
                                "class": "pcb-hex", **(attrs or {})})

    def render(self, name, value, attrs=None, renderer=None):
        field = super().render(name, value, attrs, renderer)
        native = cb.safe_color(value)
        swatches = format_html_join(
            "", '<button type="button" class="pcb-sw" data-v="{}" style="background:{}" title="{}" '
                'aria-label="{}"></button>', ((c, c, c, c) for c in cb.COLORS))
        return format_html(
            '<div class="pcb-color" data-pcb="color">'
            '<div class="pcb-line">{}'
            '<label class="pcb-own" title="{}"><input type="color" class="pcb-native" value="{}" aria-label="{}">'
            '<span>🎨 {}</span></label>'
            '<span class="pcb-err" hidden>{}</span></div>'
            '<div class="pcb-swatches" aria-label="{}">{}</div></div>',
            field, "Будь-який колір у вікні вибору кольору", native, "Свій колір", "Свій колір…",
            "Колір — # і 6 знаків 0-9 / a-f, напр. #e91e63", "Готові кольори", swatches)


class CategoryIconWidget(forms.TextInput):
    """Емодзі в полі + сітка готових значків за групами + «✕ Без значка»."""

    def __init__(self, attrs=None):
        super().__init__(attrs={"maxlength": cb.ICON_MAX, "autocomplete": "off", "class": "pcb-emoji",
                                **(attrs or {})})

    def render(self, name, value, attrs=None, renderer=None):
        field = super().render(name, value, attrs, renderer)
        groups = format_html_join("", '<div class="pcb-group"><div class="pcb-gt">{}</div><div class="pcb-grid">{}</div></div>', (
            (title, format_html_join("", '<button type="button" class="pcb-ib" data-v="{}" title="{}" aria-label="{}">{}</button>',
                                     ((e, hint or e, hint or e, e) for e, hint in icons)))
            for title, icons in cb.ICON_GROUPS))
        return format_html(
            '<div class="pcb-icon" data-pcb="icon">'
            '<div class="pcb-line">{}'
            '<button type="button" class="pcb-ib pcb-none" data-v="" title="{}">✕ {}</button>'
            '<span class="pcb-hint">{}</span>'
            '<span class="pcb-err" hidden>{}</span></div>'
            '<div class="pcb-groups">{}</div></div>',
            field, "Бейдж без значка — лише назва", "Без значка",
            "← свій емодзі або виберіть нижче (Win + . — усі емодзі Windows)",
            "Значок — один емодзі або символ, без літер і цифр", groups)


class ProductCategoryAdminForm(forms.ModelForm):
    class Meta:
        model = ProductCategory
        fields = "__all__"
        widgets = {"color": CategoryColorWidget(), "icon": CategoryIconWidget()}

    def clean_color(self):
        value = cb.clean_color(self.cleaned_data.get("color") or "")
        if value is None:
            raise forms.ValidationError("Колір — # і 6 знаків 0-9 / a-f, напр. #e91e63.")
        return value

    def clean_icon(self):
        value = cb.clean_icon(self.cleaned_data.get("icon") or "")
        if value is None:
            raise forms.ValidationError("Значок — один емодзі або символ, без літер і цифр (до 8 знаків).")
        return value


def preview(obj):
    """«Як виглядатиме»: бейдж і рядок списку товарів з ним; JS картки оновлює наживо."""
    name = (getattr(obj, "name", "") or "").strip() or "Назва категорії"
    color, icon = getattr(obj, "color", "") or cb.DEFAULT_COLOR, getattr(obj, "icon", "") or ""
    return format_html(
        '<div class="pcb-preview" id="pcb-preview" data-empty="{}">'
        '<div class="pcb-pv-big">{}</div>'
        '<div class="pcb-pv-list" aria-hidden="true">'
        '<div class="pcb-pv-h"><span>{}</span><span>{}</span><span>{}</span></div>'
        '<div class="pcb-pv-r"><span class="pcb-pv-sku" translate="no">AN220213-01C</span><span>{}</span>'
        '<span class="pcb-pv-n">120</span></div>'
        '<div class="pcb-pv-r"><span class="pcb-pv-sku" translate="no">FT220407</span><span>{}</span>'
        '<span class="pcb-pv-n">35</span></div></div>'
        '<div class="pcb-pv-filter" aria-hidden="true"><span class="pcb-pv-ft">{}</span>'
        '<span class="pcb-pv-fi">{}</span><span class="pcb-pv-fi pcb-pv-on" translate="no">'
        '<span class="pcb-pv-ic">{}</span> <span class="pcb-pv-nm">{}</span></span></div>'
        '<div class="pcb-pv-note">{}</div></div>',
        "Назва категорії", cb.badge(name, color, icon),
        "Артикул", "Категорія", "Склад", cb.badge(name, color, icon), cb.badge(name, color, icon),
        "Фільтр «Категорія»", "Усі", icon, name,
        "Так бейдж виглядає в «📦 Товари», «📊 Аналіз запасів», каталозі магазину й фільтрах — "
        "у темній і світлій темі однаково: колір тексту (білий чи чорний) Minerva вибирає сама.")
