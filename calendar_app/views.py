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
        ev.display_type_key = f'cat_{cat.id}'
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

    wt = _worktime_layer(request, year, month)
    ext_by_day = _external_events(request.user, year, month)
    weeks_data = []
    for week in raw_weeks:
        week_cells = []
        for day in week:
            week_cells.append({
                'day':      day,
                'events':   events_by_day.get(day, []) if day else [],
                'is_today': day == today_day,
                'other':    day == 0,
                'wt':       wt['days'].get(day) if wt and day else None,
                'ext':      ext_by_day.get(day, []) if day else [],
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
        'wt':                 wt,
        'wt_days':            wt['days'] if wt else {},
        'invites':            _invites(request.user),
        'deadlines_pending':  _deadlines_pending(request.user),
        'is_nav_sidebar_enabled': True,
    })


def _worktime_layer(request, year, month):
    """Шар «⏱ Робочий час» askDANTE (прихід/відхід, відсутності, час на проєкти); None — не підключено."""
    try:
        from worktime.calendar_layer import layer
        return layer(request.user, year, month, refresh=request.GET.get('wt_refresh') == '1')
    except Exception:   # календар працює й без askDANTE
        import logging
        logging.getLogger(__name__).exception('worktime layer')
        return None


def _external_events(user, year, month) -> dict:
    """Події підписаних календарів (Google / Outlook через iCal) по днях місяця — лише показ."""
    from calendar_app.models import ExternalEvent
    out = {}
    from django.db.models import Q
    shown = Q(feed__user=user, feed__enabled=True) | Q(remote__account__user=user, remote__show_in_minerva=True)
    for e in (ExternalEvent.objects.filter(shown, start_at__year=year, start_at__month=month)
              .select_related("feed", "remote__account")[:1500]):
        start = timezone.localtime(e.start_at)
        src = e.feed or e.remote
        color = (e.feed.color if e.feed else e.remote.color) or "#8e24aa"
        name = e.feed.name if e.feed else f"{e.remote.account.get_provider_display()}: {e.remote.name}"
        out.setdefault(start.day, []).append({
            "title": e.title, "color": color, "feed": name if src else "",
            "time": "" if e.all_day else f"{start:%H:%M}", "location": e.location})
    return out


def _write_targets(user) -> list:
    """Календарі, куди можна заносити: підключені Google / Outlook з правом запису."""
    from calendar_app.models import RemoteCalendar
    return list(RemoteCalendar.objects.filter(account__user=user, can_write=True)
                .select_related('account').order_by('account__provider', '-primary', 'name'))


def _deadlines_pending(user):
    from calendar_app.models import FoundDeadline
    return FoundDeadline.objects.filter(user=user, status=FoundDeadline.NEW).count()


def _invites(user):
    from calendar_app import invites
    return invites.pending(user)


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
        cfg.invites_auto     = request.POST.get('invites_auto') == '1'
        from django.utils.dateparse import parse_date
        cfg.invites_since    = parse_date(request.POST.get('invites_since') or '') or cfg.invites_since
        cfg.save()
        saved = True
        if request.POST.get('invites_scan') == '1' and cfg.invites_since:
            from calendar_app import invites
            cfg.invites_scan_status = f'⏳ Листи від {cfg.invites_since:%d.%m.%Y}: запуск…'
            cfg.save(update_fields=['invites_scan_status'])
            invites.start_scan(request.user, cfg.invites_since, trash=request.POST.get('invites_trash') == '1')

    from calendar_app.models import MeetingInvite
    return render(request, 'calendar_app/cal_settings.html', {
        'title': 'Налаштування сповіщень календаря',
        'cfg':   cfg,
        'saved': saved,
        'invites_pending': MeetingInvite.objects.filter(user=request.user,
                                                        status__in=MeetingInvite.PENDING).count(),
        'is_nav_sidebar_enabled': True,
    })


@staff_member_required
@require_POST
def invite_decide(request, pk, action):
    """✓ / ✕ запрошення з пошти (лише своє)."""
    from calendar_app import invites
    from calendar_app.models import MeetingInvite
    inv = get_object_or_404(MeetingInvite, pk=pk, user=request.user)
    if action == 'accept':
        invites.accept(inv, request.user)
    elif action == 'dismiss':
        invites.dismiss(inv, request.user)
    else:
        return JsonResponse({'ok': False, 'error': 'Невідома дія'}, status=400)
    return JsonResponse({'ok': True, 'status': inv.status, 'event_id': inv.event_id})


