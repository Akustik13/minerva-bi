"""Підказки адрес для полів «Копія (CC)» / «Кому» — групами за тим, наскільки людина ймовірно потрібна.

  1. 👥 Команда Minerva — скриньки й адреси активних співробітників Minerva (ви — останнім);
  2. 💬 У цьому листуванні — усі, хто писав або був у «Кому» / «Копії» листів гілки (лише ваші скриньки);
  3. 🎯 Можуть бути потрібні — з ким адресат зазвичай буває в одному листі (ваша пошта за `HISTORY_DAYS`),
     учасники проєктів гілки, контакти постачальника й клієнти CRM тієї самої фірми, що адресат;
  4. 📇 Інші адреси — адресна книга, клієнти CRM, контакти постачальників, відправники ваших листів.

Пошук `q` — частина або ціле слово: кожне слово запиту має бути в адресі, імені чи фірмі, спершу ті, де слово
стоїть на початку. Адреса показується лише в першій групі, до якої підходить. Без запиту — групи 1–3 і кілька
найуживаніших з адресної книги. Клієнти CRM, контакти постачальників і учасники проєктів — лише з правом бачити їх.

Використання: `suggest(user, q, message, to, exclude)`; HTTP — `views.address_suggest_view`
(`/email/address-suggest/`), поле — `static/admin/js/mv_addr.js` (атрибут `data-mv-addr`, скрипт підключено в
base_site.html для всіх сторінок). Поля адрес у формах адмінки (налаштування сповіщень, «Копія» доручень і магазину,
«Кому» звітів…) позначаються самі — `tag_field` з `BaseModelAdmin.formfield_for_dbfield` (tabele/admin.py).
"""
import re
from collections import Counter
from datetime import timedelta
from email.utils import parseaddr

from django.db.models import Q
from django.utils import timezone

GROUPS = [
    ('team', '👥 Команда Minerva'),
    ('thread', '💬 У цьому листуванні'),
    ('related', '🎯 Можуть бути потрібні'),
    ('other', '📇 Інші адреси'),
]
LIMITS = {'team': 15, 'thread': 12, 'related': 10, 'other': 15}
OTHER_WITHOUT_QUERY = 6     # без запиту — лише кілька найуживаніших з адресної книги
HISTORY_DAYS = 730          # «з ким адресат буває в одному листі» — ваша пошта за 2 роки
HISTORY_MESSAGES = 300
THREAD_MESSAGES = 200
DB_LIMIT = 40               # кандидатів з бази на одне джерело (далі — перевірка всіх слів запиту)

NOREPLY_RE = re.compile(r'(^|[._+-])(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounces?|'
                        r'notifications?|notify)([._+-]|@)', re.I)
_WORD_SPLIT = re.compile(r'[^\w]+', re.U)

# Поля моделей, у які вписують, КОМУ надсилати (не власна адреса людини чи фірми): назва поля закінчується так.
ADMIN_FIELD_RE = re.compile(r'(^|_)(email_to|emails_to|to_emails|cc_emails|bcc_emails|emails|recipients|cc|bcc|'
                            r'email_cc|forward_to|send_to|mail_to|mail_cc|mail_bcc|report_email|reply_to|to_raw|cc_raw)$')
ADMIN_FIELD_SKIP = re.compile(r'in_reply_to|message_id', re.I)


def tag_field(name: str, formfield):
    """Поле форми адмінки, у яке вписують адресатів, — з підказками адрес (атрибут `data-mv-addr`).
    Одна адреса (EmailField, «Reply-To», «Email для звіту») — `data-mv-addr="one"`. Повертає formfield."""
    from django import forms
    widget = getattr(formfield, 'widget', None)
    if (widget is None or not ADMIN_FIELD_RE.search(name or '') or ADMIN_FIELD_SKIP.search(name or '')
            or not isinstance(widget, (forms.TextInput, forms.Textarea, forms.EmailInput))
            or 'data-mv-addr' in widget.attrs):
        return formfield
    single = isinstance(formfield, forms.EmailField) or name.endswith(('reply_to', 'report_email'))
    widget.attrs['data-mv-addr'] = 'one' if single else ''
    widget.attrs.setdefault('translate', 'no')
    return formfield


