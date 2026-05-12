from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET


@staff_member_required
def calendar_view(request):
    from calendar_app.models import CalendarEvent
    import calendar as _cal

    now   = timezone.now()
    year  = int(request.GET.get('year',  now.year))
    month = int(request.GET.get('month', now.month))

    # Clamp
    year  = max(2020, min(2035, year))
    month = max(1, min(12, month))

    # Navigation
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    events = (CalendarEvent.objects
              .filter(user=request.user, start_at__year=year,
                      start_at__month=month, is_done=False)
              .select_related('crm_customer')
              .order_by('start_at'))

    # Build calendar grid (pre-processed so template needs no custom filters)
    cal  = _cal.Calendar(firstweekday=0)  # Monday first
    raw_weeks = cal.monthdayscalendar(year, month)
    month_name = _cal.month_name[month]
    today_day = now.day if now.year == year and now.month == month else None

    events_by_day: dict = {}
    for ev in events:
        events_by_day.setdefault(ev.start_at.day, []).append(ev)

    weeks_data = []
    for week in raw_weeks:
        week_cells = []
        for day in week:
            week_cells.append({
                'day':      day,
                'events':   events_by_day.get(day, []) if day else [],
                'is_today': day == today_day,
                'other':    day == 0,
            })
        weeks_data.append(week_cells)

    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']

    return render(request, 'calendar_app/calendar.html', {
        'title':      f'Календар — {month_name} {year}',
        'year':       year,
        'month':      month,
        'month_name': month_name,
        'weeks_data': weeks_data,
        'weekdays':   weekdays,
        'events':     events,
        'prev_year':  prev_year,
        'prev_month': prev_month,
        'next_year':  next_year,
        'next_month': next_month,
        'is_nav_sidebar_enabled': True,
    })


@staff_member_required
def events_json(request):
    from calendar_app.models import CalendarEvent

    events = (CalendarEvent.objects
              .filter(user=request.user, is_done=False)
              .select_related('crm_customer')
              .order_by('start_at')[:200])

    return JsonResponse({'events': [
        {
            'id':       e.pk,
            'title':    e.title,
            'start':    e.start_at.isoformat(),
            'end':      e.end_at.isoformat() if e.end_at else None,
            'all_day':  e.all_day,
            'type':     e.event_type,
            'customer': str(e.crm_customer) if e.crm_customer else None,
        }
        for e in events
    ]})


@staff_member_required
@require_POST
def event_done(request, pk):
    from calendar_app.models import CalendarEvent
    event = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
    event.is_done = True
    event.save(update_fields=['is_done'])
    return JsonResponse({'ok': True})


@staff_member_required
@require_GET
def pending_push_view(request):
    """Return events whose push reminder is due; mark them push_sent=True."""
    from datetime import timedelta
    from calendar_app.models import CalendarEvent, CalendarSettings

    cfg = CalendarSettings.for_user(request.user)
    if not cfg.notify_push:
        return JsonResponse({'events': []})

    now = timezone.now()
    candidates = (CalendarEvent.objects
                  .filter(user=request.user, is_done=False, push_sent=False)
                  .only('pk', 'title', 'event_type', 'start_at', 'remind_minutes_before'))

    due = []
    pks = []
    for ev in candidates:
        if ev.start_at - timedelta(minutes=ev.remind_minutes_before) <= now:
            due.append({
                'id':    ev.pk,
                'title': ev.title,
                'type':  ev.event_type,
                'start': ev.start_at.strftime('%d.%m.%Y %H:%M'),
            })
            pks.append(ev.pk)

    if pks:
        CalendarEvent.objects.filter(pk__in=pks).update(push_sent=True)

    return JsonResponse({'events': due})


@staff_member_required
def settings_view(request):
    from calendar_app.models import CalendarSettings

    cfg = CalendarSettings.for_user(request.user)
    saved = False

    if request.method == 'POST':
        cfg.notify_telegram = request.POST.get('notify_telegram') == '1'
        cfg.notify_email    = request.POST.get('notify_email') == '1'
        cfg.notify_push     = request.POST.get('notify_push') == '1'
        try:
            mins = int(request.POST.get('default_remind_minutes', 60))
            cfg.default_remind_minutes = max(1, min(10080, mins))
        except (ValueError, TypeError):
            pass
        cfg.email_to         = request.POST.get('email_to', '').strip()
        cfg.telegram_chat_id = request.POST.get('telegram_chat_id', '').strip()
        cfg.save()
        saved = True

    return render(request, 'calendar_app/cal_settings.html', {
        'title': 'Налаштування сповіщень календаря',
        'cfg':   cfg,
        'saved': saved,
        'is_nav_sidebar_enabled': True,
    })
