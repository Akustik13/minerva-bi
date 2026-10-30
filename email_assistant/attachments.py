"""
email_assistant/attachments.py — вкладення для листа-відповіді.

Окрім файлів з комп'ютера, до відповіді можна додати:
  - документи замовлення, згаданого в листуванні (media/orders/{source}/{order_number}/ —
    інвойси, пакувальні листи, CN23…);
  - вкладення з листів цієї гілки (з диска або, якщо ще не збережені, з IMAP).
Браузер передає лише посилання «order:<pk>:<ім'я файлу>» / «msg:<pk>:<індекс>», файли читає сервер.

Чернетки (EmailDraft) і заплановані листи (ScheduledEmail) зберігають вкладення списком записів:
  - {file, name, content_type, size} — файл з комп'ютера, збережений у MEDIA_ROOT/email_outbox/<uuid>/;
  - {ref, name} — документ Minerva («order:…», «msg:…», «offer:…»), файл читається лише під час відправки.
Файл чернетки у формі листа — посилання «draft:<pk>:<індекс>» (лише чернетки цієї скриньки).
"""
import logging
import mimetypes
import os
import re
import uuid

from django.conf import settings

logger = logging.getLogger('email_assistant')

MAX_ORDER_DOCS = 20
OUTBOX_DIR = 'email_outbox'   # MEDIA_ROOT/email_outbox/<uuid>/<файл> — вкладення чернеток і запланованих листів


def order_dir(order) -> str:
    return os.path.join(str(settings.MEDIA_ROOT), 'orders', order.source or 'manual', order.order_number)


def order_documents(order) -> list:
    """Файли з папки замовлення: [{ref, name, size}] — нові зверху."""
    folder = order_dir(order)
    try:
        entries = [e for e in os.scandir(folder) if e.is_file()]
    except OSError:
        return []
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    return [{'ref': f'order:{order.pk}:{e.name}', 'name': e.name, 'size': e.stat().st_size,
             'group': f'{order.source}:{order.order_number}'}
            for e in entries[:MAX_ORDER_DOCS]]


def thread_attachments(emails) -> list:
    """Вкладення з листів гілки (без вбудованих у текст картинок): [{ref, name, size, group}]."""
    out = []
    for m in emails:
        for i, att in enumerate(m.attachments or []):
            if att.get('is_inline'):
                continue
            out.append({'ref': f'msg:{m.pk}:{i}', 'name': att.get('name') or 'attachment',
                        'size': att.get('size') or 0,
                        'group': m.sent_at.strftime('%d.%m.%Y') if m.sent_at else ''})
    return out


def attachment_bytes(account, msg, index):
    """Вміст вкладення листа: збережений файл або, якщо немає, — з IMAP (і зберегти на диск)."""
    atts = msg.attachments or []
    if not 0 <= index < len(atts):
        return None
    file_path = atts[index].get('file_path', '')
    if file_path:
        abs_path = os.path.join(str(settings.MEDIA_ROOT), file_path)
        if os.path.exists(abs_path):
            with open(abs_path, 'rb') as fh:
                return fh.read()
    if not msg.imap_uid:
        return None
    import email as email_lib
    from email_assistant.imap_client import IMAPClient, _get_attachments_raw, persist_attachments
    imap_folder = msg.imap_folder_name or account.imap_folder_inbox
    with IMAPClient(account) as imap:
        imap.select_folder(imap_folder)
        _, raw_data = imap.conn.uid('fetch', str(msg.imap_uid).encode(), '(RFC822)')
    if not (raw_data and isinstance(raw_data[0], tuple)):
        return None
    parts = _get_attachments_raw(email_lib.message_from_bytes(raw_data[0][1]))
    if index >= len(parts):
        return None
    data = parts[index].get('_data', b'')
    try:  # кеш на диск для наступного разу
        saved = persist_attachments(account.pk, msg.imap_uid, imap_folder, [dict(parts[index])])
        if saved and saved[0].get('file_path'):
            att_list = list(msg.attachments)
            att_list[index] = {**att_list[index], 'file_path': saved[0]['file_path']}
            msg.attachments = att_list
            msg.save(update_fields=['attachments'])
    except Exception:
        pass
    return data