def addr(value) -> str:
    """«Anna Muster <Anna@Kunde.de>» → «anna@kunde.de»; не адреса — ''."""
    a = parseaddr(str(value or ''))[1].strip().lower()
    return a if '@' in a and ' ' not in a and len(a) <= 254 else ''


def addresses(text) -> list:
    """Адреси з рядка «a@x.de, Anna <b@y.de>; c@z.de» або списку — без повторів, у тому самому порядку."""
    parts = text if isinstance(text, (list, tuple, set)) else re.split(r'[,;\n]+', str(text or ''))
    out = []
    for p in parts:
        a = addr(p)
        if a and a not in out:
            out.append(a)
    return out


def _domain(a: str) -> str:
    return a.split('@', 1)[1] if '@' in a else ''


def _free(domain: str) -> bool:
    from email_assistant.crm_link import FREE_MAIL_DOMAINS
    return domain in FREE_MAIL_DOMAINS


class Query:
    """Пошуковий запит: кожне слово — частина адреси, імені чи фірми (без огляду на регістр)."""

    def __init__(self, q: str):
        self.text = (q or '').strip().lower()[:100]
        self.words = [w for w in re.split(r'[\s,;]+', self.text) if w]

    def __bool__(self):
        return bool(self.words)

    def matches(self, *fields) -> bool:
        hay = ' '.join(str(f or '') for f in fields).lower()
        return all(w in hay for w in self.words)

    def starts(self, *fields) -> bool:
        """Слово запиту стоїть на початку адреси, імені чи якогось їхнього слова — такі вище."""
        tokens = []
        for f in fields:
            f = str(f or '').lower()
            tokens.append(f)
            tokens.extend(t for t in _WORD_SPLIT.split(f) if t)
        return all(any(t.startswith(w) for t in tokens) for w in self.words)

    def db(self, *fields):
        """Q для бази: найдовше слово запиту в будь-якому з полів (решта слів — у `matches`)."""
        word = max(self.words, key=len)
        cond = Q()
        for f in fields:
            cond |= Q(**{f + '__icontains': word})
        return cond


class _Groups:
    def __init__(self, query, skip):
        self.query = query
        self.skip = set(skip)
        self.items = {key: {} for key, _ in GROUPS}
        self.where = {}             # адреса → група, де її вже показано

    def add(self, group, email, name='', why='', score=0, extra=''):
        a = addr(email)
        if not a or a in self.skip or NOREPLY_RE.search(a):
            return
        if self.query and not self.query.matches(a, name, extra):
            return
        if a in self.where:
            item = self.items[self.where[a]][a]
            if why and why not in item['why'] and self.where[a] != 'other':
                item['also'].append(why)
            if name and not item['name']:
                item['name'] = name.strip()[:120]
            return
        self.where[a] = group
        self.items[group][a] = {'email': a, 'name': (name or '').strip()[:120], 'why': why, 'also': [],
                                'score': score, 'extra': extra}

    def result(self, limits):
        groups = []
        for key, label in GROUPS:
            rows = list(self.items[key].values())
            q = self.query
            rows.sort(key=lambda r: (0 if not q or q.starts(r['email'], r['name'], r['extra']) else 1, -r['score'],
                                     r['name'].lower() or r['email']))
            rows = rows[:limits.get(key, 10)]
            if rows:
                groups.append({'key': key, 'label': label, 'items': [
                    {'email': r['email'], 'name': r['name'],
                     'why': ' · '.join([r['why']] + r['also'][:2]) if r['why'] else ' · '.join(r['also'][:2])}
                    for r in rows]})
        return groups


def _own_mailboxes(user):
    from email_assistant.models import EmailAccount
    return list(EmailAccount.objects.filter(user=user))


def _team(g, user):
    """Скриньки Minerva й адреси активних співробітників; ви — наприкінці групи."""
    from django.contrib.auth.models import User
    from email_assistant.models import EmailAccount
    for acc in (EmailAccount.objects.filter(is_active=True, user__is_active=True).select_related('user')
                .order_by('user__first_name', 'email_address')):
        own = acc.user_id == user.pk
        name = acc.user.get_full_name() or acc.display_name or acc.user.username
        g.add('team', acc.email_address, name, 'ви — копія собі' if own else 'скринька Minerva', -1000 if own else 10)
    for u in User.objects.filter(is_active=True, is_staff=True).exclude(email='').order_by('first_name')[:200]:
        own = u.pk == user.pk
        g.add('team', u.email, u.get_full_name() or u.username, 'ви — копія собі' if own else 'колега в Minerva',
              -1000 if own else 5)


