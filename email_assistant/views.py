"""email_assistant/views.py — Email клієнт інтерфейс."""
import json
import logging
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST
from django.utils import timezone

logger = logging.getLogger('email_assistant')


def _get_account(request):
    from email_assistant.models import EmailAccount
    return (EmailAccount.objects
            .filter(user=request.user, is_active=True)
            .order_by('-is_primary').first())


def _crm_contacts() -> list:
    try:
        from crm.models import Customer
        return [
            {'email': c['email'], 'name': c['name']}
            for c in Customer.objects.exclude(email='').values('email', 'name')[:300]
        ]
    except Exception:
        return []


def _build_qs(account, folder, q=''):
    from email_assistant.models import EmailMessage, EmailThread

    if folder == 'starred':
        qs = EmailMessage.objects.filter(account=account, is_starred=True, is_deleted=False)
    elif folder == 'archived':
        archived_ids = EmailThread.objects.filter(
            account=account, is_archived=True
        ).values_list('id', flat=True)
        qs = EmailMessage.objects.filter(
            account=account, thread_id__in=archived_ids, is_deleted=False
        )
    elif folder == 'inbox':
        archived_ids = EmailThread.objects.filter(
            account=account, is_archived=True
        ).values_list('id', flat=True)
        qs = EmailMessage.objects.filter(
            account=account, folder='inbox', is_deleted=False
        ).exclude(thread_id__in=archived_ids)
    else:
        qs = EmailMessage.objects.filter(
            account=account, folder=folder, is_deleted=False
        )

    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(subject__icontains=q) |
            Q(from_email__icontains=q) |
            Q(from_name__icontains=q) |
            Q(body_text__icontains=q)
        )

    return qs.order_by('-sent_at')


FOLDERS = [
    ('inbox',    '📥 Вхідні',    'inbox'),
    ('sent',     '📤 Надіслані', 'sent'),
    ('starred',  '⭐ Важливі',   'starred'),
    ('archived', '📦 Архів',     'archived'),
    ('trash',    '🗑️ Кошик',     'trash'),
]


@staff_member_required
def inbox_view(request):
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    if not account:
        return render(request, 'email_assistant/no_account.html', {'title': 'Email Асистент'})

    folder   = request.GET.get('folder', 'inbox')
    q        = request.GET.get('q', '').strip()
    page     = max(1, int(request.GET.get('page', 1)))
    per_page = 30

    qs    = _build_qs(account, folder, q)
    total = qs.count()
    start = (page - 1) * per_page
    # rename key to `emails` to avoid shadowing Django messages framework
    emails = list(qs.select_related('thread')[start:start + per_page])

    unread_count = EmailMessage.objects.filter(
        account=account, folder='inbox', is_read=False, is_deleted=False
    ).count()

    return render(request, 'email_assistant/inbox.html', {
        'title':        'Email Асистент',
        'account':      account,
        'emails':       emails,
        'folder':       folder,
        'folders':      FOLDERS,
        'q':            q,
        'page':         page,
        'total':        total,
        'per_page':     per_page,
        'has_prev':     page > 1,
        'has_next':     start + per_page < total,
        'unread_count': unread_count,
        'crm_contacts': json.dumps(_crm_contacts()),
    })


@staff_member_required
def thread_view(request, thread_pk):
    """Full-page standalone thread view (open separately)."""
    from email_assistant.models import EmailThread

    account = _get_account(request)
    thread  = get_object_or_404(EmailThread, pk=thread_pk, account=account)
    emails  = list(thread.messages.filter(is_deleted=False).order_by('sent_at'))

    if any(not m.is_read for m in emails):
        thread.messages.filter(is_read=False).update(is_read=True)
        thread.has_unread = False
        thread.save(update_fields=['has_unread'])

    return render(request, 'email_assistant/thread.html', {
        'title':    thread.subject,
        'account':  account,
        'thread':   thread,
        'emails':   emails,
        'last_msg': emails[-1] if emails else None,
    })


@staff_member_required
def message_view(request, message_pk):
    """Full-page standalone single message view."""
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    if not msg.is_read:
        msg.is_read = True
        msg.save(update_fields=['is_read'])

    return render(request, 'email_assistant/thread.html', {
        'title':    msg.subject,
        'account':  account,
        'thread':   msg.thread,
        'emails':   [msg],
        'last_msg': msg,
    })


