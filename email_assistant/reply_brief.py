"""
«📋 Підготувати відповідь» — по кроках: дані → бриф → лист (ReplyBrief). До якого кроку йти, вибирає менеджер
(ReplyBrief.mode: лише дані / + бриф / + лист); далі — кнопками «📋 Скласти бриф» і «✍️ Написати лист».

1. gather() — БЕЗ AI (0 токенів), у момент натискання; кожне джерело отримує номер [#N] і групу походження:
   - mail       — листи цієї гілки до листа, на який відповідаємо;
   - history    — стан і хронологія, те саме, що в «📜 Історії» (замовлення, відправлення, нотатки CRM);
   - overview   — збережений AI-огляд проєкту (обіцянки, відкриті питання);
   - project    — шапка проєкту й останні листи з інших його гілок;
   - others     — інші листування з клієнтом з бази Minerva (останній лист кожного; це не RAG);
   - offer      — Angebote гілки / клієнта / проєкту з позиціями;
   - attachment — текст вкладень гілки (PDF, DOCX, TXT, CSV);
   - rag        — фрагменти з бази знань RAG, лише в листуваннях цього клієнта (або проєкту),
                  запит — побажання менеджера + суть листа.
   Текст джерел зберігається в ReplyBrief.sources — бриф можна скласти пізніше без повторного збору.
2. Бриф — швидша модель (ai_helper.BRIEF_MODEL), українською, з посиланнями [#N].
3. Лист — модель із «Налаштувань пошти» за брифом; «↻ Переписати» / «✍️ Написати лист» — лише цей крок.
Усе у фоні: браузер опитує стан (stages). Дані застарілі, якщо в гілці з'явились новіші листи.
"""
import logging
import os
import re
import threading
import time

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger('email_assistant')

THREAD_LAST = 12
NEWEST_CHARS, MESSAGE_CHARS = 10000, 2500
HISTORY_EVENTS = 40
OTHER_THREAD_CHARS = 800
PROJECTS, PROJECT_MESSAGES, PROJECT_CHARS = 2, 8, 1200
OFFERS, OFFER_LINES = 5, 12
ATTACHMENT_FILES, ATTACHMENT_CHARS, ATTACHMENT_TOTAL, ATTACHMENT_MAX_MB = 4, 4000, 12000, 15
ATTACHMENT_TYPES = ('pdf', 'docx', 'txt', 'csv')
RAG_HITS, RAG_HIT_CHARS, RAG_WAIT_SECONDS = 8, 1200, 12

STAGES = [
    ('thread', '📨 Ця гілка'),
    ('minerva', '📜 Історія (як у «📜 Історії»), огляд проєкту, інші листування з клієнтом'),
    ('projects', '📁 Проєкт: інші гілки'),
    ('offers', '📝 Angebote'),
    ('attachments', '📎 Вкладення'),
    ('rag', '🔎 RAG: пошук в інших листуваннях клієнта'),
    ('brief', '📋 Бриф (AI)'),
    ('letter', '✍️ Лист (AI)'),
]
GATHER_STAGES = [k for k, _ in STAGES if k not in ('brief', 'letter')]


class Sources:
    """Нумеровані джерела [#N]: текст — для брифу й для показу менеджеру, група — звідки дані."""

    def __init__(self, items=None):
        self.items = list(items or [])

    def add(self, kind, label, text, url=''):
        self.items.append({'n': len(self.items) + 1, 'kind': kind, 'label': label[:200], 'url': url,
                           'text': (text or '').strip()})
        return len(self.items)

    def text(self) -> str:
        return '\n\n'.join(f'[#{i["n"]}] {i["label"]}\n{i.get("text") or "—"}' for i in self.items)

    def public(self, texts=True) -> list:
        keys = ('n', 'kind', 'label', 'url', 'text') if texts else ('n', 'kind', 'label', 'url')
        return [{k: i.get(k, '') for k in keys} for i in self.items]


def _dt(value) -> str:
    return timezone.localtime(value).strftime('%d.%m.%Y %H:%M') if value else '?'


def _num(value) -> str:
    if value is None:
        return '—'
    text = f'{value:f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _message_label(m, own) -> str:
    outgoing = m.folder == 'sent' or (m.from_email or '').lower() == own
    return f'{"→ НАДІСЛАЛИ МИ" if outgoing else "← ОТРИМАЛИ"} · {_dt(m.sent_at)} · {m.from_email} · «{m.subject or "—"}»'


