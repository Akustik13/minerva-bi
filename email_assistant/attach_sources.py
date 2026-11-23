"""
email_assistant/attach_sources.py — «📎» швидкої відповіді й «📂 З Minerva» сторінки «Написати»: що можна вкласти,
розкладене за джерелом, а не одним списком.

  letters(emails, account) — ця гілка по листах: [{kind: letter, own, date, who, files}] (новіші вгорі);
  projects(thread, account, user, skip_codes, skip_orders) — кожен підтверджений проєкт гілки:
      [{pk, code, name, count, groups}], groups — [{kind, icon, name, n, parts}] у порядку:
        docs  — «📎 Документи проєкту» (документи бази знань з файлом, які людина бачить: can_view);
        rfq   — кожен запит постачальникам проєкту: файли запиту (rfq:<pk>) і файли від постачальників
                (rfqreply:<pk>, по постачальнику);
        order — документи кожного замовлення проєкту (order:<pk>:<файл>);
        thread — вкладення кожної іншої гілки проєкту по листах (msg:<pk>:<i>).
  part — {kind: '' | letter | request | supplier, own, date, who, files: [{ref, name, size}]}.

Лише те, що людина бачить і може вкласти: листи — лише цієї скриньки (як «msg:» у attachments.load_ref), проєкти —
з правом бачити проєкти, запити — з правом бачити запити, замовлення — з правом бачити замовлення. Лише читання бази
(розміри — з бази й файлової системи); що вже показано вище (запит гілки, замовлення гілки, ця гілка) — не вдруге.
"""
import logging
import os

logger = logging.getLogger('email_assistant')

MAX_PROJECTS = 3
PROJECT_DOCS = 30
PROJECT_REQUESTS = 5
PROJECT_ORDERS = 5
PROJECT_THREADS = 12
PROJECT_MESSAGES = 300        # листів з вкладеннями, які переглядаємо в гілках проєкту
LETTER_FILES = 30


def _files(msg) -> list:
    out = []
    for i, att in enumerate(msg.attachments or []):
        if att.get('is_inline'):
            continue
        out.append({'ref': f'msg:{msg.pk}:{i}', 'name': att.get('name') or 'attachment', 'size': att.get('size') or 0})
    return out[:LETTER_FILES]


def _letter(msg, own) -> dict:
    from email_assistant.crm_link import is_own
    mine = is_own(msg.from_email or '', own)
    if mine:
        who = ', '.join((msg.to_emails or [])[:2])
    else:
        who = msg.from_name or msg.from_email or ''
    return {'kind': 'letter', 'own': mine, 'who': who,
            'date': msg.sent_at.strftime('%d.%m.%Y') if msg.sent_at else ''}


def letters(emails, account=None) -> list:
    """Вкладення гілки по листах — новіші вгорі; листи без файлів не показуються."""
    from email_assistant.crm_link import own_addresses
    own = own_addresses()
    out = []
    for m in sorted([m for m in emails or [] if m.attachments], key=lambda m: m.sent_at or m.pk, reverse=True):
        files = _files(m)
        if files:
            out.append(dict(_letter(m, own), files=files))
    return out


def _docs(project, user) -> list:
    try:
        from rag_assistant import knowledge_items as ki
    except ImportError:
        return []
    files = []
    for item in ki.items_for(user, 'project', project):
        if not ki.has_file(item):
            continue
        name = item.file_name or os.path.basename(item.file.name if item.file else item.source_path) or item.title
        files.append({'ref': f'kb:{item.pk}', 'name': name, 'size': item.file_size or 0})
        if len(files) >= PROJECT_DOCS:
            break
    return files


