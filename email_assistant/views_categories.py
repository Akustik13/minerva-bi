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


@staff_member_required
@require_POST
def nav_pref_view(request):
    """⭐ обране й вживання лівої колонки — лише для себе: POST {action: pin | unpin | up | down | use, key}."""
    from email_assistant import nav_prefs
    from email_assistant.models import EmailSettings
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    action, key = data.get('action'), str(data.get('key') or '')
    if action not in ('pin', 'unpin', 'up', 'down', 'use') or not nav_prefs.valid(key):
        return JsonResponse({'ok': False, 'error': 'Невідома дія або пункт'}, status=400)
    es = EmailSettings.get_for_user(request.user)
    if action == 'use':
        nav_prefs.record_use(es, key)
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': True, 'pinned': nav_prefs.change(es, action, key)})


@staff_member_required
@require_POST
def ctx_menu_view(request):
    """Меню правого кліку листа — що в меню, що в «⋯ Ще», лише для себе: POST {list: [...], letter: [...]} | {reset: true}."""
    from email_assistant import nav_prefs
    from email_assistant.models import EmailSettings
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    es = EmailSettings.get_for_user(request.user)
    return JsonResponse({'ok': True, 'hidden': nav_prefs.set_ctx_hidden(es, data)})


@staff_member_required
@require_POST
def layout_view(request):
    """🎨 Вигляд пошти, лише для себе: POST {preset: ключ} | {reset: true} | {перемикач: значення, …}."""
    from email_assistant import layout
    from email_assistant.models import EmailSettings
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    try:
        values = layout.save(EmailSettings.get_for_user(request.user), data)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    return JsonResponse({'ok': True, 'layout': values, 'preset': layout.preset_of(values),
                         'classes': layout.classes(values)})


def _mails(account, cat):
    from email_assistant.views import ALL_FOLDERS, _build_qs
    return _build_qs(account, ALL_FOLDERS).filter(work_category=cat)


def _nav_ordered(user, cats) -> list:
    """Свої категорії в порядку лівої колонки користувача (⭐ обране, далі за вживанням / назвою …)."""
    from email_assistant import nav_prefs
    from email_assistant.models import EmailSettings
    return nav_prefs.ordered(list(cats), EmailSettings.get_for_user(user), lambda c: f'wc:{c.pk}',
                             name_of=lambda c: c.name)


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


def _customer_json(c):
    if c is None:
        return None
    label = c.company or c.name
    if c.company and c.name and c.name != c.company:
        label += f' — {c.name}'
    return {'id': c.pk, 'label': label + (f' <{c.email}>' if c.email else ''),
            'url': reverse('admin:crm_customer_change', args=[c.pk])}


@staff_member_required
def customers_search_view(request):
    """Пошук клієнта CRM для гілки (компанія, ім'я, email; від 2 символів)."""
    from django.db.models import Q
    from crm.models import Customer
    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    found = Customer.objects.filter(Q(company__icontains=q) | Q(name__icontains=q) | Q(email__icontains=q))
    return JsonResponse({'results': [_customer_json(c) for c in found.order_by('company', 'name')[:15]]})


@staff_member_required
@require_POST
def message_customer_view(request, message_pk):
    """Клієнт CRM гілки листа вручну: {customer: id | ''} → crm_link.set_thread_customer."""
    from crm.models import Customer
    from email_assistant.crm_link import set_thread_customer
    from email_assistant.knowledge_hook import touch
    from email_assistant.models import EmailMessage
    msg = get_object_or_404(EmailMessage.objects.select_related('thread'), pk=message_pk, account__user=request.user)
    if not msg.thread_id:
        return JsonResponse({'ok': False, 'error': 'Лист без гілки листування'})
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    value = str(data.get('customer') or '')
    customer = get_object_or_404(Customer, pk=int(value)) if value.isdigit() else None
    from email_assistant.crm_link import adopt_thread
    done = adopt_thread(msg.thread, customer, user=request.user) if customer else \
        {'mails': set_thread_customer(msg.thread, None, user=request.user)}
    touch(thread_id=msg.thread_id)   # клієнт гілки — у документах бази знань
    return JsonResponse({'ok': True, 'customer': _customer_json(customer), **done})


