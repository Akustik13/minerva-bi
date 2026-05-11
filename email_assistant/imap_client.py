"""email_assistant/imap_client.py — IMAP читання пошти."""
import email as email_lib
import imaplib
import logging
from datetime import datetime, timedelta, timezone as dt_tz
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

from django.utils import timezone

logger = logging.getLogger('email_assistant')


def _decode_str(raw) -> str:
    if not raw:
        return ''
    parts = decode_header(raw)
    result = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(enc or 'utf-8', errors='replace'))
        else:
            result.append(str(chunk))
    return ' '.join(result).strip()


def _get_body(msg) -> tuple:
    text_body = html_body = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct  = part.get_content_type()
            cd  = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or 'utf-8'
            decoded = payload.decode(charset, errors='replace')
            if ct == 'text/plain' and not text_body:
                text_body = decoded[:10000]
            elif ct == 'text/html' and not html_body:
                html_body = decoded[:20000]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset   = msg.get_content_charset() or 'utf-8'
            text_body = payload.decode(charset, errors='replace')[:10000]
    return text_body, html_body


def _get_attachments(msg) -> list:
    attachments = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        if 'attachment' in cd or ('inline' in cd and part.get_filename()):
            filename = _decode_str(part.get_filename(''))
            if filename:
                attachments.append({
                    'name':         filename,
                    'content_type': part.get_content_type(),
                    'size':         len(part.get_payload(decode=True) or b''),
                })
    return attachments


class IMAPClient:
    def __init__(self, account):
        self.account = account
        self.conn    = None

    def connect(self):
        if self.account.imap_use_ssl:
            self.conn = imaplib.IMAP4_SSL(self.account.imap_host, self.account.imap_port)
        else:
            self.conn = imaplib.IMAP4(self.account.imap_host, self.account.imap_port)
            self.conn.starttls()
        self.conn.login(self.account.imap_username, self.account.imap_password)
        return self

    def disconnect(self):
        try:
            self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.disconnect()

    def select_folder(self, folder: str) -> bool:
        candidates = [folder, f'"{folder}"']
        if any(k in folder.lower() for k in ('sent', 'gesendet', 'gesend')):
            candidates += [
                'Sent', '"Sent"', 'Gesendete Objekte', '"Gesendete Objekte"',
                'INBOX.Sent', '[Gmail]/Sent Mail', 'Sent Items',
            ]
        for c in candidates:
            try:
                status, _ = self.conn.select(c, readonly=True)
                if status == 'OK':
                    return True
            except Exception:
                continue
        return False

    def fetch_messages(self, folder: str = 'INBOX', days_back: int = 30, since_uid: int = 0) -> list:
        if not self.select_folder(folder):
            logger.warning('Cannot select folder: %s', folder)
            return []

        since = (datetime.now() - timedelta(days=days_back)).strftime('%d-%b-%Y')
        _, data = self.conn.uid('search', None, f'SINCE {since}')
        if not data or not data[0]:
            return []

        uids = data[0].split()
        if since_uid > 0:
            uids = [u for u in uids if int(u) > since_uid]

        messages = []
        for uid_bytes in uids[-100:]:
            uid = int(uid_bytes)
            try:
                _, msg_data = self.conn.uid('fetch', uid_bytes, '(RFC822 FLAGS)')
                if not msg_data or not msg_data[0]:
                    continue

                raw   = msg_data[0][1]
                msg   = email_lib.message_from_bytes(raw)
                flags = str(msg_data[0][0])

                subject    = _decode_str(msg.get('Subject', ''))
                from_name, from_email = parseaddr(msg.get('From', ''))
                from_name  = _decode_str(from_name)

                to_raw  = msg.get('To', '')
                cc_raw  = msg.get('Cc', '')
                to_list = [e for _, e in [parseaddr(a) for a in to_raw.split(',') if a.strip()] if e]
                cc_list = [e for _, e in [parseaddr(a) for a in cc_raw.split(',') if a.strip()] if e]

                try:
                    sent_at = parsedate_to_datetime(msg.get('Date', ''))
                    if sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=dt_tz.utc)
                except Exception:
                    sent_at = timezone.now()

                text_body, html_body = _get_body(msg)

                messages.append({
                    'uid':         uid,
                    'message_id':  msg.get('Message-ID', '').strip(),
                    'in_reply_to': msg.get('In-Reply-To', '').strip(),
                    'subject':     subject,
                    'from_email':  from_email.lower(),
                    'from_name':   from_name,
                    'to_emails':   to_list,
                    'cc_emails':   cc_list,
                    'body_text':   text_body,
                    'body_html':   html_body,
                    'attachments': _get_attachments(msg),
                    'is_read':     '\\Seen' in flags,
                    'sent_at':     sent_at,
                })
            except Exception as e:
                logger.error('Error fetching uid %s: %s', uid, e)

        return messages
