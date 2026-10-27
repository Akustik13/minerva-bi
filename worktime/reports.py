"""Звіт по працівниках askDANTE: що заповнено, що ні, час на проєкти — на сторінці й поштою за розкладом.

Дані — askDANTE (свіже читання днів і записів часу за період; збій — з копії в Minerva, з позначкою).
Тип відсутності (лікарняний) — чутливі дані: у повному звіті лише за галочкою, працівник свої бачить завжди.
"""
from __future__ import annotations

import calendar as pycal
import datetime
import logging
import re

from django.conf import settings
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone

from . import services
from .client import DanteError
from .models import Booking, DanteSettings, DanteUser, TimeRecord, WorkReport

logger = logging.getLogger(__name__)


def hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _ranges(days: list[datetime.date], st: DanteSettings) -> str:
    """[1.10, 2.10, 5.10, 9.10] → «01–05.10, 09.10» (вихідні між днями не розривають проміжок)."""
    out, days = [], sorted(days)
    i = 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and all(not st.is_workday(days[j] + datetime.timedelta(days=k))
                                        for k in range(1, (days[j + 1] - days[j]).days)):
            j += 1
        a, b = days[i], days[j]
        out.append(f"{a:%d.%m}" if a == b else f"{a:%d}–{b:%d.%m}" if a.month == b.month else f"{a:%d.%m}–{b:%d.%m}")
        i = j + 1
    return ", ".join(out)


def person(st: DanteSettings, du: DanteUser, date_from, date_to, refresh: bool = True) -> dict:
    """Підсумок одного працівника за період."""
    error = ""
    today = services.local_today()
    last = min(date_to, today - datetime.timedelta(days=1)) if date_to >= today else date_to
    if refresh and st.enabled and date_from <= last:
        try:
            services.refresh_days(du, date_from, last)
        except DanteError as e:
            error = e.message
    days = services.cached_days(du, date_from, date_to)
    workdays = filled = 0
    empty, no_project, absences = [], [], []
    worked = target = 0
    records = services.day_records(du, date_from, date_to)
    day = date_from
    while day <= last:
        wd = days.get(day)
        blocker = services.day_blocker(st, wd, day)
        if wd and wd.times:
            filled += 1
            workdays += 1
            worked += wd.minutes
            if not records.get(day):
                no_project.append(day)
        elif blocker and not blocker.startswith("вже є"):
            if blocker not in ("вихідний", "неробочий день"):
                absences.append((day, blocker))
        else:
            workdays += 1
            empty.append(day)
        if wd and wd.target_minutes:
            target += wd.target_minutes
        day += datetime.timedelta(days=1)
    projects = (TimeRecord.objects.filter(dante_user=du, day__range=(date_from, date_to)).exclude(dante_project=None)
                .values("dante_project__name").annotate(m=Sum("duration")).order_by("-m"))
    project_minutes = sum(p["m"] for p in projects)
    by_minerva = Booking.objects.filter(dante_user=du, date__range=(date_from, date_to), status=Booking.SENT).count()
    kinds = {}
    for _, label in absences:
        kinds[label] = kinds.get(label, 0) + 1
    return {
        "du": du, "error": error, "workdays": workdays, "filled": filled, "empty": empty,
        "empty_text": _ranges(empty, st), "no_project": no_project, "no_project_text": _ranges(no_project, st),
        "absent_days": len(absences), "absence_kinds": sorted(kinds.items()), "worked": hm(worked),
        "target": hm(target) if target else "", "project_minutes": project_minutes, "projects_hm": hm(project_minutes),
        "share": round(100 * project_minutes / worked) if worked else None,
        "projects": [(p["dante_project__name"], hm(p["m"])) for p in projects[:5]], "by_minerva": by_minerva,
        "fill": round(100 * filled / workdays) if workdays else None,
    }


def build(people, date_from, date_to, refresh: bool = True) -> dict:
    st = DanteSettings.get()
    if refresh and st.enabled:
        try:
            services.sync_records(st, date_from, min(date_to, services.local_today()))
        except DanteError as e:
            logger.info("report records: %s", e.message)
    rows = [person(st, du, date_from, date_to, refresh) for du in people]
    return {"rows": rows, "from": date_from, "to": date_to, "generated": timezone.localtime(),
            "empty_total": sum(len(r["empty"]) for r in rows),
            "no_project_total": sum(len(r["no_project"]) for r in rows)}


def period_of(rep: WorkReport, today: datetime.date):
    if rep.period == WorkReport.MONTH:
        last = today.replace(day=1) - datetime.timedelta(days=1)
        return last.replace(day=1), last
    if rep.period == WorkReport.WEEK:
        monday = today - datetime.timedelta(days=today.weekday() + 7)
        return monday, monday + datetime.timedelta(days=6)
    return today - datetime.timedelta(days=max(1, rep.days)), today - datetime.timedelta(days=1)


