"""
scraper/sites/email_imap.py — IMAP email scraper.

Завантажує PDF-вкладення з електронних листів (рахунки від постачальників).
Не потребує Playwright — чиста IMAP-бібліотека.

Flow:
  1. Підключається до IMAP сервера
  2. Шукає листи з указаного періоду (SINCE date)
  3. Опційно фільтрує за відправником і темою
  4. Завантажує PDF/XML вкладення в output_dir/email/
  5. Позначає листи прочитаними (якщо увімкнено)
"""
import asyncio
import email as email_lib
import email.header
import imaplib
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger('scraper.email')


class EmailScraper:
    """IMAP invoice scraper — implements run_async(start, end) → list[str]."""

    SITE_NAME = 'email'

    def __init__(self, username: str, password: str, output_dir: str,
                 imap_host: str = 'imap.gmail.com',
                 imap_port: int = 993,
                 imap_use_ssl: bool = True,
                 imap_folder: str = 'INBOX',
                 email_from_filter: str = '',
                 email_subject_kw: str = '',
                 email_attach_ext: str = '.pdf',
                 email_mark_read: bool = True,
                 **kwargs):
        self.username          = username
        self.password          = password
        self.output_dir        = Path(output_dir) / 'email'
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.imap_host         = imap_host
        self.imap_port         = imap_port
        self.imap_use_ssl      = imap_use_ssl
        self.imap_folder       = imap_folder or 'INBOX'
        self.email_from_filter = email_from_filter.strip()
        self.email_subject_kw  = email_subject_kw.strip()
        self.email_mark_read   = email_mark_read

        raw_exts = email_attach_ext.split(',') if email_attach_ext else ['.pdf']
        self.attach_exts = {e.strip().lower() for e in raw_exts if e.strip()}
        if not self.attach_exts:
            self.attach_exts = {'.pdf'}

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_async(self, start: str, end: str) -> list[str]:
        """Async entry-point called by run_site() in services.py."""
        return await asyncio.to_thread(self._run_sync, start, end)

    # ── IMAP logic ────────────────────────────────────────────────────────────

    def _connect(self):
        log.info('Підключення до %s:%d (SSL=%s)', self.imap_host, self.imap_port, self.imap_use_ssl)
        if self.imap_use_ssl:
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        else:
            conn = imaplib.IMAP4(self.imap_host, self.imap_port)
        conn.login(self.username, self.password)
        log.info('Вхід виконано: %s', self.username)
        return conn

    def _build_search_criteria(self, since_str: str) -> list:
        """Build IMAP SEARCH criteria list."""
        criteria = [f'SINCE {since_str}']
        if self.email_from_filter:
            criteria.append(f'FROM "{self.email_from_filter}"')
        if self.email_subject_kw:
            # Use first keyword; OR for multiple is handled in _check_subject
            first_kw = self.email_subject_kw.split()[0]
            criteria.append(f'SUBJECT "{first_kw}"')
        return criteria

    def _decode_header_value(self, raw) -> str:
        parts = email_lib.header.decode_header(raw or '')
        result = []
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                result.append(chunk.decode(enc or 'utf-8', errors='replace'))
            else:
                result.append(str(chunk))
        return ''.join(result)

    def _check_subject(self, subject: str) -> bool:
        """OR match: any keyword present → True."""
        if not self.email_subject_kw:
            return True
        keywords = self.email_subject_kw.lower().split()
        subj_low = subject.lower()
        return any(kw in subj_low for kw in keywords)

    def _safe_filename(self, name: str) -> str:
        return re.sub(r'[^\w.\-]', '_', name)

    def _unique_path(self, fpath: Path) -> Path:
        if not fpath.exists():
            return fpath
        counter = 1
        while True:
            candidate = fpath.parent / f'{fpath.stem}_{counter}{fpath.suffix}'
            if not candidate.exists():
                return candidate
            counter += 1

    def _process_message(self, conn, msg_id: bytes) -> list[str]:
        status, data = conn.fetch(msg_id, '(RFC822)')
        if status != 'OK' or not data or not data[0]:
            return []

        msg = email_lib.message_from_bytes(data[0][1])
        subject = self._decode_header_value(msg.get('Subject', ''))

        if not self._check_subject(subject):
            return []

        log.info('Обробляємо лист: "%s"', subject[:70])

        saved = []
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            disposition = part.get('Content-Disposition', '')
            if not disposition:
                continue

            raw_name = part.get_filename()
            if not raw_name:
                continue

            fname = self._decode_header_value(raw_name)
            suffix = Path(fname).suffix.lower()
            if suffix not in self.attach_exts:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            safe_name = self._safe_filename(fname)
            fpath     = self._unique_path(self.output_dir / safe_name)
            fpath.write_bytes(payload)
            log.info('✅ Збережено: %s (%d KB)', fpath.name, len(payload) // 1024)
            saved.append(str(fpath))

        if saved and self.email_mark_read:
            conn.store(msg_id, '+FLAGS', '\\Seen')

        return saved

    def _run_sync(self, start: str, end: str) -> list[str]:
        start_dt  = datetime.strptime(start, '%Y-%m-%d')
        since_str = start_dt.strftime('%d-%b-%Y')  # IMAP format: 01-Jan-2026

        conn = self._connect()
        try:
            status, _ = conn.select(self.imap_folder)
            if status != 'OK':
                raise RuntimeError(f'Не вдалося відкрити папку: {self.imap_folder}')

            criteria = self._build_search_criteria(since_str)
            log.info('IMAP search: %s', ' '.join(criteria))

            status, data = conn.search(None, *criteria)
            if status != 'OK' or not data or not data[0]:
                log.info('Листів за критеріями не знайдено')
                return []

            msg_ids = data[0].split()
            log.info('Знайдено листів: %d', len(msg_ids))

            downloaded = []
            for msg_id in msg_ids:
                files = self._process_message(conn, msg_id)
                downloaded.extend(files)

            log.info('Всього завантажено вкладень: %d', len(downloaded))
            return downloaded

        finally:
            try:
                conn.logout()
            except Exception:
                pass
