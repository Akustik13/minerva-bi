import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

EVENT_COLORS = {
    'deadline':      '#e53935',
    'meeting':       '#1a73e8',
    'reminder':      '#f5a623',
    'email_follow_up': '#43a047',
    'other':         '#607d8b',
}

EVENT_TYPE_LABELS = {
    'deadline':        '⏰ Дедлайн',
    'meeting':         '🤝 Зустріч',
    'reminder':        '🔔 Нагадування',
    'email_follow_up': '📧 Email follow-up',
    'other':           '📌 Інше',
}


def _event_display(ev, custom_cats_map):
    """Attach display_color / display_type_key / display_label to an event object."""
    if ev.custom_category_id and ev.custom_category_id in custom_cats_map:
        cat = custom_cats_map[ev.custom_category_id]
        ev.display_color    = cat.color
        ev.display_type_key = f'cat_{cat.pk}'
        ev.display_label    = f'{cat.emoji} {cat.name}'
    else:
        ev.display_color    = EVENT_COLORS.get(ev.event_type, '#607d8b')
        ev.display_type_key = ev.event_type
        ev.display_label    = EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type)


@staff_member_required
def calendar_view(request):
    from calendar_app.models import CalendarEvent, CalendarCategory
    import calendar as _cal

    now   = timezone.now()
    year  = int(request.GET.get('year',  now.year))
    month = int(request.GET.get('month', now.month))

    year  = max(2020, min(2035, year))
    month = max(1, min(12, month))

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    show_done = request.GET.get('show_done') == '1'
    qs = CalendarEvent.objects.filter(user=request.user, start_at__year=year, start_at__month=month)
    if not show_done:
        qs = qs.filter(is_done=False)
    events = list(qs.select_related('crm_customer', 'custom_category').order_by('start_at'))

    custom_cats     = list(CalendarCategory.objects.filter(user=request.user).values('id', 'name', 'color', 'emoji'))
    custom_cats_map = {cat['id']: type('C', (), cat)() for cat in custom_cats}

    for ev in events:
        _event_display(ev, {cat['id']: type('C', (), cat)() for cat in custom_cats})

    cal        = _cal.Calendar(firstweekday=0)
    raw_weeks  = cal.monthdayscalendar(year, month)
    month_name = _cal.month_name[month]
    today_day  = now.day if now.year == year and now.month == month else None

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
        'title':              f'Календар — {month_name} {year}',
        'year':               year,
        'month':              month,
        'month_name':         month_name,
        'weeks_data':         weeks_data,
        'weekdays':           weekdays,
        'events':             events,
        'prev_year':          prev_year,
        'prev_month':         prev_month,
        'next_year':          next_year,
        'next_month':         next_month,
        'show_done':          show_done,
        'event_colors_json':  json.dumps(EVENT_COLORS),
        'event_labels_json':  json.dumps(EVENT_TYPE_LABELS),
        'custom_cats_json':   json.dumps(custom_cats),
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
@require_GET
def event_detail_api(request, pk):
    from calendar_app.models import CalendarEvent
    ev = get_object_or_404(CalendarEvent, pk=pk, user=request.user)

    if ev.custom_category_id:
        cat = ev.custom_category
        type_key   = f'cat_{cat.pk}'
        type_label = f'{cat.emoji} {cat.name}'
        color      = cat.color
    else:
        type_key   = ev.event_type
        type_label = EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type)
        color      = EVENT_COLORS.get(ev.event_type, '#607d8b')

    source_email = None
    if ev.email_message_id:
        source_email = {
            'pk':      ev.email_message_id,
            'subject': ev.email_message.subject if ev.email_message else '',
            'url':     f'/email/message/{ev.email_message_id}/',
        }

    return JsonResponse({
        'id':           ev.pk,
        'title':        ev.title,
        'description':  ev.description,
        'event_type':   ev.event_type,
        'type_key':     type_key,
        'type_label':   type_label,
        'color':        color,
        'start':        ev.start_at.strftime('%d.%m.%Y %H:%M'),
        'end':          ev.end_at.strftime('%d.%m.%Y %H:%M') if ev.end_at else '',
        'all_day':      ev.all_day,
        'is_done':      ev.is_done,
        'customer':     str(ev.crm_customer) if ev.crm_customer_id else '',
        'source_email': source_email,
        'custom_cat_id': ev.custom_category_id,
    })


@staff_member_required
@require_POST
def event_toggle_done(request, pk):
    from calendar_app.models import CalendarEvent
    ev = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
    ev.is_done = not ev.is_done
    ev.save(update_fields=['is_done'])
    return JsonResponse({'ok': True, 'is_done': ev.is_done})


