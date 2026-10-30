"""📅 Сторінка дня робочого часу: усе за день в одному місці — прихід / відхід askDANTE (з підказкою «👣 за
активністю в Minerva»), записи на проєкти (✍️ опис AI, заміна, новий — спільне вікно `_record_modal.html`), пошта
дня (✉️), зустрічі (🤝) і що людина робила в Minerva (👣). Нової логіки немає — лише наявні сервіси."""
import datetime

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from . import mail_time, meetings, services
from .client import DanteError, hhmm
from .models import DanteActivity, DanteProject, DanteSettings, TimeRecord


def _day(value, default):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def day_page(model_admin, request):
    from .admin import WEEKDAYS, WorkDayAdmin, _time
    st = DanteSettings.get()
    people = list(services.users_for(request.user))
    du = WorkDayAdmin._pick(request, people)
    today = services.local_today()
    g = request.POST if request.method == "POST" else request.GET
    day = _day(g.get("d"), today)
    url = reverse("admin:worktime_workday_day")
    back = f"{url}?u={du.pk if du else ''}&d={day:%Y-%m-%d}"

    if request.method == "POST" and du is not None:   # ⏱ прихід і відхід за цей день
        try:
            start, stop = _time(request.POST.get("start")), _time(request.POST.get("stop"))
            if not (start and stop):
                raise DanteError("bad_request", "Вкажіть прихід і відхід.")
            b = services.book_day(du, day, start, stop, user=request.user, st=st)
            WorkDayAdmin._report_booking(request, b)
        except DanteError as e:
            messages.error(request, f"❌ {e.message}")
        return redirect(back)

    error = ""
    wd, plan_row, records, mail_rows, unmapped, meet_rows, log = None, None, [], [], 0, [], []
    mail_private = False
    if du is not None:
        if st.enabled and st.configured and day <= today:
            try:   # свіжий день з askDANTE: прихід / відхід, відсутності, записи на проєкти
                days = services.refresh_days(du, day, day)
                services.sync_records(st, day, day)
                plan_row = services.propose(st, du, days, day, day, DanteSettings.MODE_ACTIVITY, fallback=False)[0]
            except DanteError as e:
                error = f"askDANTE: {e.message} Показано останні збережені дані."
        wd = services.cached_days(du, day, day).get(day)
        records = list(TimeRecord.objects.filter(dante_user=du, day=day).select_related("dante_project")
                       .order_by("start", "pk"))
        info = mail_time.estimate(du, day, day, st).get(day, {})
        mail_private = not mail_time.mail_owner(du, request.user)
        if mail_private and info:   # чужі листи — лише кількості й хвилини
            mail_time.hide_mail(info, st.comment_lang)
        mail_rows, unmapped = info.get("rows", []), info.get("unmapped", 0)
        for r in mail_rows:
            r["dur"], r["todo_dur"] = _hm(r["minutes"]), _hm(r["to_book"])
        meet_rows = meetings.plan(du, day, day) if day <= today else []
        for r in meet_rows:
            r["dur"] = _hm(r["minutes"])
        log = services.activity_log(du.user, day, viewer=request.user) if du.user_id else []
    span = services.activity_span(du.user, day) if du and du.user_id else None
    times = (wd.times if wd else None) or []
    def_start, def_stop = du.times(st) if du else (st.default_start, st.default_stop)
    from .admin import WorkDayAdmin as W
    context = {
        **model_admin.admin_site.each_context(request), "title": f"📅 {WEEKDAYS[day.weekday()]} {day:%d.%m.%Y}",
        "st": st, "du": du, "people": people, "day": day, "wd_name": WEEKDAYS[day.weekday()], "today": today,
        "prev": day - datetime.timedelta(days=1), "next": day + datetime.timedelta(days=1), "error": error,
        "times": times, "day_minutes": _hm(wd.minutes) if wd else "", "target": _hm(wd.target_minutes) if wd and
        wd.target_minutes else "", "absences": services.absences_text(wd), "blocker": services.day_blocker(st, wd, day),
        "plan": plan_row, "span": span, "def_start": hhmm(def_start), "def_stop": hhmm(def_stop),
        "open_stamps": services.open_stamps(st, du, [wd]) if du and wd else [], "dante_url": st.base_url,
        "open_since": timezone.localtime(du.open_since) if du and du.open_since
        and timezone.localtime(du.open_since).date() == day else None,
        "auto_out": services.clock_out_at(du, st) if du else None,
        "records": [{"r": r, "dur": _hm(r.duration)} for r in records],
        "mail_rows": mail_rows, "unmapped": unmapped, "meet_rows": meet_rows, "log": log,
        "mail_private": mail_private, "mail_private_text": mail_time.MAIL_PRIVATE,
        "cal_url": f"{reverse('admin:worktime_workday_changelist')}?u={du.pk if du else ''}&m={day:%Y-%m}",
        "mail_url": f"{reverse('admin:worktime_workday_mailtime')}?u={du.pk if du else ''}&from={day}&to={day}",
        "meetings_url": f"{reverse('admin:worktime_workday_meetings')}?u={du.pk if du else ''}&from={day}&to={day}",
        # спільне вікно запису (_record_modal.html)
        "cur_m": f"{day:%Y-%m}", "rec_scope": f"{day:%Y-%m-%d}", "rec_next": back,
        "rec_data": W._rec_data({day: records}),
        "my_projects": list(services.my_projects(request.user)),
        "all_projects": list(DanteProject.objects.filter(archived=False, gone=False, time_trackable=True)
                             .select_related("parent").order_by("name")),
        "activities": list(DanteActivity.objects.filter(archived=False)),
        "default_activity": st.default_activity_id,
    }
    return render(request, "admin/worktime/day.html", context)
