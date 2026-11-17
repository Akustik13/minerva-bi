"""🏷️ Бейдж категорії товару — значок і назва на кольорі категорії (ProductCategory.icon / color).

  COLORS, ICON_GROUPS — палітри картки категорії (клік — вибрати; свій колір і свій емодзі теж можна);
  clean_color / clean_icon — перевірка значень форми (неправильне → None);
  safe_color — колір для показу (невідоме → DEFAULT_COLOR), text_color — білий чи темний текст на ньому (WCAG);
  badge(name, color, icon) — HTML бейджа; info() — {код у нижньому регістрі: {...}} усіх категорій;
  annotate(qs, field) + column(obj) — бейдж у колонці списку без N+1 (назва / колір / значок — підзапитами);
  CategoryValuesFilter — фільтр «Категорія»: значок і назва замість коду, у порядку довідника.
Де видно: список категорій, «📦 Товари», «📊 Аналіз запасів», «Каталог → додати в магазин»,
«Склад → огляд залишків» (кнопки категорій), вибір категорії в картці товару, панель праворуч.
"""
import re

from django.contrib.admin.filters import AllValuesFieldListFilter
from django.utils.html import format_html

DEFAULT_COLOR = "#607d8b"

# готові кольори бейджа: з кожним видно назву (білий або темний текст — text_color)
COLORS = [
    "#607d8b", "#455a64", "#78909c", "#8d6e63", "#5d4037", "#757575", "#212121",
    "#e53935", "#c62828", "#d81b60", "#ad1457", "#8e24aa", "#5e35b1", "#3949ab",
    "#1e88e5", "#0277bd", "#039be5", "#00acc1", "#00897b", "#2e7d32", "#43a047",
    "#7cb342", "#c0ca33", "#fdd835", "#ffb300", "#fb8c00", "#f4511e", "#ff7043",
]

# значки: (група, [(емодзі, підказка)]) — без емодзі новіших за Unicode 12 (їх не малює Windows 10)
ICON_GROUPS = [
    ("Радіо й ВЧ", [
        ("📡", "Антени"), ("📶", "Сигнал, Wi-Fi, LTE"), ("🛰️", "GPS, супутник"), ("📻", "Радіо"),
        ("〰️", "Фільтри"), ("🎚️", "Атенюатори"), ("🔊", "Підсилювачі"), ("📢", "Гучномовці"),
    ]),
    ("З'єднання", [
        ("🔌", "Роз'єми"), ("🔗", "Кабелі"), ("➰", "Кабельні збірки"), ("🔀", "Адаптери, перехідники"),
        ("↔️", "Подовжувачі"), ("🧷", "Затискачі"),
    ]),
    ("Електроніка", [
        ("💎", "Кварци"), ("⏱️", "Генератори"), ("🟩", "Друковані плати"), ("🧩", "Модулі"),
        ("🔲", "Мікросхеми"), ("💡", "Світлодіоди"), ("🔋", "Акумулятори"), ("⚡", "Живлення"),
        ("🧲", "Індуктивності"), ("🌡️", "Датчики"), ("🎛️", "Керування"), ("🖥️", "Пристрої"),
    ]),
    ("Інструменти й механіка", [
        ("🛠️", "Інструменти"), ("🔧", "Ключі"), ("🔩", "Кріплення"), ("⚙️", "Механіка"),
        ("🧰", "Набори"), ("📏", "Вимірювання"), ("🧪", "Хімія, витратні"), ("🧱", "Корпуси"),
    ]),
    ("Інше", [
        ("📦", "Упаковка"), ("🏷️", "Мітки"), ("📄", "Документи"), ("🎁", "Комплекти"),
        ("⭐", "Важливе"), ("🧾", "Послуги"), ("♻️", "Повернення, б/у"), ("❓", "Інше"),
    ]),
    ("Кольори", [
        ("🟥", ""), ("🟧", ""), ("🟨", ""), ("🟦", ""), ("🟪", ""), ("🟫", ""), ("⬛", ""), ("⬜", ""),
    ]),
]

ICON_MAX = 8                        # = ProductCategory.icon max_length
_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_ICON_BAD = re.compile(r"[A-Za-z0-9<>&\"'\\\s;:{}()]")
_DARK, _LIGHT = "#000000", "#ffffff"   # чорний / білий текст: контраст завжди ≥ 4,58 : 1


def clean_color(value):
    """«#E91E63» / «e91e63» / «#e1e» → «#e91e63»; не HEX — None."""
    m = _COLOR_RE.match((value or "").strip()) if isinstance(value, str) else None
    if not m:
        return None
    h = m.group(1).lower()
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return "#" + h


def safe_color(value) -> str:
    """Колір для показу: правильний HEX, інакше DEFAULT_COLOR (у style не потрапить нічого іншого)."""
    return clean_color(value) or DEFAULT_COLOR