def _thread(g, user, message):
    """Учасники гілки листа (лише ваші скриньки): хто частіше — вище."""
    from email_assistant.models import EmailMessage
    if message is None:
        return []
    qs = EmailMessage.objects.filter(account__user=user, is_deleted=False)
    msgs = (qs.filter(thread_id=message.thread_id) if message.thread_id else qs.filter(pk=message.pk))
    count, names = Counter(), {}
    for m in msgs.order_by('-sent_at').values('from_email', 'from_name', 'to_emails', 'cc_emails')[:THREAD_MESSAGES]:
        a = addr(m['from_email'])
        if a:
            count[a] += 2
            names.setdefault(a, m['from_name'] or '')
        for raw in list(m['to_emails'] or []) + list(m['cc_emails'] or []):
            name, a = parseaddr(str(raw))
            a = addr(a)
            if a:
                count[a] += 1
                if name:
                    names.setdefault(a, name)
    for a, n in count.most_common():
        g.add('thread', a, names.get(a, ''), 'у цьому листуванні', n)
    return [a for a, _ in count.most_common(5)]


def _co_recipients(g, user, anchors):
    """З ким адресати зазвичай бувають в одному листі (ваша пошта за HISTORY_DAYS)."""
    from email_assistant.models import EmailMessage
    if not anchors:
        return
    cond = Q()
    for a in anchors[:5]:
        cond |= Q(from_email__icontains=a) | Q(to_emails__icontains=a) | Q(cc_emails__icontains=a)
    since = timezone.now() - timedelta(days=HISTORY_DAYS)
    rows = (EmailMessage.objects.filter(account__user=user, is_deleted=False, is_spam=False, sent_at__gte=since)
            .filter(cond).order_by('-sent_at').values('from_email', 'from_name', 'to_emails', 'cc_emails')
            [:HISTORY_MESSAGES])
    count, names = Counter(), {}
    anchor_set = set(anchors)
    for m in rows:
        people = {}
        a = addr(m['from_email'])
        if a:
            people[a] = m['from_name'] or ''
        for raw in list(m['to_emails'] or []) + list(m['cc_emails'] or []):
            name, a = parseaddr(str(raw))
            a = addr(a)
            if a:
                people.setdefault(a, name or '')
        if not anchor_set & set(people):
            continue                      # збіг лише частиною адреси — не той адресат
        for a, name in people.items():
            if a not in anchor_set:
                count[a] += 1
                if name:
                    names.setdefault(a, name)
    for a, n in count.most_common(40):
        g.add('related', a, names.get(a, ''), f'разом з адресатом у листах: {n}', 100 + n)


def _same_company(g, user, anchors):
    """Контакти постачальника й клієнти CRM з того самого домену фірми, що адресат (не Gmail / GMX…)."""
    domains = {_domain(a) for a in anchors if _domain(a) and not _free(_domain(a))}
    if not domains:
        return
    cond_c, cond_s = Q(), Q()
    for d in domains:
        cond_c |= Q(email__iendswith='@' + d)
        cond_s |= Q(email__iendswith='@' + d)
    if user.has_perm('crm.view_customer'):
        from crm.models import Customer
        for c in Customer.objects.filter(cond_c).exclude(email='')[:DB_LIMIT]:
            g.add('related', c.email, c.name, f'та сама фірма, що адресат ({_domain(addr(c.email))})', 50,
                  c.company or '')
    if user.has_perm('suppliers.view_suppliercontact'):
        from suppliers.models import SupplierContact
        for c in SupplierContact.objects.filter(cond_s).exclude(email='').select_related('supplier')[:DB_LIMIT]:
            why = f'контакт постачальника {c.supplier.name}' + (f' · {c.position}' if c.position else '')
            g.add('related', c.email, c.name, why, 60, c.supplier.name)


