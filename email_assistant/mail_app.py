"""
email_assistant/mail_app.py — 📱 додаток «Minerva Пошта» (Android, Flutter: mobile/mail_app): підключення телефону
QR-кодом, ключ телефону й дані для екранів додатка. HTTP — email_assistant/app_api.py (/email/phone/api/…), сторінка
підключення — /email/phone/ (app_api.page_view).

Своєї логіки пошти тут немає: список — ті самі _build_qs / filter_messages / мітки рядка, що скринька (views.py),
дії (прочитано, ⭐, спам, кошик, відповідь, чернетки, AI, агент) — ті самі функції веб-пошти: app_api викликає їх
від імені людини, під якою підключено телефон. Скринька — та сама, що у веб-пошті (основна активна, account_for).

  new_pairing(user, base_url) — одноразовий код (PAIR_TTL) → текст QR {"minerva": "mail", "v": 1, "url", "code"};
  pair(code, name, version, ip) — телефон показав код → MailAppDevice + ключ (лише раз; у базі — SHA-256);
  pair_state(user, key)        — чи вже підключили телефон за цим кодом (сторінка опитує);
  device_for(header, ip)       — «Authorization: Bearer mvm_…» → живий телефон активного співробітника;
  meta(user, account, device)  — хто, скринька, підпис, права, папки з непрочитаними, версія додатка;
  nav(user, account)           — свої категорії, типи співрозмовників, клієнти, проєкти, запити, «⏰ Чекаю реакції»;
  messages(account, params)    — список листів (або гілок) з фільтрами й сторінками; drafts(account, params);
  conversation(account, msg, user) — гілка листа для екрана листа: тексти без цитат, вкладення, зв'язки, агент…;
  compose(account, msg, mode)  — поля відповіді / пересилання (reply | all | forward);
  app_html(account, msg)       — HTML листа для перегляду в додатку (картинки cid: усередині, до HTML_INLINE_MAX).
"""
import base64
import hashlib
import json
import logging
import re
import secrets

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('email_assistant')

TOKEN_PREFIX = 'mvm_'
PAIR_TTL = 600                 # QR підключення діє 10 хв
PAIR_PER_HOUR = 20             # спроб підключення з однієї адреси за годину
SEEN_EVERY = 60                # «востаннє на зв'язку» — не частіше
PAGE = 30                      # листів на сторінку (як у скриньці)
HTML_INLINE_MAX = 3 * 1024 * 1024   # картинки cid: усередині HTML листа для додатка — разом не більше
APP_VERSION = '1.0.0'          # остання версія додатка — сторінка підключення й «є нова версія» в додатку
APP_TAG = f'mail-app-v{APP_VERSION}'
APP_URL = getattr(settings, 'MINERVA_MAIL_APP_URL',
                  f'https://github.com/Akustik13/minerva-bi/releases/download/{APP_TAG}/minerva-mail.apk')
RELEASES_URL = getattr(settings, 'MINERVA_MAIL_RELEASES_URL',
                       f'https://github.com/Akustik13/minerva-bi/releases/tag/{APP_TAG}')
ATT_REF_RE = re.compile(r'^/email/message/(\d+)/attachment/(\d+)/$')


class PairError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash(value) -> str:
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()


def account_for(user):
    """Скринька людини — та сама, що у веб-пошті (views._get_account): основна активна."""
    from email_assistant.models import EmailAccount
    if user is None or not user.is_authenticated:
        return None
    return EmailAccount.objects.filter(user=user, is_active=True).order_by('-is_primary').first()


def site_base(request) -> str:
    """Адреса Minerva для телефона: «Налаштування системи → домен сайту», інакше — як відкрили сторінку."""
    try:
        from config.models import SystemSettings
        s = SystemSettings.objects.filter(pk=1).first()
        if s is not None and s.site_domain:
            return s.site_url.rstrip('/')
    except Exception:  # noqa: BLE001
        pass
    return request.build_absolute_uri('/').rstrip('/')


# ── 🔗 Підключення телефону ──

