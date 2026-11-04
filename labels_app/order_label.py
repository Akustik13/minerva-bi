"""
Етикетка замовлення DYMO: номер замовлення з кодом країни, усі артикули з кількістю й, за бажанням, їх фото.

Шаблон shipping/templates_dymo/order_label_template.dymo — макет антен без QR, логотипа, штрихкода й Copyright:
  {{ORDER}} {{COUNTRY}} — один рядок на всю ширину: «Order: 102497269   NZ» (країна жирним, `country`);
  {{LINE}} / {{FS}} — нумеровані позиції «1. AN220214-01A × 5» жирним моноширинним шрифтом (Courier New Bold) у стовпчиках; розмір
                      шрифту — найбільший, з яким усе вміщується (`layout`), інакше останній рядок «+N more».
Фото артикулів (`SalesSettings.order_label_photos`, для одного друку — `photos=`): праворуч від списку, коли
позицій не більше `PHOTO_MAX` — одне на всю висоту, два — одне над одним, три-чотири — сіткою 2 × 2 з номером
позиції; список тоді вужчий. Фото — як у мініатюр списку товарів (inventory/thumbs.py: файл у картці → копія
магазину → посилання), для етикетки — сіре з підтягнутим контрастом, кеш MEDIA_ROOT/thumbs/label_*.png.
Що з фото (`photo_info`) віддає заголовок відповіді X-Minerva-Photos (вікно перегляду): XML-коментарів
DYMO не приймає ніде в етикетці («Could not find ILabelObject of type»).
Артикул — як у картці замовлення: з рядка замовлення, інакше товару; однакові артикули складаються.
"""
import base64
import hashlib
import math
import re
import time
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings

TEMPLATE_PATH = Path(settings.BASE_DIR) / "shipping" / "templates_dymo" / "order_label_template.dymo"

TEXT_W, TEXT_H = 3.13, 1.04      # блок позицій, дюйми (як у шаблоні)
LIST_X, LIST_Y = 0.2233334, 0.3  # його лівий верхній кут
FONT_SIZES = (12, 11, 10, 9, 8, 7, 6)
CHAR_EM = 0.6                    # ширина символу Courier New
FIT = 0.97                       # запас ширини: останній символ не впритул до краю / фото
LINE_EM = 1.2                    # висота рядка з запасом
GAP = 3                          # пробіли між стовпчиками
NBSP = " "                  # DYMO не стискає нерозривні пробіли

PHOTO_MAX = 4                    # фото — лише коли позицій не більше (інакше списку бракує місця)
PHOTO_MIN_FONT = 8               # дрібніше заради фото текст не стає — тоді етикетка без фото
PHOTO_GAP = 0.06                 # між списком і фото, дюйми
PHOTO_PAD = 0.02                 # поле всередині клітинки фото
PHOTO_PX = 240                   # сторона фото (≈ 230 точок на дюйм на 1″)
PHOTO_RETRY = 3600               # недоступне фото не пробуємо знову годину
WHITE_FROM = 232                 # світліше — чисто білий (термодрук розсипав би світло-сіре цятками)
STYLES = ("outline", "gray")     # фото: контур (без заливки) / сіре
PHOTO_RING = 2                   # контур: товщина обвідки темних площ, px (тонше — деталь лишається суцільною)
EDGE_SHARE = 0.06                # контур: частка найсильніших перепадів яскравості, що стають лініями
EDGE_MIN = 60                    # … і не слабші за цей перепад (рівний фон — без ліній)
PREP_VERSION = 2                 # змінилась обробка — новий кеш фото


def _qty(value) -> str:
    value = Decimal(value or 0)
    return str(value.quantize(Decimal(1))) if value == value.to_integral_value() else f"{value.normalize():f}"


def _sku(line) -> str:
    return (line.sku_raw or "").strip() or (line.product.sku if line.product_id else "")


def items(order) -> list[tuple[str, Decimal]]:
    """[(артикул, кількість)] у порядку рядків замовлення."""
    merged = {}
    for line in order.lines.select_related("product").order_by("pk"):
        sku = _sku(line)
        if sku:
            merged[sku] = merged.get(sku, Decimal(0)) + Decimal(line.qty or 0)
    return list(merged.items())