MASK_KEYS = ('accounts', 'folder', 'date_from', 'date_to', 'sender', 'subject', 'category', 'customer', 'project',
             'trash')


@staff_member_required
def deadlines_view(request):
    """🔎 Терміни з пошти: маска → скільки листів → пошук у фоні (AI) → ✓ / ✕ знайдених термінів."""
    from calendar_app import deadlines
    from calendar_app.models import CalendarSettings, FoundDeadline
    from email_assistant.models import EmailAccount, EmailMessage
    cfg = CalendarSettings.for_user(request.user)
    src = request.POST if request.method == 'POST' else request.GET
    mask = {k: (src.getlist(k) if k == 'accounts' else src.get(k, '').strip()) for k in MASK_KEYS} \
        if any(k in src for k in MASK_KEYS) else dict(cfg.deadline_mask or {})
    mask = {k: v for k, v in mask.items() if v}
    if mask.get('customer') and not str(mask['customer']).isdigit():
        from worktime.mapping import find_customer
        c = find_customer(mask['customer'])
        mask['customer'] = str(c.pk) if c else ''
    info = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'start':
            try:
                limit = max(1, min(deadlines.MAX_PER_RUN, int(request.POST.get('limit') or 200)))
            except ValueError:
                limit = 200
            cfg.deadline_mask = mask
            cfg.deadline_scan_status = '⏳ Запуск…'
            cfg.save(update_fields=['deadline_mask', 'deadline_scan_status'])
            deadlines.start_scan(request.user, mask, limit)
        elif action == 'auto':
            cfg.deadlines_auto = request.POST.get('deadlines_auto') == '1'
            cfg.save(update_fields=['deadlines_auto'])
        elif action in ('accept', 'dismiss'):
            from django.contrib import messages as msgs
            from calendar_app.models import RemoteCalendar
            ids = [int(x) for x in request.POST.getlist('ids') if x.isdigit()]
            target = None
            if action == 'accept':   # крок 2: у який календар; останній вибір — і для «без підтвердження»
                tid = request.POST.get('target', '')
                target = RemoteCalendar.objects.filter(pk=tid, account__user=request.user, can_write=True) \
                    .select_related('account').first() if tid.isdigit() else None
                cfg.deadline_target = target
                cfg.save(update_fields=['deadline_target'])
            done, errors = 0, []
            for d in FoundDeadline.objects.filter(user=request.user, pk__in=ids, status=FoundDeadline.NEW) \
                    .select_related('email_message__thread'):
                if action == 'dismiss':
                    deadlines.dismiss(d)
                    continue
                try:
                    deadlines.accept(d, target)
                    done += 1
                except Exception as e:  # noqa: BLE001 — Google / Outlook відмовив: лишається на ✓
                    errors.append(f'{d.title}: {e}')
            if action == 'accept':
                if done:
                    msgs.success(request, f'✓ Занесено в «{target or "календар Minerva"}»: {done}.')
                for e in errors[:5]:
                    msgs.error(request, f'❌ {e}')
        from django.shortcuts import redirect
        return redirect('/calendar/deadlines/')
    if 'count' in src:
        total, cands = deadlines.candidates(request.user, mask)
        info = {'total': total, 'ai': len(cands),
                'tokens': sum(len((m.body_text or '')[:5000]) // 4 + 500 for m in cands)}
    from projects.models import Project
    found = list(FoundDeadline.objects.filter(user=request.user, status=FoundDeadline.NEW)
                 .select_related('email_message').order_by('due_at')[:300])
    today = timezone.localdate()
    for d in found:
        d.past = timezone.localtime(d.due_at).date() < today
    return render(request, 'calendar_app/deadlines.html', {
        'title': '🔎 Терміни з пошти', 'cfg': cfg, 'mask': mask, 'info': info, 'found': found,
        'accounts': EmailAccount.objects.filter(user=request.user),
        'categories': EmailMessage.CATEGORY_CHOICES,
        'projects': Project.objects.filter(status__in=Project.OPEN_STATUSES).order_by('code')[:300],
        'added': FoundDeadline.objects.filter(user=request.user, status=FoundDeadline.ADDED).count(),
        'targets': _write_targets(request.user),
        'is_nav_sidebar_enabled': True,
    })


def calendar_feed(request, token):
    """ICS календаря Minerva за секретною адресою — для підписки в Google / Outlook (без входу в Minerva)."""
    from django.http import Http404, HttpResponse
    from calendar_app import feeds
    from calendar_app.models import CalendarSettings
    if len(token) < 20:
        raise Http404
    cfg = CalendarSettings.objects.filter(feed_token=token).select_related('user').first()
    if cfg is None or not cfg.user.is_active:
        raise Http404
    resp = HttpResponse(feeds.export(cfg.user, request.get_host().split(':')[0]),
                        content_type='text/calendar; charset=utf-8')
    resp['Content-Disposition'] = 'inline; filename="minerva.ics"'
    resp['Cache-Control'] = 'private, max-age=300'
    return resp


@staff_member_required
def connections_view(request):
    """🔗 Календарі: підписка на Minerva (ICS), підписані календарі Google / Outlook, далі — двостороння синхронізація."""
    from django.shortcuts import redirect
    from calendar_app import feeds
    from calendar_app.models import CalendarFeed, CalendarSettings
    cfg = CalendarSettings.for_user(request.user)
    if request.method == 'POST':
        p = request.POST
        action = p.get('action')
        if action == 'feed_on':
            feeds.new_token(cfg)
        elif action == 'feed_off':
            cfg.feed_token = ''
            cfg.save(update_fields=['feed_token'])
        elif action == 'feed_opts':
            cfg.feed_worktime = p.get('feed_worktime') == '1'
            cfg.feed_busy_only = p.get('feed_busy_only') == '1'
            cfg.save(update_fields=['feed_worktime', 'feed_busy_only'])
        elif action == 'add_feed':
            url = (p.get('url') or '').strip()
            if url.lower().startswith(('https://', 'http://', 'webcal://')):
                feed = CalendarFeed.objects.create(user=request.user, name=(p.get('name') or 'Календар')[:100],
                                                   url=url[:1000], color=(p.get('color') or '#8e24aa')[:7])
                feeds.fetch(feed)
        elif action in ('refresh', 'delete', 'toggle'):
            feed = CalendarFeed.objects.filter(pk=p.get('feed') or 0, user=request.user).first()
            if feed and action == 'refresh':
                feeds.fetch(feed)
            elif feed and action == 'delete':
                feed.delete()
            elif feed:
                feed.enabled = not feed.enabled
                feed.save(update_fields=['enabled'])
        return redirect('/calendar/connections/')
    feed_url = request.build_absolute_uri(f'/calendar/feed/{cfg.feed_token}.ics') if cfg.feed_token else ''
    sync = _sync_context(request)
    return render(request, 'calendar_app/connections.html', {
        'title': '🔗 Календарі', 'cfg': cfg, 'feed_url': feed_url,
        'webcal_url': feed_url.replace('https://', 'webcal://').replace('http://', 'webcal://') if feed_url else '',
        'feeds': [(f, feeds.masked(f.url)) for f in CalendarFeed.objects.filter(user=request.user)],
        **sync, 'is_nav_sidebar_enabled': True,
    })


def _sync_context(request):
    """Двостороння синхронізація (рівень 2) — підключення Google / Microsoft і правила; без неї — порожньо."""
    try:
        from calendar_app import sync
        return sync.page_context(request)
    except ImportError:
        return {}


@staff_member_required
def full_view(request):
    """🗓 Повний календар у форматі Google / Outlook: день, тиждень, місяць, список; джерела — Minerva, підключені
    календарі (наживо), підписки iCal. ?only=r:<pk> — лише один календар, щоб звірити з оригіналом."""
    from calendar_app import sync
    srcs = sync.sources(request.user)
    return render(request, 'calendar_app/full.html', {
        'title': '🗓 Повний календар', 'sources': srcs, 'only': request.GET.get('only', ''),
        'is_nav_sidebar_enabled': True,
    })


@staff_member_required
@require_GET
def full_events(request):
    from django.utils.dateparse import parse_datetime
    from calendar_app import sync
    start, end = parse_datetime(request.GET.get('start', '')), parse_datetime(request.GET.get('end', ''))
    if not start or not end or end <= start or (end - start).days > 62:
        return JsonResponse({'events': [], 'error': 'неправильний період'}, status=400)
    if timezone.is_naive(start):
        start, end = timezone.make_aware(start), timezone.make_aware(end)
    events, error = sync.view_events(request.user, request.GET.get('src', ''), start, end)
    return JsonResponse({'events': events, 'error': error})