def new_pairing(user, base_url) -> dict:
    from core.shared_cache import cache
    code = secrets.token_urlsafe(24)
    key = _hash(code)
    cache.set(f'mail-app-pair:{key}', {'user': user.pk}, PAIR_TTL)
    qr = json.dumps({'minerva': 'mail', 'v': 1, 'url': base_url.rstrip('/'), 'code': code},
                    ensure_ascii=False, separators=(',', ':'))
    return {'key': key[:16], 'qr': qr, 'code': code, 'expires_in': PAIR_TTL, 'url': base_url.rstrip('/')}


def pair_state(user, key) -> dict:
    """Сторінка опитує: {paired: False}, поки телефон не показав код; {paired: True, device} — підключено."""
    from core.shared_cache import cache
    key = str(key or '')[:16]
    if not re.fullmatch(r'[0-9a-f]{16}', key):
        return {'paired': False, 'expired': True}
    state = cache.get(f'mail-app-paired:{key}')
    if not state or state.get('user') != user.pk:
        return {'paired': False}
    return {'paired': True, 'device': state.get('device') or ''}


def pair(code, name='', version='', ip=None):
    """→ (MailAppDevice, ключ). Код — одноразовий, PAIR_TTL; з однієї адреси — PAIR_PER_HOUR спроб на годину;
    людина — активний співробітник зі своєю скринькою в Minerva."""
    from django.contrib.auth import get_user_model
    from core.shared_cache import cache
    from email_assistant.models import MailAppDevice
    hour = timezone.now().strftime('%Y%m%d%H')
    rate_key = f'mail-app-pair-ip:{ip or "-"}:{hour}'
    cache.add(rate_key, 0, 3700)
    try:
        tries = cache.incr(rate_key)
    except ValueError:
        tries = 1
    if tries > PAIR_PER_HOUR:
        raise PairError('Забагато спроб підключення — спробуйте за годину.', 429)
    code = str(code or '').strip()
    key = _hash(code)
    data = cache.get(f'mail-app-pair:{key}') if code else None
    if not data or not cache.add(f'mail-app-pair-used:{key}', 1, PAIR_TTL + 60):
        raise PairError('QR-код недійсний або застарів — на сторінці «📱 Пошта на телефоні» в Minerva покажіть '
                        'новий.', 410)
    cache.delete(f'mail-app-pair:{key}')
    user = get_user_model().objects.filter(pk=data.get('user'), is_active=True, is_staff=True).first()
    if user is None:
        raise PairError('Цей акаунт Minerva не активний.', 403)
    if account_for(user) is None:
        raise PairError('У цього акаунта Minerva немає скриньки пошти — додайте її в «Email → Акаунти».', 403)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = MailAppDevice.objects.create(
        user=user, name=' '.join(str(name or '').split())[:120] or 'Android',
        app_version=str(version or '')[:20], token_hash=_hash(token), token_hint=token[-4:],
        last_seen_at=timezone.now(), last_ip=ip or None)
    cache.set(f'mail-app-paired:{key[:16]}', {'user': user.pk, 'device': device.name}, PAIR_TTL + 300)
    return device, token


def device_for(header, ip=None, version=''):
    """«Bearer mvm_…» → MailAppDevice (з user) або None: ключ невідомий / відключений, людина не активна."""
    from email_assistant.models import MailAppDevice
    m = re.match(r'^\s*(?:Bearer|Token)\s+(\S+)\s*$', str(header or ''))
    if not m or not m.group(1).startswith(TOKEN_PREFIX):
        return None
    device = (MailAppDevice.objects.select_related('user')
              .filter(token_hash=_hash(m.group(1)), revoked_at=None).first())
    if device is None or not device.user.is_active or not device.user.is_staff:
        return None
    now = timezone.now()
    version = str(version or '')[:20]
    if device.last_seen_at is None or (now - device.last_seen_at).total_seconds() > SEEN_EVERY \
            or (ip and device.last_ip != ip) or (version and version != device.app_version):
        fields = {'last_seen_at': now, 'last_ip': ip or None}
        if version:
            fields['app_version'] = version
        MailAppDevice.objects.filter(pk=device.pk).update(**fields)
    return device


def devices_for(user, everyone=False):
    from email_assistant.models import MailAppDevice
    qs = MailAppDevice.objects.select_related('user')
    return qs if everyone else qs.filter(user=user)


