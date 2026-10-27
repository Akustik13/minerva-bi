"""Шар «⏱ Робочий час» у календарі Minerva (/calendar/): прихід/відхід, відпустки й лікарняні, час на проєкти,
зустрічі дня — і швидкий запис в askDANTE. Дані — з копії в Minerva (кеш днів і записів), askDANTE читається лише
кнопкою «Оновити» (refresh), щоб календар не чекав askDANTE на кожному відкритті."""
from __future__ import annotations

import calendar as pycal
import datetime

from django.urls import reverse

from . import services
from .client import DanteError
from .models import DanteActivity, DanteSettings, DanteUser


def _hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def layer(user, year: int, month: int, refresh: bool = False) -> dict | None:
    """None — askDANTE не налаштовано або користувач не прив'язаний до працівника askDANTE."""
    st = DanteSettings.get()
    if not st.configured:
        return None
    du = DanteUser.objects.filter(user=user).first()
    if du is None:
        return None
    first = datetime.date(year, month, 1)
    last = first.replace(day=pycal.monthrange(year, month)[1])
    today = services.local_today()
    error = ""
    if refresh and st.enabled and first <= today:
        try:
            services.refresh_days(du, first, min(last, today))
            services.sync_records(st, first, min(last, today))
        except DanteError as e:
            error = e.message
    days = services.cached_days(du, first, last)
    records = services.day_records(du, first, last)
    meetings = _meetings(user, first, last)
    out = {}
    day = first
    while day <= last:
        wd = days.get(day)
        times = ", ".join(f"{t['coming']}–{t.get('leaving') or '…'}" for t in (wd.times if wd else []) or [])
        blocker = services.day_blocker(st, wd, day) if not times else ""
        recs = records.get(day, [])
        out[day.day] = {
            "date": day.isoformat(), "times": times, "worked": _hm(wd.minutes) if wd and wd.minutes else "",
            "absences": services.absences_text(wd) or (blocker if blocker not in ("", "вихідний") else ""),
            "empty": bool(wd and wd.fetched_at and not times and not blocker and day < today),
            "projects": [{"name": str(r.dante_project or r.project_name), "dur": _hm(r.duration),
                          "note": " · ".join(x for x in (r.activity_name, r.description) if x)} for r in recs],
            "project_total": _hm(sum(r.duration for r in recs)) if recs else "",
            "meetings": meetings.get(day, []),
        }
        day += datetime.timedelta(days=1)
    fetched = [wd.fetched_at for wd in days.values() if wd.fetched_at]
    return {"du": du, "days": out, "enabled": st.enabled, "error": error,
            "fetched": max(fetched) if fetched else None, "today": today.isoformat(),
            "my_projects": list(services.my_projects(user)),
            "activities": list(DanteActivity.objects.filter(archived=False)),
            "default_activity": st.default_activity_id, "absence_url": st.base_url,
            "post_url": reverse("admin:worktime_workday_changelist"),
            "mapping_url": reverse("admin:worktime_danteproject_changelist"),
            "start": services.hhmm(du.times(st)[0]), "stop": services.hhmm(du.times(st)[1])}


def _meetings(user, first, last) -> dict:
    """Зустрічі користувача по днях: назва й тривалість — щоб одним кліком записати їх як час на проєкт."""
    try:
        from calendar_app.models import CalendarEvent
    except ImportError:
        return {}
    out = {}
    for ev in CalendarEvent.objects.filter(user=user, event_type=CalendarEvent.TYPE_MEETING,
                                           start_at__date__range=(first, last)).order_by("start_at"):
        from django.utils import timezone
        start = timezone.localtime(ev.start_at)
        minutes = int((ev.end_at - ev.start_at).total_seconds() // 60) if ev.end_at and ev.end_at > ev.start_at else 60
        out.setdefault(start.date(), []).append({"title": ev.title[:120], "time": f"{start:%H:%M}",
                                                 "minutes": min(minutes, 12 * 60), "dur": _hm(min(minutes, 720))})
    return out