def thread_messages(msg) -> list:
    """Листи гілки до листа, на який відповідаємо (як і для швидкої AI-відповіді)."""
    if not msg.thread_id:
        return [msg]
    msgs = list(msg.thread.messages.filter(is_deleted=False).order_by('sent_at', 'pk'))
    before = [m for m in msgs if not m.sent_at or not msg.sent_at or m.sent_at <= msg.sent_at] or [msg]
    return before[-THREAD_LAST:]


def latest_message_id(msg) -> int:
    if not msg.thread_id:
        return msg.pk
    return (msg.thread.messages.filter(is_deleted=False).order_by('-sent_at', '-pk')
            .values_list('pk', flat=True).first() or msg.pk)


# ── Текст вкладень ───────────────────────────────────────────────────────────

def _attachment_path(att) -> str:
    rel = att.get('file_path') or ''
    if not rel:
        return ''
    root = os.path.normpath(str(settings.MEDIA_ROOT))
    path = os.path.normpath(os.path.join(root, rel))
    return path if path.startswith(root + os.sep) and os.path.isfile(path) else ''


def attachment_text(att, limit=ATTACHMENT_CHARS) -> str:
    """Текст PDF / DOCX / TXT / CSV із збереженого вкладення; '' — якщо недоступне чи інший тип."""
    name = (att.get('name') or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    path = _attachment_path(att)
    if ext not in ATTACHMENT_TYPES or not path or os.path.getsize(path) > ATTACHMENT_MAX_MB * 1024 * 1024:
        return ''
    try:
        if ext == 'pdf':
            from pypdf import PdfReader
            parts = []
            for page in PdfReader(path).pages[:15]:
                parts.append(page.extract_text() or '')
                if sum(len(p) for p in parts) > limit:
                    break
            text = '\n'.join(parts)
        elif ext == 'docx':
            import docx
            d = docx.Document(path)
            text = '\n'.join([p.text for p in d.paragraphs] +
                             [' | '.join(c.text for c in row.cells) for t in d.tables for row in t.rows])
        else:
            with open(path, 'rb') as fh:
                text = fh.read(limit * 4).decode('utf-8', errors='replace')
    except Exception as e:  # noqa: BLE001 — пошкоджений файл не зупиняє підготовку
        logger.info('reply brief: attachment %s not readable: %s', name, e)
        return ''
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n{3,}', '\n\n', text)).strip()[:limit]


# ── Збір джерел (без AI) ─────────────────────────────────────────────────────

def gather(brief, on_stage=None) -> tuple:
    """(Sources, листи гілки, мітка листа-адресата). on_stage(key, status, detail) — для показу етапів."""
    from email_assistant.context import build_context
    on_stage = on_stage or (lambda *a: None)
    msg, user, wish = brief.message, brief.user, brief.wish
    account = msg.account
    own = (account.email_address or '').lower()
    src, used = Sources(), []

    on_stage('thread', 'running')
    msgs = thread_messages(msg)
    target = ''
    for m in msgs:
        n = src.add('mail', _message_label(m, own), (m.body_text or '')[:NEWEST_CHARS if m.pk == msg.pk else MESSAGE_CHARS],
                    f'/email/message/{m.pk}/')
        if m.pk == msg.pk:
            target = f'[#{n}] {_message_label(m, own)}'
    used.append(f'листів гілки: {len(msgs)}')
    on_stage('thread', 'done', f'{len(msgs)} лист.')

    on_stage('minerva', 'running')
    ctx = build_context(msg)
    detail = _minerva(src, ctx)
    used += [d for d in detail if d]
    on_stage('minerva', 'done' if detail else 'skipped', ' · '.join(detail) or 'нічого пов\'язаного в базі')
    customer = ctx.get('customer')

    on_stage('projects', 'running')
    projects = _projects(msg)
    project_letters = 0
    for p in projects:
        head = (f'Проєкт {p.code} «{p.name}» · {p.get_status_display()}'
                + (f' · строк {p.due_date:%d.%m.%Y}' if p.due_date else '')
                + (f' · наступна дія: {p.next_action}' if p.next_action else ''))
        src.add('project', head, head, f'/admin/projects/project/{p.pk}/change/')
        project_letters += _project_letters(src, p, msg, own)
    if projects:
        used.append(f'проєкт {", ".join(p.code for p in projects)}: листів з інших гілок {project_letters}')
    on_stage('projects', 'done' if projects else 'skipped',
             f'{", ".join(p.code for p in projects)} · листів з інших гілок: {project_letters}' if projects
             else 'гілку не прив\'язано до проєкту')

    on_stage('offers', 'running')
    offers = _offers(src, msg, customer, projects)
    if offers:
        used.append(f'Angebote: {", ".join(offers)}')
    on_stage('offers', 'done' if offers else 'skipped', ', '.join(offers) or 'немає')

    on_stage('attachments', 'running')
    files = _attachments(src, msgs)
    if files:
        used.append(f'вкладення: {len(files)}')
    on_stage('attachments', 'done' if files else 'skipped', ', '.join(files) or 'немає текстових вкладень')

    on_stage('rag', 'running')
    status, detail, note = _rag(src, user, msg, ctx, wish, customer, projects)
    if note:
        used.append(note)
    on_stage('rag', status, detail)

    brief.used = used
    return src, msgs, target


def _minerva(src, ctx) -> list:
    """Дані з бази Minerva, розкладені за походженням (історія / огляд проєкту / інші листування)."""
    from email_assistant.context import _fmt, history_events, state_lines
    detail = []
    state, events = state_lines(ctx), history_events(ctx)
    if state or events:
        lines = (['ПОТОЧНИЙ СТАН:'] + state if state else []) + \
            (['ХРОНОЛОГІЯ:'] + [f'{_fmt(d)} {icon} {text}' + ('' if scope == 'order' else ' [інше по клієнту]')
                                for d, icon, text, scope in events[-HISTORY_EVENTS:]] if events else [])
        src.add('history', 'Історія: стан і хронологія (замовлення, відправлення, нотатки CRM)', '\n'.join(lines))
        if ctx.get('customer') is not None:
            detail.append(f'клієнт {ctx["customer"].name}')
        if ctx['orders']:
            detail.append(f'замовлень: {len(ctx["orders"])}')
        if ctx['notes']:
            detail.append(f'нотаток CRM: {len(ctx["notes"])}')
    if ctx.get('projects'):
        from projects.overview import facts_text
        for project, overview, stale in ctx['projects'][:PROJECTS]:
            when = timezone.localtime(overview.updated_at).strftime('%d.%m.%Y') if getattr(overview, 'updated_at', None) else ''
            src.add('overview', f'Огляд проєкту {project.code}' + (f' від {when}' if when else '')
                    + (' (застарів)' if stale else ''), facts_text(project, overview, stale)[:2500],
                    f'/admin/projects/project/{project.pk}/change/')
            detail.append(f'огляд {project.code}' + (' (застарів)' if stale else ''))
    for t in ctx.get('other_threads') or []:
        last = t['last']
        src.add('others', f'Інше листування «{t["thread"].subject or "—"}» ({t["count"]} лист.) — останній лист '
                          f'{_dt(last.sent_at)} від {last.from_email}', (last.body_text or '')[:OTHER_THREAD_CHARS],
                f'/email/message/{last.pk}/')
    if ctx.get('other_threads'):
        detail.append(f'інших листувань: {len(ctx["other_threads"])}')
    return detail


def _projects(msg) -> list:
    if not msg.thread_id:
        return []
    try:
        from projects.services import thread_projects
    except ImportError:
        return []
    found = {}
    for link in thread_projects(msg.thread)['confirmed']:
        found.setdefault(link.project_id, link.project)
    return list(found.values())[:PROJECTS]


def _project_letters(src, project, msg, own) -> int:
    from projects.services import project_messages
    qs = project_messages(project)
    if msg.thread_id:
        qs = qs.exclude(thread_id=msg.thread_id)
    letters = list(qs.order_by('-sent_at')[:PROJECT_MESSAGES])
    for m in reversed(letters):
        src.add('project', f'{project.code}: {_message_label(m, own)}', (m.body_text or '')[:PROJECT_CHARS],
                f'/email/message/{m.pk}/')
    return len(letters)


def _offers(src, msg, customer, projects) -> list:
    try:
        from offers.models import Offer
    except ImportError:
        return []
    cond = Q(source_message__thread_id=msg.thread_id) if msg.thread_id else Q(source_message=msg)
    if customer is not None:
        cond |= Q(customer=customer)
    if projects:
        cond |= Q(project__in=projects)
    numbers = []
    for o in Offer.objects.filter(cond).prefetch_related('lines').order_by('-created_at')[:OFFERS]:
        lines = []
        for line in list(o.lines.all())[:OFFER_LINES]:
            what = line.sku or (line.description or '').split('\n')[0][:60] or '—'
            lines.append(f'- {what}: {_num(line.qty)} {line.unit} × {_num(line.unit_price)} {o.currency}'
                         + (f', знижка {_num(line.discount_percent)}%' if line.discount_percent else '')
                         + (f', строк {line.lead_time}' if line.lead_time else '')
                         + (f' ({line.price_note})' if line.price_note else ''))
        text = (f'Статус: {o.get_status_display()} · створено {o.created_at:%d.%m.%Y}'
                + (f' · надіслано {o.sent_at:%d.%m.%Y}' if getattr(o, 'sent_at', None) else '')
                + (f' · дійсний до {o.valid_until:%d.%m.%Y}' if o.valid_until else '')
                + f' · нетто {_num(o.net_total)} {o.currency}, брутто {_num(o.gross_total)} {o.currency}\n'
                + '\n'.join(lines))
        src.add('offer', f'Angebot {o.number} (версія {o.revision})', text, f'/admin/offers/offer/{o.pk}/change/')
        numbers.append(o.number)
    return numbers


def _attachments(src, msgs) -> list:
    names, total = [], 0
    for m in reversed(msgs):                       # найновіші листи — першими
        for index, att in enumerate(m.attachments or []):
            if len(names) >= ATTACHMENT_FILES or total >= ATTACHMENT_TOTAL:
                return names
            text = attachment_text(att, min(ATTACHMENT_CHARS, ATTACHMENT_TOTAL - total))
            if text:
                src.add('attachment', f'Вкладення «{att.get("name")}» з листа {_dt(m.sent_at)} від {m.from_email}', text,
                        f'/email/message/{m.pk}/attachment/{index}/')
                names.append(att.get('name') or '?')
                total += len(text)
    return names


def _rag(src, user, msg, ctx, wish, customer, projects) -> tuple:
    """RAG лише в листуваннях цього клієнта (або єдиного проєкту); → (status, detail для етапу, note для «врахувала»)."""
    try:
        from rag_assistant import retrieval
    except ImportError:
        return 'skipped', 'RAG не підключено', ''
    if not retrieval.available():
        return 'skipped', 'RAG не налаштований', ''
    customer_id = customer.pk if customer is not None else None
    project_id = projects[0].pk if len(projects) == 1 and not customer_id else None
    if not customer_id and not project_id:
        return 'skipped', 'клієнта чи проєкт не визначено — у всій пошті не шукаємо', ''
    query = ' · '.join(p for p in (wish.strip(), retrieval.query_for(ctx)) if p)
    job = retrieval.start(user, query, purpose='reply', thread=msg.thread, customer_id=customer_id,
                          project_id=project_id, exclude_thread_ids=[msg.thread_id] if msg.thread_id else [],
                          limit=RAG_HITS, wait_ms=8000)
    deadline = time.monotonic() + RAG_WAIT_SECONDS
    while not job.is_terminal and time.monotonic() < deadline:
        time.sleep(min(max(job.poll_after_ms or 1000, 300), 1500) / 1000)
        retrieval.poll(job)
    scope = f'клієнта {customer.name}' if customer_id else f'проєкту {projects[0].code}'
    asked = f'запит «{query[:70]}{"…" if len(query) > 70 else ""}»'
    if job.status != job.COMPLETED:
        return 'skipped', f'{(job.message or "RAG не встиг")[:90]} · {asked}', ''
    hits = job.hits[:RAG_HITS]
    for h in hits:
        src.add('rag', f'RAG · {(h.get("sent_at") or "")[:10]} · {h.get("from_email", "")} · «{(h.get("subject") or "")[:80]}»',
                (h.get('text') or '')[:RAG_HIT_CHARS], h.get('url') or '')
    return 'done', f'фрагментів {len(hits)} у листуваннях {scope} · {asked}', f'RAG: фрагментів {len(hits)}'


# ── Запуск ───────────────────────────────────────────────────────────────────

def _stages(keys=None, skip=()) -> list:
    return [{'key': k, 'label': label, 'status': 'skipped' if k in skip else 'pending',
             'detail': 'наступний крок — кнопкою нижче' if k in skip else ''}
            for k, label in STAGES if keys is None or k in keys]


def _restage(brief, run, skip=()) -> list:
    """Наступний крок: етапи збору лишаються (видно, звідки дані), кроки run — заново, skip — «кнопкою нижче»."""
    stages = brief.stages or _stages()
    for s in stages:
        if s['key'] in run:
            s['status'], s['detail'] = 'pending', ''
        elif s['key'] in skip:
            s['status'], s['detail'] = 'skipped', 'наступний крок — кнопкою нижче'
    return stages


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def _in_background(fn):
    caller = threading.current_thread()

    def run():
        try:
            fn()
        finally:
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(run)


def start(user, msg, *, wish='', recipient='', to_email='', cc=(), mode='letter'):
    """Нова підготовка для листа msg у фоні до кроку mode (data / brief / letter). → ReplyBrief (running)."""
    from email_assistant.models import ReplyBrief
    mode = mode if mode in dict(ReplyBrief.MODE_CHOICES) else ReplyBrief.MODE_LETTER
    skip = {'data': ('brief', 'letter'), 'brief': ('letter',)}.get(mode, ())
    brief = ReplyBrief.objects.create(
        user=user, message=msg, thread=msg.thread if msg.thread_id else None, wish=(wish or '')[:2000], mode=mode,
        recipient=recipient[:300], to_email=to_email[:254], cc=list(cc), stages=_stages(skip=skip),
        last_message_id=latest_message_id(msg))
    _in_background(lambda: _prepare(brief.pk))
    return brief


def start_brief(brief, wish=None):
    """«📋 Скласти бриф» зі збережених даних (без повторного збору). → той самий ReplyBrief (running)."""
    from email_assistant.models import ReplyBrief
    if wish is not None:
        brief.wish = wish[:2000]
    brief.status, brief.error, brief.stages = ReplyBrief.RUNNING, '', _restage(brief, ['brief'], skip=['letter'])
    brief.save()
    _in_background(lambda: _compose(brief.pk))
    return brief


def start_rewrite(brief, text, wish=None):
    """«↻ Переписати» / «✍️ Написати лист»: лише лист, з (виправленим) брифом. → той самий ReplyBrief (running)."""
    from email_assistant.models import ReplyBrief
    brief.brief = (text or '').strip()[:20000]
    if wish is not None:
        brief.wish = wish[:2000]
    brief.status, brief.error, brief.stages = ReplyBrief.RUNNING, '', _restage(brief, ['letter'])
    brief.save()
    _in_background(lambda: _rewrite(brief.pk))
    return brief


def _load(pk):
    from email_assistant.models import ReplyBrief
    return ReplyBrief.objects.select_related('message__account__user', 'message__thread', 'user').get(pk=pk)


def _stage_setter(brief):
    def set_stage(key, status, detail=''):
        for s in brief.stages:
            if s['key'] == key:
                s['status'], s['detail'] = status, (detail or '')[:220]
        brief.save(update_fields=['stages', 'updated_at'])
    return set_stage


def _model_label(model) -> str:
    from email_assistant.views import _model_label as label   # «Claude Opus 5.5» — як у «Minerva врахувала»
    return label(model) if model else '—'


def _prepare(pk):
    from email_assistant.models import ReplyBrief
    brief = _load(pk)
    set_stage = _stage_setter(brief)
    try:
        src, msgs, target = gather(brief, set_stage)
        brief.sources, brief.target = src.public(), target
        brief.save(update_fields=['sources', 'target', 'used', 'updated_at'])
        if brief.mode != ReplyBrief.MODE_DATA:
            _make_brief(brief, src, set_stage)
            if brief.mode == ReplyBrief.MODE_LETTER:
                _write_letter(brief, msgs, set_stage)
        brief.status = ReplyBrief.DONE
    except Exception as e:  # noqa: BLE001
        logger.exception('reply brief %s failed: %s', pk, e)
        _fail(brief, e)
    brief.save()


def _compose(pk):
    from email_assistant.models import ReplyBrief
    brief = _load(pk)
    set_stage = _stage_setter(brief)
    try:
        _make_brief(brief, Sources(brief.sources), set_stage)
        brief.status = ReplyBrief.DONE
    except Exception as e:  # noqa: BLE001
        logger.exception('reply brief compose %s failed: %s', pk, e)
        _fail(brief, e)
    brief.save()


def _rewrite(pk):
    from email_assistant.models import ReplyBrief
    brief = _load(pk)
    set_stage = _stage_setter(brief)
    try:
        _write_letter(brief, thread_messages(brief.message), set_stage)
        brief.status = ReplyBrief.DONE
    except Exception as e:  # noqa: BLE001
        logger.exception('reply brief rewrite %s failed: %s', pk, e)
        _fail(brief, e)
    brief.save()


def _make_brief(brief, src, set_stage):
    from email_assistant import ai_helper
    set_stage('brief', 'running')
    profile = getattr(brief.user, 'profile', None)
    text = ai_helper.make_reply_brief(src.text(), brief.wish, brief.target, brief.message.account, profile)
    if not text:
        raise RuntimeError('AI не повернув бриф — перевірте ключ AI або спробуйте ще раз')
    brief.brief, brief.brief_model, brief.draft = text, ai_helper.last_model(), ''
    brief.used = [u for u in brief.used if not u.startswith(('бриф —', 'лист —'))] + \
        [f'бриф — {_model_label(brief.brief_model)}']
    set_stage('brief', 'done', f'модель {_model_label(brief.brief_model)}')


def _write_letter(brief, msgs, set_stage):
    from email_assistant import ai_helper
    from email_assistant.models import EmailMessage
    set_stage('letter', 'running')
    profile = getattr(brief.user, 'profile', None)
    draft = ai_helper.write_reply_from_brief(msgs, brief.message.account, profile, brief=brief.brief,
                                             wish=brief.wish, recipient=brief.recipient)
    if not draft:
        raise RuntimeError('AI не повернув лист — спробуйте «Переписати»')
    brief.draft, brief.letter_model = draft, ai_helper.last_model()
    brief.used = [u for u in brief.used if not u.startswith('лист —')] + \
        [f'лист — {_model_label(brief.letter_model)}']
    set_stage('letter', 'done', f'модель {_model_label(brief.letter_model)}')
    EmailMessage.objects.filter(pk=brief.message_id).update(ai_reply_draft=draft)


def _fail(brief, error):
    from email_assistant.models import ReplyBrief
    for s in brief.stages:
        if s['status'] == 'running':
            s['status'] = 'failed'
    brief.status, brief.error = ReplyBrief.ERROR, str(error)[:300]


# ── Для браузера ─────────────────────────────────────────────────────────────

def latest_for(user, msg):
    """Остання підготовка користувача для гілки листа (або для самого листа без гілки)."""
    from email_assistant.models import ReplyBrief
    qs = ReplyBrief.objects.filter(user=user)
    qs = qs.filter(thread_id=msg.thread_id) if msg.thread_id else qs.filter(message=msg)
    return qs.select_related('message').first()


def as_json(brief) -> dict:
    """Стан для браузера; повний текст джерел — лише коли крок завершено (опитування під час роботи — легке)."""
    running = brief.status == brief.RUNNING
    stale = not running and latest_message_id(brief.message) != brief.last_message_id
    return {'id': brief.pk, 'status': brief.status, 'mode': brief.mode, 'error': brief.error, 'stages': brief.stages,
            'brief': brief.brief, 'sources': Sources(brief.sources).public(texts=not running), 'draft': brief.draft,
            'to': brief.to_email, 'cc': brief.cc, 'used': brief.used, 'wish': brief.wish,
            'message_id': brief.message_id, 'stale': stale,
            'updated': timezone.localtime(brief.updated_at).strftime('%d.%m %H:%M'),
            'gathered': timezone.localtime(brief.created_at).strftime('%d.%m.%Y %H:%M')}