def revoke(device):
    from email_assistant.models import MailAppDevice
    MailAppDevice.objects.filter(pk=device.pk, revoked_at=None).update(revoked_at=timezone.now())


# ── Дані для екранів додатка ──

def _iso(dt) -> str:
    return timezone.localtime(dt).isoformat() if dt else ''


def _person(u) -> str:
    return (u.get_full_name() or u.username) if u is not None else ''


def _int(value, default, low, high) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _own(account, m) -> bool:
    return m.folder == 'sent' or (m.from_email or '').strip().lower() == (account.email_address or '').lower()


def _cat_json(c) -> dict:
    return {'id': c.pk, 'name': c.name, 'icon': c.icon or '', 'color': c.color or ''}


def _signature(account) -> dict:
    from email_assistant.imap_client import html_to_text
    from email_assistant.views import _get_signature
    sig = _get_signature(account)
    if not sig:
        return {'html': '', 'text': ''}
    from email_assistant.html_clean import clean_html
    is_html = bool(re.search(r'<[a-zA-Z][^>]*>', sig))
    return {'html': clean_html(sig) if is_html else '', 'text': html_to_text(sig) if is_html else sig}


def perms(user) -> dict:
    return {
        'agent_rule': user.has_perm('agent.add_agentrule'),
        'agent_send': user.has_perm('agent.change_agentrule'),
        'agent_task': user.has_perm('agent.add_agenttask'),
        'category_add': user.has_perm('email_assistant.add_mailcategory'),
        'category_change': user.has_perm('email_assistant.change_mailcategory'),
        'offers': user.has_perm('offers.view_offer'),
        'offers_add': user.has_perm('offers.add_offer'),
    }


def folders(account) -> list:
    """Папки скриньки з непрочитаними — як ліва колонка (views.FOLDERS, _unread_counts)."""
    from email_assistant import drafts as _drafts
    from email_assistant.views import FOLDERS, _unread_counts
    unread = _unread_counts(account)
    out = []
    for key, label, icon in FOLDERS:
        n = _drafts.count(account) if key == 'drafts' else unread.get(key, 0)
        out.append({'key': key, 'label': label, 'icon': icon, 'unread': n or 0})
    return out


def meta(user, account, device=None) -> dict:
    from email_assistant.models import EmailSettings
    from email_assistant.views import _latest_pk, _rag_flags
    es = EmailSettings.get_for_user(user)
    sig = _signature(account)
    return {
        'user': {'id': user.pk, 'name': _person(user), 'email': user.email},
        'account': {'id': account.pk, 'email': account.email_address,
                    'name': account.display_name or _person(user),
                    'sync_minutes': max(1, account.sync_interval_minutes or 1),
                    'last_sync': _iso(account.last_sync_at)},
        'device': {'id': device.pk, 'name': device.name} if device is not None else None,
        'signature': {**sig, 'auto': bool(es.auto_signature)},
        'perms': perms(user),
        'rag': bool(_rag_flags(user).get('rag_available')),
        'folders': folders(account),
        'latest': _latest_pk(account),
        'translate_to': es.ai_translate_to or 'uk',
        'app': {'version': APP_VERSION, 'url': APP_URL, 'releases': RELEASES_URL},
    }


