"""Календар без сміття: «🙅 Не потрібен», «🚫 Більше не заносити від …», «🧹 Почистити календар». Логіка — blocks.py."""
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from calendar_app import blocks


def _source_message(request, data):
    """Лист, з якого подія / запрошення / термін (лише свої), або None."""
    from calendar_app.models import CalendarEvent, FoundDeadline, MeetingInvite
    from email_assistant.models import EmailMessage
    user = request.user
    for name, model, field in (('event', CalendarEvent, 'email_message'), ('invite', MeetingInvite, 'email_message'),
                               ('deadline', FoundDeadline, 'email_message')):
        pk = str(data.get(name) or '')
        if pk.isdigit():
            obj = get_object_or_404(model, pk=int(pk), user=user)
            return getattr(obj, field)
    pk = str(data.get('message') or '')
    if pk.isdigit():
        return get_object_or_404(EmailMessage, pk=int(pk), account__user=user)
    return None


def sender_info(user, message) -> dict:
    """Для вікна події: від кого лист, чи вже заблоковано (адреса / домен)."""
    if message is None:
        return {}
    address = blocks.counterpart(message)
    if not address:
        return {}
    return {'address': address, 'domain': address.split('@', 1)[1], 'name': message.from_name or '',
            'blocked': blocks.blocked_key(user, address), 'spam': bool(message.is_spam)}


def _key(request, data):
    """Ключ блоку з параметрів: key=… вручну або (подія / лист…, scope=sender|domain)."""
    if data.get('key'):
        return blocks.normalize_key(data['key']), None
    message = _source_message(request, data)
    address = blocks.counterpart(message) if message else ''
    return blocks.key_for(address, data.get('scope') or blocks.SENDER), message


@staff_member_required
@require_GET
def block_preview(request):
    """Що прибереться з календаря, якщо більше не заносити від цього відправника (без змін)."""
    key, _ = _key(request, request.GET)
    if not key:
        return JsonResponse({'ok': False, 'error': 'Не видно, від кого лист'}, status=400)
    return JsonResponse({'ok': True, **blocks.preview(request.user, key)})


@staff_member_required
@require_POST
def block_create(request):
    """🚫 Більше не заносити від відправника (і прибрати внесене). reason=spam — ще й лист у Спам, як у пошті."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = request.POST
    key, message = _key(request, data)
    if not key:
        return JsonResponse({'ok': False, 'error': 'Не видно, від кого лист'}, status=400)
    if data.get('reason') == blocks.SPAM and message is not None:
        from email_assistant import spam
        result = spam.mark(message.account, [message])
        blocks.block(request.user, key, blocks.SPAM)   # домен вибрали в календарі — і його
        return JsonResponse({'ok': True, 'key': key, 'removed': result['calendar'], 'spam': True,
                             'more': result['more']})
    result = blocks.block(request.user, key, blocks.NOT_NEEDED)
    return JsonResponse({'ok': True, 'key': key, 'removed': result['removed'], 'spam': False})


@staff_member_required
@require_POST
def event_not_needed(request, pk):
    """🙅 Не потрібен: прибрати лише цю подію (запрошення / знайдений термін, з якого вона, не повернуться)."""
    from calendar_app.models import CalendarEvent
    ev = get_object_or_404(CalendarEvent, pk=pk, user=request.user)
    return JsonResponse({'ok': True, **blocks.not_needed(ev)})


def _ids(post, name):
    return [int(x) for x in post.getlist(name) if str(x).isdigit()]


@staff_member_required
def cleanup_view(request):
    """🧹 Почистити календар: що підпадає під фільтри (спам і «не заносити від …») → перегляд → підтвердження."""
    from calendar_app.models import CalendarSenderBlock
    user = request.user
    done = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'unblock':
            blocks.unblock(user, request.POST.get('block'))
            return redirect('calendar_app:cleanup')
        if action == 'clean':
            for key in request.POST.getlist('block_new'):
                key = blocks.normalize_key(key)
                if key:
                    blocks.block(user, key, blocks.NOT_NEEDED, do_purge=False)
            done = blocks.cleanup(user, _ids(request.POST, 'ev'), _ids(request.POST, 'inv'),
                                  _ids(request.POST, 'dl'))
    extra = blocks.normalize_key(request.GET.get('add', ''))
    add_error = bool(request.GET.get('add')) and not extra
    groups = blocks.cleanup_plan(user, extra)
    return render(request, 'calendar_app/cleanup.html', {
        'title': '🧹 Почистити календар', 'groups': groups, 'extra': extra, 'add_error': add_error,
        'total': sum(g['count'] for g in groups), 'done': done,
        'filters': CalendarSenderBlock.objects.filter(user=user),
        'is_nav_sidebar_enabled': True,
    })
