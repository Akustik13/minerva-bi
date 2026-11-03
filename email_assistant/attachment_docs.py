"""📎 Вкладення листів → документи замовлення й проєкту.

Вмикає власник скриньки — `EmailSettings.attachments_to_docs` («⋯ Ще» у пошті; пошта — лише його):
• orders — замовлення листа (`_orders_of`: подія хронології CRM з `related_order_id`, а без неї — номер у темі,
  тексті чи вкладенні, `crm_link.find_order`, як рядок 🔗 над листом) → документи з нього копіюються в
  media/orders/{джерело}/{номер}/ («📄 Документи замовлення» в картці замовлення);
• all — ще й проєкти, до яких підтверджено гілку чи лист (`projects.ProjectLink`) → «📎 Документи й посилання»
  проєкту (`rag_assistant.knowledge_items.add_file`: власник — власник скриньки, видно тим, хто бачить проєкт).
Документи — `DOC_TYPES` (PDF, Word, Excel, CSV, TXT, XML, ZIP…), без картинок (логотипи в підписах) і вкладень,
вставлених у текст. Той самий файл (той самий вміст під будь-якою назвою) уже є — не зберігається; та сама назва
з іншим вмістом — «X (2).pdf». Зроблене — attachments[i]['saved'] (['order:<pk>', 'project:<pk>'], UPDATE без
сигналу): файл, який людина видалила з документів, наступна прив'язка листа не повертає.

Хто викликає — сигнали email_assistant/signals.py: новий лист, подія CRM із замовленням (і `relink_email_crm`),
підтверджена гілка / лист проєкту; `start_recent` — «📥 Зберегти вкладення за 30 днів» у «⋯ Ще» (у фоні, хід —
`state`): листи з документами, що прийшли раніше; ще не прив'язані до замовлення — спершу `link_message_to_crm`
(лист з'являється й у хронології клієнта).
"""
import copy
import datetime
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

OFF, ORDERS, ALL = 'off', 'orders', 'all'
DOC_TYPES = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'xlsm', 'odt', 'ods', 'rtf', 'csv', 'txt', 'xml', 'zip'}
FILE_MAX_MB = 25
NAME_MAX = 150
RECENT_DAYS = 30
RECENT_MAX = 500           # листів з документами за одне «📥 Зберегти вкладення за N днів»
THREAD_MAX = 200           # листів гілки, щойно підтвердженої в проєкті
PROGRESS_EVERY = 10        # листів між оновленнями стану
STALE_SECONDS = 180        # стільки без оновлення — збереження перервалось (перезапуск сервера)
BACKGROUND = True          # тести — False: усе в тому самому потоці
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')   # і для локальної копії на Windows
_local = threading.local()


def busy() -> bool:
    """Іде `save_recent` у цьому потоці — сигнали не дублюють його роботу (і його лічильники)."""
    return getattr(_local, 'busy', False)


def mode_of(user_id) -> str:
    """Що власник скриньки вибрав (без створення EmailSettings)."""
    from email_assistant.models import EmailSettings
    if not user_id:
        return OFF
    return (EmailSettings.objects.filter(user_id=user_id).values_list('attachments_to_docs', flat=True).first()
            or OFF)


def is_document(att) -> bool:
    if not isinstance(att, dict) or att.get('is_inline') or not att.get('file_path'):
        return False
    name = att.get('name') or ''
    return '.' in name and name.rsplit('.', 1)[-1].lower() in DOC_TYPES


def safe_name(name) -> str:
    """Назва файлу для теки документів: без шляху, службових символів і «..» (delete_doc_view їх не приймає)."""
    name = os.path.basename((name or '').replace('\\', '/'))
    name = _BAD_CHARS.sub('_', name)
    name = re.sub(r'\.{2,}', '.', name).strip(' .')
    if not name:
        return 'document'
    stem, dot, ext = name.rpartition('.')
    if not dot:
        return name[:NAME_MAX]
    return stem[:NAME_MAX - len(ext) - 1].rstrip(' .') + '.' + ext[:10]


def order_dir(order) -> Path:
    return Path(settings.MEDIA_ROOT) / 'orders' / (order.source or 'manual') / order.order_number


def _read(att):
    from email_assistant.reply_brief import _attachment_path
    path = _attachment_path(att)
    if not path or os.path.getsize(path) > FILE_MAX_MB * 1024 * 1024:
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _folder_hashes(folder: Path) -> dict:
    """{sha256: назва} файлів, що вже лежать у теці документів замовлення."""
    out = {}
    if not folder.is_dir():
        return out
    for f in folder.iterdir():
        if not f.is_file() or f.name.startswith('.') or f.stat().st_size > FILE_MAX_MB * 1024 * 1024:
            continue
        try:
            out.setdefault(_sha(f.read_bytes()), f.name)
        except OSError:
            continue
    return out