def clean_icon(value):
    """Значок — емодзі / символ без літер і цифр ASCII і без розмітки, ≤ ICON_MAX символів; '' — без значка;
    інакше None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return ""
    if len(v) > ICON_MAX or _ICON_BAD.search(v):
        return None
    return v


def _luminance(color: str) -> float:
    def ch(x):
        c = int(x, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    h = safe_color(color)[1:]
    r, g, b = ch(h[0:2]), ch(h[2:4]), ch(h[4:6])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_color(color) -> str:
    """Білий чи темний текст на кольорі бейджа — той, що контрастніший (так само рахує JS картки)."""
    lum = _luminance(color)
    on_white = 1.05 / (lum + 0.05)
    on_dark = (lum + 0.05) / (_luminance(_DARK) + 0.05)
    return _LIGHT if on_white >= on_dark else _DARK


BADGE_STYLE = ("display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:10px;"
               "font-size:11.5px;font-weight:600;line-height:1.5;white-space:nowrap;max-width:100%")


def badge(name, color, icon="", title=""):
    """HTML бейджа «📡 Антени» на кольорі категорії; назва — дані (translate="no")."""
    c = safe_color(color)
    ic = clean_icon(icon or "") or ""
    return format_html('<span class="mv-cat-badge" translate="no" title="{}" style="{};background:{};color:{}">'
                       '{}{}</span>', title or name, BADGE_STYLE, c, text_color(c),
                       format_html('<span aria-hidden="true">{}</span>', ic) if ic else "", name)


def unknown_badge(slug):
    """Код у товарі, якого немає в довіднику категорій, — сірим, пунктиром."""
    if not slug:
        return "—"
    return format_html('<span class="mv-cat-badge mv-cat-unknown" translate="no" title="{}" style="{};'
                       'border:1px dashed var(--border-strong,#9aa5b1);color:var(--text-muted,#6b7785)">{}</span>',
                       "Такого коду немає в «Категоріях товарів»", BADGE_STYLE, slug)


def info() -> dict:
    """{код у нижньому регістрі: {slug, name, color, icon, label, fg}} у порядку довідника (order, name)."""
    from inventory.models import ProductCategory
    out = {}
    for slug, name, color, icon in ProductCategory.objects.order_by("order", "name").values_list(
            "slug", "name", "color", "icon"):
        c = safe_color(color)
        ic = clean_icon(icon or "") or ""
        out.setdefault(slug.lower(), {"slug": slug, "name": name, "color": c, "icon": ic,
                                      "label": f"{ic} {name}" if ic else name, "fg": text_color(c)})
    return out


def choices() -> list:
    """[(slug, «📡 Антени»)] — для списків вибору категорії."""
    return [(c["slug"], c["label"]) for c in info().values()]


def annotate(qs, field="category"):
    """Назва, колір і значок категорії товару (поле `field` — код) — підзапитами, без запиту на рядок."""
    from django.db.models import OuterRef, Subquery
    from django.db.models.functions import Lower
    from inventory.models import ProductCategory
    sub = (ProductCategory.objects.annotate(_l=Lower("slug")).filter(_l=Lower(OuterRef(field)))
           .order_by("order", "pk"))
    return qs.annotate(_cat_name=Subquery(sub.values("name")[:1]),
                       _cat_color=Subquery(sub.values("color")[:1]),
                       _cat_icon=Subquery(sub.values("icon")[:1]))


def column(obj, field="category"):
    """Бейдж категорії для колонки списку: з анотацій annotate(), без них — одним запитом."""
    slug = _path(obj, field)
    if hasattr(obj, "_cat_name"):
        if obj._cat_name is None:
            return unknown_badge(slug)
        return badge(obj._cat_name, obj._cat_color, obj._cat_icon or "", title=slug)
    c = info().get((slug or "").lower())
    return badge(c["name"], c["color"], c["icon"], title=slug) if c else unknown_badge(slug)


def _path(obj, field):
    for part in field.split("__"):
        obj = getattr(obj, part, None)
        if obj is None:
            return ""
    return obj


class CategoryValuesFilter(AllValuesFieldListFilter):
    """Фільтр «Категорія» за кодом (той самий параметр адреси, що й у звичайного фільтра поля), але з
    назвами й значками з довідника, у його порядку; коди без категорії — після них."""

    def choices(self, changelist):
        self.lookup_choices = list(self.lookup_choices)   # super() і лічильники — один запит
        cats = info()
        rank = {k: i for i, k in enumerate(cats)}
        items = list(super().choices(changelist))
        head, rest = items[:1], items[1:]
        values = [str(v) for v in self.lookup_choices if v is not None]
        ranked = []
        for i, item in enumerate(rest):
            if i < len(values):
                val = values[i]
                c = cats.get(val.lower())
                if c:   # «код (12)» з лічильниками → «📡 Антени (12)»
                    item = {**item, "display": c["label"] + str(item["display"])[len(val):]}
                ranked.append((rank.get(val.lower(), len(rank)), str(item["display"]).lower(), item))
            else:   # «порожньо» — останнім
                ranked.append((len(rank) + 1, "", item))
        ranked.sort(key=lambda t: (t[0], t[1]))
        return head + [t[2] for t in ranked]