def _requests(project, user, skip_codes) -> list:
    if not user.has_perm('suppliers.view_supplierrequest'):
        return []
    try:
        from suppliers.models import SupplierReplyFile, SupplierRequest
    except (ImportError, LookupError):
        return []
    groups = []
    reqs = (SupplierRequest.objects.filter(project=project).exclude(code__in=list(skip_codes))
            .prefetch_related('files').order_by('-created_at')[:PROJECT_REQUESTS])
    for req in reqs:
        parts = []
        own = [{'ref': f'rfq:{f.pk}', 'name': f.name, 'size': f.size} for f in req.files.all()]
        if own:
            parts.append({'kind': 'request', 'files': own})
        by_supplier = {}
        for f in (SupplierReplyFile.objects.filter(recipient__request=req)
                  .select_related('recipient__supplier').order_by('recipient_id', '-received_at', 'pk')):
            by_supplier.setdefault(f.recipient_id, {'kind': 'supplier', 'who': f.recipient.supplier.name,
                                                    'files': []})['files'].append(
                {'ref': f'rfqreply:{f.pk}', 'name': f.name, 'size': f.size})
        parts += list(by_supplier.values())
        if parts:
            groups.append({'kind': 'rfq', 'icon': '📨', 'name': f'{req.code} · {req.title}', 'parts': parts})
    return groups


def _orders(project, user, skip_orders) -> list:
    if not user.has_perm('sales.view_salesorder'):
        return []
    from email_assistant.attachments import order_documents
    from projects.models import ProjectLink
    groups = []
    links = (ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED, order__isnull=False)
             .exclude(order_id__in=list(skip_orders)).select_related('order').order_by('-created_at')[:PROJECT_ORDERS])
    for link in links:
        docs = [{'ref': d['ref'], 'name': d['name'], 'size': d['size']} for d in order_documents(link.order)]
        if docs:
            groups.append({'kind': 'order', 'icon': '📄', 'name': f'{link.order.source}:{link.order.order_number}',
                           'parts': [{'kind': '', 'files': docs}]})
    return groups


def _threads(project, account, thread) -> list:
    if account is None:
        return []
    from email_assistant.crm_link import own_addresses
    from email_assistant.models import EmailMessage
    from projects.models import ProjectLink
    confirmed = ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
    from django.db.models import Q
    qs = (EmailMessage.objects.filter(Q(thread_id__in=confirmed.exclude(thread=None).values('thread_id'))
                                      | Q(pk__in=confirmed.exclude(message=None).values('message_id')),
                                      account=account, is_deleted=False)
          .exclude(attachments=[]).select_related('thread').order_by('-sent_at'))
    if thread is not None:
        qs = qs.exclude(thread_id=thread.pk)
    own = own_addresses()
    groups, by_thread = [], {}
    for m in qs[:PROJECT_MESSAGES]:
        files = _files(m)
        if not files:
            continue
        key = m.thread_id or f'm{m.pk}'
        if key not in by_thread:
            if len(by_thread) >= PROJECT_THREADS:
                continue
            by_thread[key] = {'kind': 'thread', 'icon': '✉️',
                              'name': (m.thread.subject if m.thread_id else '') or m.subject or '', 'parts': []}
            groups.append(by_thread[key])
        by_thread[key]['parts'].append(dict(_letter(m, own), files=files))
    return groups


def projects(thread, account, user, skip_codes=(), skip_orders=()) -> list:
    """Підтверджені проєкти гілки — кожен окремим розділом з джерелами всередині."""
    if thread is None or user is None or not user.has_perm('projects.view_project'):
        return []
    try:
        from projects.services import thread_projects
    except ImportError:
        return []
    out = []
    for link in thread_projects(thread)['confirmed'][:MAX_PROJECTS]:
        p = link.project
        if any(s['pk'] == p.pk for s in out):
            continue
        groups = []
        docs = _docs(p, user)
        if docs:
            groups.append({'kind': 'docs', 'icon': '📎', 'name': '', 'parts': [{'kind': '', 'files': docs}]})
        for part in (_requests(p, user, skip_codes), _orders(p, user, skip_orders), _threads(p, account, thread)):
            groups += part
        for g in groups:
            g['n'] = sum(len(part['files']) for part in g['parts'])
        if groups:
            out.append({'pk': p.pk, 'code': p.code, 'name': p.name, 'groups': groups,
                        'count': sum(g['n'] for g in groups)})
    return out
