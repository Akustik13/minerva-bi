"""email_assistant/views.py — Email клієнт інтерфейс."""
import json
import logging
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone

from email_assistant.sync import set_read

logger = logging.getLogger('email_assistant')


def _imap_bg(account_pk: int, imap_folder: str, uid: int, action: str):
    """Run an IMAP flag/move operation in a background daemon thread."""
    import threading

    def _run():
        try:
            from django.db import close_old_connections
            close_old_connections()
            from email_assistant.models import EmailAccount
            from email_assistant.imap_client import IMAPClient
            acc = EmailAccount.objects.get(pk=account_pk)
            with IMAPClient(acc) as imap:
                if action == 'seen':
                    imap.mark_seen(imap_folder, uid)
                elif action == 'unseen':
                    imap.mark_unseen(imap_folder, uid)
                elif action == 'spam':
                    imap.move_to_spam(imap_folder, uid)
                elif action == 'trash':
                    imap.move_to_trash(imap_folder, uid)
        except Exception as ex:
            logger.warning('_imap_bg %s uid=%s: %s', action, uid, ex)

    threading.Thread(target=_run, daemon=True).start()


def _imap_bg_many(account, items, action: str):
    """Перемістити кілька листів [(папка, uid)] у Кошик/Спам на сервері — одне підключення, у фоні."""
    items = [(f, u) for f, u in items if f and u]
    if not items:
        return

    def _run():
        from email_assistant.imap_client import IMAPClient
        try:
            with IMAPClient(account) as imap:
                for folder, uid in items:
                    if action == 'trash':
                        imap.move_to_trash(folder, uid)
                    elif action == 'spam':
                        imap.move_to_spam(folder, uid)
        except Exception as ex:
            logger.warning('_imap_bg_many %s %s: %s', action, items[:3], ex)

    from email_assistant.sync import _start_background
    _start_background(_run)


def _get_account(request):
    from email_assistant.models import EmailAccount
    return (EmailAccount.objects
            .filter(user=request.user, is_active=True)
            .order_by('-is_primary').first())


def _ctx(request, ctx: dict) -> dict:
    """Inject is_nav_sidebar_enabled based on user's EmailSettings."""
    from email_assistant.models import EmailSettings
    ctx['is_nav_sidebar_enabled'] = EmailSettings.get_for_user(request.user).show_admin_sidebar
    return ctx


def _get_signature(account) -> str:
    """Return rendered HTML signature for an account (account-level > UserProfile)."""
    try:
        sig = (account.signature or '').strip()
        if not sig:
            sig = (account.user.profile.smtp_signature or '').strip()
        if sig:
            name = (account.user.get_full_name() or account.display_name or account.user.username)
            sig = sig.replace('{name}', name)
        return sig
    except Exception:
        return ''


def _crm_contacts(user=None) -> list:
    """Merge CRM customers + user's EmailContact address book."""
    contacts = {}
    try:
        from crm.models import Customer
        for c in Customer.objects.exclude(email='').values('email', 'name')[:300]:
            contacts[c['email'].lower()] = {
                'email': c['email'], 'name': c['name'] or c['email']}
    except Exception:
        pass
    if user and user.is_authenticated:
        try:
            from email_assistant.models import EmailContact
            for ec in EmailContact.objects.filter(user=user).values('email', 'name')[:300]:
                key = ec['email'].lower()
                if key not in contacts:
                    contacts[key] = {'email': ec['email'], 'name': ec['name'] or ec['email']}
        except Exception:
            pass
    return list(contacts.values())


def _save_contacts(user, recipients: list):
    """Upsert EmailContact for every successfully used recipient address."""
    from email_assistant.models import EmailContact
    for raw in recipients:
        raw = (raw or '').strip()
        if not raw or '@' not in raw:
            continue
        name, email = '', raw
        if '<' in raw and '>' in raw:
            name  = raw.split('<')[0].strip().strip('"\'')
            email = raw.split('<')[1].rstrip('>')
        email = email.lower().strip()
        if not email:
            continue
        try:
            obj, created = EmailContact.objects.get_or_create(
                user=user, email=email, defaults={'name': name})
            if not created:
                if name and not obj.name:
                    obj.name = name
                obj.use_count += 1
                obj.save(update_fields=['use_count', 'name', 'last_used_at'])
        except Exception:
            pass


_STANDARD_FOLDERS = {'inbox', 'sent', 'starred', 'spam', 'archived', 'trash'}