def nav(user, account) -> dict:
    """Ліва колонка скриньки для додатка: свої категорії (⭐ обране й порядок — як у скриньці), типи
    співрозмовників, клієнти з листами, відкриті проєкти, запити пропозицій, «⏰ Чекаю реакції»."""
    from email_assistant import followups, nav_prefs
    from email_assistant import work_categories as wc
    from email_assistant.models import EmailSettings
    from email_assistant.views import ALL_FOLDERS, _build_qs, _category_counts, _nav_projects, _quote_counts
    qs = _build_qs(account, ALL_FOLDERS)
    kinds, customers, no_category = _category_counts(qs, with_no_category=True)
    es = EmailSettings.get_for_user(user)
    hidden = set(es.hidden_work_categories or [])
    work = nav_prefs.ordered(wc.counts(qs), es, lambda r: f'wc:{r[0].pk}', n_of=lambda r: r[1],
                             name_of=lambda r: r[0].name)
    kinds = nav_prefs.ordered(kinds, es, lambda r: f'kind:{r[0]}', n_of=lambda r: r[2], name_of=lambda r: r[1])
    quotes, quotes_done = _quote_counts(qs)
    fu = followups.counts(account)
    return {
        'categories': [dict(_cat_json(c), n=n, unread=u) for c, n, u in work if c.pk not in hidden],
        'no_category': no_category if work else 0,
        'kinds': [{'key': k, 'label': label, 'n': n, 'unread': u} for k, label, n, u in kinds if n],
        'customers': [{'id': c['id'], 'name': c['name'] or c['email'], 'n': c['n'], 'unread': c['unread']}
                      for c in customers[:40]],
        'projects': [{'id': p['project_id'], 'code': p['project__code'], 'name': p['project__name'],
                      'icon': p['project__icon'] or '', 'n': p['n']} for p in _nav_projects(account, 30)],
        'quotes': quotes, 'quotes_done': quotes_done,
        'followups': {'open': fu.get('open', 0), 'bad': fu.get('bad', 0)},
    }


def _filters(account, params) -> dict:
    """Параметри списку (як адреса скриньки: cat, wcat, customer, project, quote, fu) → аргументи filter_messages."""
    from crm.models import Customer
    from email_assistant.models import EmailMessage, MailCategory
    from email_assistant.views import NO_CATEGORY
    out = {}
    cat = str(params.get('cat') or '')
    if cat in dict(EmailMessage.CATEGORY_CHOICES):
        out['cat'] = cat
    wcat = str(params.get('wcat') or '')
    if wcat == '0':
        out['work'] = NO_CATEGORY
    elif wcat.isdigit():
        out['work'] = MailCategory.objects.filter(pk=int(wcat)).first()
    customer = str(params.get('customer') or '')
    if customer.isdigit():
        out['customer'] = Customer.objects.filter(pk=int(customer)).first()
    project = str(params.get('project') or '')
    if project.isdigit():
        from projects.models import Project
        out['project'] = Project.objects.filter(pk=int(project)).first()
    if params.get('quote') in ('1', 'all'):
        out['quote'], out['quote_all'] = True, params.get('quote') == 'all'
    if params.get('fu') == '1':
        out['fu'] = True
    return {k: v for k, v in out.items() if v}


def messages(account, params) -> dict:
    """Список листів для додатка — те саме, що скринька: папка (inbox, sent, starred, spam, archived, trash або
    папка сервера), пошук q і фільтри — по всіх папках (у Спамі й Кошику — лише в них), grouped=1 — один рядок на
    гілку, page. «📝 Чернетки» (folder=drafts без фільтрів) — drafts()."""
    from django.db.models.functions import Substr
    from email_assistant import followups
    from email_assistant import views as V
    folder = str(params.get('folder') or 'inbox')[:200] or 'inbox'
    q = ' '.join(str(params.get('q') or '').split())[:200]
    page = _int(params.get('page'), 1, 1, 10000)
    flt = _filters(account, params)
    if folder == V.DRAFTS_FOLDER and not flt:
        return drafts(account, params)
    search_all = bool(q or flt) and folder not in ('spam', 'trash')
    qs = V._build_qs(account, V.ALL_FOLDERS if search_all else folder, q)
    qs = V.filter_messages(qs, account, **flt)
    grouped = str(params.get('grouped') or '') == '1'
    if grouped:
        qs = V._group_by_thread(qs)
    start = (page - 1) * PAGE
    rows = list(qs.select_related('thread', 'thread__crm_customer', 'work_category')
                .defer(*V.LIST_DEFER).annotate(body_head=Substr('body_text', 1, V.BODY_HEAD))[start:start + PAGE + 1])
    has_next = len(rows) > PAGE
    emails = rows[:PAGE]
    if grouped:
        V._attach_thread_stats(emails)
    V._attach_crm_customers(emails)
    V._attach_project_tags(emails)
    V._attach_work_cats(emails)
    V._attach_quote_tags(emails)
    followups.attach(emails)
    V._attach_agent_marks(account, emails)
    V._attach_logos(emails)
    V._attach_chips(emails)
    if search_all:
        V._attach_folder_tags(account, emails)
    elif folder == 'trash':
        V._attach_trash_tags(account, emails)
    return {'kind': 'messages', 'folder': folder, 'page': page, 'has_next': has_next,
            'items': [row(account, m) for m in emails]}


