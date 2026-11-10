"""Поля таблиць «Хто що отримує» (Налаштування → Сповіщення, картка магазину): адреси й Telegram-чати чипами,
«＋ з Minerva» (співробітники, скриньки, Telegram ID) і своя адреса; прапорці «✉️ / 💬».

Без JavaScript це звичайне текстове поле «a@x.de, b@y.de» (працює й так); static/admin/js/mv_notify_pick.js
малює чипи. sync — поле форми картки з таким id: значення пишеться туди (і назад), тоді власного name немає.
"""
from __future__ import annotations

from django.templatetags.static import static
from django.utils.html import format_html, format_html_join, json_script
from django.utils.safestring import mark_safe

ASSETS_VERSION = "2"


def assets(email_options, chat_options) -> str:
    """Скрипт, стилі й списки «з Minerva» — один раз на сторінку (у першій клітинці таблиці)."""
    return json_script({"email": email_options, "tg": chat_options}, "nt-opts") + format_html(
        '<script src="{}?v={}" defer></script>', static("admin/js/mv_notify_pick.js"), ASSETS_VERSION) + STYLE


def picker(kind: str, value: str, *, name: str = "", sync: str = "", empty: str = "", hint: str = "") -> str:
    """kind email | tg. name — поле форми (своє), sync — id поля картки. empty — що діє, коли порожньо."""
    attrs = format_html(' name="{}"', name) if name else format_html(' data-sync="{}" disabled', sync)
    return format_html(
        '<div class="nt-pick" data-kind="{}" data-empty="{}">'
        '<input type="text" class="nt-in" value="{}"{} placeholder="{}" autocomplete="off" '
        'style="width:100%;box-sizing:border-box">{}</div>',
        kind, empty, value, attrs, "a@firma.de, b@firma.de" if kind == "email" else "Chat ID, напр. -1001234567890",
        format_html('<div class="help">{}</div>', hint) if hint else "")


def check(label: str, checked: bool, *, name: str = "", sync: str = "", title: str = "", role: str = "") -> str:
    """role main — головний вимикач рядка (вимкнено — рядок блідий), aux — не канал (напр. «✉️ сам»)."""
    attrs = format_html(' name="{}"', name) if name else format_html(' data-sync="{}" disabled', sync)
    if role:
        attrs = format_html('{} data-nt-{}="1"', attrs, role)
    return format_html('<label class="nt-chk" title="{}"><input type="checkbox"{}{}> {}</label>', title,
                       format_html(" checked") if checked else "", attrs, label)


def links(items) -> str:
    """[(go | url, підпис, external)] → «↓» до поля на сторінці (data-nt-go), «→» на іншу сторінку."""
    def one(target, label, external=False):
        if target.startswith(("/", "http")):
            return format_html('<a href="{}"{}>{} →</a>', target,
                               format_html(' target="_blank" rel="noopener"') if external else "", label)
        return format_html('<a href="#" data-nt-go="{}">{} ↓</a>', target, label)
    return format_html_join(" · ", "{}", ((one(*i),) for i in items if i and i[0]))


STYLE = mark_safe("""<style>
.form-row.field-notify_map_col .flex-container>label,.form-row.field-mail_map_col .flex-container>label{display:none}
.form-row.field-notify_map_col .readonly,.form-row.field-mail_map_col .readonly{margin-left:0;width:100%;max-width:none}
.mm-table td{vertical-align:top}
.mm-table select.mm-route{max-width:260px}
.mm-table .help{white-space:normal}
.nt-table{font-size:12px;width:100%;border-collapse:collapse}
.nt-table th{text-align:left;font-weight:600;padding:4px 6px;border-bottom:1px solid var(--hairline-color,#ddd)}
.nt-table td{padding:6px;vertical-align:top;border-bottom:1px solid var(--hairline-color,#eee)}
.nt-table tr.nt-sec td{background:var(--darkened-bg,#f6f6f6);font-weight:600;padding:6px}
.nt-table tr.nt-off td.nt-what{opacity:.55}
.nt-table .nt-when{color:var(--body-quiet-color,#777);font-size:11px;margin-top:2px}
.nt-chk{display:inline-flex;align-items:center;gap:4px;margin:0 8px 4px 0;white-space:nowrap;font-weight:600}
.nt-chips{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin-top:2px}
.nt-chip{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--hairline-color,#ccc);border-radius:12px;
  padding:1px 4px 1px 8px;background:var(--body-bg,#fff);max-width:100%}
.nt-chip small{color:var(--body-quiet-color,#777)}
.nt-chip button{border:0;background:none;cursor:pointer;font-size:12px;padding:0 3px;color:inherit;opacity:.6}
.nt-chip button:hover{opacity:1}
.nt-empty{color:var(--body-quiet-color,#777);font-style:italic}
.nt-tools{display:flex;gap:4px;margin-top:4px;flex-wrap:wrap}
.nt-tools select,.nt-tools input{font-size:12px;max-width:100%}
.nt-tools input{flex:1 1 140px;min-width:120px}
.nt-bad{outline:2px solid #d32f2f}
.nt-flash{outline:3px solid var(--accent,#79aec8);outline-offset:2px;transition:outline-color .4s}
</style>""")