@staff_member_required
def thread_preview_view(request, thread_pk):
    """AJAX: HTML fragment loaded into panel-3 of 3-panel inbox."""
    from email_assistant.models import EmailThread

    account = _get_account(request)
    thread  = get_object_or_404(EmailThread, pk=thread_pk, account=account)
    emails  = list(thread.messages.filter(is_deleted=False).order_by('sent_at'))

    if any(not m.is_read for m in emails):
        thread.messages.filter(is_read=False).update(is_read=True)
        thread.has_unread = False
        thread.save(update_fields=['has_unread'])

    return render(request, 'email_assistant/preview.html', {
        'account':  account,
        'thread':   thread,
        'emails':   emails,
        'last_msg': emails[-1] if emails else None,
    })


@staff_member_required
def message_preview_view(request, message_pk):
    """AJAX: HTML fragment for a single message in panel-3."""
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    if not msg.is_read:
        msg.is_read = True
        msg.save(update_fields=['is_read'])

    return render(request, 'email_assistant/preview.html', {
        'account':  account,
        'thread':   msg.thread,
        'emails':   [msg],
        'last_msg': msg,
    })


@staff_member_required
def compose_view(request):
    account     = _get_account(request)
    reply_to_pk = request.GET.get('reply_to')
    forward_pk  = request.GET.get('forward')
    reply_to    = None
    initial     = {}

    if reply_to_pk:
        from email_assistant.models import EmailMessage
        reply_to = get_object_or_404(EmailMessage, pk=reply_to_pk, account=account)
        subj = reply_to.subject
        if not subj.lower().startswith('re:'):
            subj = f'Re: {subj}'
        initial = {
            'to':      reply_to.from_email,
            'subject': subj,
            'quote':   f'\n\n--- Оригінальний лист ---\nВід: {reply_to.from_email}\n{reply_to.body_text[:2000]}',
        }
    elif forward_pk:
        from email_assistant.models import EmailMessage
        fwd  = get_object_or_404(EmailMessage, pk=forward_pk, account=account)
        subj = fwd.subject
        if not subj.lower().startswith('fwd:'):
            subj = f'Fwd: {subj}'
        initial = {
            'subject': subj,
            'quote':   f'\n\n--- Переслано ---\nВід: {fwd.from_email}\nКому: {", ".join(fwd.to_emails)}\n{fwd.body_text[:2000]}',
        }

    if request.method == 'POST':
        return _handle_send(request, account, reply_to)

    # Signature
    sig = ''
    try:
        sig = (account.user.profile.smtp_signature or '').strip()
        if sig:
            name = (account.user.get_full_name() or account.display_name or account.user.username)
            sig = sig.replace('{name}', name)
    except Exception:
        pass

    return render(request, 'email_assistant/compose.html', {
        'title':        'Новий лист' if not reply_to else 'Відповідь',
        'account':      account,
        'reply_to':     reply_to,
        'initial':      initial,
        'signature':    sig,
        'crm_contacts': json.dumps(_crm_contacts()),
    })


def _handle_send(request, account, reply_to=None):
    from email_assistant.smtp_client import SMTPClient
    from email_assistant.models import EmailMessage

    to_raw  = request.POST.get('to', '')
    cc_raw  = request.POST.get('cc', '')
    subject = request.POST.get('subject', '')
    body    = request.POST.get('body', '')

    to_list = [e.strip() for e in to_raw.split(',') if e.strip()]
    cc_list = [e.strip() for e in cc_raw.split(',') if e.strip()]

    if not to_list:
        return JsonResponse({'ok': False, 'error': 'Вкажіть отримувача'})
    if not subject:
        return JsonResponse({'ok': False, 'error': 'Вкажіть тему листа'})

    attachments = []
    for f in request.FILES.getlist('attachments'):
        attachments.append({'name': f.name, 'content': f.read(), 'content_type': f.content_type})

    result = SMTPClient(account).send(
        to_emails=to_list, subject=subject, body_text=body,
        cc_emails=cc_list, reply_to_message=reply_to, attachments=attachments,
    )

    if result['ok']:
        thread = reply_to.thread if reply_to else None
        EmailMessage.objects.create(
            account=account, thread=thread, folder='sent',
            subject=subject, from_email=account.email_address,
            from_name=account.display_name, to_emails=to_list, cc_emails=cc_list,
            body_text=body, is_read=True, sent_at=timezone.now(),
        )

    return JsonResponse(result)


@staff_member_required
@require_POST
def send_api(request):
    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Email акаунт не налаштований'})

    reply_to = None
    reply_pk = request.POST.get('reply_to_pk')
    if reply_pk:
        from email_assistant.models import EmailMessage
        try:
            reply_to = EmailMessage.objects.get(pk=reply_pk, account=account)
        except EmailMessage.DoesNotExist:
            pass

    return _handle_send(request, account, reply_to)


