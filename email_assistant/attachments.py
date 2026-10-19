"""
email_assistant/attachments.py — вкладення для листа-відповіді.

Окрім файлів з комп'ютера, до відповіді можна додати:
  - документи замовлення, згаданого в листуванні (media/orders/{source}/{order_number}/ —
    інвойси, пакувальні листи, CN23…);
  - вкладення з листів цієї гілки (з диска або, якщо ще не збережені, з IMAP).
Браузер передає лише посилання «order:<pk>:<ім'я файлу>» / «msg:<pk>:<індекс>», файли читає сервер.
"""
import logging
import mimetypes
import os

from django.conf import settings

logger = logging.getLogger('email_assistant')

MAX_ORDER_DOCS = 20


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


def load_ref(account, ref: str):
    """«order:…» / «msg:…» → {name, content, content_type} або None (чуже/неіснуюче — None)."""
    from email_assistant.models import EmailMessage
    from sales.models import SalesOrder

    kind, _, rest = (ref or '').partition(':')
    try:
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


def reply_options(emails, links) -> dict:
    """Що можна швидко додати до відповіді: документи замовлень гілки і вкладення її листів."""
    docs = []
    for link in links or []:
        docs += order_documents(link['order'])
    return {'order_docs': docs, 'thread_files': thread_attachments(emails)}
