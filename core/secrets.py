"""Показ ключів і секретів в адмінці: замасковано, лише початок і кінець — щоб ключ можна було впізнати.

mask("sk_test_51AbCdEf…Xy9Z") → "sk_test_51••••••••Xy9Z"; mask("abcd…wxyz") → "abcd••••••••wxyz".
reveal(value) — замаскований ключ + «👁 Показати» і «📋 Копіювати» (повне значення лише в атрибуті сторінки адмінки).
"""
from django.utils.html import format_html

# Відомі префікси показуються повністю (видно, що це за ключ і який режим), далі ще 2 символи
PREFIXES = ("sk_test_", "sk_live_", "rk_test_", "rk_live_", "pk_test_", "pk_live_", "whsec_")
DOT = "•"


def mask(value, head: int = 4, tail: int = 4) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    prefix = next((p for p in PREFIXES if value.startswith(p)), "")
    head = len(prefix) + 2 if prefix else head
    if len(value) <= head + tail + 4:   # короткий ключ — показати лише кінець
        return DOT * 8 + value[-2:]
    return value[:head] + DOT * 8 + value[-tail:]


def placeholder(value, empty: str = "не задано") -> str:
    """Підказка в полі-паролі: що збережено зараз (поле лишається порожнім)."""
    return f"збережено: {mask(value)} — введіть новий, щоб замінити" if value else empty


_REVEAL_JS = ("var c=this.closest('.mv-secret'),v=c.querySelector('code');"
              "if(this.dataset.act==='show'){var s=v.textContent===c.dataset.mask;"
              "v.textContent=s?c.dataset.value:c.dataset.mask;this.textContent=s?'🙈 Сховати':'👁 Показати';}"
              "else{navigator.clipboard.writeText(c.dataset.value).then(()=>{var t=this.textContent;"
              "this.textContent='✓ Скопійовано';setTimeout(()=>this.textContent=t,1500)})}")


def reveal(value, note=""):
    """Замаскований ключ з кнопками «Показати» / «Копіювати» (для сторінок адмінки, де ключ треба скопіювати)."""
    if not value:
        return "—"
    return format_html(
        '<span class="mv-secret" data-value="{}" data-mask="{}">'
        '<code style="font-size:13px;background:#0d1117;padding:6px 10px;border-radius:4px;'
        'border:1px solid #2a3f52;display:inline-block">{}</code> '
        '<button type="button" class="button" data-act="show" onclick="{}">👁 Показати</button> '
        '<button type="button" class="button" data-act="copy" onclick="{}">📋 Копіювати</button></span>{}',
        value, mask(value), mask(value), _REVEAL_JS, _REVEAL_JS,
        format_html('<div style="margin-top:6px;font-size:12px;opacity:.8">{}</div>', note) if note else "")