def row(account, m) -> dict:
    """Рядок списку: від кого / кому, тема, початок тексту, мітки (свої категорії, клієнт, проєкт, 📝 запит, ⏰,
    🤖 агент, вкладення) і pk листів гілки (дії рядка — над усією гілкою, як у скриньці з групуванням)."""
    customer = getattr(m, 'crm_customer_tag', None)
    fu = getattr(m, 'fu_state', None)
    own = _own(account, m)
    to = list(m.to_emails or [])
    return {
        'id': m.pk, 'thread': m.thread_id, 'folder': m.folder, 'own': own,
        'from': (m.from_name or m.from_email or '').strip(), 'from_email': m.from_email or '',
        'to': to[:5],
        'subject': m.subject or '', 'snippet': ' '.join((getattr(m, 'body_head', '') or '').split())[:200],
        'date': _iso(m.sent_at), 'read': m.is_read, 'starred': m.is_starred, 'importance': m.importance or '',
        'spam': m.is_spam, 'archived': bool(m.thread_id and m.thread.is_archived),
        'attachments': [name for _i, name in getattr(m, 'att_chips', [])],
        'att_more': getattr(m, 'att_more', 0),
        'categories': [_cat_json(c) for c in getattr(m, 'wcats', [])],
        'customer': {'id': customer.pk, 'name': customer.name} if customer is not None else None,
        'projects': [{'id': pk, 'code': code, 'name': name, 'icon': icon or ''}
                     for code, pk, name, icon in getattr(m, 'project_tags', [])],
        'quote': bool(getattr(m, 'quote_request_tag', False)),
        'followup': {'short': fu['short'], 'tone': fu['tone']} if fu else None,
        'agent': bool(getattr(m, 'agent_mark', None)),
        'folder_tag': getattr(m, 'folder_tag', ''),
        'logo': getattr(m, 'logo_domain', '') or '', 'logo_v': getattr(m, 'logo_v', '') or '',
        'thread_total': getattr(m, 'thread_total', 1), 'thread_unread': getattr(m, 'thread_unread', 0 if m.is_read else 1),
        'pks': [int(x) for x in str(getattr(m, 'thread_pks', '') or m.pk).split(',') if x],
    }


def drafts(account, params) -> dict:
    from email_assistant import drafts as _drafts
    from email_assistant.views import _attach_agent_drafts
    q = ' '.join(str(params.get('q') or '').split())[:200]
    page = _int(params.get('page'), 1, 1, 10000)
    start = (page - 1) * PAGE
    rows = list(_drafts.for_account(account, q)[start:start + PAGE + 1])
    items = rows[:PAGE]
    _attach_agent_drafts(account, items)
    return {'kind': 'drafts', 'folder': 'drafts', 'page': page, 'has_next': len(rows) > PAGE,
            'items': [draft_row(d) for d in items]}


def draft_row(d) -> dict:
    from email_assistant import drafts as _drafts
    mark = getattr(d, 'agent_mark', None)
    return {'id': d.pk, 'to': list(d.to_emails or []), 'cc': list(d.cc_emails or []), 'subject': d.subject,
            'snippet': _drafts.preview(d), 'date': _iso(d.updated_at), 'reply_to': d.reply_to_id,
            'attachments': len(d.attachments or []), 'agent': bool(mark),
            'agent_title': (mark or {}).get('title', '') if isinstance(mark, dict) else ''}


def draft_full(account, d) -> dict:
    """Чернетка для форми листа в додатку: текст (без HTML), вкладення-посилання, ❗ / 📬 / ⏰."""
    from email_assistant import drafts as _drafts
    from email_assistant.imap_client import html_to_text
    body = d.body or ''
    if not body.strip() and d.body_html:
        body = html_to_text(d.body_html)
    reply_to = d.reply_to if d.reply_to_id and d.reply_to.account_id == account.pk else None
    return {**draft_row(d), 'body': body, 'has_html': bool(d.body_html),
            'reply_to': reply_to.pk if reply_to else None,
            'attachments': _drafts.chips(d), 'options': dict(d.send_options or {})}


