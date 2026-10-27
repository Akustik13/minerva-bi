"""Блоки «⏱ Час askDANTE» для карток інших розділів (проєкт Minerva, клієнт CRM). Лише читають копію записів."""
from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services

MONTHS = ["", "січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]


def hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def hours_html(data: dict | None, empty: str) -> str:
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
                              ((f"{r.day:%d.%m.%Y}", r.dante_user or r.person_id,
                                (r.activity_name + " · " if r.activity_name else "") + (r.description or "")[:80],
                                hm(r.duration)) for r in data["recent"]))
    synced = (f"оновлено {timezone.localtime(data['synced_at']):%d.%m.%Y %H:%M}" if data["synced_at"]
              else "ще не оновлювалось")
    shared = (f" · проєкт askDANTE спільний ще з {data['shared']} проєктами Minerva — години спільні"
              if data.get("shared") else "")
    cost = format_html(" · за ставками askDANTE ≈ <b>{}</b>", f"{data['cost']:,.2f}".replace(",", " ")) \
        if data.get("cost") else ""
    return format_html(
        '<div><b style="font-size:15px">{}</b> год за останні {} днів{}{}</div>'
        '<div style="margin-top:4px">{}</div><div style="margin-top:2px;color:var(--body-quiet-color)">{}</div>'
        '<table style="margin-top:6px;font-size:12px">{}</table>'
        '<div class="help" style="margin-top:4px">askDANTE: {} · {} · <a href="{}">📁 зв\'язки проєктів</a></div>',
        hm(data["total"]), data["days"], cost, shared, people or "—", months, recent, names, synced, mapping)


def project_panel(project) -> str:
    return hours_html(services.project_hours(project),
                      "Проєкт ще не зв'язаний з проєктом askDANTE —")


def customer_panel(customer) -> str:
    return hours_html(services.customer_hours(customer),
                      "Немає проєктів askDANTE цього клієнта —")