def _page_range(page, total_pages):
    """Return list of page numbers/None (None = ellipsis) for smart pagination."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    pages = []
    shown = set()

    def add(p):
        if 1 <= p <= total_pages and p not in shown:
            pages.append(p)
            shown.add(p)

    for p in [1, 2]:
        add(p)
    if page - 2 > 3:
        pages.append(None)  # ellipsis
    for p in range(max(1, page - 1), min(total_pages, page + 1) + 1):
        add(p)
    if page + 2 < total_pages - 1:
        pages.append(None)  # ellipsis
    for p in [total_pages - 1, total_pages]:
        add(p)
    return pages


def _build_qs(account, folder, q=''):
    from email_assistant.models import EmailMessage, EmailThread

    if folder == 'spam':
        qs = EmailMessage.objects.filter(account=account, is_spam=True, is_deleted=False)
    elif folder == 'starred':
        qs = EmailMessage.objects.filter(account=account, is_starred=True, is_deleted=False)
    elif folder == 'archived':
        archived_ids = EmailThread.objects.filter(
            account=account, is_archived=True
        ).values_list('id', flat=True)
        qs = EmailMessage.objects.filter(
            account=account, thread_id__in=archived_ids, is_deleted=False
        )
    elif folder == 'inbox':
        from django.db.models import Q
        archived_ids = EmailThread.objects.filter(
            account=account, is_archived=True
        ).values_list('id', flat=True)
        qs = EmailMessage.objects.filter(
            account=account, folder='inbox', is_deleted=False
        ).filter(
            Q(imap_folder_name='') | Q(imap_folder_name__iexact=account.imap_folder_inbox)
        ).exclude(thread_id__in=archived_ids)
    elif folder not in _STANDARD_FOLDERS:
        # Custom IMAP folder (e.g. "Meine Order")
        qs = EmailMessage.objects.filter(
            account=account, imap_folder_name=folder, is_deleted=False
        )
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


def _category_filter(request, qs):
    """(категорія, клієнт, [(ключ, назва, листів, непрочитаних)], клієнти з листами в папці)."""
    from django.db.models import Count, Q
    from crm.models import Customer
    from email_assistant.models import EmailMessage

    cat = request.GET.get('cat', '')
    if cat not in dict(EmailMessage.CATEGORY_CHOICES):
        cat = ''
    customer = None
    if str(request.GET.get('customer', '')).isdigit():
        customer = Customer.objects.filter(pk=int(request.GET['customer'])).first()

    base = qs.order_by()
    counts = {r['category']: r for r in base.values('category').annotate(
        n=Count('id'), unread=Count('id', filter=Q(is_read=False)))}
    categories = [(key, label, counts.get(key, {}).get('n', 0), counts.get(key, {}).get('unread', 0))
                  for key, label in EmailMessage.CATEGORY_CHOICES]
    customers = list(base.exclude(thread__crm_customer=None)
                     .values('thread__crm_customer', 'thread__crm_customer__name')
                     .annotate(n=Count('id'), unread=Count('id', filter=Q(is_read=False)))
                     .order_by('-unread', '-n')[:200])
    return cat, customer, categories, customers


def _group_by_thread(qs):
    """Залишити по одному листу на гілку — найновіший серед листів поточного списку."""
    from django.db.models import OuterRef, Q, Subquery
    latest = qs.filter(thread_id=OuterRef('thread_id')).order_by('-sent_at', '-pk').values('pk')[:1]
    return qs.filter(Q(thread__isnull=True) | Q(pk=Subquery(latest)))


def _attach_crm_customers(emails):
    """Клієнт для мітки в рядку: клієнт гілки, інакше — з події CRM-хронології цього листа."""
    from crm.models import CustomerTimeline
    missing = [m.pk for m in emails if not (m.thread_id and m.thread.crm_customer_id)]
    by_message = {}
    if missing:
        for ev in CustomerTimeline.objects.filter(email_message_id__in=missing).select_related('customer'):
            by_message.setdefault(ev.email_message_id, ev.customer)
    for m in emails:
        m.crm_customer_tag = (m.thread.crm_customer if m.thread_id and m.thread.crm_customer_id
                              else by_message.get(m.pk))


def _attach_contacts(emails):
    """Співрозмовник рядка (для «Віднести до категорії» в меню правого кліку)."""
    from email_assistant.categorize import counterparts
    from email_assistant.crm_link import own_addresses
    own = own_addresses()
    for m in emails:
        contacts = counterparts(m, own)
        m.contact = contacts[0] if contacts else ''


def _attach_thread_stats(emails):
    """Скільки листів у гілці (усі, як у прев'ю: і Вхідні, і Надіслані), скільки непрочитаних, їхні pk."""
    from collections import defaultdict
    from email_assistant.models import EmailMessage
    thread_ids = {m.thread_id for m in emails if m.thread_id}
    pks, unread = defaultdict(list), defaultdict(int)
    for tid, pk, is_read in (EmailMessage.objects
                             .filter(thread_id__in=thread_ids, is_deleted=False)
                             .values_list('thread_id', 'pk', 'is_read')):
        pks[tid].append(pk)
        if not is_read:
            unread[tid] += 1
    for m in emails:
        if m.thread_id in pks:
            m.thread_total = len(pks[m.thread_id])
            m.thread_unread = unread[m.thread_id]
            m.thread_pks = ','.join(map(str, pks[m.thread_id]))


def _filter_qs(cat, customer) -> str:
    from urllib.parse import urlencode
    params = {}
    if cat:
        params['cat'] = cat
    if customer:
        params['customer'] = customer.pk
    return ('&' + urlencode(params)) if params else ''


FOLDERS = [
    ('inbox',    'Вхідні',    '📥'),
    ('sent',     'Надіслані', '📤'),
    ('starred',  'Важливі',   '⭐'),
    ('spam',     'Спам',      '🚫'),
    ('archived', 'Архів',     '📦'),
    ('trash',    'Кошик',     '🗑️'),
]


@staff_member_required
def inbox_view(request):
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    if not account:
        return render(request, 'email_assistant/no_account.html', _ctx(request, {'title': 'Email Асистент'}))

    folder   = request.GET.get('folder', 'inbox')
    q        = request.GET.get('q', '').strip()
    page     = max(1, int(request.GET.get('page', 1)))
    per_page = 30

    qs    = _build_qs(account, folder, q)
    # Фільтр «Категорія / Клієнт» (лічильники — по поточній папці до фільтра)
    cat_filter, customer_filter, categories, mail_customers = _category_filter(request, qs)
    if cat_filter:
        qs = qs.filter(category=cat_filter)
    if customer_filter:
        from django.db.models import Q as _CQ
        qs = qs.filter(pk__in=EmailMessage.objects.filter(
            _CQ(thread__crm_customer_id=customer_filter.pk) |
            _CQ(crm_events__customer_id=customer_filter.pk)).values('pk'))
    # «Групувати по гілці» (⎘, cookie): один рядок на гілку — її найновіший лист у цьому списку
    grouped = request.COOKIES.get('em_group') == '1'
    if grouped:
        qs = _group_by_thread(qs)
    total = qs.count()
    start = (page - 1) * per_page
    # rename key to `emails` to avoid shadowing Django messages framework
    emails = list(qs.select_related('thread', 'thread__crm_customer')[start:start + per_page])
    if grouped:
        _attach_thread_stats(emails)
    _attach_crm_customers(emails)
    _attach_contacts(emails)

    from email_assistant.models import EmailThread as _ET
    from django.db.models import Q as _Q
    _archived_ids = _ET.objects.filter(account=account, is_archived=True).values_list('id', flat=True)
    unread_inbox = EmailMessage.objects.filter(
        account=account, folder='inbox',
        is_read=False, is_deleted=False,
    ).filter(
        _Q(imap_folder_name='') | _Q(imap_folder_name__iexact=account.imap_folder_inbox)
    ).exclude(thread_id__in=_archived_ids).count()
    unread_counts = {
        'inbox':    unread_inbox,
        'starred':  EmailMessage.objects.filter(account=account, is_starred=True, is_read=False, is_deleted=False).count(),
        'spam':     EmailMessage.objects.filter(account=account, is_spam=True, is_read=False, is_deleted=False).count(),
        'sent':     0,
        'archived': 0,
        'trash':    0,
    }
    unread_count = unread_inbox  # backwards-compat for template

    from email_assistant.imap_client import decode_mutf7
    from email_assistant.models import EmailSettings
    return render(request, 'email_assistant/inbox.html', _ctx(request, {
        'title':                 'Email Асистент',
        'account':               account,
        'email_settings':        EmailSettings.get_for_user(request.user),
        'folder_label':          decode_mutf7(folder),
        'cat_filter':            cat_filter,
        'customer_filter':       customer_filter,
        'categories':            categories,
        'mail_customers':        mail_customers,
        'filter_qs':             _filter_qs(cat_filter, customer_filter),
        'grouped':               grouped,
        'category_choices':      EmailMessage.CATEGORY_CHOICES,
        'emails':                emails,
        'folder':                folder,
        'folders':               FOLDERS,
        'q':                     q,
        'page':                  page,
        'total':                 total,
        'per_page':              per_page,
        'has_prev':              page > 1,
        'has_next':              start + per_page < total,
        'total_pages':           max(1, (total + per_page - 1) // per_page),
        'page_range':            _page_range(page, max(1, (total + per_page - 1) // per_page)),
        'unread_count':          unread_count,
        'unread_counts':         unread_counts,
        'crm_contacts':          json.dumps(_crm_contacts(request.user)),
        'sync_interval_minutes': max(1, account.sync_interval_minutes),
    }))


@staff_member_required
def thread_view(request, thread_pk):
    """Full-page standalone thread view (open separately)."""
    from email_assistant.models import EmailThread

    account = _get_account(request)
    thread  = get_object_or_404(EmailThread, pk=thread_pk, account=account)
    emails  = list(thread.messages.filter(is_deleted=False).order_by('sent_at'))
    for m in emails:
        m.fix_html_body()
    set_read(account, emails, True)

    return render(request, 'email_assistant/thread.html', _ctx(request, {
        'title':    thread.subject,
        'account':  account,
        'thread':   thread,
        'emails':   emails,
        'last_msg': emails[-1] if emails else None,
    }))


@staff_member_required
def message_view(request, message_pk):
    """Full-page standalone single message view."""
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    msg.fix_html_body()
    set_read(account, [msg], True)

    return render(request, 'email_assistant/thread.html', _ctx(request, {
        'title':    msg.subject,
        'account':  account,
        'thread':   msg.thread,
        'emails':   [msg],
        'last_msg': msg,
    }))


@staff_member_required
def thread_preview_view(request, thread_pk):
    """AJAX: HTML fragment loaded into panel-3 of 3-panel inbox."""
    from email_assistant.models import EmailThread

    account = _get_account(request)
    thread  = get_object_or_404(EmailThread, pk=thread_pk, account=account)
    emails  = list(thread.messages.filter(is_deleted=False).order_by('sent_at'))
    for m in emails:
        m.fix_html_body()

    # Minerva → сервер у фоні: прев'ю відкривається без очікування IMAP
    set_read(account, emails, True)

    return render(request, 'email_assistant/preview.html', _ctx(request, {
        'account':         account,
        'thread':          thread,
        'emails':          emails,
        'last_msg':        emails[-1] if emails else None,
        'reply_signature': _get_signature(account),
    }))


@staff_member_required
def message_preview_view(request, message_pk):
    """AJAX: HTML fragment for a single message in panel-3."""
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    msg.fix_html_body()
    set_read(account, [msg], True)

    return render(request, 'email_assistant/preview.html', _ctx(request, {
        'account':         account,
        'thread':          msg.thread,
        'emails':          [msg],
        'last_msg':        msg,
        'reply_signature': _get_signature(account),
    }))


def _inject_email_css(html: str) -> str:
    """Inject normalization CSS so email body expands fully and scrolls horizontally."""
    css = (
        '<style>'
        'html,body{'
        'height:auto!important;min-height:0!important;max-height:none!important;'
        'overflow-x:auto!important;'
        '}'
        '</style>'
    )
    lower = html.lower()
    pos = lower.rfind('</head>')
    if pos != -1:
        return html[:pos] + css + html[pos:]
    pos = lower.find('<body')
    if pos != -1:
        end = html.find('>', pos)
        if end != -1:
            return html[:end + 1] + css + html[end + 1:]
    return css + html


@staff_member_required
@xframe_options_exempt
def message_html_view(request, message_pk):
    """Serve raw HTML email body for sandboxed iframe display."""
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    msg.fix_html_body()
    if msg.body_html:
        html = msg.body_html
        if 'cid:' in html.lower():
            html = _resolve_cid_images(msg, html)
        html = _inject_email_css(html)
        return HttpResponse(html, content_type='text/html; charset=utf-8')
    import html as html_mod
    body = msg.body_text or ''
    if 'BEGIN:VCALENDAR' in body:
        return HttpResponse(
            _render_icalendar_card(body),
            content_type='text/html; charset=utf-8',
        )
    txt = html_mod.escape(body or '(Лист порожній)')
    return HttpResponse(
        f'<!DOCTYPE html><html><body style="font-family:sans-serif;font-size:14px;line-height:1.7;'
        f'white-space:pre-wrap;padding:12px;margin:0">{txt}</body></html>',
        content_type='text/html; charset=utf-8',
    )


def _render_icalendar_card(ical_text: str) -> str:
    """Parse iCalendar VEVENT and return a styled HTML card."""
    import re, html as _h
    from datetime import datetime

    def _field(name, text):
        pattern = re.compile(
            r'^' + re.escape(name) + r'(?:;[^:]*)?:(.+)$',
            re.MULTILINE | re.IGNORECASE,
        )
        m = pattern.search(text)
        return m.group(1).strip() if m else ''

    def _fields(name, text):
        pattern = re.compile(
            r'^' + re.escape(name) + r'(?:;[^:]*)?:(.+)$',
            re.MULTILINE | re.IGNORECASE,
        )
        return [m.group(1).strip() for m in pattern.finditer(text)]

    def _fmt_dt(raw):
        raw = re.sub(r'Z$', '', raw.strip())
        for fmt in ('%Y%m%dT%H%M%S', '%Y%m%dT%H%M', '%Y%m%d'):
            try:
                dt = datetime.strptime(raw, fmt)
                if 'T' in raw:
                    return dt.strftime('%d.%m.%Y, %H:%M')
                return dt.strftime('%d.%m.%Y')
            except ValueError:
                continue
        return raw

    def _clean_desc(raw):
        raw = raw.replace('\\n', '\n').replace('\\,', ',').replace('\\;', ';')
        raw = re.sub(r'<[^>]+>', '', raw)
        return raw.strip()

    def _organizer_name(raw):
        m = re.search(r'CN="?([^":]+)"?', raw, re.IGNORECASE)
        return m.group(1).strip() if m else raw.split(':')[-1]

    def _attendee_name(raw):
        m = re.search(r'CN="?([^":]+)"?', raw, re.IGNORECASE)
        return m.group(1).strip() if m else raw.split(':')[-1]

    # unfold continuation lines (space/tab at start = continuation)
    unfolded = re.sub(r'\r?\n[ \t]', '', ical_text)

    vevent_m = re.search(r'BEGIN:VEVENT(.+?)END:VEVENT', unfolded, re.DOTALL | re.IGNORECASE)
    block = vevent_m.group(1) if vevent_m else unfolded

    summary   = _h.escape(_field('SUMMARY', block))
    location  = _h.escape(_field('LOCATION', block))
    organizer = _h.escape(_organizer_name(_field('ORGANIZER', block)))
    dt_start  = _fmt_dt(_field('DTSTART', block))
    dt_end    = _fmt_dt(_field('DTEND', block))
    desc_raw  = _clean_desc(_field('DESCRIPTION', block))
    attendees = [_h.escape(_attendee_name(a)) for a in _fields('ATTENDEE', block)]

    zoom_url = ''
    for line in desc_raw.splitlines():
        if 'zoom.us' in line and line.strip().startswith('http'):
            zoom_url = line.strip()
            break
    if not zoom_url and 'zoom.us' in location:
        zoom_url = location

    desc_lines = []
    for line in desc_raw.splitlines():
        if line.strip().startswith('http') and 'zoom.us' in line:
            continue
        if line.strip():
            desc_lines.append(_h.escape(line))
    desc_html = '<br>'.join(desc_lines[:8])

    attendees_html = ''.join(
        f'<span style="display:inline-block;background:#1e3a5f;border-radius:4px;'
        f'padding:2px 8px;margin:2px;font-size:12px">{a}</span>'
        for a in attendees
    ) if attendees else ''

    zoom_btn = (
        f'<a href="{_h.escape(zoom_url)}" target="_blank" '
        f'style="display:inline-block;margin-top:12px;padding:10px 20px;'
        f'background:#2d8cf0;color:#fff;border-radius:6px;text-decoration:none;'
        f'font-weight:600;font-size:14px">📹 Приєднатися до Zoom</a>'
    ) if zoom_url else ''

    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body{{margin:0;padding:12px;background:#10192a;color:#c9d8e4;font-family:sans-serif;font-size:14px;line-height:1.6}}
  .card{{background:#1a2535;border:1px solid #243347;border-radius:8px;padding:16px 20px;max-width:600px}}
  .badge{{display:inline-block;background:#7c3aed;color:#fff;border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700;letter-spacing:.5px;margin-bottom:10px}}
  .title{{font-size:18px;font-weight:700;color:#fff;margin-bottom:12px}}
  .row{{display:flex;gap:8px;align-items:flex-start;margin-bottom:8px;font-size:13px}}
  .icon{{width:18px;flex-shrink:0;opacity:.7;margin-top:1px}}
  .label{{color:#9aafbe;min-width:90px;flex-shrink:0}}
  .val{{color:#c9d8e4}}
  .sep{{border:none;border-top:1px solid #243347;margin:12px 0}}
  .desc{{font-size:13px;color:#9aafbe;white-space:pre-wrap}}
</style></head><body>
<div class="card">
  <div class="badge">📅 Запрошення на зустріч</div>
  <div class="title">{summary or 'Зустріч'}</div>
  <div class="row"><span class="icon">🕐</span><span class="label">Початок:</span><span class="val">{dt_start}</span></div>
  <div class="row"><span class="icon">🕔</span><span class="label">Кінець:</span><span class="val">{dt_end}</span></div>
  {'<div class="row"><span class="icon">👤</span><span class="label">Організатор:</span><span class="val">' + organizer + '</span></div>' if organizer else ''}
  {'<div class="row"><span class="icon">📍</span><span class="label">Місце:</span><span class="val">' + location + '</span></div>' if location and 'zoom' not in location.lower() else ''}
  {'<hr class="sep"><div style="font-size:12px;color:#9aafbe;margin-bottom:6px">Учасники:</div><div>' + attendees_html + '</div>' if attendees_html else ''}
  {'<hr class="sep"><div class="desc">' + desc_html + '</div>' if desc_html else ''}
  {zoom_btn}
</div>
</body></html>'''


def _resolve_cid_images(msg_obj, html: str) -> str:
    """Replace cid: img references with real URLs or base64 data URIs."""
    import re, base64
    cid_pattern = re.compile(r'cid:([^\s"\'<>&]+)', re.IGNORECASE)

    # Phase 1: stored attachments with content_id field
    remaining = {m.group(1).lower() for m in cid_pattern.finditer(html)}
    for i, att in enumerate(msg_obj.attachments or []):
        raw_cid = att.get('content_id', '').strip('<>')
        if not raw_cid:
            continue
        key = raw_cid.lower()
        if key in remaining:
            html = re.sub(r'(?i)cid:' + re.escape(raw_cid),
                          f'/email/message/{msg_obj.pk}/attachment/{i}/', html)
            remaining.discard(key)
    if not remaining:
        return html

    # Phase 2: IMAP fallback — fetch raw email, extract CID parts as base64 data URIs
    try:
        import email as email_lib
        from email_assistant.imap_client import IMAPClient
        imap_uid = msg_obj.imap_uid
        if not imap_uid:
            return html
        account = msg_obj.account
        imap_folder = msg_obj.imap_folder_name or account.imap_folder_inbox
        cid_map = {}
        with IMAPClient(account) as imap:
            imap.select_folder(imap_folder)
            _, raw_data = imap.conn.uid('fetch', str(imap_uid).encode(), '(RFC822)')
        if raw_data and isinstance(raw_data[0], tuple):
            parsed = email_lib.message_from_bytes(raw_data[0][1])
            for part in parsed.walk():
                part_cid = str(part.get('Content-ID', '')).strip().strip('<>').lower()
                if part_cid and part.get_content_maintype() == 'image':
                    data = part.get_payload(decode=True)
                    if data:
                        ct = part.get_content_type()
                        cid_map[part_cid] = f'data:{ct};base64,{base64.b64encode(data).decode()}'
        if cid_map:
            html_new = cid_pattern.sub(
                lambda m: cid_map.get(m.group(1).lower(), m.group(0)), html)
            if html_new != html:
                html = html_new
                # Cache: avoid IMAP on subsequent views
                try:
                    msg_obj.body_html = html
                    msg_obj.save(update_fields=['body_html'])
                except Exception:
                    pass
    except Exception as e:
        logger.warning('CID resolve failed msg=%s: %s', msg_obj.pk, e)
    return html


def _sanitize_quote_html(raw_html):
    import re as _re
    if not raw_html:
        return ''
    raw_html = _re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=_re.IGNORECASE | _re.DOTALL)
    raw_html = _re.sub(r'<style[^>]*>.*?</style>',   '', raw_html, flags=_re.IGNORECASE | _re.DOTALL)
    raw_html = _re.sub(r'<link\b[^>]*/?>',           '', raw_html, flags=_re.IGNORECASE)
    raw_html = _re.sub(r'<meta\b[^>]*/?>',           '', raw_html, flags=_re.IGNORECASE)
    return raw_html.strip()


def _build_quote_html(header_html, source_html, fallback_text):
    import html as _html
    inner = _sanitize_quote_html(source_html) if source_html else ''
    if not inner:
        inner = f'<pre style="white-space:pre-wrap;margin:0">{_html.escape(fallback_text[:4000])}</pre>'
    return (
        f'<div style="color:var(--text-dim);font-size:12px;margin:8px 0 4px">{header_html}</div>'
        f'<blockquote style="border-left:3px solid var(--border-strong);padding:6px 12px;'
        f'margin:0 0 0 4px;font-size:13px;background:rgba(128,128,128,.06);'
        f'border-radius:0 4px 4px 0;color:var(--text-muted)">'
        f'{inner}</blockquote>'
    )


@staff_member_required
def compose_view(request):
    import html as _html
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
            'to':         reply_to.from_email,
            'subject':    subj,
            'quote':      f'\n\n--- Оригінальний лист ---\nВід: {reply_to.from_email}\n{reply_to.body_text[:2000]}',
            'quote_html': _build_quote_html(
                f'--- Оригінальний лист від {_html.escape(reply_to.from_email)} ---',
                reply_to.body_html,
                reply_to.body_text,
            ),
        }
    elif forward_pk:
        from email_assistant.models import EmailMessage
        fwd  = get_object_or_404(EmailMessage, pk=forward_pk, account=account)
        subj = fwd.subject
        if not subj.lower().startswith('fwd:'):
            subj = f'Fwd: {subj}'
        to_str = _html.escape(', '.join(fwd.to_emails))
        initial = {
            'subject':    subj,
            'quote':      f'\n\n--- Переслано ---\nВід: {fwd.from_email}\nКому: {", ".join(fwd.to_emails)}\n{fwd.body_text[:2000]}',
            'quote_html': _build_quote_html(
                f'--- Переслано від {_html.escape(fwd.from_email)} / Кому: {to_str} ---',
                fwd.body_html,
                fwd.body_text,
            ),
        }

    if request.method == 'POST':
        return _handle_send(request, account, reply_to)

    sig = _get_signature(account)

    return render(request, 'email_assistant/compose.html', _ctx(request, {
        'title':        'Новий лист' if not reply_to else 'Відповідь',
        'account':      account,
        'reply_to':     reply_to,
        'initial':      initial,
        'signature':    sig,
        'crm_contacts': json.dumps(_crm_contacts(request.user)),
    }))


def _handle_send(request, account, reply_to=None):
    from email_assistant.smtp_client import SMTPClient
    from email_assistant.models import EmailMessage

    to_raw  = request.POST.get('to', '')
    cc_raw  = request.POST.get('cc', '')
    subject = request.POST.get('subject', '')
    body      = request.POST.get('body', '')
    body_html = request.POST.get('body_html', '').strip()

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
        body_html=body_html or '',
        cc_emails=cc_list, reply_to_message=reply_to, attachments=attachments,
    )

    if result['ok']:
        try:
            thread = reply_to.thread if reply_to else None
            EmailMessage.objects.create(
                account=account, thread=thread, folder='sent',
                message_id=result.get('message_id', ''),
                subject=subject, from_email=account.email_address,
                from_name=account.display_name, to_emails=to_list, cc_emails=cc_list,
                body_text=body, is_read=True, sent_at=timezone.now(),
            )
        except Exception as e:
            logger.warning('store sent message: %s', e)
        _save_contacts(request.user, to_list + cc_list)

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
    from email_assistant.ai_helper import generate_reply

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    profile = getattr(request.user, 'profile', None)

    thread_messages = [msg]
    if msg.thread:
        thread_messages = list(msg.thread.messages.order_by('sent_at')[:10])

    reply = generate_reply(thread_messages, account, profile)
    msg.ai_reply_draft = reply
    msg.save(update_fields=['ai_reply_draft'])
    return JsonResponse({'ok': bool(reply), 'reply': reply,
                         'error': '' if reply else 'AI не повернув відповідь'})


@staff_member_required
def ai_translate(request, message_pk):
    from email_assistant.models import EmailMessage, EmailSettings
    from email_assistant.ai_helper import translate_email

    account  = _get_account(request)
    msg      = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    settings = EmailSettings.get_for_user(request.user)
    target   = request.GET.get('lang', settings.ai_translate_to or 'uk')
    profile  = getattr(request.user, 'profile', None)

    translation = translate_email(msg.body_text or '', target, profile)
    if translation:
        msg.ai_translated   = translation
        msg.ai_translate_to = target
        msg.save(update_fields=['ai_translated', 'ai_translate_to'])
    return JsonResponse({'ok': bool(translation), 'translation': translation,
                         'lang': target})


@staff_member_required
@require_POST
def translate_text_view(request):
    """Translate arbitrary text (e.g. reply draft) to target language."""
    import json
    from email_assistant.ai_helper import translate_email
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})
    text   = (data.get('text') or '').strip()
    target = data.get('lang') or 'uk'
    if not text:
        return JsonResponse({'ok': False, 'error': 'Текст порожній'})
    profile = getattr(request.user, 'profile', None)
    translation = translate_email(text, target, profile)
    return JsonResponse({'ok': bool(translation), 'translation': translation or '', 'lang': target})


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
def list_imap_folders_view(request):
    """Return JSON list of IMAP folders from the server."""
    account = _get_account(request)
    if not account:
        return JsonResponse({'folders': []})
    try:
        from email_assistant.imap_client import IMAPClient
        with IMAPClient(account) as imap:
            folders = imap.list_folders()
        return JsonResponse({'folders': folders})
    except Exception as e:
        logger.warning('list_imap_folders failed: %s', e)
        return JsonResponse({'folders': [], 'error': str(e)})


@staff_member_required
@require_POST
def sync_imap_folder_view(request):
    """Sync messages from a specific IMAP folder name into DB, then return count."""
    from email_assistant.models import EmailMessage

    account    = _get_account(request)
    imap_folder = request.POST.get('imap_folder', '').strip()
    if not account or not imap_folder:
        return JsonResponse({'ok': False, 'error': 'Параметри відсутні'})

    try:
        from email_assistant.imap_client import IMAPClient
        from email_assistant.models import EmailSettings
        from email_assistant.sync import pull_read_flags, sync_folder
        _lim = None if account.sync_no_limit else account.sync_limit
        with IMAPClient(account) as client:
            new = sync_folder(client, account, imap_folder, limit=_lim)
            if new is None:
                return JsonResponse({'ok': False, 'error': f'Не вдалося відкрити папку {imap_folder}'})
            pull_read_flags(client, account, imap_folder,
                            two_way=EmailSettings.get_for_user(account.user).mark_read_on_server)
        created = len(new)

        count = EmailMessage.objects.filter(
            account=account, imap_folder_name=imap_folder, is_deleted=False,
        ).count()
        return JsonResponse({'ok': True, 'created': created, 'total': count})
    except Exception as e:
        logger.error('sync_imap_folder %s: %s', imap_folder, e)
        return JsonResponse({'ok': False, 'error': str(e)})


@staff_member_required
@require_POST
def sync_all_folders_view(request):
    """Full historical sync: inbox+sent (all time) + all IMAP custom folders."""
    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

    from django.core.management import call_command
    from io import StringIO
    out = StringIO()
    try:
        call_command('sync_email', account=account.pk, stdout=out, stderr=out, **{'all': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})

    # Now also sync every selectable IMAP folder (уся історія, яких листів ще немає)
    folder_results = []
    try:
        from email_assistant.imap_client import IMAPClient
        from email_assistant.models import EmailSettings
        from email_assistant.sync import folders_to_sync, pull_read_flags, sync_folder
        two_way = EmailSettings.get_for_user(account.user).mark_read_on_server
        _lim = None if account.sync_no_limit else account.sync_limit
        with IMAPClient(account) as imap:
            for f in folders_to_sync(imap.list_folders()):
                try:
                    new = sync_folder(imap, account, f['name'], full=True, limit=_lim)
                    if new is None:
                        folder_results.append({'folder': f['path'], 'error': 'не вдалося відкрити'})
                        continue
                    pull_read_flags(imap, account, f['name'], two_way=two_way)
                    folder_results.append({'folder': f['path'], 'created': len(new)})
                except Exception as e:
                    folder_results.append({'folder': f['path'], 'error': str(e)})
                    logger.warning('sync_all_folders %s: %s', f['name'], e)
    except Exception as e:
        logger.warning('imap list_folders in sync_all: %s', e)

    return JsonResponse({'ok': True, 'output': out.getvalue(), 'folders': folder_results})


@staff_member_required
def unread_count_view(request):
    from email_assistant.models import EmailMessage, EmailThread
    account = _get_account(request)
    if not account:
        return JsonResponse({'count': 0, 'inbox': 0, 'starred': 0, 'spam': 0})
    from django.db.models import Q
    base = EmailMessage.objects.filter(account=account, is_deleted=False, is_read=False)
    archived_ids = EmailThread.objects.filter(account=account, is_archived=True).values_list('id', flat=True)
    inbox = base.filter(
        folder='inbox',
    ).filter(
        Q(imap_folder_name='') | Q(imap_folder_name__iexact=account.imap_folder_inbox)
    ).exclude(thread_id__in=archived_ids).count()
    starred = base.filter(is_starred=True).count()
    spam    = base.filter(is_spam=True).count()
    return JsonResponse({'count': inbox, 'inbox': inbox, 'starred': starred, 'spam': spam})


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
        # Папка на сервері — та, де лежить саме цей лист (UID унікальний лише в межах папки)
        from email_assistant.sync import default_imap_folder
        imap_folder = default_imap_folder(account, msg)
        msg.folder = EmailMessage.FOLDER_TRASH
        msg.save(update_fields=['folder'])
        if msg.imap_uid and imap_folder:
            _imap_bg(account.pk, imap_folder, msg.imap_uid, 'trash')

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
def toggle_spam_view(request, message_pk):
    from email_assistant.models import EmailMessage, EmailRule
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    was_spam = msg.is_spam
    msg.is_spam = not msg.is_spam
    msg.folder  = EmailMessage.FOLDER_SPAM if msg.is_spam else EmailMessage.FOLDER_INBOX
    msg.save(update_fields=['is_spam', 'folder'])
    if msg.is_spam and not was_spam and msg.imap_uid:
        from email_assistant.sync import default_imap_folder
        _imap_bg(account.pk, default_imap_folder(account, msg), msg.imap_uid, 'spam')

    auto_rule = None
    if msg.is_spam and msg.from_email and '@' in msg.from_email:
        domain = '@' + msg.from_email.split('@')[-1].lower().rstrip('>')
        _, created = EmailRule.objects.get_or_create(
            account=account,
            condition_field=EmailRule.FIELD_FROM_EMAIL,
            condition_op=EmailRule.OP_CONTAINS,
            condition_value=domain,
            action=EmailRule.ACTION_MARK_SPAM,
            defaults={'name': f'Спам: {domain}'},
        )
        if created:
            auto_rule = domain

    return JsonResponse({'ok': True, 'spam': msg.is_spam, 'auto_rule': auto_rule})


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


@staff_member_required
@require_POST
def ai_grammar_check(request):
    """Check and fix grammar in email body."""
    import json as _json
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})
    text    = data.get('text', '').strip()
    profile = getattr(request.user, 'profile', None)
    from email_assistant.ai_helper import check_grammar
    return JsonResponse(check_grammar(text, profile))


@staff_member_required
def crm_contacts_view(request):
    """Return merged CRM + address-book contacts as JSON."""
    return JsonResponse({'contacts': _crm_contacts(request.user)})


@staff_member_required
@require_POST
def ai_generate_email(request):
    """Generate email subject + body from a natural-language prompt."""
    import json as _json
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})

    prompt = data.get('prompt', '').strip()
    if not prompt:
        return JsonResponse({'ok': False, 'error': 'Введіть опис листа'})

    account = _get_account(request)
    profile = getattr(request.user, 'profile', None)

    from email_assistant.ai_helper import generate_from_prompt
    return JsonResponse(generate_from_prompt(prompt, account, profile))


@staff_member_required
@require_POST
def schedule_email_api(request):
    """Schedule an email for future delivery."""
    import json as _json
    from django.utils.dateparse import parse_datetime
    from django.utils import timezone
    from email_assistant.models import ScheduledEmail

    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})

    to_raw       = data.get('to', '').strip()
    subject      = data.get('subject', '').strip()
    body         = data.get('body', '')
    body_html    = data.get('body_html', '')
    scheduled_at = data.get('scheduled_at', '')

    if not to_raw or not subject or not scheduled_at:
        return JsonResponse({'ok': False,
                             'error': 'Вкажіть отримувача, тему і час відправки'})

    dt = parse_datetime(scheduled_at)
    if not dt:
        return JsonResponse({'ok': False, 'error': 'Невірний формат дати'})
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    if dt <= timezone.now():
        return JsonResponse({'ok': False,
                             'error': 'Час відправки має бути в майбутньому'})

    to_list   = [e.strip() for e in to_raw.split(',') if e.strip()]
    cc_list   = [e.strip() for e in data.get('cc', '').split(',') if e.strip()]

    scheduled = ScheduledEmail.objects.create(
        account=account, subject=subject,
        to_emails=to_list, cc_emails=cc_list,
        body=body, body_html=body_html,
        scheduled_at=dt, trigger='manual',
    )
    _save_contacts(request.user, to_list + cc_list)

    return JsonResponse({
        'ok':          True,
        'id':          scheduled.pk,
        'scheduled_at': dt.strftime('%d.%m.%Y %H:%M'),
    })


# ── Attachment serving ─────────────────────────────────────────────────────

def _is_previewable(content_type: str) -> bool:
    return (content_type.startswith('image/') or
            content_type == 'application/pdf' or
            content_type.startswith('text/'))


@staff_member_required
def attachment_download_view(request, message_pk, index):
    """Serve an email attachment: from saved file or fetched live from IMAP."""
    import os
    from django.conf import settings
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    msg     = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    atts    = msg.attachments or []
    if index >= len(atts):
        return HttpResponse('Вкладення не знайдено', status=404)

    att          = atts[index]
    name         = att.get('name', 'attachment')
    content_type = att.get('content_type', 'application/octet-stream')
    force_dl     = request.GET.get('download') == '1'

    # Try saved file first
    file_path = att.get('file_path', '')
    if file_path:
        abs_path = os.path.join(settings.MEDIA_ROOT, file_path)
        if os.path.exists(abs_path):
            with open(abs_path, 'rb') as fh:
                data = fh.read()
            disp = 'attachment' if force_dl else ('inline' if _is_previewable(content_type) else 'attachment')
            resp = HttpResponse(data, content_type=content_type)
            resp['Content-Disposition'] = f'{disp}; filename="{name}"'
            return resp

    # Fall back: fetch from IMAP using stored UID
    imap_uid    = msg.imap_uid
    imap_folder = msg.imap_folder_name or account.imap_folder_inbox
    if imap_uid:
        try:
            import email as email_lib
            from email_assistant.imap_client import IMAPClient, _get_attachments_raw, persist_attachments
            data = None
            with IMAPClient(account) as imap:
                imap.select_folder(imap_folder)
                _, raw_data = imap.conn.uid('fetch', str(imap_uid).encode(), '(RFC822)')
                if raw_data and isinstance(raw_data[0], tuple):
                    parsed_msg = email_lib.message_from_bytes(raw_data[0][1])
                    parts = _get_attachments_raw(parsed_msg)
                    if index < len(parts):
                        data = parts[index].get('_data', b'')
                        # Cache to disk for next request
                        try:
                            saved = persist_attachments(account.pk, imap_uid, imap_folder,
                                                        [dict(parts[index])])
                            if saved and saved[0].get('file_path'):
                                att_list = list(msg.attachments)
                                att_list[index] = {**att_list[index], 'file_path': saved[0]['file_path']}
                                msg.attachments = att_list
                                msg.save(update_fields=['attachments'])
                        except Exception:
                            pass
            if data:
                disp = 'attachment' if force_dl else ('inline' if _is_previewable(content_type) else 'attachment')
                resp = HttpResponse(data, content_type=content_type)
                resp['Content-Disposition'] = f'{disp}; filename="{name}"'
                return resp
        except Exception as e:
            logger.warning('attachment IMAP fetch msg=%s idx=%s: %s', message_pk, index, e)

    return HttpResponse('Файл не знайдено. Пересинхронізуйте пошту.', status=404)


# ── IMAP folder management ──────────────────────────────────────────────────

@staff_member_required
@require_POST
def imap_create_folder_view(request):
    account = _get_account(request)
    name = request.POST.get('name', '').strip()
    if not account or not name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву папки'})
    try:
        from email_assistant.imap_client import IMAPClient
        with IMAPClient(account) as imap:
            result = imap.create_folder(name)
        if result is True:
            return JsonResponse({'ok': True})
        return JsonResponse({'ok': False, 'error': str(result) if result else 'Сервер відхилив запит'})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@staff_member_required
@require_POST
def imap_rename_folder_view(request):
    account = _get_account(request)
    old_name = request.POST.get('old_name', '').strip()
    new_name = request.POST.get('new_name', '').strip()
    if not account or not old_name or not new_name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть стару і нову назву'})
    try:
        from email_assistant.imap_client import IMAPClient
        with IMAPClient(account) as imap:
            result = imap.rename_folder(old_name, new_name)
        if result is True:
            from email_assistant.models import EmailMessage
            EmailMessage.objects.filter(
                account=account, imap_folder_name=old_name
            ).update(imap_folder_name=new_name)
            return JsonResponse({'ok': True})
        return JsonResponse({'ok': False, 'error': str(result) if result else 'Сервер відхилив запит'})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@staff_member_required
@require_POST
def imap_delete_folder_view(request):
    account = _get_account(request)
    name = request.POST.get('name', '').strip()
    if not account or not name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву папки'})
    try:
        from email_assistant.imap_client import IMAPClient
        with IMAPClient(account) as imap:
            result = imap.delete_folder(name)
        if result is True:
            from email_assistant.models import EmailMessage
            EmailMessage.objects.filter(
                account=account, imap_folder_name=name
            ).update(imap_folder_name='')
            return JsonResponse({'ok': True})
        return JsonResponse({'ok': False, 'error': str(result) if result else 'Сервер відхилив запит'})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


# ── Email export / import ───────────────────────────────────────────────────

@staff_member_required
def export_emails_view(request):
    """Download all emails as .eml files in a zip archive."""
    import zipfile, io, email as email_lib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email_assistant.models import EmailMessage

    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

    folder_filter = request.GET.get('folder')
    qs = EmailMessage.objects.filter(account=account, is_deleted=False)
    if folder_filter:
        qs = qs.filter(imap_folder_name=folder_filter)
    qs = qs.order_by('-sent_at')[:500]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for msg in qs:
            if msg.body_html:
                eml = MIMEMultipart('alternative')
                eml.attach(MIMEText(msg.body_text or '', 'plain', 'utf-8'))
                eml.attach(MIMEText(msg.body_html, 'html', 'utf-8'))
            else:
                eml = MIMEText(msg.body_text or '', 'plain', 'utf-8')
            eml['Subject'] = msg.subject or '(no subject)'
            eml['From']    = msg.from_email or ''
            eml['To']      = ', '.join(msg.to_emails or [])
            if msg.cc_emails:
                eml['Cc'] = ', '.join(msg.cc_emails)
            if msg.message_id:
                eml['Message-ID'] = msg.message_id
            if msg.sent_at:
                from email.utils import format_datetime
                eml['Date'] = format_datetime(msg.sent_at)
            safe = ''.join(c for c in (msg.subject or 'email')[:40] if c.isalnum() or c in ' -_')
            fname = f"{msg.pk}_{safe}.eml"
            zf.writestr(fname, eml.as_string())

    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/zip')
    resp['Content-Disposition'] = 'attachment; filename="emails_export.zip"'
    return resp


@staff_member_required
@require_POST
def mark_read_api(request, message_pk):
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    set_read(account, [msg], True)
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def mark_unread_api(request, message_pk):
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    set_read(account, [msg], False)
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def set_category_view(request, message_pk):
    """POST {category, scope: sender|domain}: віднести лист до категорії й запам'ятати для контакту."""
    import json as _json
    from email_assistant.categorize import remember_category
    from email_assistant.models import EmailMessage
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})
    category = data.get('category') or ''
    if category and category not in dict(EmailMessage.CATEGORY_CHOICES):
        return JsonResponse({'ok': False, 'error': 'Невідома категорія'})
    key, changed = remember_category(msg, category, 'domain' if data.get('scope') == 'domain' else 'sender')
    if key is None:
        return JsonResponse({'ok': False, 'error': 'Внутрішній лист — немає зовнішнього співрозмовника'})
    msg.refresh_from_db(fields=['category'])
    return JsonResponse({'ok': True, 'key': key, 'changed': changed,
                         'category': msg.category, 'label': msg.get_category_display()})


_SETTINGS_TOGGLES = {'mark_read_on_server'}


@staff_member_required
@require_POST
def settings_toggle_view(request):
    """POST {field, value}: швидкі перемикачі EmailSettings з інтерфейсу пошти."""
    import json as _json
    from email_assistant.models import EmailSettings
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})
    field = data.get('field')
    if field not in _SETTINGS_TOGGLES:
        return JsonResponse({'ok': False, 'error': 'Невідоме налаштування'})
    es = EmailSettings.get_for_user(request.user)
    setattr(es, field, bool(data.get('value')))
    es.save(update_fields=[field])
    return JsonResponse({'ok': True, field: getattr(es, field)})


@staff_member_required
def rules_list(request):
    """Return JSON list of email rules for this account."""
    from email_assistant.models import EmailRule
    account = _get_account(request)
    if not account:
        return JsonResponse({'rules': []})
    rules = list(account.rules.filter(is_active=True).values(
        'id', 'name', 'condition_field', 'condition_op',
        'condition_value', 'action', 'action_value', 'is_active',
    ))
    return JsonResponse({'rules': rules})


@staff_member_required
@require_POST
def create_rule_api(request):
    """Create an EmailRule from POST data."""
    import json as _json
    from email_assistant.models import EmailRule
    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})
    name = data.get('name', '').strip()
    cond_field = data.get('condition_field', 'from_email')
    cond_op    = data.get('condition_op',    'contains')
    cond_val   = data.get('condition_value', '').strip()
    action     = data.get('action',          'mark_read')
    action_val = data.get('action_value',    '').strip()
    if not name or not cond_val:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву і значення умови'})
    rule = EmailRule.objects.create(
        account=account, name=name,
        condition_field=cond_field, condition_op=cond_op, condition_value=cond_val,
        action=action, action_value=action_val,
    )
    # Retroactively apply spam rule to existing inbox messages
    if action == EmailRule.ACTION_MARK_SPAM:
        from email_assistant.models import EmailMessage
        for msg in EmailMessage.objects.filter(account=account, folder='inbox', is_spam=False, is_deleted=False).iterator():
            if rule.matches(msg):
                rule.apply_to(msg)
    return JsonResponse({'ok': True, 'id': rule.pk})


@staff_member_required
@require_POST
def bulk_action_view(request):
    """Bulk actions on multiple messages: read/unread/delete/archive/spam."""
    import json as _json
    from email_assistant.models import EmailMessage, EmailThread
    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'})
    action = data.get('action', '')
    pks = [int(p) for p in (data.get('pks') or []) if str(p).isdigit()]
    if not pks or action not in ('read', 'unread', 'delete', 'archive', 'spam', 'unspam'):
        return JsonResponse({'ok': False, 'error': 'Невірні параметри'})
    msgs = EmailMessage.objects.filter(pk__in=pks, account=account)
    if action in ('read', 'unread'):
        set_read(account, list(msgs), action == 'read')
    elif action == 'delete':
        # Як одиночне видалення: спершу в Кошик (і в Кошик на сервері), з Кошика — назовсім
        from email_assistant.sync import default_imap_folder
        to_trash = list(msgs.exclude(folder=EmailMessage.FOLDER_TRASH))
        _imap_bg_many(account, [(default_imap_folder(account, m), m.imap_uid)
                                for m in to_trash if m.imap_uid], 'trash')
        msgs.filter(folder=EmailMessage.FOLDER_TRASH).update(is_deleted=True)
        EmailMessage.objects.filter(pk__in=[m.pk for m in to_trash]).update(folder=EmailMessage.FOLDER_TRASH)
    elif action == 'spam':
        from email_assistant.sync import default_imap_folder
        new_spam = list(msgs.filter(is_spam=False))
        _imap_bg_many(account, [(default_imap_folder(account, m), m.imap_uid)
                                for m in new_spam if m.imap_uid], 'spam')
        msgs.update(is_spam=True, folder=EmailMessage.FOLDER_SPAM)
    elif action == 'unspam':
        msgs.filter(is_spam=True).update(is_spam=False, folder=EmailMessage.FOLDER_INBOX)
    elif action == 'archive':
        tpk_set = set(msgs.exclude(thread=None).values_list('thread_id', flat=True))
        if tpk_set:
            EmailThread.objects.filter(pk__in=tpk_set, account=account).update(is_archived=True)
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def delete_rule(request, rule_pk):
    from email_assistant.models import EmailRule
    account = _get_account(request)
    rule = get_object_or_404(EmailRule, pk=rule_pk, account=account)
    rule.delete()
    return JsonResponse({'ok': True})


@staff_member_required
@require_POST
def create_rule_from_message(request, message_pk):
    """Quick-create a rule from a message's sender."""
    import json as _json
    from email_assistant.models import EmailMessage, EmailRule
    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    action     = data.get('action',     'mark_read')
    action_val = data.get('action_value','')
    cond_field = data.get('condition_field', 'from_email')
    cond_op    = data.get('condition_op',    'contains')
    cond_val   = data.get('condition_value', msg.from_email)
    name = data.get('name') or f'Від {msg.from_name or msg.from_email}'
    rule = EmailRule.objects.create(
        account=account, name=name[:200],
        condition_field=cond_field, condition_op=cond_op,
        condition_value=cond_val[:500],
        action=action, action_value=action_val[:200],
    )
    return JsonResponse({'ok': True, 'id': rule.pk, 'name': rule.name})


@staff_member_required
@require_POST
def create_task_from_email(request, message_pk):
    """Create a CalendarEvent task from an email."""
    import json as _json
    from email_assistant.models import EmailMessage
    from calendar_app.models import CalendarEvent
    from django.utils.dateparse import parse_datetime
    from django.utils import timezone as tz

    account = _get_account(request)
    msg = get_object_or_404(EmailMessage, pk=message_pk, account=account)
    try:
        data = _json.loads(request.body or b'{}')
    except ValueError:
        data = {}

    title = (data.get('title') or msg.subject or 'Завдання з листа')[:300]
    desc  = (data.get('description') or '')[:500]
    dt_str = data.get('start_at', '')
    try:
        dt = parse_datetime(dt_str)
        if dt is None:
            raise ValueError
        if tz.is_naive(dt):
            dt = tz.make_aware(dt)
    except Exception:
        dt = tz.now().replace(hour=9, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        dt += timedelta(days=1)

    ev = CalendarEvent.objects.create(
        user=request.user,
        title=title,
        description=desc,
        event_type=CalendarEvent.TYPE_EMAIL,
        start_at=dt,
        email_message=msg,
        remind_minutes_before=60,
    )
    return JsonResponse({'ok': True, 'event_id': ev.pk, 'title': ev.title,
                         'start': ev.start_at.strftime('%d.%m.%Y %H:%M')})


@staff_member_required
@require_POST
def import_emails_view(request):
    """Import .eml files (or .zip of .emls) into the account."""
    import zipfile, io, email as email_lib
    from email.utils import parseaddr, parsedate_to_datetime
    from email.header import decode_header as _dh
    from email_assistant.models import EmailMessage, EmailThread

    account = _get_account(request)
    if not account:
        return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

    def _ds(raw):
        if not raw:
            return ''
        parts = _dh(raw)
        result = []
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                result.append(chunk.decode(enc or 'utf-8', errors='replace'))
            else:
                result.append(str(chunk))
        return ' '.join(result).strip()

    def _save_eml(raw_bytes):
        try:
            msg = email_lib.message_from_bytes(raw_bytes)
            msg_id = (msg.get('Message-ID') or '').strip()
            subject = _ds(msg.get('Subject', ''))
            from_name, from_email = parseaddr(msg.get('From', ''))
            from_name  = _ds(from_name)
            from_email = from_email.lower()
            from email_assistant.imap_client import parse_addresses
            to_list    = parse_addresses(msg.get('To', ''))
            cc_list    = parse_addresses(msg.get('Cc', ''))
            try:
                from django.utils import timezone as tz
                from datetime import timezone as dt_tz
                sent_at = parsedate_to_datetime(msg.get('Date', ''))
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=dt_tz.utc)
            except Exception:
                from django.utils import timezone as tz
                sent_at = tz.now()
            if msg_id and EmailMessage.objects.filter(account=account, message_id=msg_id).exists():
                return False
            from email_assistant.imap_client import _get_body
            text_body, html_body = _get_body(msg)
            thread_key = msg.get('In-Reply-To', '').strip() or msg_id or subject
            thread = EmailThread.objects.filter(account=account, thread_id=thread_key[:500]).first()
            if not thread:
                thread = EmailThread.objects.create(
                    account=account, thread_id=thread_key[:500] if thread_key else '',
                    subject=subject[:500], participants=[from_email] + to_list,
                )
            EmailMessage.objects.create(
                account=account, thread=thread, folder='inbox',
                message_id=msg_id, subject=subject,
                from_email=from_email, from_name=from_name,
                to_emails=to_list, cc_emails=cc_list, body_text=text_body, body_html=html_body,
                is_read=True, sent_at=sent_at,
            )
            return True
        except Exception as e:
            logger.warning('import_eml failed: %s', e)
            return False

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'ok': False, 'error': 'Файл не вибрано'})

    created = 0
    name = uploaded.name.lower()
    raw = uploaded.read()

    if name.endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for fname in zf.namelist():
                    if fname.lower().endswith('.eml'):
                        if _save_eml(zf.read(fname)):
                            created += 1
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Помилка zip: {e}'})
    elif name.endswith('.eml'):
        if _save_eml(raw):
            created = 1
    else:
        return JsonResponse({'ok': False, 'error': 'Підтримуються файли .eml або .zip'})

    return JsonResponse({'ok': True, 'created': created})