def _attachments(m) -> list:
    out = []
    for i, a in enumerate(m.attachments or []):
        if not isinstance(a, dict):
            continue
        inline = bool(a.get('is_inline') or (a.get('content_id') and
                                             str(a.get('content_type') or '').startswith('image/')))
        out.append({'i': i, 'name': a.get('name') or 'file', 'size': a.get('size') or 0,
                    'type': a.get('content_type') or '', 'inline': inline})
    return out


def message_json(account, m) -> dict:
    from email_assistant.quotes import split_quote
    text = m.body_text or ''
    if not text.strip() and m.body_html:
        from email_assistant.imap_client import html_to_text
        text = html_to_text(m.body_html)
    new, quote = split_quote(text)
    mark = getattr(m, 'agent_mark', None)
    return {
        'id': m.pk, 'folder': m.folder, 'own': _own(account, m),
        'from': (m.from_name or m.from_email or '').strip(), 'from_email': m.from_email or '',
        'to': list(m.to_emails or []), 'cc': list(m.cc_emails or []),
        'subject': m.subject or '', 'date': _iso(m.sent_at),
        'text': new, 'quote': quote, 'html': bool(m.body_html),
        'read': m.is_read, 'starred': m.is_starred, 'importance': m.importance or '',
        'imp_text': getattr(m, 'imp_text', ''), 'spam': m.is_spam,
        'attachments': _attachments(m),
        'agent': (mark or {}).get('title', '') if isinstance(mark, dict) else '',
        'logo': getattr(m, 'logo_domain', '') or '', 'logo_v': getattr(m, 'logo_v', '') or '',
    }


def _absolute(base, url) -> str:
    return (base.rstrip('/') + url) if url and url.startswith('/') else (url or '')


def conversation(account, msg, user, base='') -> dict:
    """Гілка листа (або сам лист) для екрана листа; відкриття позначає прочитаним — як прев'ю скриньки."""
    from email_assistant import followups
    from email_assistant import views as V
    from email_assistant.sync import set_read
    thread = msg.thread
    emails = list(thread.messages.filter(is_deleted=False).order_by('sent_at', 'pk')) if thread else [msg]
    if msg not in emails:
        emails.append(msg)
    for m in emails:
        m.fix_html_body()
    unread = [m for m in emails if not m.is_read]
    if unread:
        set_read(account, unread, True)
    V._attach_logos(emails)
    V._attach_importance(emails)
    V._attach_agent_marks(account, emails)
    last = emails[-1]
    to, cc = V._reply_addresses(account, last)
    links = V._mail_links(emails, read=False)
    quote = V._quote_request(thread) if thread else None
    fu = []
    found = followups.for_messages(emails)
    for m in emails:
        if m.pk in found:
            st = followups.state(found[m.pk])
            fu.append({'id': found[m.pk].pk, 'message': m.pk, 'icon': st['icon'], 'text': st['text'],
                       'tone': st['tone'], 'status': found[m.pk].status})
    runs = []
    for run in V._agent_runs(emails):
        runs.append({'message': run.message_id, 'rule': run.rule.name if run.rule_id else '',
                     'status': run.get_status_display(), 'reason': run.reason or '',
                     'done': getattr(run, 'done_text', '') or '', 'draft': run.draft_id})
    tags = {}
    cats = []
    if thread:
        from projects.services import tags_for_threads
        tags = tags_for_threads([thread.pk])
        V._attach_work_cats([last])
        cats = [_cat_json(c) for c in getattr(last, 'wcats', [])]
    customer = thread.crm_customer if thread and thread.crm_customer_id else None
    return {
        'thread': {'id': thread.pk if thread else None, 'subject': (thread.subject if thread else msg.subject) or '',
                   'archived': bool(thread and thread.is_archived),
                   'customer': {'id': customer.pk, 'name': customer.name,
                                'url': _absolute(base, f'/admin/crm/customer/{customer.pk}/change/')}
                   if customer else None,
                   'projects': [{'id': pk, 'code': code, 'name': name, 'icon': icon or '',
                                 'url': _absolute(base, f'/admin/projects/project/{pk}/change/')}
                                for code, pk, name, icon in tags.get(thread.pk, [])] if thread else [],
                   'categories': cats},
        'messages': [message_json(account, m) for m in emails],
        'reply': {'reply_to': last.pk, 'to': to, 'cc': cc, 'subject': V._reply_subject(last.subject)},
        'links': [{'label': l['label'], 'url': _absolute(base, l['url']), 'reason': l.get('reason') or '',
                   'customer': l['customer'].name if l.get('customer') else '',
                   'shipments': [{'tracking': s['tracking'], 'status': s['status'], 'carrier': s['carrier']}
                                 for s in l.get('shipments') or []]} for l in links],
        'quote_request': {'id': quote.pk, 'reason': quote.reason or '',
                          'url': _absolute(base, f'/offers/quick/?thread={thread.pk}')} if quote else None,
        'followups': fu,
        'agent_runs': runs,
        'web_url': _absolute(base, f'/email/?msg={msg.pk}&solo=1'),
    }


