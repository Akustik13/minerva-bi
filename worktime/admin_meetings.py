"""🤝 Зустрічі → askDANTE за період: перегляд з підказаним проєктом, вибір і запис (логіка — meetings.py)."""
import datetime

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from . import meetings, services
from .client import DanteError
from .models import DanteActivity, DanteProject, DanteSettings


def _day(value, default):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _period(g, today):
    preset = g.get("preset", "")
    first = today.replace(day=1)
    if preset == "last_month":
        last = first - datetime.timedelta(days=1)
        return last.replace(day=1), last
    if preset == "this_month":
        return first, today
    date_to = min(_day(g.get("to"), today), today)
    date_from = _day(g.get("from"), date_to - datetime.timedelta(days=30))
    date_from = min(date_from, date_to)
    if (date_to - date_from).days >= meetings.MAX_DAYS:
        date_from = date_to - datetime.timedelta(days=meetings.MAX_DAYS - 1)
    return date_from, date_to


def meetings_page(model_admin, request):
    from .admin import WEEKDAYS, WorkDayAdmin
    st = DanteSettings.get()
    people = list(services.users_for(request.user))
    du = WorkDayAdmin._pick(request, people)
    today = services.local_today()
    g = request.POST if request.method == "POST" else request.GET
    date_from, date_to = _period(g, today)
    back = f"{reverse('admin:worktime_workday_meetings')}?u={du.pk if du else ''}&from={date_from}&to={date_to}"

    if request.method == "POST" and du is not None:
        if not (st.enabled and st.configured):
            messages.error(request, "askDANTE вимкнено або не налаштовано.")
            return redirect(back)
        picks = {k: request.POST.get(f"p_{k}") for k in request.POST.getlist("pick")}
        picks = {k: int(v) for k, v in picks.items() if v and v.isdigit()}
        activity = DanteActivity.objects.filter(pk=request.POST.get("activity") or 0).first()
        result = meetings.book(du, picks, date_from, date_to, user=request.user, activity=activity, st=st)
        if result["written"]:
            messages.success(request, f"Записано в askDANTE зустрічей: {result['written']} ({_hm(result['minutes'])}).")
        for e in result["errors"][:10]:
            messages.error(request, e)
        if result["left"]:
            messages.warning(request, f"За раз записується до {meetings.MAX_PER_SUBMIT}; ще {result['left']} — "
                                      "натисніть «Записати» ще раз.")
        return redirect(back)

    error = ""
    rows = []
    if du is not None:
        if st.enabled and st.configured:
            try:   # свіжа копія записів askDANTE: «уже записано» й перекриття
                services.sync_records(st, date_from, date_to)
            except DanteError as e:
                error = f"Записи askDANTE не оновлено ({e.message}) — «уже записано» може бути неточним."
        rows = meetings.plan(du, date_from, date_to)
        for r in rows:
            r["wd"] = WEEKDAYS[r["day"].weekday()]
            r["dur"] = _hm(r["minutes"])
    mine = list(services.my_projects(request.user))
    others = list(DanteProject.objects.filter(archived=False, gone=False, time_trackable=True)
                  .exclude(pk__in=[p.pk for p in mine]).order_by("name"))
    ready = [r for r in rows if r["state"] == "ready"]
    context = {
        **model_admin.admin_site.each_context(request), "title": "🤝 Зустрічі → askDANTE", "st": st, "du": du,
        "people": people, "date_from": date_from, "date_to": date_to, "rows": rows, "error": error,
        "mine": mine, "others": others, "activities": list(DanteActivity.objects.filter(archived=False)),
        "n_ready": len(ready), "n_checked": sum(1 for r in ready if r["checked"]),
        "ready_dur": _hm(sum(r["minutes"] for r in ready if r["checked"])),
        "calendar_url": reverse("admin:worktime_workday_changelist"),
        "mapping_url": reverse("admin:worktime_danteproject_changelist"),
        "max_per_submit": meetings.MAX_PER_SUBMIT,
        "needs_activity": st.activity_required and not st.default_activity_id,
        "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
    }
    return render(request, "admin/worktime/meetings.html", context)
