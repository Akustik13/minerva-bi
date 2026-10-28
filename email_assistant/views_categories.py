"""Правий клік на своїй категорії в лівій колонці скриньки: правила, усе прочитано, сховати, у Кошик, видалити.
Правила категорій — work_categories.py; листи — лише поточної скриньки, як лічильники в колонці."""
import json
import re

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from email_assistant import work_categories

COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _mails(account, cat):
    from email_assistant.views import ALL_FOLDERS, _build_qs
    return _build_qs(account, ALL_FOLDERS).filter(work_category=cat)


def _lines(text) -> str:
    """Рядки правила без порожніх і повторів (без різниці великі чи малі літери)."""
    seen, out = set(), []
    for ln in str(text or '').splitlines():
        ln = ln.strip()
        if ln and ln.lower() not in seen:
            seen.add(ln.lower())
            out.append(ln)
    return '\n'.join(out)


def _dante(cat) -> dict | None:
    """Куди в askDANTE йде час на ці листи і посилання «⏱ Занести в askDANTE» (якщо є застосунок worktime)."""
    try:
        from worktime.admin_mail import transfer_url
    except ImportError:
        return None
    ct = getattr(cat, 'dante', None)
    return {'project': str(ct.dante_project) if ct and ct.dante_project_id else '',
            'url': transfer_url(cat)}


@staff_member_required
def category_info_view(request, pk):
    from email_assistant.models import EmailMessage, EmailSettings, MailCategory
    from email_assistant.views import _get_account
    cat = get_object_or_404(MailCategory, pk=pk)
    account = _get_account(request)
    mails = _mails(account, cat) if account else EmailMessage.objects.none()
    return JsonResponse({
        'pk': cat.pk, 'name': cat.name, 'icon': cat.icon, 'color': cat.color, 'label': cat.label,
        'senders': cat.senders, 'subject_words': cat.subject_words, 'rules': work_categories.rules_summary(cat),
        'mails': mails.count(), 'unread': mails.filter(is_read=False).count(),
        'sent': mails.filter(folder=EmailMessage.FOLDER_SENT).count(),
        'hidden': cat.pk in (EmailSettings.get_for_user(request.user).hidden_work_categories or []),
        'dante': _dante(cat),
        'card_url': reverse('admin:email_assistant_mailcategory_change', args=[cat.pk]),
        'can_change': request.user.has_perm('email_assistant.change_mailcategory'),
        'can_delete': request.user.has_perm('email_assistant.delete_mailcategory'),
        'scan': work_categories.status(),
    })


@staff_member_required
def message_why_view(request, message_pk):
    """«🔍 Чому лист тут»: своя категорія й правило, що спрацювало, інші категорії, що теж підходять,
    хто пише (тип співрозмовника — авто чи вручну), проєкти й клієнт гілки."""
    from email_assistant.categorize import counterparts
    from email_assistant.crm_link import own_addresses
    from email_assistant.models import EmailCategoryRule, EmailMessage, MailCategory
    msg = get_object_or_404(EmailMessage.objects.select_related('thread', 'account', 'work_category'),
                            pk=message_pk, account__user=request.user)
    why = work_categories.explain(msg)

    def cat_json(c):
        return {'pk': c.pk, 'label': c.label, 'color': c.color} if c else None
    contact = (counterparts(msg, own_addresses()) or [''])[0]
    domain = '@' + contact.split('@', 1)[1] if '@' in contact else ''
    manual = EmailCategoryRule.objects.filter(account=msg.account, key__in=[k for k in (contact, domain) if k]).first()
    projects = []
    if msg.thread_id:
        try:
            from projects.models import ProjectLink
            projects = [f'{lk.project.code} {lk.project.name}'.strip() for lk in ProjectLink.objects.filter(
                thread_id=msg.thread_id, status=ProjectLink.CONFIRMED).select_related('project')]
        except ImportError:
            pass
    customer = msg.thread.crm_customer if msg.thread_id and msg.thread.crm_customer_id else None
    return JsonResponse({
        'subject': msg.subject or '', 'category': cat_json(why['category']), 'how': why['how'], 'rule': why['rule'],
        'text': why['text'], 'also': [cat_json(c) for c in why['also']], 'stale': why['stale'],
        'expected': cat_json(why['expected']),
        'field': {'sender': 'senders', 'word': 'subject_words'}.get(why['how'], ''),
        'contact': contact, 'domain': domain,
        'kind': dict(EmailMessage.CATEGORY_CHOICES).get(msg.category, '') if msg.category else '',
        'kind_manual': manual.key if manual else '',
        'projects': projects, 'customer': str(customer) if customer else '',
        'categories': [cat_json(c) for c in MailCategory.objects.filter(is_active=True)],
        'can_change': request.user.has_perm('email_assistant.change_mailcategory'),
    })