def due(rep: WorkReport, now=None) -> bool:
    now = timezone.localtime(now) if now else timezone.localtime()
    if not rep.enabled or now.hour < rep.hour:
        return False
    if rep.last_sent_at and timezone.localtime(rep.last_sent_at).date() >= now.date():
        return False
    if rep.frequency == WorkReport.WEEKLY:
        return now.weekday() == rep.weekday
    if rep.frequency == WorkReport.MONTHLY:
        return now.day == min(max(1, rep.month_day), 28)
    return True


def people_of(rep: WorkReport):
    chosen = list(rep.people.all())
    return chosen or list(DanteUser.objects.filter(archived=False))


def addresses(text: str) -> list[str]:
    return [a for a in re.split(r"[\s,;]+", text or "") if "@" in a]


def render(report: dict, title: str, show_types: bool) -> tuple[str, str]:
    """(html, text) листа."""
    ctx = {**report, "title": title, "show_types": show_types}
    html = render_to_string("admin/worktime/report_mail.html", ctx)
    lines = [f"{title} · {report['from']:%d.%m.%Y}–{report['to']:%d.%m.%Y}", ""]
    for r in report["rows"]:
        lines.append(f"{r['du']}: заповнено {r['filled']} з {r['workdays']} робочих днів, відпрацьовано {r['worked']}"
                     f", на проєкти {r['projects_hm']}")
        if r["empty"]:
            lines.append(f"  порожні дні: {r['empty_text']}")
        if r["no_project"]:
            lines.append(f"  без часу на проєкти: {r['no_project_text']}")
        if r["absent_days"]:
            lines.append("  відсутності: " + (", ".join(f"{k} {n}" for k, n in r["absence_kinds"]) if show_types
                                              else f"{r['absent_days']} дн."))
    return html, "\n".join(lines)


def deliver(sender, to: list[str], subject: str, text: str, html: str) -> str:
    """'' — надіслано; інакше текст помилки. Скринька Minerva (SMTP акаунта) або SMTP сповіщень."""
    if not to:
        return "немає адрес"
    if sender is not None and sender.is_active and sender.smtp_host:
        from email_assistant.smtp_client import SMTPClient
        r = SMTPClient(sender).send(to, subject, text, html)
        return "" if r.get("ok") else r.get("error") or "помилка SMTP"
    from django.core.mail import EmailMultiAlternatives, get_connection
    from config.models import NotificationSettings
    ns = NotificationSettings.get()
    if not (ns.email_host and (ns.email_from or ns.email_host_user)):
        return "не задано SMTP: виберіть скриньку Minerva у звіті або заповніть Налаштування → Сповіщення"
    conn = get_connection(backend=getattr(settings, "WORKTIME_MAIL_BACKEND", "")
                          or "django.core.mail.backends.smtp.EmailBackend",
                          host=ns.email_host, port=ns.email_port, username=ns.email_host_user,
                          password=ns.email_host_password, use_ssl=bool(ns.email_use_ssl),
                          use_tls=bool(ns.email_use_tls) and not ns.email_use_ssl, timeout=20)
    msg = EmailMultiAlternatives(subject, text, ns.email_from or ns.email_host_user, to, connection=conn)
    msg.attach_alternative(html, "text/html")
    try:
        msg.send()
    except Exception as e:  # noqa: BLE001 — показати людині, не впасти
        return str(e)[:200]
    return ""


def send(rep: WorkReport, today=None, extra_to: list[str] | None = None) -> str:
    """Надіслати звіт: повний — адресатам, кожному працівнику (з «Email для звіту») — його частину."""
    today = today or services.local_today()
    date_from, date_to = period_of(rep, today)
    people = people_of(rep)
    report = build(people, date_from, date_to)
    subject = f"{rep.name} · {date_from:%d.%m}–{date_to:%d.%m.%Y}"
    to = addresses(rep.send_to) + list(extra_to or [])
    results = []
    if to:
        html, text = render(report, rep.name, rep.show_absence_types)
        err = deliver(rep.sender, to, subject, text, html)
        results.append(f"повний: {err or 'надіслано'} ({len(to)})")
    if rep.send_to_employees:
        sent = failed = 0
        for row in report["rows"]:
            addr = addresses(row["du"].report_email)
            if not addr:
                continue
            html, text = render({**report, "rows": [row]}, f"{rep.name} — {row['du']}", True)
            err = deliver(rep.sender, addr, subject, text, html)
            sent, failed = sent + (not err), failed + bool(err)
        results.append(f"працівникам: {sent}" + (f", помилок {failed}" if failed else ""))
    result = " · ".join(results) or "немає кому надсилати: заповніть «Кому» або «Email для звіту» працівників"
    rep.last_sent_at, rep.last_result = timezone.now(), result[:255]
    rep.save(update_fields=["last_sent_at", "last_result"])
    return result


def month_range(value: str, today: datetime.date):
    try:
        y, m = map(int, value.split("-"))
        first = datetime.date(y, m, 1)
    except (ValueError, AttributeError):
        first = today.replace(day=1)
    return first, first.replace(day=pycal.monthrange(first.year, first.month)[1])