@staff_member_required
@require_POST
def message_customer_create_view(request, message_pk):
    """«👤 Створити клієнта CRM з листа»: {name, company, email, phone, addr_street, addr_zip, addr_city, country}
    (порожнє — з листа, crm_link.customer_draft) → клієнт (той самий email — наявний) і гілка, її Angebote та
    проєкти — до нього."""
    from email_assistant.crm_link import adopt_thread, create_customer, customer_draft
    from email_assistant.knowledge_hook import touch
    from email_assistant.models import EmailMessage
    if not request.user.has_perm('crm.add_customer'):
        return JsonResponse({'ok': False, 'error': 'Немає права створювати клієнтів CRM'}, status=403)
    msg = get_object_or_404(EmailMessage.objects.select_related('thread'), pk=message_pk, account__user=request.user)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    draft = customer_draft(msg) or {}
    fields = {k: str(data.get(k) or draft.get(k) or '').strip() for k in
              ('name', 'company', 'email', 'phone', 'addr_street', 'addr_zip', 'addr_city', 'country')}
    if not (fields['email'] or fields['name']):
        return JsonResponse({'ok': False, 'error': 'У листі немає адреси клієнта — введіть email або ім\'я'})
    when = msg.sent_at.strftime('%d.%m.%Y') if msg.sent_at else ''
    customer, created = create_customer(fields, request.user, note=f'Створено з листа «{msg.subject or ""}» {when}')
    done = adopt_thread(msg.thread, customer, user=request.user) if msg.thread_id else {}
    touch(thread_id=msg.thread_id)
    return JsonResponse({'ok': True, 'created': created, 'customer': _customer_json(customer), **done})


@staff_member_required
def message_why_view(request, message_pk):
    """«🔍 Чому лист тут»: своя категорія й правило, що спрацювало, інші категорії, що теж підходять,
    хто пише (тип співрозмовника — авто чи вручну), клієнт гілки й відкриті проєкти для вибору
    (проєкти гілки браузер бере з /projects/api/thread/<pk>/)."""
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
    customer = msg.thread.crm_customer if msg.thread_id and msg.thread.crm_customer_id else None
    from email_assistant.crm_link import customer_draft, find_customer
    draft = customer_draft(msg) if customer is None else None   # переслане колегою — відправник оригіналу
    by_address = find_customer([a for a in (contact, (draft or {}).get('email')) if a])
    open_projects = []
    try:
        from projects.models import Project
        open_projects = [{'id': p.pk, 'label': f'{p.code} {p.name}'.strip()} for p in
                         Project.objects.filter(status__in=Project.OPEN_STATUSES).order_by('-updated_at')[:200]]
    except ImportError:
        pass
    quote = None
    try:   # 📝 запит пропозиції: чи схоже і чому ні (offers/detection.py)
        from offers.detection import explain
        quote = explain(msg)
    except ImportError:
        pass
    return JsonResponse({
        'quote': quote,
        'subject': msg.subject or '', 'category': cat_json(why['category']), 'how': why['how'], 'rule': why['rule'],
        'text': why['text'], 'also': [cat_json(c) for c in why['also']], 'stale': why['stale'],
        'expected': cat_json(why['expected']),
        'field': {'sender': 'senders', 'word': 'subject_words'}.get(why['how'], ''),
        'contact': contact, 'domain': domain,
        'kind': dict(EmailMessage.CATEGORY_CHOICES).get(msg.category, '') if msg.category else '',
        'kind_manual': manual.key if manual else '',
        'thread': msg.thread_id,
        'customer': _customer_json(customer),
        'customer_suggest': _customer_json(by_address) if by_address and by_address != customer else None,
        'customer_draft': draft if draft and not by_address else None,
        'can_add_customer': request.user.has_perm('crm.add_customer'),
        'open_projects': open_projects,
        'categories': [cat_json(c) for c in _nav_ordered(request.user, MailCategory.objects.filter(is_active=True))],
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
        from django.db.models import F
        EmailMessage.objects.filter(pk__in=pks).update(   # trashed_from — куди «↩ Відновити» поверне
            folder=EmailMessage.FOLDER_TRASH, trashed_from=F('folder'))
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