def compose(account, msg, mode) -> dict:
    """Поля форми листа: reply — відправнику, all — ще й решті учасників у копію, forward — тема «Fwd:», текст
    пересланого листа й вкладення (посилання msg:<pk>:<i> — файл читає сервер під час відправки)."""
    from email_assistant import reply_quote
    from email_assistant import views as V
    if mode == 'forward':
        subject = msg.subject or ''
        if not re.match(r'\s*(fwd?|wg)\s*:', subject, re.IGNORECASE):
            subject = f'Fwd: {subject}'
        refs = [{'ref': f'msg:{msg.pk}:{a["i"]}', 'name': a['name'], 'size': a['size']}
                for a in _attachments(msg) if not a['inline']]
        return {'mode': 'forward', 'to': '', 'cc': '', 'subject': subject,
                'body': reply_quote.quote_text(msg).lstrip('\n'), 'attachments': refs, 'reply_to': None,
                'with_quote': False}
    to, cc = V._reply_addresses(account, msg)
    return {'mode': mode, 'to': to, 'cc': ', '.join(cc) if mode == 'all' else '',
            'subject': V._reply_subject(msg.subject), 'body': '', 'attachments': [], 'reply_to': msg.pk,
            'with_quote': True, 'quote': reply_quote.quote_text(msg).strip()[:4000]}


def app_html(account, msg) -> str:
    """HTML листа для перегляду в додатку: той самий, що віконце листа у скриньці (views.message_html_view), але
    картинки cid: (підпис, логотип) — data: усередині (до HTML_INLINE_MAX разом), бо WebView додатка не має сесії
    Minerva. Скрипти листа не діють (CSP відповіді і вимкнений JavaScript у WebView)."""
    from email_assistant import views as V
    from email_assistant.attachments import attachment_bytes
    msg.fix_html_body()
    if not msg.body_html:
        return ''
    html = msg.body_html
    if 'cid:' in html.lower():
        html = V._resolve_cid_images(msg, html)
    budget = [HTML_INLINE_MAX]

    def inline(match):
        url = match.group(0)
        m = ATT_REF_RE.match(url)
        if not m or int(m.group(1)) != msg.pk:
            return url
        index = int(m.group(2))
        try:
            data = attachment_bytes(account, msg, index)
        except Exception:  # noqa: BLE001
            data = None
        att = (msg.attachments or [])[index] if index < len(msg.attachments or []) else {}
        ctype = str(att.get('content_type') or '')
        if not data or not ctype.startswith('image/') or len(data) > budget[0]:
            return url
        budget[0] -= len(data)
        return f'data:{ctype};base64,{base64.b64encode(data).decode()}'
    html = re.sub(r'/email/message/\d+/attachment/\d+/', inline, html)
    return V._inject_email_css(html).replace('<base target="_blank">', '<base target="_blank">' + PHONE_FIT, 1)


# лист на вузькому екрані: ширина телефона, картинки не ширші за екран, довгі рядки переносяться
PHONE_FIT = ('<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<style>body{margin:8px;overflow-wrap:anywhere}img{max-width:100%!important;height:auto!important}'
             'pre{white-space:pre-wrap}</style>')
