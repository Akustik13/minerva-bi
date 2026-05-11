"""email_assistant/imap_client.py — IMAP читання пошти."""
import email as email_lib
import imaplib
import logging
import time
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

    def list_folders(self) -> list:
        """Return list of dicts {name, selectable, level} for all IMAP folders."""
        import re
        try:
            _, data = self.conn.list()
        except Exception as e:
            logger.warning('IMAP list() failed: %s', e)
            return []
        folders = []
        for item in data:
            if not item:
                continue
            if isinstance(item, bytes):
                item = item.decode('utf-8', errors='replace')
            # Format: (\flags) "delimiter" "folder_name" or (\flags) "delimiter" folder_name
            m = re.match(r'\(([^)]*)\)\s+"([^"]+)"\s+(.+)', item.strip())
            if not m:
                continue
            flags_str = m.group(1)
            delimiter = m.group(2)
            raw_name  = m.group(3).strip().strip('"')
            noselect  = 'Noselect' in flags_str or 'noselect' in flags_str
            level     = raw_name.count(delimiter) if delimiter else 0
            folders.append({'name': raw_name, 'selectable': not noselect, 'level': level})
        return folders

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

    def _select_writable(self, folder: str) -> str | None:
        """Select a folder in read-write mode. Returns the folder name on success."""
        candidates = [folder, f'"{folder}"']
        if any(k in folder.lower() for k in ('sent', 'gesendet', 'gesend')):
            candidates += [
                'Gesendete Objekte', '"Gesendete Objekte"',
                'Sent', 'INBOX.Sent', '[Gmail]/Sent Mail', 'Sent Items',
            ]
        for c in candidates:
            try:
                status, _ = self.conn.select(c)  # writable (no readonly)
                if status == 'OK':
                    return c
            except Exception:
                continue
        return None

    def append_to_sent(self, msg_bytes: bytes) -> bool:
        """Save sent message to the IMAP Sent folder via APPEND."""
        folder = self.account.imap_folder_sent
        if not folder:
            return False
        selected = self._select_writable(folder)
        if not selected:
            logger.warning('Cannot select sent folder for append: %s', folder)
            return False
        try:
            self.conn.append(
                selected,
                '(\\Seen)',
                imaplib.Time2Internaldate(time.time()),
                msg_bytes,
            )
            return True
        except Exception as e:
            logger.warning('IMAP append to sent failed: %s', e)
            return False

    def mark_seen(self, folder: str, uid: int) -> bool:
        """Mark a message as \\Seen on the IMAP server."""
        try:
            self._select_writable(folder)
            self.conn.uid('STORE', str(uid).encode(), '+FLAGS', '(\\Seen)')
            return True
        except Exception as e:
            logger.warning('IMAP mark_seen failed uid=%s: %s', uid, e)
            return False

    def move_to_trash(self, folder: str, uid: int) -> bool:
        """Move a message to Trash folder. Falls back to marking \\Deleted."""
        try:
            self._select_writable(folder)
            uid_str = str(uid).encode()
            trash_candidates = [
                'Trash', '[Gmail]/Trash', 'INBOX.Trash',
                'Deleted Items', 'Deleted Messages',
            ]
            for trash in trash_candidates:
                try:
                    ok, _ = self.conn.uid('COPY', uid_str, trash)
                    if ok == 'OK':
                        self.conn.uid('STORE', uid_str, '+FLAGS', '(\\Deleted)')
                        self.conn.expunge()
                        return True
                except Exception:
                    continue
            # Fallback: just mark deleted
            self.conn.uid('STORE', uid_str, '+FLAGS', '(\\Deleted)')
            self.conn.expunge()
            return True
        except Exception as e:
            logger.warning('IMAP move_to_trash failed uid=%s: %s', uid, e)
            return False