def _products(order) -> dict:
    """{артикул: товар першого рядка з ним}."""
    out = {}
    for line in order.lines.select_related("product").order_by("pk"):
        sku = _sku(line)
        if sku and line.product_id and sku not in out:
            out[sku] = line.product
    return out


def _cells(rows) -> list[str]:
    num_w = len(str(len(rows)))
    sku_w = max(len(sku) for sku, _q in rows)
    qty_w = max(len(_qty(q)) for _s, q in rows)
    return [f"{str(n).rjust(num_w)}. {sku.ljust(sku_w)} × {_qty(q).rjust(qty_w)}"
            for n, (sku, q) in enumerate(rows, 1)]


def _per_line(width, size) -> int:
    """Скільки символів Courier New розміру size вміщується в ширину width (дюйми)."""
    return int(width * FIT / (CHAR_EM * size / 72))


def fit_size(rows, width=TEXT_W):
    """Найбільший шрифт, з яким усі позиції вміщуються в блок шириною width (і по ширині рядка, і за кількістю
    рядків), або None — навіть найменшим не вміщується."""
    if not rows:
        return FONT_SIZES[0]
    cell_w = len(_cells(rows)[0])
    for size in FONT_SIZES:
        cols_max = (_per_line(width, size) + GAP) // (cell_w + GAP)   # 0 — рядок ширший за блок
        lines_max = max(1, int(TEXT_H / (LINE_EM * size / 72)))
        if cols_max and len(rows) <= cols_max * lines_max:
            return size
    return None