def _unique(folder: Path, name: str) -> str:
    if not (folder / name).exists():
        return name
    stem, dot, ext = name.rpartition('.')
    stem, ext = (stem, '.' + ext) if dot else (name, '')
    n = 2
    while (folder / f'{stem} ({n}){ext}').exists():
        n += 1
    return f'{stem} ({n}){ext}'


def _write(folder: Path, name: str, data: bytes):
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f'.mail-{uuid.uuid4().hex}.part'
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)


def _linked_order_ids(msg) -> set:
    from crm.models import CustomerTimeline
    return set(CustomerTimeline.objects.filter(email_message=msg, related_order_id__isnull=False)
               .values_list('related_order_id', flat=True))


def _orders_of(msg) -> list:
    """Замовлення листа: з хронології CRM; лист туди не потрапив (клієнта не знайдено, внутрішнє пересилання) —
    номер у темі, тексті чи вкладенні, як рядок 🔗 над листом."""
    from sales.models import SalesOrder
    ids = _linked_order_ids(msg)
    if ids:
        return list(SalesOrder.objects.filter(pk__in=ids).order_by('pk'))
    from email_assistant.crm_link import find_order
    try:
        order = find_order(msg.subject or '', msg.body_text or '', message=msg)
    except Exception as e:  # noqa: BLE001 — нечитабельний лист: без замовлення
        logger.info('attachment docs: order of message %s: %s', msg.pk, e)
        return []
    return [order] if order is not None else []


def _projects_of(msg) -> list:
    from django.db.models import Q

    from projects.models import Project, ProjectLink
    links = ProjectLink.objects.filter(status=ProjectLink.CONFIRMED)
    links = links.filter(Q(message=msg) | Q(thread_id=msg.thread_id)) if msg.thread_id else links.filter(message=msg)
    return list(Project.objects.filter(pk__in=links.values('project_id')).select_related('customer').order_by('pk'))


def _add_to_project(project, owner, name, data, content_type, msg):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rag_assistant import knowledge_items
    when = timezone.localtime(msg.sent_at).strftime('%d.%m.%Y') if msg.sent_at else ''
    note = f'📧 З листа {when} від {msg.from_email}: {msg.subject or "(без теми)"}'
    links = {'project': project}
    if project.customer_id:
        links['customer'] = project.customer
    knowledge_items.add_file(owner, SimpleUploadedFile(name, data, content_type=content_type or ''),
                             title=name, note=note, links=links)


def _project_has(project, sha) -> bool:
    from rag_assistant.models import KnowledgeItem
    return KnowledgeItem.objects.filter(project=project, file_sha256='sha256:' + sha).exists()


def _skipped(msg) -> bool:
    from email_assistant.models import EmailMessage
    return (msg is None or msg.is_spam or msg.is_deleted
            or msg.folder in (EmailMessage.FOLDER_SPAM, EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_TRASH))


def save_for_message(msg, mode=None) -> dict:
    """Документи листа → документи його замовлень (і проєктів для «all»). → {'orders', 'projects', 'already'}"""
    from email_assistant.models import EmailMessage
    out = {'orders': 0, 'projects': 0, 'already': 0}
    if _skipped(msg):
        return out
    raw = copy.deepcopy(list(msg.attachments or []))
    docs = [att for att in raw if is_document(att)]
    if not docs:
        return out
    mode = mode or mode_of(msg.account.user_id if msg.account_id else None)
    if mode not in (ORDERS, ALL):
        return out
    orders = _orders_of(msg)
    projects = _projects_of(msg) if mode == ALL else []
    if not orders and not projects:
        return out
    owner = msg.account.user
    known, changed = {}, False
    for att in docs:
        saved = list(att.get('saved') or [])
        todo = ([('order', o) for o in orders if f'order:{o.pk}' not in saved]
                + [('project', p) for p in projects if f'project:{p.pk}' not in saved])
        if not todo:
            continue
        try:
            data = _read(att)
        except OSError as e:
            logger.warning('attachment docs: message %s, %s not readable: %s', msg.pk, att.get('name'), e)
            continue
        if data is None:
            continue
        sha, name = _sha(data), safe_name(att.get('name'))
        for kind, obj in todo:
            try:
                if kind == 'order':
                    folder = order_dir(obj)
                    if str(folder) not in known:
                        known[str(folder)] = _folder_hashes(folder)
                    have = known[str(folder)]
                    if sha in have:
                        out['already'] += 1
                    else:
                        fname = _unique(folder, name)
                        _write(folder, fname, data)
                        have[sha] = fname
                        out['orders'] += 1
                elif _project_has(obj, sha):
                    out['already'] += 1
                else:
                    _add_to_project(obj, owner, name, data, att.get('content_type'), msg)
                    out['projects'] += 1
            except Exception as e:  # noqa: BLE001 — наступна прив'язка спробує ще раз
                logger.warning('attachment docs: message %s → %s %s: %s', msg.pk, kind, obj.pk, e)
                continue
            saved.append(f'{kind}:{obj.pk}')
        if saved != list(att.get('saved') or []):
            att['saved'] = saved
            changed = True
    if changed:
        EmailMessage.objects.filter(pk=msg.pk).update(attachments=raw)
        msg.attachments = raw
    return out


