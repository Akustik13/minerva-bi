"""Блоки «⏱ Час askDANTE» для карток інших розділів (проєкт Minerva, клієнт CRM). Лише читають копію записів.

Як «⏱ Час askDANTE» у панелі клієнта (crm/side_panel.hours_html): блок — лише тим, кому відкрито розділ
«Робочий час» (has_module_perms('worktime')); опис запису (там і теми листів «✉️ Часу на пошту») — лише його автору;
вартість за ставками askDANTE — лише адміністраторам (services.is_manager). viewer — хто дивиться картку."""
from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services

MONTHS = ["", "січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]


def hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


VIEWER_ATTR = "_worktime_viewer"


def remember_viewer(obj, user):
    """ModelAdmin.get_object → запис картки знає, хто її дивиться (поле картки «Години» отримує лише obj)."""
    if obj is not None:
        setattr(obj, VIEWER_ATTR, user)
    return obj


def viewer_of(obj):
    return getattr(obj, VIEWER_ATTR, None)


def can_see(viewer) -> bool:
    """Блок «⏱ Час askDANTE» у картках — лише тим, кому відкрито розділ «Робочий час»; невідомо хто — ні."""
    return bool(viewer is not None and viewer.has_module_perms("worktime"))


def _what(r, viewer) -> str:
    """«Що» в записі: вид роботи завжди, опис — лише автору запису (там і теми листів, і зміст роботи)."""
    own = bool(r.dante_user_id and r.dante_user and r.dante_user.user_id == viewer.pk)
    parts = [r.activity_name, (r.description or "")[:80] if own else ""]
    return " · ".join(p for p in parts if p) or "—"


def hours_html(data: dict | None, empty: str, viewer) -> str:
    if not can_see(viewer):
        return ""
    mapping = reverse("admin:worktime_danteproject_changelist")
    if not data:
        return format_html('<span style="color:var(--body-quiet-color)">{}</span> '
                           '<a href="{}">📁 Проєкти askDANTE ↔ Minerva</a>', empty, mapping)
    names = ", ".join(str(p) for p in data["projects"][:4]) + (" …" if len(data["projects"]) > 4 else "")
    people = format_html_join(" · ", "{} <b>{}</b>", ((n, hm(m)) for n, m in data["people"]))
    months = format_html_join(" · ", "{} <b>{}</b>",
                              ((f"{MONTHS[m.month]} {m:%Y}" if m else "—", hm(v)) for m, v in data["months"]))
    recent = format_html_join("", '<tr><td style="white-space:nowrap;padding-right:10px">{}</td><td>{}</td>'
                                  '<td style="padding:0 10px">{}</td><td style="text-align:right">{}</td></tr>',
                              ((f"{r.day:%d.%m.%Y}", r.dante_user or r.person_id, _what(r, viewer),
                                hm(r.duration)) for r in data["recent"]))
    synced = (f"оновлено {timezone.localtime(data['synced_at']):%d.%m.%Y %H:%M}" if data["synced_at"]
              else "ще не оновлювалось")
    shared = (f" · проєкт askDANTE спільний ще з {data['shared']} проєктами Minerva — години спільні"
              if data.get("shared") else "")
    cost = format_html(" · за ставками askDANTE ≈ <b>{}</b>", f"{data['cost']:,.2f}".replace(",", " ")) \
        if data.get("cost") and services.is_manager(viewer) else ""
    return format_html(
        '<div><b style="font-size:15px">{}</b> год за останні {} днів{}{}</div>'
        '<div style="margin-top:4px">{}</div><div style="margin-top:2px;color:var(--body-quiet-color)">{}</div>'
        '<table style="margin-top:6px;font-size:12px">{}</table>'
        '<div class="help" style="margin-top:4px">askDANTE: {} · {} · <a href="{}">📁 зв\'язки проєктів</a>'
        ' · опис запису видно лише його автору</div>',
        hm(data["total"]), data["days"], cost, shared, people or "—", months, recent, names, synced, mapping)


def project_panel(project, viewer) -> str:
    if not can_see(viewer):
        return ""
    return hours_html(services.project_hours(project),
                      "Проєкт ще не зв'язаний з проєктом askDANTE —", viewer)


def customer_panel(customer, viewer) -> str:
    if not can_see(viewer):
        return ""
    return hours_html(services.customer_hours(customer),
                      "Немає проєктів askDANTE цього клієнта —", viewer)