def layout(rows, width=TEXT_W) -> tuple[int, list[str]]:
    """(розмір шрифту, рядки етикетки) — найбільший шрифт і найменше стовпчиків, з якими все вміщується в блок
    шириною width. Рядок позиції: «1. AN220214-01A × 5» (номер позиції, артикул, кількість — вирівняні)."""
    if not rows:
        return FONT_SIZES[0], []
    cells = _cells(rows)
    cell_w = len(cells[0])
    size = fit_size(rows, width) or FONT_SIZES[-1]
    cols_max = max(1, (_per_line(width, size) + GAP) // (cell_w + GAP))
    lines_max = max(1, int(TEXT_H / (LINE_EM * size / 72)))
    if len(cells) > cols_max * lines_max:     # навіть найменшим шрифтом не вміщується
        keep = cols_max * lines_max - 1
        cells = cells[:keep] + [f"+{len(cells) - keep} more"]
    cols = min(cols_max, math.ceil(len(cells) / lines_max))
    depth = math.ceil(len(cells) / cols)       # заповнення стовпчиками: згори вниз, потім праворуч
    lines = []
    for r in range(depth):
        parts = [cells[c * depth + r] for c in range(cols) if c * depth + r < len(cells)]
        lines.append((" " * GAP).join(p.ljust(cell_w) for p in parts).rstrip())
    return size, lines


def country(order) -> str:
    """Код країни доставки (ISO 2): адреса замовлення → отримувач останнього відправлення → регіон доставки."""
    from config.country_utils import normalize_to_iso2
    shipment = order.shipments.exclude(recipient_country="").order_by("-pk").first()
    for code in (order.addr_country, shipment.recipient_country if shipment else "",
                 normalize_to_iso2(order.shipping_region or "")):
        code = (code or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", code):
            return code
    return ""


# ── Фото артикулів ────────────────────────────────────────────────────────────

def photos_default() -> bool:
    """«Фото артикулів на етикетці замовлення» (SalesSettings.order_label_photos), за замовчуванням — так."""
    try:
        from sales.models import SalesSettings
        return SalesSettings.get().order_label_photos
    except Exception:
        return True


def photo_style_default() -> str:
    """«Фото на етикетці замовлення» (SalesSettings.order_label_photo_style): контур, за замовчуванням, або сіре."""
    try:
        from sales.models import SalesSettings
        style = SalesSettings.get().order_label_photo_style
    except Exception:
        style = ""
    return style if style in STYLES else STYLES[0]


def _otsu(gray) -> int:
    """Поріг, що найкраще ділить сіре зображення на темне й світле (метод Оцу)."""
    hist = gray.histogram()[:256]
    total, s_all = sum(hist), sum(i * h for i, h in enumerate(hist))
    w_b = s_b = 0
    best, threshold = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if not w_b or w_b == total:
            continue
        s_b += t * hist[t]
        m_b, m_f = s_b / w_b, (s_all - s_b) / (total - w_b)
        spread = w_b * (total - w_b) * (m_b - m_f) ** 2
        if spread > best:
            best, threshold = spread, t
    return threshold


def _outline(gray):
    """Сіре фото → лінії без заливки: великі темні площі — лише обвідка зсередини (PHOTO_RING), тонкі темні
    деталі (кабель, дріт) — суцільні, плюс найсильніші перепади яскравості (обриси світлих деталей, написи).
    Суцільне чорне термопринтер друкує плямою, а контур лишає видно, що це за річ."""
    from PIL import ImageChops, ImageFilter, ImageOps
    g = gray.filter(ImageFilter.GaussianBlur(0.7))
    dark = g.point(lambda v, t=min(_otsu(g), 150): 255 if v < t else 0)
    rim = ImageChops.subtract(dark, dark.filter(ImageFilter.MinFilter(2 * PHOTO_RING + 1)))
    sobel = [ImageFilter.Kernel((3, 3), k, scale=1, offset=128) for k in
             ([-1, 0, 1, -2, 0, 2, -1, 0, 1], [-1, -2, -1, 0, 0, 0, 1, 2, 1])]
    edge = ImageChops.add(*(g.filter(k).point(lambda v: abs(v - 128) * 2) for k in sobel))
    hist, need, cut = edge.histogram(), edge.size[0] * edge.size[1] * EDGE_SHARE, 255
    while cut > 0 and sum(hist[cut:]) < need:
        cut -= 1
    lines = ImageChops.lighter(rim, edge.point(lambda v: 255 if v >= max(cut, EDGE_MIN) else 0))
    w, h = lines.size                     # край фото — не обрис речі
    for box in ((0, 0, w, 2), (0, h - 2, w, h), (0, 0, 2, h), (w - 2, 0, w, h)):
        lines.paste(0, box)
    return ImageOps.invert(lines)


def _prepare(data: bytes, style: str = "outline") -> bytes:
    """Фото → PNG для термодруку, сторона ≤ PHOTO_PX, прозоре — на білому: контур (`_outline`) або сіре з
    підтягнутим контрастом і білим світлим фоном."""
    from PIL import Image, ImageOps
    with Image.open(BytesIO(data)) as im:
        im.draft("RGB", (PHOTO_PX * 2, PHOTO_PX * 2))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((PHOTO_PX, PHOTO_PX))
        if style == "outline" and max(im.size) < PHOTO_PX:     # дрібне фото — до клітинки, щоб обвідка не з'їла його
            k = PHOTO_PX / max(im.size)
            im = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))), Image.LANCZOS)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        im = ImageOps.autocontrast(im.convert("L"), cutoff=1)
        if style == "outline":
            im = _outline(im)
        else:
            im = im.point(lambda v: 255 if v >= WHITE_FROM else v)   # світлий фон — білий, без сірих цяток
        out = BytesIO()
        im.save(out, "PNG", optimize=True)
        return out.getvalue()


def label_photo(product, style: str = "outline") -> bytes | None:
    """PNG фото товару для етикетки або None (фото немає / недоступне); джерело — як у мініатюр."""
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    from inventory import thumbs
    if product is None or not thumbs.has_image(product):
        return None
    style = style if style in STYLES else STYLES[0]
    key = hashlib.sha1(f"label{PHOTO_PX}:{style}:v{PREP_VERSION}:{thumbs._key(product)}".encode()).hexdigest()[:24]
    name = f"{thumbs.THUMB_DIR}/label_{key}.png"
    if default_storage.exists(name):
        with default_storage.open(name, "rb") as f:
            return f.read()
    fail = name + ".fail"
    if default_storage.exists(fail):
        try:
            with default_storage.open(fail, "rb") as f:
                if time.time() - float(f.read() or 0) < PHOTO_RETRY:
                    return None
        except (OSError, ValueError):
            pass
        default_storage.delete(fail)
    try:
        data = _prepare(thumbs._source_bytes(product), style)
    except Exception:  # noqa: BLE001 — недоступне посилання або зіпсоване фото
        default_storage.save(fail, ContentFile(str(time.time()).encode()))
        return None
    default_storage.save(name, ContentFile(data))
    return data


