"""«👥 Учасники й запрошення» у вікні події календаря: запросити людей на зустріч зі своєї скриньки
(iCalendar REQUEST / CANCEL), бачити їхні відповіді. Логіка — calendar_app/outgoing_invites.py."""
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from . import outgoing_invites as oi
from .models import CalendarEvent, OutgoingInvite


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


@staff_member_required
def event_invite(request, pk):
    """GET — стан вікна (і нові відповіді з уже отриманих листів); POST {action: save | preview | send | cancel |
    check, title, start, end, all_day, attendees, location, join_url, note, account, lang, resend, expect}.
    pk 0 — нова зустріч. Лише своя подія; лист — лише зі своєї скриньки."""
    event = None if pk == 0 else get_object_or_404(CalendarEvent, pk=pk, user=request.user)
    if request.method == 'GET':
        inv = oi.for_event(event) if event is not None else None
        if inv is not None and inv.status == OutgoingInvite.SENT:
            oi.check_replies(inv)
        return JsonResponse({'ok': True, 'state': oi.state(request.user, event)})
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST'}, status=405, headers={'Allow': 'GET, POST'})
    data = _body(request)
    action = data.get('action', '')
    out = {}
    try:
        if action == 'save':
            with transaction.atomic():
                event = oi.apply(request.user, event, data).event
        elif action == 'preview':
            out['plan'] = oi.preview(request.user, event, data)
        elif action == 'send':
            if event is None:   # нова зустріч: спершу в календар, щоб повтор після збою не створив другу
                with transaction.atomic():
                    event = oi.apply(request.user, None, data).event
            out = oi.send(request.user, event, data)
        elif action in ('cancel', 'check'):
            inv = oi.for_event(event) if event is not None else None
            if inv is None:
                raise oi.InviteError('запрошення ще немає')
            if action == 'cancel':
                out = oi.cancel(request.user, inv)
                event.refresh_from_db()
            else:
                out['found'] = oi.check_replies(inv)
        else:
            return JsonResponse({'ok': False, 'error': 'Невідома дія'}, status=400)
    except oi.InviteError as e:
        return JsonResponse({'ok': False, 'error': str(e), 'state': oi.state(request.user, event)})
    out.pop('ok', None)
    return JsonResponse({'ok': True, **out, 'state': oi.state(request.user, event)})


@staff_member_required
@require_POST
def outgoing_cancel(request, pk, action):
    """Подію видалено, а «скасовано» учасникам не пішло: retry — надіслати ще раз, drop — не надсилати."""
    inv = get_object_or_404(OutgoingInvite, pk=pk, user=request.user, cancel_pending=True)
    try:
        if action == 'retry':
            out = oi.cancel(request.user, inv, deleted=inv.event_id is None)
            return JsonResponse({'ok': True, 'to': out['to']})
        if action == 'drop':
            oi.drop_cancel(request.user, inv)
            return JsonResponse({'ok': True})
    except oi.InviteError as e:
        return JsonResponse({'ok': False, 'error': str(e)})
    return JsonResponse({'ok': False, 'error': 'Невідома дія'}, status=400)