def _projects(g, user, message, anchors):
    """Учасники проєктів гілки листа й проєктів, де адресат — учасник."""
    if not user.has_perm('projects.view_project'):
        return
    try:
        from projects.models import Project, ProjectLink, ProjectParticipant
    except Exception:
        return
    project_ids = set()
    if message is not None and message.thread_id:
        project_ids |= set(ProjectLink.objects.filter(thread_id=message.thread_id, status=ProjectLink.CONFIRMED)
                           .values_list('project_id', flat=True))
    if anchors:
        cond = Q()
        for a in anchors[:5]:
            cond |= Q(email__iexact=a)
        project_ids |= set(ProjectParticipant.objects.filter(cond).values_list('project_id', flat=True)[:20])
    if not project_ids:
        return
    codes = dict(Project.objects.filter(pk__in=project_ids).values_list('pk', 'code'))
    for p in ProjectParticipant.objects.filter(project_id__in=project_ids).order_by('project_id')[:100]:
        role = p.get_role_display().split(' ', 1)[-1].lower()
        g.add('related', p.email, p.name, f'учасник проєкту {codes.get(p.project_id, "")} ({role})'.replace('  ', ' '),
              80)


def _others(g, user, query):
    """Адресна книга, клієнти CRM, контакти постачальників, відправники ваших листів."""
    from email_assistant.models import EmailContact, EmailMessage
    book = EmailContact.objects.filter(user=user)
    if not query:
        for c in book.order_by('-use_count', '-last_used_at')[:OTHER_WITHOUT_QUERY * 3]:
            g.add('other', c.email, c.name, f'адресна книга · {c.use_count} раз', c.use_count)
        return
    for c in book.filter(query.db('email', 'name')).order_by('-use_count')[:DB_LIMIT]:
        g.add('other', c.email, c.name, f'адресна книга · {c.use_count} раз', c.use_count)
    if user.has_perm('crm.view_customer'):
        from crm.models import Customer
        for c in Customer.objects.exclude(email='').filter(query.db('email', 'name', 'company'))[:DB_LIMIT]:
            g.add('other', c.email, c.name, 'клієнт CRM' + (f' · {c.company}' if c.company and c.company != c.name else ''),
                  0, c.company or '')
    if user.has_perm('suppliers.view_suppliercontact'):
        from suppliers.models import SupplierContact
        for c in (SupplierContact.objects.exclude(email='').select_related('supplier')
                  .filter(query.db('email', 'name', 'supplier__name'))[:DB_LIMIT]):
            g.add('other', c.email, c.name, f'контакт постачальника {c.supplier.name}', 0, c.supplier.name)
    seen = set()
    for row in (EmailMessage.objects.filter(account__user=user, is_spam=False, is_deleted=False)
                .filter(query.db('from_email', 'from_name')).order_by('-sent_at')
                .values('from_email', 'from_name')[:DB_LIMIT * 4]):
        a = addr(row['from_email'])
        if a and a not in seen:
            seen.add(a)
            g.add('other', a, row['from_name'], 'писав(ла) вам', -1)


def suggest(user, q='', message=None, to=(), exclude=()) -> list:
    """→ [{key, label, items: [{email, name, why}]}] — групи в порядку GROUPS, порожні не повертаються.
    `message` — лист, до якого пишемо (лише з ваших скриньок, інакше ігнорується); `to` — адреси «Кому» (їх не
    пропонуємо, а за ними шукаємо, з ким вони бувають в одному листі); `exclude` — уже вписані в поле."""
    if message is not None and not message.account.user_id == user.pk:
        message = None
    query = Query(q)
    to = addresses(to)
    g = _Groups(query, set(to) | set(addresses(exclude)))
    _team(g, user)
    thread_people = _thread(g, user, message)
    anchors = to or [a for a in thread_people if a not in g.where or g.where[a] != 'team'][:3]
    own = {addr(acc.email_address) for acc in _own_mailboxes(user)}
    anchors = [a for a in anchors if a not in own]
    _projects(g, user, message, anchors)
    _co_recipients(g, user, anchors)
    _same_company(g, user, anchors)
    _others(g, user, query)
    limits = dict(LIMITS)
    if not query:
        limits['other'] = OTHER_WITHOUT_QUERY
    return g.result(limits)