def photo_grid(count) -> tuple[int, int, float]:
    """(стовпчиків, рядків, сторона клітинки) для count фото праворуч від списку (висота — як у списку)."""
    if count <= 0:
        return 0, 0, 0.0
    if count == 1:
        return 1, 1, TEXT_H
    if count == 2:
        return 1, 2, TEXT_H / 2
    return 2, 2, TEXT_H / 2


def build(order, photos=None, style=None) -> dict:
    """photos — None: як у налаштуваннях; True / False — для цього друку. style — «outline» / «gray» для цього
    друку, None — як у налаштуваннях."""
    from inventory import thumbs
    rows = items(order)
    want = photos_default() if photos is None else bool(photos)
    style = style if style in STYLES else photo_style_default()
    products = _products(order)
    available = sum(1 for sku, _q in rows if products.get(sku) is not None and thumbs.has_image(products[sku]))
    pics, too_long = [], False
    if want and 0 < len(rows) <= PHOTO_MAX:
        for n, (sku, _q) in enumerate(rows, 1):
            png = label_photo(products.get(sku), style)
            if png:
                pics.append((n, png))
    if pics:      # фото забирають ширину — довгі артикули тоді не обрізаються, а етикетка без фото
        size = fit_size(rows, _text_width(len(pics)))
        too_long = size is None or size < PHOTO_MIN_FONT
        if too_long:
            pics = []
    width = _text_width(len(pics))
    size, lines = layout(rows, width)
    return {
        "order": order.order_number,
        "country": country(order),
        "font_size": size,
        "lines": lines,
        "items": [(sku, _qty(q)) for sku, q in rows],
        "photos": pics,
        "text_w": width,
        "photo_info": {"on": want, "shown": len(pics), "available": available, "positions": len(rows),
                       "max": PHOTO_MAX, "too_long": too_long, "style": style},
    }


def _text_width(photos) -> float:
    """Ширина списку позицій, коли праворуч photos фото."""
    cols, _rows, cell = photo_grid(photos)
    return TEXT_W - (cols * cell + PHOTO_GAP if photos else 0)


_IMAGE = """        <ImageObject>
          <Name>MinervaPhoto{n}</Name>
          <Brushes>
            <BackgroundBrush><SolidColorBrush><Color A="0" R="0" G="0" B="0"></Color></SolidColorBrush></BackgroundBrush>
            <BorderBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></BorderBrush>
            <StrokeBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></StrokeBrush>
            <FillBrush><SolidColorBrush><Color A="0" R="0" G="0" B="0"></Color></SolidColorBrush></FillBrush>
          </Brushes>
          <Rotation>Rotation0</Rotation>
          <OutlineThickness>1</OutlineThickness>
          <IsOutlined>False</IsOutlined>
          <BorderStyle>SolidLine</BorderStyle>
          <Margin><DYMOThickness Left="0" Top="0" Right="0" Bottom="0" /></Margin>
          <Data>{data}</Data>
          <ScaleMode>Uniform</ScaleMode>
          <HorizontalAlignment>Center</HorizontalAlignment>
          <VerticalAlignment>Middle</VerticalAlignment>
          <ObjectLayout>
            <DYMOPoint><X>{x:.4f}</X><Y>{y:.4f}</Y></DYMOPoint>
            <Size><Width>{w:.4f}</Width><Height>{h:.4f}</Height></Size>
          </ObjectLayout>
        </ImageObject>
"""