@staff_member_required
@require_POST
def event_done(request, pk):
    from calendar_app.models import CalendarEvent
    event = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
    event.is_done = True
    event.save(update_fields=['is_done'])
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def event_set_type(request, pk):
    from calendar_app.models import CalendarEvent, CalendarCategory
    ev = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False}, status=400)

    new_type = data.get('event_type', '')
    if new_type.startswith('cat_'):
        try:
            cat_id = int(new_type[4:])
            cat = get_object_or_404(CalendarCategory, pk=cat_id, user=request.user)
            ev.custom_category = cat
            ev.save(update_fields=['custom_category'])
            return JsonResponse({
                'ok':        True,
                'type_key':  new_type,
                'color':     cat.color,
                'type_label': f'{cat.emoji} {cat.name}',
            })
        except (ValueError, Exception):
            return JsonResponse({'ok': False, 'error': 'invalid category'}, status=400)

    if new_type not in EVENT_COLORS:
        return JsonResponse({'ok': False, 'error': 'invalid type'}, status=400)
    ev.event_type = new_type
    ev.custom_category = None
    ev.save(update_fields=['event_type', 'custom_category'])
    return JsonResponse({
        'ok':        True,
        'type_key':  new_type,
        'color':     EVENT_COLORS[new_type],
        'type_label': EVENT_TYPE_LABELS.get(new_type, new_type),
    })


# ── Custom category CRUD ─────────────────────────────────────

@staff_member_required
@require_GET
def category_list_api(request):
    from calendar_app.models import CalendarCategory
    cats = list(CalendarCategory.objects.filter(user=request.user).values('id', 'name', 'color', 'emoji'))
    return JsonResponse({'categories': cats})


@staff_member_required
@require_POST
def category_create_api(request):
    from calendar_app.models import CalendarCategory
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False}, status=400)
    name  = data.get('name', '').strip()[:100]
    color = data.get('color', '#607d8b')
    emoji = data.get('emoji', '📌')[:10]
    if not name:
        return JsonResponse({'ok': False, 'error': 'name required'}, status=400)
    cat, created = CalendarCategory.objects.get_or_create(
        user=request.user, name=name, defaults={'color': color, 'emoji': emoji})
    return JsonResponse({'ok': True, 'id': cat.pk, 'name': cat.name,
                         'color': cat.color, 'emoji': cat.emoji, 'created': created})


@staff_member_required
@require_POST
def category_delete_api(request, cat_pk):
    from calendar_app.models import CalendarCategory
    cat = get_object_or_404(CalendarCategory, pk=cat_pk, user=request.user)
    cat.delete()
    return JsonResponse({'ok': True})


# ── Bulk actions ─────────────────────────────────────────────

@staff_member_required
@require_POST
def events_bulk_api(request):
    from calendar_app.models import CalendarEvent, CalendarCategory
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False}, status=400)

    raw_pks = data.get('pks', [])
    pks     = [int(p) for p in raw_pks if str(p).isdigit()]
    action  = data.get('action', '')
    qs      = CalendarEvent.objects.filter(pk__in=pks, user=request.user)

    if action == 'set_type':
        new_type = data.get('value', '')
        if new_type.startswith('cat_'):
            try:
                cat_id = int(new_type[4:])
                cat = CalendarCategory.objects.get(pk=cat_id, user=request.user)
                n = qs.update(custom_category=cat)
                return JsonResponse({'ok': True, 'updated': n,
                                     'color': cat.color, 'type_key': new_type,
                                     'type_label': f'{cat.emoji} {cat.name}'})
            except (CalendarCategory.DoesNotExist, ValueError):
                return JsonResponse({'ok': False, 'error': 'invalid category'}, status=400)
        elif new_type in EVENT_COLORS:
            n = qs.update(event_type=new_type, custom_category=None)
            return JsonResponse({'ok': True, 'updated': n,
                                 'color': EVENT_COLORS[new_type], 'type_key': new_type,
                                 'type_label': EVENT_TYPE_LABELS.get(new_type, new_type)})
        return JsonResponse({'ok': False, 'error': 'invalid type'}, status=400)

    elif action == 'done':
        n = qs.update(is_done=True)
        return JsonResponse({'ok': True, 'updated': n})

    elif action == 'undone':
        n = qs.update(is_done=False)
        return JsonResponse({'ok': True, 'updated': n})

    elif action == 'delete':
        n, _ = qs.delete()
        return JsonResponse({'ok': True, 'deleted': n})

    return JsonResponse({'ok': False, 'error': 'unknown action'}, status=400)


# ── Notifications ────────────────────────────────────────────

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
@require_POST
def calendar_ai_chat(request):
    from calendar_app.ai_service import calendar_chat
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)
    user_message = data.get('message', '').strip()[:2000]
    history      = data.get('history', [])
    if not user_message:
        return JsonResponse({'ok': False, 'error': 'empty message'}, status=400)
    result = calendar_chat(user_message, history, request.user)
    return JsonResponse(result)


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
