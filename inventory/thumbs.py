"""Мініатюри фото й посилання на даташит для списків (Товари, Асортимент і ціни, Склад → огляд залишків)
і фільтр «Фото / даташит».

Мініатюра 96 px робиться на сервері й кешується в MEDIA_ROOT/thumbs/: джерело — файл у картці товару,
інакше перевірена копія «🖼 Фото й даташити» магазину, інакше посилання (завантажується один раз).
Ключ кешу — ім'я файлу або адреса: змінили фото в картці — нова мініатюра. Невдача запам'ятовується на годину,
щоб список не чекав щоразу на недоступне посилання.
"""
from __future__ import annotations

import hashlib
import time
from io import BytesIO

from django.contrib import admin
from django.core.files.base import ContentFile
from django.db.models import Q
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils.html import format_html

from .services.media_files import IMAGE, fetch, read_field

SIZE = 96
THUMB_DIR = "thumbs"
RETRY_AFTER = 3600


def has_image(p) -> bool:
    return bool((p.image and p.image.name) or (p.image_url or "").strip())


def has_datasheet(p) -> bool:
    return bool((p.datasheet_file and p.datasheet_file.name) or (p.datasheet_url or "").strip())


def _key(p) -> str:
    if p.image and p.image.name:
        return "file:" + p.image.name
    return "url:" + (p.image_url or "").strip()


def thumb_url(p) -> str:
    """Адреса мініатюри (з версією — змінили фото, змінилась адреса) або '' — фото немає."""
    if not has_image(p):
        return ""
    v = hashlib.sha1(_key(p).encode()).hexdigest()[:10]
    return reverse("admin:inventory_product_thumb", args=[p.pk]) + "?v=" + v


def image_href(p) -> str:
    return p.image_display_url


def datasheet_href(p) -> str:
    """Файл — через Minerva (обхід маршрутизації медіа на NAS), інакше посилання."""
    if p.datasheet_file and p.datasheet_file.name:
        return reverse("admin:inventory_product_serve_datasheet", args=[p.pk])
    return (p.datasheet_url or "").strip()


def _source_bytes(p) -> bytes:
    if p.image and p.image.name:
        return read_field(p.image, IMAGE)
    try:   # перевірена копія магазину для того самого посилання — без мережі
        from shop.media import fingerprint
        from shop.models import ProductMedia
        row = ProductMedia.objects.filter(product=p, kind=IMAGE).exclude(stored="").first()
        if row and row.inputs == fingerprint(p, IMAGE) and default_storage.exists(row.stored):
            with default_storage.open(row.stored, "rb") as f:
                return f.read()
    except Exception:  # noqa: BLE001 — магазин не обов'язковий
        pass
    return fetch(p.image_url.strip(), IMAGE, timeout=(4, 8))


def render(data: bytes) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(BytesIO(data)) as im:
        im.draft("RGB", (SIZE * 2, SIZE * 2))   # JPEG: швидше декодування великих фото
        im = ImageOps.exif_transpose(im)
        im.thumbnail((SIZE, SIZE))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        out = BytesIO()
        im.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()


def get(p) -> bytes | None:
    """JPEG-мініатюра товару або None (фото немає / недоступне)."""
    if not has_image(p):
        return None
    name = f"{THUMB_DIR}/{hashlib.sha1(_key(p).encode()).hexdigest()[:24]}.jpg"
    if default_storage.exists(name):
        with default_storage.open(name, "rb") as f:
            return f.read()
    fail = name + ".fail"
    if default_storage.exists(fail):
        try:
            with default_storage.open(fail, "rb") as f:
                if time.time() - float(f.read() or 0) < RETRY_AFTER:
                    return None
        except (OSError, ValueError):
            pass
        default_storage.delete(fail)
    try:
        data = render(_source_bytes(p))
    except Exception:  # noqa: BLE001 — недоступне посилання (MediaError) або зіпсоване фото (Pillow)
        default_storage.save(fail, ContentFile(str(time.time()).encode()))
        return None
    default_storage.save(name, ContentFile(data))
    return data


_NONE = "display:inline-block;width:36px;text-align:center;opacity:.35;font-size:13px;white-space:nowrap"


def cell(p, note="") -> str:
    """Мініатюра + 📄 для колонки списку; бліді значки — немає. note — додаткова позначка (напр. стан на сайті)."""
    thumb, ds = thumb_url(p), datasheet_href(p)
    img = (format_html('<a href="{}" target="_blank" rel="noopener" title="Фото — відкрити" onclick="event.stopPropagation()">'
                       '<img src="{}" alt="" loading="lazy" width="36" height="36" class="inv-thumb" '
                       'style="width:36px;height:36px;object-fit:contain;border-radius:4px;background:#fff;display:block" '
                       'onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),'
                       '{{textContent:\'🖼✕\',title:\'Фото недоступне\',className:\'inv-thumb-none\'}},'
                       '{{style:\'{}\'}}))"></a>', image_href(p), thumb, _NONE)
           if thumb else format_html('<span class="inv-thumb-none" style="{}" title="Немає фото">🖼</span>', _NONE))
    sheet = (format_html('<a href="{}" target="_blank" rel="noopener" class="inv-ds" title="Даташит — відкрити" '
                         'style="font-size:16px;text-decoration:none" onclick="event.stopPropagation()">📄</a>', ds)
             if ds else format_html('<span class="inv-ds inv-ds-none" style="font-size:16px;opacity:.25" '
                                    'title="Немає даташита">📄</span>'))
    return format_html('<span class="inv-media" style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap">'
                       '{}{}{}</span>', img, sheet, note)


class MediaFilter(admin.SimpleListFilter):
    """Є / немає фото чи даташита (файл у картці або посилання). prefix — шлях до товару (напр. «product__»)."""
    title = "Фото / даташит"
    parameter_name = "media"
    prefix = ""

    def lookups(self, request, model_admin):
        return [("no_image", "🖼 Без фото"), ("no_ds", "📄 Без даташита"), ("no_both", "Без фото й без даташита"),
                ("image", "З фото"), ("ds", "З даташитом"),
                ("broken", "❌ Посилання не відповідає (перевірка для сайту)")]

    def queryset(self, request, qs):
        p = self.prefix
        no_image = (Q(**{p + "image": ""}) | Q(**{p + "image__isnull": True})) & Q(**{p + "image_url": ""})
        no_ds = ((Q(**{p + "datasheet_file": ""}) | Q(**{p + "datasheet_file__isnull": True}))
                 & Q(**{p + "datasheet_url": ""}))
        v = self.value()
        if v == "no_image":
            return qs.filter(no_image)
        if v == "no_ds":
            return qs.filter(no_ds)
        if v == "no_both":
            return qs.filter(no_image & no_ds)
        if v == "image":
            return qs.exclude(no_image)
        if v == "ds":
            return qs.exclude(no_ds)
        if v == "broken":   # «🖼 Фото й даташити»: перевірка не вдалась і копії немає
            return qs.filter(**{p + "shop_media__status": "error", p + "shop_media__stale": False}).distinct()
        return qs


class ListingMediaFilter(MediaFilter):
    prefix = "product__"