@staff_member_required
@require_POST
def category_action_view(request, pk):
    """hide / unhide — лише для себе; read, trash — листи категорії в поточній скриньці;
    save, recompute — право change; delete — право delete (категорія спільна для всіх скриньок)."""
    from email_assistant.models import EmailMessage, EmailSettings, MailCategory
    from email_assistant.sync import default_imap_folder, set_read
    from email_assistant.views import _get_account, _imap_bg_many
    cat = get_object_or_404(MailCategory, pk=pk)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Невірний запит'}, status=400)
    action = data.get('action', '')
    user = request.user

    def denied():
        return JsonResponse({'ok': False, 'error': 'Немає права змінювати свої категорії'}, status=403)

    if action in ('hide', 'unhide'):
        es = EmailSettings.get_for_user(user)
        hidden = [i for i in (es.hidden_work_categories or []) if i != cat.pk]
        if action == 'hide':
            hidden.append(cat.pk)
        es.hidden_work_categories = hidden
        es.save(update_fields=['hidden_work_categories'])
        return JsonResponse({'ok': True})

    if action in ('read', 'trash'):
        account = _get_account(request)
        if account is None:
            return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})
        if action == 'read':
            return JsonResponse({'ok': True, 'changed': set_read(account, list(_mails(account, cat).filter(
                is_read=False)), True)})
        msgs = list(_mails(account, cat))   # без Спаму й Кошика — як лічильник у колонці
        _imap_bg_many(account, [(default_imap_folder(account, m), m.imap_uid) for m in msgs if m.imap_uid], 'trash')
        pks = [m.pk for m in msgs]
        EmailMessage.objects.filter(pk__in=pks).update(folder=EmailMessage.FOLDER_TRASH)
        from email_assistant.knowledge_hook import touch
        touch(pks)   # масовий .update() без сигналів → база знань
        return JsonResponse({'ok': True, 'moved': len(pks)})

    if action == 'save':
        if not user.has_perm('email_assistant.change_mailcategory'):
            return denied()
        senders, words = _lines(data.get('senders')), _lines(data.get('subject_words'))
        rules_changed = (senders, words) != (_lines(cat.senders), _lines(cat.subject_words))
        cat.name = (str(data.get('name') or '').strip() or cat.name)[:80]
        cat.icon = str(data.get('icon') if data.get('icon') is not None else cat.icon).strip()[:8]
        if COLOR_RE.match(str(data.get('color') or '')):
            cat.color = data['color']
        cat.senders, cat.subject_words = senders, words
        cat.save(update_fields=['name', 'icon', 'color', 'senders', 'subject_words'])
        started = work_categories.start_recompute(365) if rules_changed else None
        return JsonResponse({'ok': True, 'recompute': started})

    if action == 'remove_line':   # «✂ Прибрати це правило» з «🔍 Чому тут»
        if not user.has_perm('email_assistant.change_mailcategory'):
            return denied()
        field = data.get('field') if data.get('field') in ('senders', 'subject_words') else None
        line = str(data.get('line') or '').strip().lower()
        if not field or not line:
            return JsonResponse({'ok': False, 'error': 'Невірне правило'}, status=400)
        lines = [ln for ln in (getattr(cat, field) or '').splitlines() if ln.strip()]
        keep = [ln for ln in lines if ln.strip().lower() != line]
        if len(keep) == len(lines):
            return JsonResponse({'ok': False, 'error': 'Такого правила в категорії вже немає'})
        setattr(cat, field, '\n'.join(keep))
        cat.save(update_fields=[field])
        return JsonResponse({'ok': True, 'recompute': work_categories.start_recompute(365)})

    if action == 'recompute':
        if not user.has_perm('email_assistant.change_mailcategory'):
            return denied()
        return JsonResponse({'ok': True, 'recompute': work_categories.start_recompute(365)})

    if action == 'delete':
        if not user.has_perm('email_assistant.delete_mailcategory'):
            return JsonResponse({'ok': False, 'error': 'Немає права видаляти свої категорії'}, status=403)
        label = cat.label
        cat.delete()   # листи лишаються без своєї категорії; інші правила можуть їх підхопити
        work_categories.start_recompute(365)
        return JsonResponse({'ok': True, 'deleted': label})

    return JsonResponse({'ok': False, 'error': 'Невідома дія'}, status=400)