def save_message_pk(pk) -> dict:
    from email_assistant.models import EmailMessage
    msg = EmailMessage.objects.select_related('account').filter(pk=pk).first()
    return save_for_message(msg) if msg is not None else {'orders': 0, 'projects': 0, 'already': 0}


def project_linked(link_pk) -> dict:
    """Гілку / лист підтверджено в проєкті — документи всіх його листів (для власників з «all»)."""
    from email_assistant.models import EmailMessage
    from projects.models import ProjectLink
    link = ProjectLink.objects.filter(pk=link_pk, status=ProjectLink.CONFIRMED).first()
    total = {'orders': 0, 'projects': 0, 'already': 0}
    if link is None or not (link.thread_id or link.message_id):
        return total
    qs = (EmailMessage.objects.filter(thread_id=link.thread_id) if link.thread_id
          else EmailMessage.objects.filter(pk=link.message_id))
    modes = {}
    for msg in qs.exclude(attachments=[]).select_related('account').order_by('-sent_at')[:THREAD_MAX]:
        uid = msg.account.user_id
        if uid not in modes:
            modes[uid] = mode_of(uid)
        if modes[uid] != ALL:
            continue
        for k, v in save_for_message(msg, modes[uid]).items():
            total[k] += v
    return total


def save_recent(user, days=RECENT_DAYS, progress=None) -> dict:
    """«📥 Зберегти вкладення за N днів»: листи скриньок людини з документами. Ще не прив'язаний до замовлення —
    спершу в хронологію CRM (`link_message_to_crm` бачить номер і у вкладенні), далі `save_for_message`."""
    from email_assistant.crm_link import link_message_to_crm
    from email_assistant.models import EmailMessage, EmailSettings
    mode = EmailSettings.get_for_user(user).attachments_to_docs
    total = {'orders': 0, 'projects': 0, 'already': 0, 'messages': 0, 'linked': 0, 'total': 0, 'mode': mode}
    if mode not in (ORDERS, ALL):
        return total
    since = timezone.now() - datetime.timedelta(days=int(days))
    qs = (EmailMessage.objects.filter(account__user=user, sent_at__gte=since, is_spam=False, is_deleted=False)
          .exclude(folder__in=(EmailMessage.FOLDER_SPAM, EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_TRASH))
          .exclude(attachments=[]).select_related('account').order_by('-sent_at'))
    msgs = [m for m in qs.iterator(chunk_size=200) if any(is_document(a) for a in m.attachments or [])]
    msgs = msgs[:RECENT_MAX]
    total['total'] = len(msgs)
    _local.busy = True
    try:
        for i, msg in enumerate(msgs, 1):
            if not _linked_order_ids(msg):
                link_message_to_crm(msg)
                if _linked_order_ids(msg):
                    total['linked'] += 1
            for k, v in save_for_message(msg, mode).items():
                total[k] += v
            total['messages'] = i
            if progress and i % PROGRESS_EVERY == 0:
                progress(dict(total))
    finally:
        _local.busy = False
    return total


# ── «📥 Зберегти вкладення за 30 днів» у фоні ─────────────────────────────────

def _key(user_id) -> str:
    return f'attachment-docs-recent:{user_id}'


def _set_state(user_id, **st):
    from core.shared_cache import cache
    cache.set(_key(user_id), {**st, 'at': time.time()}, 24 * 3600)


def state(user) -> dict:
    """Хід збереження людини: running, messages / total, orders, projects, already, linked, error, done."""
    from core.shared_cache import cache
    st = dict(cache.get(_key(user.pk)) or {})
    if st.get('running') and time.time() - st.get('at', 0) > STALE_SECONDS:
        st.update(running=False, error='Збереження зупинилось — мабуть, сервер перезапускався. Запустіть ще раз.')
    st.pop('at', None)
    return st


def start_recent(user, days=RECENT_DAYS) -> bool:
    """Запустити `save_recent` у фоні. False — уже йде."""
    from django.db import connection
    if state(user).get('running'):
        return False
    uid = user.pk
    _set_state(uid, running=True, messages=0, total=0)

    def run():
        try:
            result = save_recent(user, days, progress=lambda st: _set_state(uid, running=True, **st))
            _set_state(uid, running=False, done=True, **result)
        except Exception as e:  # noqa: BLE001
            logger.exception('attachment docs for user %s: %s', uid, e)
            _set_state(uid, running=False, error=f'Збереження перервано ({type(e).__name__})')
        finally:
            if BACKGROUND:
                connection.close()

    if BACKGROUND:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()
    return True
