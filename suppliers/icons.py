"""🗂 Значок категорії постачальників: стандартний емодзі з палітри, свій емодзі або своя картинка.

  PALETTE — стандартні значки (клік у формі категорії — вибрати);
  IconPickerWidget — поле «Значок»: емодзі + палітра + як виглядатиме;
  clean_image(file) → PNG до IMAGE_PX px (PNG / JPG / GIF / WEBP за першими байтами, ≤ IMAGE_MAX_MB);
  SupplierCategory.icon_html — картинка, якщо є, інакше емодзі (у списках, плитках, формах).
Картинки — MEDIA_ROOT/supplier_categories/ (публічні, як мініатюри товарів: лише значки).
"""
import io

from django import forms
from django.core.files.base import ContentFile
from django.utils.html import format_html

PALETTE = [
    # виробництво електроніки
    "🏭", "🟩", "🔧", "🔩", "🔌", "📡", "📶", "🛰️", "🔋", "⚡", "💡", "🧲", "🎛️", "🖥️", "📱", "🖨️", "🔊", "📷",
    "🌡️", "🧪", "📐", "🧵", "🪛", "🛠️", "⚙️", "🧱", "🧰", "🪵", "✂️", "🔦",
    # закупівля й логістика
    "📦", "🚚", "✈️", "🚢", "🛃", "🛒", "🏬", "🏢", "🧾", "💶", "💳", "📄", "🗂️", "🔗",
    # люди й послуги
    "👥", "👤", "🤝", "🎓", "⚖️", "🌐", "📢", "✉️", "🔔", "⭐", "🚀", "🧩",
    # кольори
    "🟥", "🟧", "🟨", "🟦", "🟪", "⬛", "⬜",
]
IMAGE_PX = 128
IMAGE_MAX_MB = 5
SIGNATURES = ((b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF8", "gif"), (b"RIFF", "webp"))


class IconPickerWidget(forms.TextInput):
    """Поле емодзі + палітра (клік — вибрати) + «як виглядатиме». Будь-який свій емодзі теж можна вписати."""

    def __init__(self, attrs=None):
        super().__init__(attrs={"maxlength": 8, "style": "width:5em;font-size:20px;text-align:center", **(attrs or {})})

    def render(self, name, value, attrs=None, renderer=None):
        field = super().render(name, value, attrs, renderer)
        box_id = f"ip-{(attrs or {}).get('id') or name}"
        from django.utils.html import format_html_join
        buttons = format_html_join("", '<button type="button" class="ip-b" data-v="{}">{}</button>',
                                   ((e, e) for e in PALETTE))
        return format_html(
            '<div class="ip" id="{}">{} <span class="ip-hint">← свій емодзі або виберіть нижче '
            '(Win + . — усі емодзі Windows)</span>'
            '<div class="ip-pal">{}<button type="button" class="ip-b ip-x" data-v="" title="Без значка">✕</button>'
            '</div>'
            '<script>(function(){{var box=document.currentScript.parentNode,inp=box.querySelector("input");'
            'function mark(){{box.querySelectorAll(".ip-b").forEach(function(b){{b.classList.toggle("on",'
            'b.dataset.v===inp.value.trim()&&b.dataset.v!=="");}});}}'
            'box.querySelectorAll(".ip-b").forEach(function(b){{b.addEventListener("click",function(){{'
            'inp.value=b.dataset.v;inp.dispatchEvent(new Event("input",{{bubbles:true}}));mark();}});}});'
            'inp.addEventListener("input",mark);mark();}})();</script></div>'
            '<style>.ip-pal{{display:flex;flex-wrap:wrap;gap:3px;margin-top:6px;max-width:640px}}'
            '.ip-b{{font-size:18px;width:34px;height:34px;border:1px solid var(--border-strong,#ccc);border-radius:6px;'
            'background:transparent;cursor:pointer;padding:0}}.ip-b:hover,.ip-b.on{{outline:2px solid '
            'var(--primary,#5b8def)}}.ip-x{{font-size:13px;color:var(--text-muted,#888)}}'
            '.ip-hint{{font-size:11px;color:var(--text-muted,#888)}}</style>',
            box_id, field, buttons)


def clean_image(upload):
    """Завантажена картинка → ContentFile PNG до IMAGE_PX px з прозорістю; не картинка / завелика — ValidationError."""
    if not upload:
        return upload
    data = upload.read()
    if len(data) > IMAGE_MAX_MB * 1024 * 1024:
        raise forms.ValidationError(f"Картинка понад {IMAGE_MAX_MB} МБ.")
    kind = next((k for sig, k in SIGNATURES if data.startswith(sig)), "")
    if kind == "webp" and data[8:12] != b"WEBP":
        kind = ""
    if not kind:
        raise forms.ValidationError("Картинка — PNG, JPG, GIF або WEBP.")
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGBA")
        img.thumbnail((IMAGE_PX, IMAGE_PX))
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
    except Exception:  # noqa: BLE001 — зламаний файл, бомба розпакування
        raise forms.ValidationError("Не вдалося відкрити картинку.")
    stem = "".join(ch for ch in str(getattr(upload, "name", "icon")).rsplit(".", 1)[0] if ch.isalnum() or ch in "-_")
    return ContentFile(out.getvalue(), name=f"{stem[:40] or 'icon'}.png")