def can_attach(account, kind: str) -> bool:
    """Документ Minerva у листі — лише якщо власник скриньки (той, хто надсилає) сам може бачити Angebote /
    замовлення (has_perm іде за ролями Minerva, core/permissions.py). Інакше будь-хто з поштою міг би вкласти
    чужий Angebot чи документ замовлення, просто вписавши «offer:<pk>» / «order:<pk>:<файл>»."""
    user = getattr(account, 'user', None)
    if user is None:
        return False
    perm = {'offer': 'offers.view_offer', 'offerint': 'offers.view_offer', 'order': 'sales.view_salesorder'}.get(kind)
    return perm is None or user.has_perm(perm)


def load_ref(account, ref: str):
    """«order:…» / «msg:…» / «offer:…» / «draft:…» → {name, content, content_type} або None (чуже/неіснуюче /
    немає права бачити — None)."""
    from email_assistant.models import EmailMessage
    from sales.models import SalesOrder

    kind, _, rest = (ref or '').partition(':')
    try:
        if kind in ('offer', 'offerint', 'order') and not can_attach(account, kind):
            logger.warning('attachment ref %s: no permission for %s', ref, getattr(account, 'user', None))
            return None
        if kind == 'draft':   # файл / документ, уже збережений у чернетці цієї скриньки
            found = draft_item(account, ref)
            if found is None or str(found[1].get('ref', '')).startswith('draft:'):
                return None
            return load_item(account, found[1])
        if kind in ('offer', 'offerint'):   # документ версії Angebot (PDF / DOCX); offerint — колезі, без «надіслано»
            from offers.services import revision_attachment
            return revision_attachment(int(rest))
        if kind == 'order':
            pk, _, name = rest.partition(':')
            order = SalesOrder.objects.filter(pk=int(pk)).first()
            name = os.path.basename(name)
            if order is None or not name or name.startswith('.'):
                return None
            path = os.path.join(order_dir(order), name)
            if not os.path.isfile(path):
                return None
            with open(path, 'rb') as fh:
                content = fh.read()
            return {'name': name, 'content': content,
                    'content_type': mimetypes.guess_type(name)[0] or 'application/octet-stream'}
        if kind == 'msg':
            pk, _, index = rest.partition(':')
            msg = EmailMessage.objects.filter(pk=int(pk), account=account).first()
            if msg is None:
                return None
            att = (msg.attachments or [])[int(index)]
            content = attachment_bytes(account, msg, int(index))
            if content is None:
                return None
            return {'name': att.get('name') or 'attachment', 'content': content,
                    'content_type': att.get('content_type') or 'application/octet-stream'}
    except (ValueError, IndexError, OSError) as e:
        logger.warning('attachment ref %s: %s', ref, e)
    return None


# ── Вкладення чернеток і запланованих листів ─────────────────────────────────────────────────

def safe_name(name) -> str:
    """Ім'я файлу без шляху й службових символів (для диска)."""
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', name).strip(' .')
    return name[:150] or 'attachment'