_NUMBER = """        <TextObject>
          <Name>MinervaPhotoNo{n}</Name>
          <Brushes>
            <BackgroundBrush><SolidColorBrush><Color A="1" R="1" G="1" B="1"></Color></SolidColorBrush></BackgroundBrush>
            <BorderBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></BorderBrush>
            <StrokeBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></StrokeBrush>
            <FillBrush><SolidColorBrush><Color A="0" R="0" G="0" B="0"></Color></SolidColorBrush></FillBrush>
          </Brushes>
          <Rotation>Rotation0</Rotation>
          <OutlineThickness>1</OutlineThickness>
          <IsOutlined>False</IsOutlined>
          <BorderStyle>SolidLine</BorderStyle>
          <Margin><DYMOThickness Left="0" Top="0" Right="0" Bottom="0" /></Margin>
          <HorizontalAlignment>Left</HorizontalAlignment>
          <VerticalAlignment>Top</VerticalAlignment>
          <FitMode>None</FitMode>
          <IsVertical>False</IsVertical>
          <FormattedText>
            <FitMode>None</FitMode>
            <HorizontalAlignment>Left</HorizontalAlignment>
            <VerticalAlignment>Top</VerticalAlignment>
            <IsVertical>False</IsVertical>
            <LineTextSpan>
              <TextSpan>
                <Text>{n}</Text>
                <FontInfo>
                  <FontName>Arial</FontName>
                  <FontSize>7</FontSize>
                  <IsBold>True</IsBold>
                  <IsItalic>False</IsItalic>
                  <IsUnderline>False</IsUnderline>
                  <FontBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></FontBrush>
                </FontInfo>
              </TextSpan>
            </LineTextSpan>
          </FormattedText>
          <ObjectLayout>
            <DYMOPoint><X>{x:.4f}</X><Y>{y:.4f}</Y></DYMOPoint>
            <Size><Width>0.1300</Width><Height>0.1300</Height></Size>
          </ObjectLayout>
        </TextObject>
"""


def _photo_objects(pics) -> str:
    cols, _rows, cell = photo_grid(len(pics))
    x0 = LIST_X + TEXT_W - cols * cell
    out = []
    for i, (n, png) in enumerate(pics):
        x, y = x0 + (i % cols) * cell, LIST_Y + (i // cols) * cell
        out.append(_IMAGE.format(n=n, data=base64.b64encode(png).decode("ascii"), x=x + PHOTO_PAD, y=y + PHOTO_PAD,
                                 w=cell - 2 * PHOTO_PAD, h=cell - 2 * PHOTO_PAD))
        if len(pics) > 1:      # номер позиції в куті фото
            out.append(_NUMBER.format(n=n, x=x + PHOTO_PAD, y=y + PHOTO_PAD))
    return "".join(out)


def render(data) -> str:
    """XML етикетки (.dymo) з шаблону."""
    xml = TEMPLATE_PATH.read_text(encoding="utf-8-sig")
    block = re.search(r"[ \t]*<!--LINE-->\r?\n(.*?)[ \t]*<!--/LINE-->\r?\n", xml, re.S)
    span = block.group(1).replace("{{FS}}", str(data["font_size"]))
    spans = "".join(span.replace("{{LINE}}", escape(line.replace(" ", NBSP))) for line in data["lines"])
    xml = xml[:block.start()] + spans + xml[block.end():]
    country = NBSP * 3 + data["country"] if data["country"] else ""      # той самий рядок, що й номер
    for marker, value in (("{{ORDER}}", data["order"]), ("{{COUNTRY}}", country)):
        xml = xml.replace(marker, escape(value))
    # Список: ширина — без місця для фото, висота — рівно під рядки нашим шрифтом, AlwaysFit (шаблон) — DYMO сам
    # зменшує текст, якщо він не вміщується (власні метрики шрифту, «✏️ Правки»), і не роздуває його понад цей розмір
    width = data.get("text_w", TEXT_W)
    height = min(TEXT_H, max(1, len(data["lines"])) * LINE_EM * data["font_size"] / 72)
    head, body = xml.split("<Name>ITextObject1</Name>", 1)
    obj, rest = body.split("</TextObject>", 1)
    obj = re.sub(r"<Width>[\d.]+</Width>", f"<Width>{width:.4f}</Width>", obj, count=1)
    obj = re.sub(r"<Height>[\d.]+</Height>", f"<Height>{height:.4f}</Height>", obj, count=1)
    xml = head + "<Name>ITextObject1</Name>" + obj + "</TextObject>" + rest
    extra = _photo_objects(data.get("photos") or [])
    if extra:
        head, sep, tail = xml.rpartition("</LabelObjects>")
        xml = head + extra + "      " + sep + tail
    return xml
