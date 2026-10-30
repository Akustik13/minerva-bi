"""email_assistant/imap_client.py — IMAP читання пошти."""
import base64
import email as email_lib
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone as dt_tz
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

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


class _HTMLText(HTMLParser):
    """Мінімальний HTML → текст: блоки → нові рядки, script/style відкидаються."""
    _BLOCK = {'p', 'div', 'tr', 'table', 'ul', 'ol', 'blockquote', 'hr', 'section',
              'article', 'header', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre'}
    _SKIP = {'script', 'style', 'head', 'title'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == 'br':
            self.parts.append('\n')
        elif tag == 'li':
            self.parts.append('\n• ')
        elif tag in self._BLOCK:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if tag in ('br', 'hr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(re.sub(r'\s+', ' ', data))


def html_to_text(html: str) -> str:
    """Читабельний текст з HTML-листа (для прев'ю, пошуку, AI і CRM)."""
    if not html:
        return ''
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r'<[^>]+>', ' ', html)
    lines = [ln.replace('\xa0', ' ').strip() for ln in ''.join(parser.parts).split('\n')]
    text = '\n'.join(lines)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def looks_like_html(text: str) -> bool:
    """Чи це HTML-розмітка, збережена як текст (старі однокомпонентні text/html листи)."""
    head = (text or '').lstrip()[:2000].lower()
    if not head.startswith('<'):
        return False
    return bool(re.search(r'</(html|body|div|p|table|span|font)>|<br\s*/?>', head))


def _decode_payload(part, payload: bytes) -> str:
    charset = part.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, errors='replace')
    except LookupError:  # невідоме кодування в заголовку (напр. unknown-8bit)
        return payload.decode('utf-8', errors='replace')


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
            decoded = _decode_payload(part, payload)
            if ct == 'text/plain' and not text_body:
                text_body = decoded[:200000]
            elif ct == 'text/html' and not html_body:
                html_body = decoded[:1000000]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = _decode_payload(msg, payload)
            # Веб-пошта (IONOS/OX, GMX…) часто шле лише text/html без multipart
            if msg.get_content_type() == 'text/html':
                html_body = decoded[:1000000]
            else:
                text_body = decoded[:200000]
    if html_body and not text_body:
        text_body = html_to_text(html_body)[:200000]
    return text_body, html_body


def parse_addresses(*headers) -> list:
    """Адреси з заголовків To/Cc (коми в іменах "Прізвище, Ім'я" не ламають розбір)."""
    return [addr.lower() for _, addr in getaddresses([str(h) for h in headers if h]) if '@' in addr]


_CONTENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


def safe_content_type(value) -> str:
    """Тип вкладення лише у вигляді «тип/підтип» (його задає відправник листа: «image/<svg onload=…>» — теж
    «тип»); інше → application/octet-stream."""
    ct = str(value or '').split(';')[0].strip().lower()
    return ct if _CONTENT_TYPE_RE.match(ct) else 'application/octet-stream'


def _get_attachments(msg) -> list:
    attachments = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        if 'attachment' in cd or ('inline' in cd and part.get_filename()):
            filename = _decode_str(part.get_filename(''))
            if filename:
                attachments.append({
                    'name':         filename,
                    'content_type': safe_content_type(part.get_content_type()),
                    'size':         len(part.get_payload(decode=True) or b''),
                })
    return attachments


def _get_attachments_raw(msg) -> list:
    """Like _get_attachments but includes '_data': bytes for each attachment."""
    attachments = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        cid = str(part.get('Content-ID', '')).strip().strip('<>')
        ct_main = part.get_content_maintype()
        if 'attachment' in cd or ('inline' in cd and part.get_filename()):
            filename = _decode_str(part.get_filename(''))
            if filename:
                data = part.get_payload(decode=True) or b''
                attachments.append({
                    'name':         filename,
                    'content_type': safe_content_type(part.get_content_type()),
                    'size':         len(data),
                    '_data':        data,
                    'content_id':   cid,
                })
        elif part.get_content_type() == 'text/calendar':
            # Запрошення Outlook / Teams: блок text/calendar без імені файлу — зберігається як invite.ics
            # (календар Minerva знаходить у ньому зустріч, calendar_app/invites.py)
            data = part.get_payload(decode=True) or b''
            if data:
                attachments.append({
                    'name':         'invite.ics',
                    'content_type': 'text/calendar',
                    'size':         len(data),
                    '_data':        data,
                    'content_id':   cid,
                })
        elif cid and ct_main == 'image':
            # Inline CID image (embedded in HTML body)
            filename = _decode_str(part.get_filename('') or '') or f'img_{cid[:16]}'
            data = part.get_payload(decode=True) or b''
            attachments.append({
                'name':         filename,
                'content_type': safe_content_type(part.get_content_type()),
                'size':         len(data),
                '_data':        data,
                'content_id':   cid,
                'is_inline':    True,
            })
    return attachments


def persist_attachments(account_pk, imap_uid, folder_name, attachments: list) -> list:
    """Save attachment binaries to media/email_attachments/. Returns cleaned list (no _data)."""
    import os, hashlib
    from django.conf import settings
    folder_hash = hashlib.md5(folder_name.encode()).hexdigest()[:8]
    rel_dir = f'email_attachments/{account_pk}/{imap_uid}_{folder_hash}'
    saved = []
    for i, att in enumerate(attachments):
        data = att.pop('_data', None)
        clean = {'name': att['name'], 'content_type': safe_content_type(att['content_type']), 'size': att['size']}
        if data:
            try:
                abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
                os.makedirs(abs_dir, exist_ok=True)
                safe = ''.join(c for c in att['name'] if c.isalnum() or c in '.-_ ')[:80].strip() or 'file'
                abs_path = os.path.join(abs_dir, f'{i}_{safe}')
                with open(abs_path, 'wb') as fh:
                    fh.write(data)
                clean['file_path'] = f'{rel_dir}/{i}_{safe}'
            except Exception as ex:
                logger.warning('save attachment %s: %s', att['name'], ex)
        saved.append(clean)
    return saved


# LIST-відповідь: (\HasNoChildren \Sent) "/" "INBOX/Meine Order"  |  (...) NIL Name
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$', re.I)
_SPECIAL_USE = ('\\Sent', '\\Trash', '\\Junk', '\\Drafts', '\\Archive', '\\All', '\\Flagged')


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r'\\(.)', r'\1', value[1:-1])
    return value