def store_file(name, content: bytes, content_type: str = '') -> dict:
    """Зберегти файл у MEDIA_ROOT/email_outbox/<uuid>/ → запис вкладення."""
    display = os.path.basename(str(name or '').replace('\\', '/')).strip()[:255] or 'attachment'
    rel = f'{OUTBOX_DIR}/{uuid.uuid4().hex}/{safe_name(display)}'
    path = os.path.join(str(settings.MEDIA_ROOT), *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(content)
    return {'file': rel, 'name': display, 'size': len(content),
            'content_type': content_type or mimetypes.guess_type(display)[0] or 'application/octet-stream'}


def store_upload(f) -> dict:
    return store_file(f.name, f.read(), getattr(f, 'content_type', '') or '')


def _stored_path(item):
    """Абсолютний шлях збереженого файлу — лише всередині MEDIA_ROOT/email_outbox."""
    rel = str((item or {}).get('file') or '')
    if not rel:
        return None
    root = os.path.realpath(os.path.join(str(settings.MEDIA_ROOT), OUTBOX_DIR))
    path = os.path.realpath(os.path.join(str(settings.MEDIA_ROOT), *rel.split('/')))
    return path if path.startswith(root + os.sep) else None


def stored_bytes(item):
    path = _stored_path(item)
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def copy_stored(item):
    """Копія збереженого файлу (чернетку заплановано — у листа свій файл, чернетка видаляється)."""
    content = stored_bytes(item)
    return None if content is None else store_file(item.get('name'), content, item.get('content_type', ''))


def remove_stored(items, keep=()):
    """Видалити збережені файли записів (крім тих, що лишаються в `keep`)."""
    kept = {i.get('file') for i in keep or () if isinstance(i, dict) and i.get('file')}
    for item in items or ():
        if not isinstance(item, dict) or not item.get('file') or item['file'] in kept:
            continue
        path = _stored_path(item)
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
            os.rmdir(os.path.dirname(path))   # тека <uuid> — лише якщо порожня
        except OSError:
            pass


def draft_item(account, ref):
    """«draft:<pk>:<індекс>» → (чернетка, запис вкладення) — лише з чернеток цієї скриньки; інакше None."""
    from email_assistant.models import EmailDraft
    kind, _, rest = (ref or '').partition(':')
    pk, _, index = rest.partition(':')
    if kind != 'draft' or not pk.isdigit() or not index.isdigit() or account is None:
        return None
    draft = EmailDraft.objects.filter(pk=int(pk), account=account).first()
    items = (draft.attachments or []) if draft else []
    if int(index) >= len(items) or not isinstance(items[int(index)], dict):
        return None
    return draft, items[int(index)]


def load_item(account, item):
    """Запис вкладення чернетки / запланованого листа → {name, content, content_type} або None."""
    if not isinstance(item, dict):
        return None
    if item.get('file'):
        content = stored_bytes(item)
        if content is None:
            return None
        return {'name': item.get('name') or 'attachment', 'content': content,
                'content_type': item.get('content_type') or 'application/octet-stream'}
    if item.get('ref'):
        return load_ref(account, item['ref'])
    return None


def load_items(account, items):
    """Усі вкладення для відправки: (список, '') або ([], ім'я недоступного) — без файлу лист не йде."""
    out = []
    for item in items or ():
        att = load_item(account, item)
        if att is None:
            name = item.get('name') if isinstance(item, dict) else ''
            return [], name or str((item or {}).get('ref', '')).rsplit(':', 1)[-1] or 'attachment'
        out.append(att)
    return out, ''


def from_form(account, refs, names, files, draft=None, check=False):
    """Вкладення форми листа → записи для чернетки / запланованого листа: (записи, '') або ([], помилка).

    refs — att_ref (документи Minerva й «draft:<pk>:<i>» — уже збережене в чернетці), names — їхні назви з форми
    (у тому самому порядку; лише для показу), files — нові файли з комп'ютера (зберігаються в MEDIA).
    draft — чернетка, яку оновлюємо: її файли лишаються на місці; файли іншої чернетки копіюються.
    check — одразу перевірити документи Minerva (запланований лист: людина бачить помилку зараз, а не вночі)."""
    names = list(names or [])
    plan = []
    for i, ref in enumerate(refs or []):
        ref = (ref or '').strip()
        if not ref:
            continue
        name = (names[i] if len(names) == len(refs) else '') or ref.rsplit(':', 1)[-1]
        if ref.startswith('draft:'):
            found = draft_item(account, ref)
            if found is None:
                return [], f'Вкладення недоступне: {name}'
            src, item = found
            plan.append(('keep' if draft is not None and src.pk == draft.pk else 'copy', dict(item)))
            continue
        if check:
            att = load_ref(account, ref)
            if att is None:
                return [], f'Вкладення недоступне: {name}'
            name = att['name']
        plan.append(('ref', {'ref': ref, 'name': os.path.basename(str(name))[:255] or 'attachment'}))
    out, copies = [], []
    for how, item in plan:
        if how == 'copy' and item.get('file'):
            copy = copy_stored(item)
            if copy is None:
                remove_stored(copies)
                return [], f'Вкладення недоступне: {item.get("name") or "attachment"}'
            copies.append(copy)
            item = copy
        out.append(item)
    out += [store_upload(f) for f in files or ()]
    return out, ''


def reply_options(emails, links) -> dict:
    """Що можна швидко додати до відповіді: документи замовлень гілки і вкладення її листів."""
    docs = []
    for link in links or []:
        docs += order_documents(link['order'])
    return {'order_docs': docs, 'thread_files': thread_attachments(emails)}