@staff_member_required
def ai_suggest_reply(request, message_pk):
    from email_assistant.models import EmailMessage
    from ai_assistant.service import chat

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    profile = getattr(request.user, 'profile', None)

    context = msg.body_text[:3000]
    if msg.thread:
        parts = [f'Від: {m.from_email}\n{m.body_text[:500]}'
                 for m in msg.thread.messages.order_by('sent_at')[:8]]
        context = '\n\n---\n'.join(parts)

    prompt = (
        f'Прочитай цю переписку і склади відповідь від імені {account.from_header}.\n\n'
        f'ПЕРЕПИСКА:\n{context}\n\nНапиши ТІЛЬКИ текст відповіді, без пояснень.'
    )

    reply = chat(prompt, profile=profile, channel='system_briefing')
    msg.ai_reply_draft = reply or ''
    msg.save(update_fields=['ai_reply_draft'])
    return JsonResponse({'ok': True, 'reply': reply or ''})


@staff_member_required
def ai_translate(request, message_pk):
    from email_assistant.models import EmailMessage, EmailSettings
    from ai_assistant.service import chat

    account  = _get_account(request)
    msg      = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    settings = EmailSettings.get_for_user(request.user)
    target   = request.GET.get('lang', settings.ai_translate_to or 'uk')
    profile  = getattr(request.user, 'profile', None)

    lang_names = {'uk': 'українську', 'de': 'німецьку', 'en': 'англійську'}
    prompt = (f'Перекладі цей лист на {lang_names.get(target, target)}. '
              f'Поверни ТІЛЬКИ переклад без пояснень.\n\n{msg.body_text[:3000]}')

    translation = chat(prompt, profile=profile, channel='system_briefing')
    msg.ai_translated   = translation or ''
    msg.ai_translate_to = target
    msg.save(update_fields=['ai_translated', 'ai_translate_to'])
    return JsonResponse({'ok': True, 'translation': translation or ''})


@staff_member_required
@require_POST
def sync_now(request):
    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

    full = request.POST.get('full') == '1'
    from django.core.management import call_command
    from io import StringIO
    out = StringIO()
    try:
        kwargs = {'account': account.pk, 'stdout': out, 'stderr': out}
        if full:
            kwargs['all'] = True
        call_command('sync_email', **kwargs)
        return JsonResponse({'ok': True, 'output': out.getvalue()})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@staff_member_required
def unread_count_view(request):
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    if not account:
        return JsonResponse({'count': 0})
    count = EmailMessage.objects.filter(
        account=account, folder='inbox', is_read=False, is_deleted=False
    ).count()
    return JsonResponse({'count': count})


@staff_member_required
@require_POST
def toggle_star_view(request, message_pk):
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    msg.is_starred = not msg.is_starred
    msg.save(update_fields=['is_starred'])
    return JsonResponse({'ok': True, 'starred': msg.is_starred})


@staff_member_required
@require_POST
def delete_message_view(request, message_pk):
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)

    if msg.folder == EmailMessage.FOLDER_TRASH:
        msg.is_deleted = True
        msg.save(update_fields=['is_deleted'])
    else:
        old_folder = msg.folder
        msg.folder = EmailMessage.FOLDER_TRASH
        msg.save(update_fields=['folder'])
        if msg.imap_uid:
            try:
                from email_assistant.imap_client import IMAPClient
                folder_map = {
                    EmailMessage.FOLDER_INBOX: account.imap_folder_inbox,
                    EmailMessage.FOLDER_SENT:  account.imap_folder_sent,
                }
                imap_folder = folder_map.get(old_folder, account.imap_folder_inbox)
                with IMAPClient(account) as imap:
                    imap.move_to_trash(imap_folder, msg.imap_uid)
            except Exception as e:
                logger.warning('IMAP delete failed: %s', e)

    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def restore_message_view(request, message_pk):
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    msg.folder = EmailMessage.FOLDER_INBOX
    msg.is_deleted = False
    msg.save(update_fields=['folder', 'is_deleted'])
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def archive_thread_view(request, thread_pk):
    from email_assistant.models import EmailThread
    account = _get_account(request)
    thread = get_object_or_404(EmailThread, pk=thread_pk, account=account)
    thread.is_archived = True
    thread.save(update_fields=['is_archived'])
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def unarchive_thread_view(request, thread_pk):
    from email_assistant.models import EmailThread
    account = _get_account(request)
    thread = get_object_or_404(EmailThread, pk=thread_pk, account=account)
    thread.is_archived = False
    thread.save(update_fields=['is_archived'])
    return JsonResponse({'ok': True})