def _list_line(item) -> str:
    """Рядок LIST; назви з особливими символами сервер шле літералом: (b'(...) "/" {12}', b'name')."""
    if isinstance(item, tuple):
        head = item[0].decode('utf-8', errors='replace') if isinstance(item[0], bytes) else str(item[0])
        name = item[1].decode('utf-8', errors='replace') if isinstance(item[1], bytes) else str(item[1])
        name = name.replace('\\', '\\\\').replace('"', '\\"')
        return re.sub(r'\{\d+\}\s*$', '', head).rstrip() + ' "' + name + '"'
    if isinstance(item, bytes):
        item = item.decode('utf-8', errors='replace')
    return (item or '').strip()


def decode_mutf7(name: str) -> str:
    """Назви IMAP-папок у modified UTF-7 (RFC 3501): «Entw&APw-rfe» → «Entwürfe»."""
    def _dec(m):
        chunk = m.group(1)
        if not chunk:
            return '&'
        b64 = chunk.replace(',', '/')
        try:
            return base64.b64decode(b64 + '=' * (-len(b64) % 4)).decode('utf-16-be')
        except Exception:
            return m.group(0)
    return re.sub(r'&([A-Za-z0-9+,]*)-', _dec, name or '')


class IMAPClient:
    def __init__(self, account):
        self.account = account
        self.conn    = None

    def connect(self):
        if self.account.imap_use_ssl:
            self.conn = imaplib.IMAP4_SSL(self.account.imap_host, self.account.imap_port,
                                          timeout=90)
        else:
            self.conn = imaplib.IMAP4(self.account.imap_host, self.account.imap_port,
                                      timeout=90)
            self.conn.starttls()
        # ensure the timeout applies to all operations, not just initial connect
        try:
            self.conn.sock.settimeout(90)
        except Exception:
            pass
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
        """
        Папки сервера: {name (сирa IMAP-назва для команд), label (читабельна, остання частина),
        path (читабельний повний шлях), selectable, level, special (\\Sent, \\Trash…)}.
        """
        try:
            _, data = self.conn.list()
        except Exception as e:
            logger.warning('IMAP list() failed: %s', e)
            return []
        folders = []
        for item in data or []:
            line = _list_line(item)
            if not line:
                continue
            m = _LIST_RE.match(line)
            if not m:
                logger.info('IMAP LIST: не розібрано рядок %r', line)
                continue
            flags = m.group('flags').lower()
            delim = _unquote(m.group('delim')) if m.group('delim').upper() != 'NIL' else ''
            raw = _unquote(m.group('name').strip())
            path = decode_mutf7(raw)
            folders.append({
                'name':       raw,
                'label':      path.rsplit(delim, 1)[-1] if delim else path,
                'path':       path,
                'selectable': '\\noselect' not in flags and '\\nonexistent' not in flags,
                'level':      raw.count(delim) if delim else 0,
                'special':    next((f for f in _SPECIAL_USE if f.lower() in flags), ''),
            })
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

    def search_uids_after(self, folder: str, after_uid: int = 0):
        """UID листів папки, новіших за after_uid (0 — усі). None — папку не вдалось вибрати."""
        if not self.select_folder(folder):
            logger.warning('Cannot select folder: %s', folder)
            return None
        criteria = f'UID {after_uid + 1}:*' if after_uid else 'ALL'
        _, data = self.conn.uid('search', None, criteria)
        if not data or not data[0]:
            return []
        # «UID n:*» завжди повертає останній лист, навіть якщо його UID < n
        return [u for u in data[0].split() if int(u) > after_uid]

    def search_unseen(self, folder: str):
        """Множина UID непрочитаних (без \\Seen) листів папки; None — папку не вдалось вибрати."""
        if not self.select_folder(folder):
            return None
        _, data = self.conn.uid('search', None, 'UNSEEN')
        return {int(u) for u in (data[0].split() if data and data[0] else [])}

    def set_seen(self, folder: str, uids, seen: bool = True) -> bool:
        """Поставити/зняти \\Seen для кількох листів однією командою."""
        uids = [str(int(u)) for u in uids if u]
        if not uids:
            return True
        if not self._select_writable(folder):
            logger.warning('IMAP set_seen: cannot select %s', folder)
            return False
        try:
            for start in range(0, len(uids), 200):
                self.conn.uid('STORE', ','.join(uids[start:start + 200]),
                              '+FLAGS' if seen else '-FLAGS', '(\\Seen)')
            return True
        except Exception as e:
            logger.warning('IMAP set_seen %s %s: %s', folder, uids[:5], e)
            return False

    def search_uids(self, folder: str, days_back: int = 30,
                    since_uid: int = 0, limit=None) -> list:
        """SEARCH only — fast, no body download. Returns list of UID bytes."""
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
        if limit is not None:
            uids = uids[-limit:]
        return uids

    def _parse_one_message(self, header_part: bytes, raw_body: bytes) -> 'dict | None':
        """Parse a single (header, body) pair from an IMAP FETCH response."""
        try:
            uid_match = re.search(rb'UID\s+(\d+)', header_part)
            uid       = int(uid_match.group(1)) if uid_match else 0
            flags_str = header_part.decode('ascii', errors='replace')

            msg       = email_lib.message_from_bytes(raw_body)
            subject   = _decode_str(msg.get('Subject', ''))
            from_name, from_email = parseaddr(msg.get('From', ''))
            from_name = _decode_str(from_name)

            to_list = parse_addresses(msg.get('To', ''))
            cc_list = parse_addresses(msg.get('Cc', ''))

            try:
                sent_at = parsedate_to_datetime(msg.get('Date', ''))
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=dt_tz.utc)
            except Exception:
                sent_at = timezone.now()

            text_body, html_body = _get_body(msg)

            return {
                'uid':         uid,
                'message_id':  msg.get('Message-ID', '').strip(),
                'in_reply_to': msg.get('In-Reply-To', '').strip(),
                'references':  re.findall(r'<[^<>\s]+>', str(msg.get('References', '') or '')),
                'subject':     subject,
                'from_email':  from_email.lower(),
                'from_name':   from_name,
                'to_emails':   to_list,
                'cc_emails':   cc_list,
                'body_text':   text_body,
                'body_html':   html_body,
                'attachments': _get_attachments_raw(msg),
                'is_read':     '\\Seen' in flags_str,
                'sent_at':     sent_at,
                'list_unsubscribe': bool(msg.get('List-Unsubscribe')),
            }
        except Exception as e:
            logger.error('Error parsing message: %s', e)
            return None

    def fetch_by_uids_iter(self, uids: list, chunk_size: int = 50):
        """Generator: fetch in chunks of chunk_size. Yields message dicts with _data in attachments."""
        for start in range(0, len(uids), chunk_size):
            chunk   = uids[start:start + chunk_size]
            uid_set = b','.join(chunk)
            try:
                _, raw_data = self.conn.uid('fetch', uid_set, '(RFC822 FLAGS)')
            except Exception as e:
                logger.error('IMAP chunk fetch [%d:%d] failed: %s', start, start + chunk_size, e)
                continue
            if not raw_data:
                continue
            i = 0
            while i < len(raw_data):
                item = raw_data[i]
                i += 1
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                parsed = self._parse_one_message(item[0], item[1])
                if parsed:
                    yield parsed

    def fetch_messages(self, folder: str = 'INBOX', days_back: int = 30,
                       since_uid: int = 0, limit=200) -> list:
        """Fetch messages as a list. Attachment _data is stripped (use fetch_by_uids_iter for raw)."""
        uids = self.search_uids(folder, days_back, since_uid, limit)
        if not uids:
            return []
        result = []
        for m in self.fetch_by_uids_iter(uids):
            for att in m.get('attachments', []):
                att.pop('_data', None)
            result.append(m)
        return result

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

    def create_folder(self, name: str):
        """Create a new IMAP folder. Returns True on success or error string."""
        try:
            status, data = self.conn.create(name)
            if status == 'OK':
                return True
            msg = (data[0].decode() if data and data[0] else '') or 'NO'
            logger.warning('IMAP create_folder %s: %s %s', name, status, msg)
            return msg or status
        except Exception as e:
            logger.warning('IMAP create_folder %s: %s', name, e)
            return str(e)

    def rename_folder(self, old_name: str, new_name: str):
        """Rename an IMAP folder. Returns True on success or error string."""
        try:
            status, data = self.conn.rename(old_name, new_name)
            if status == 'OK':
                return True
            msg = (data[0].decode() if data and data[0] else '') or 'NO'
            logger.warning('IMAP rename_folder %s→%s: %s %s', old_name, new_name, status, msg)
            return msg or status
        except Exception as e:
            logger.warning('IMAP rename_folder %s→%s: %s', old_name, new_name, e)
            return str(e)

    def delete_folder(self, name: str):
        """Delete an IMAP folder. Returns True on success or error string."""
        try:
            status, data = self.conn.delete(name)
            if status == 'OK':
                return True
            msg = (data[0].decode() if data and data[0] else '') or 'NO'
            logger.warning('IMAP delete_folder %s: %s %s', name, status, msg)
            return msg or status
        except Exception as e:
            logger.warning('IMAP delete_folder %s: %s', name, e)
            return str(e)

    def mark_seen(self, folder: str, uid: int) -> bool:
        """Mark a message as \\Seen on the IMAP server."""
        try:
            self._select_writable(folder)
            self.conn.uid('STORE', str(uid).encode(), '+FLAGS', '(\\Seen)')
            return True
        except Exception as e:
            logger.warning('IMAP mark_seen failed uid=%s: %s', uid, e)
            return False

    def mark_unseen(self, folder: str, uid: int) -> bool:
        """Remove \\Seen flag on the IMAP server (mark as unread)."""
        try:
            self._select_writable(folder)
            self.conn.uid('STORE', str(uid).encode(), '-FLAGS', '(\\Seen)')
            return True
        except Exception as e:
            logger.warning('IMAP mark_unseen failed uid=%s: %s', uid, e)
            return False

    def move_to_spam(self, folder: str, uid: int) -> bool:
        """Copy message to Spam/Junk folder and delete from source."""
        try:
            self._select_writable(folder)
            uid_str = str(uid).encode()
            spam_candidates = [
                'Spam', 'Junk', '[Gmail]/Spam', 'INBOX.Spam', 'INBOX.Junk',
                'Junk Email', 'Junk Mail',
            ]
            for spam in spam_candidates:
                try:
                    ok, _ = self.conn.uid('COPY', uid_str, spam)
                    if ok == 'OK':
                        self.conn.uid('STORE', uid_str, '+FLAGS', '(\\Deleted)')
                        self.conn.expunge()
                        return True
                except Exception:
                    continue
            return False
        except Exception as e:
            logger.warning('IMAP move_to_spam failed uid=%s: %s', uid, e)
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
