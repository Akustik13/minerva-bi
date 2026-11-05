"""Лист → замовлення за текстом вкладення (email_assistant/links.attachment_tokens / order_in_attachments).

Супровідний документ експорту (ABD) приходить від noreply@dakosy.de: у темі — лише LRN і MRN, у тексті — підпис
експедитора, а номер замовлення є тільки в PDF («Referenznummer/UCR [12 08] 102497269»).

python manage.py test email_assistant.tests_attachment_links --settings=tabele.settings_test
"""
import io
import os
import shutil
import tempfile
from datetime import datetime, timezone as dt_tz
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from crm.models import Customer, CustomerTimeline
from email_assistant.models import EmailAccount, EmailMessage
from email_assistant.imap_client import IMAPClient as RealIMAPClient
from sales.models import SalesOrder

SUBJECT = 'ABD - Ausfuhrbegleitdokument (DE), 187472-1, 26DE760298696748B4'
SIGNATURE = 'Mit freundlichen Grüßen\nNicolas Fischer\nM2 Logistik GmbH\n85570 Markt Schwaben\nTel: +49 (8121) 2230-14'


def pdf_bytes(*lines) -> bytes:
    """Найпростіший PDF з рядками тексту (Helvetica) — без reportlab; pypdf читає його, як справжній."""
    esc = [ln.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') for ln in lines]
    stream = 'BT /F1 11 Tf 40 800 Td 14 TL ' + ' '.join(f'({ln}) Tj T*' for ln in esc) + ' ET'
    objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> '
        '/Contents 4 0 R >>',
        f'<< /Length {len(stream.encode("latin-1"))} >>\nstream\n{stream}\nendstream',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    buf = io.BytesIO()
    buf.write(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(buf.tell())
        buf.write(f'{i} 0 obj\n{body}\nendobj\n'.encode('latin-1'))
    xref = buf.tell()
    buf.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode('latin-1'))
    for off in offsets:
        buf.write(f'{off:010d} 00000 n \n'.encode('latin-1'))
    buf.write(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode('latin-1'))
    return buf.getvalue()


class AttachmentLinkTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(
            user=self.user, email_address='viacheslav.pryimak@sevskiy.de',
            imap_username='viacheslav.pryimak@sevskiy.de', imap_password='x',
            smtp_username='viacheslav.pryimak@sevskiy.de', smtp_password='x',
            imap_folder_inbox='INBOX', imap_folder_sent='Gesendete Objekte')
        self.diwakar = Customer.objects.create(name='Diwakar Somu', email='diwakar.somu@halter.co.nz')
        self.order = self._order('102497269', self.diwakar)

    def _order(self, number, customer):
        return SalesOrder.objects.create(source='digikey', order_number=number, customer_key=customer.external_key,
                                         email=customer.email, affects_stock=False)

    def _file(self, name, data):
        rel = os.path.join('email_attachments', str(self.account.pk), name)
        os.makedirs(os.path.join(self.media, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(self.media, rel), 'wb') as fh:
            fh.write(data)
        return {'name': name, 'file_path': rel, 'size': len(data), 'content_type': 'application/pdf'}

    def _abd(self, *attachments, subject=SUBJECT, body=SIGNATURE):
        return EmailMessage.objects.create(
            account=self.account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name='INBOX', imap_uid=7,
            subject=subject, from_email='noreply@dakosy.de', to_emails=['viacheslav.pryimak@sevskiy.de'],
            cc_emails=[], body_text=body, attachments=list(attachments),
            sent_at=datetime(2026, 11, 2, 14, 1, tzinfo=dt_tz.utc))

    def _abd_pdf(self, *numbers):
        return self._file('EXP-1-DE2211955-0000.pdf', pdf_bytes(
            'EUROPAEISCHE UNION - AUSFUHRBEGLEITDOKUMENT', 'Empfaenger [13 03] Diwakar Somu',
            'SEVSKIY Schatzbogen 43 81829 Muenchen', 'LRN [12 09] 187472-1',
            *[f'Referenznummer/UCR [12 08] {n}' for n in numbers], 'Warennummer 85177100'))

    def test_abd_pdf_links_mail_to_order_and_customer(self):
        msg = self._abd(self._abd_pdf('102497269'))
        ev = CustomerTimeline.objects.get(email_message=msg)
        self.assertEqual((ev.customer, ev.related_order_id), (self.diwakar, self.order.pk))
        msg.refresh_from_db()
        self.assertIn('102497269', msg.attachments[0]['tokens'])          # прочитано раз — номери в кеші
        self.assertNotIn('81829', msg.attachments[0]['tokens'])           # 5 цифр (індекс) з вкладень — ні

    def test_cached_tokens_are_used_without_the_file(self):
        from email_assistant.links import order_in_attachments
        msg = self._abd(self._abd_pdf('102497269'))
        msg.refresh_from_db()
        os.remove(os.path.join(self.media, msg.attachments[0]['file_path']))
        self.assertEqual(order_in_attachments(msg), self.order)

    def test_two_orders_of_different_customers_are_not_guessed(self):
        other = Customer.objects.create(name='AEVEX', email='buyer@aevex.com')
        self._order('102330189', other)
        msg = self._abd(self._abd_pdf('102497269', '102330189'))
        self.assertFalse(CustomerTimeline.objects.filter(email_message=msg).exists())

    def test_short_numbers_in_attachments_are_ignored(self):
        self.order.order_number = '81829'                                 # як поштовий індекс у PDF
        self.order.save()
        msg = self._abd(self._abd_pdf())
        self.assertFalse(CustomerTimeline.objects.filter(email_message=msg).exists())

    def test_number_in_text_wins_over_attachment(self):
        other = self._order('102009980', self.diwakar)
        msg = self._abd(self._abd_pdf('102497269'), body='Bitte Rechnung zu Auftrag 102009980 prüfen.\n' + SIGNATURE)
        self.assertEqual(CustomerTimeline.objects.get(email_message=msg).related_order_id, other.pk)

    def test_preview_links_show_order_from_attachment(self):
        from email_assistant.links import find_links
        msg = self._abd(self._abd_pdf('102497269'))
        self.assertEqual(find_links([SUBJECT, SIGNATURE]), [])
        links = find_links([SUBJECT, SIGNATURE], messages=[msg])
        self.assertEqual((links[0]['order'], links[0]['reason']), (self.order, 'номер замовлення у вкладенні'))

    def test_unreadable_attachment_does_not_break_linking(self):
        bad = self._file('broken.pdf', b'%PDF-1.4 not really')
        msg = self._abd(bad)
        self.assertFalse(CustomerTimeline.objects.filter(email_message=msg).exists())
        msg.refresh_from_db()
        self.assertEqual(msg.attachments[0]['tokens'], [])


# ── Синхронізація зберегла лист без PDF (cron sync_email): номер з вкладення не знаходився ніколи ─────────────
PDF_NAME = 'EXP-1-DE2211955-0000-DE007602_00000004295972841-A#20261105-065822924-410.pdf'


def avm_raw(pdf: bytes, uid=7) -> bytes:
    """Лист dakosy «AVM - Ausgangsvermerk» з PDF і картинкою-логотипом, як його віддає IMAP."""
    m = MIMEMultipart()
    m['Subject'] = 'AVM - Ausgangsvermerk (DE), 187472-1, 26DE760298696748B4'
    m['From'] = 'noreply@dakosy.de'
    m['To'] = 'viacheslav.pryimak@sevskiy.de'
    m['Message-ID'] = f'<avm{uid}@dakosy.de>'
    m['Date'] = 'Thu, 05 Nov 2026 06:58:22 +0100'
    m.attach(MIMEText(SIGNATURE, 'plain', 'utf-8'))
    part = MIMEApplication(pdf, 'pdf')
    part.add_header('Content-Disposition', 'attachment', filename=PDF_NAME)
    m.attach(part)
    logo = MIMEImage(b'GIF89a' + bytes(20), 'gif')
    logo.add_header('Content-Disposition', 'attachment', filename='logo.gif')
    m.attach(logo)
    return m.as_bytes()


class FakeConn:
    """IMAP-з'єднання в пам'яті: {uid: сирий лист}; рахує завантаження листів."""

    def __init__(self, mails):
        self.mails, self.fetched = mails, 0

    def select(self, folder, readonly=True):
        return 'OK', [str(len(self.mails)).encode()]

    def uid(self, cmd, *args):
        if cmd == 'search':
            if 'UNSEEN' in args:
                return 'OK', [b'']
            return 'OK', [b' '.join(str(u).encode() for u in sorted(self.mails))]
        if cmd == 'fetch':
            self.fetched += 1
            out = []
            for u in args[0].split(b','):
                raw = self.mails[int(u)]
                out += [(f'{int(u)} (UID {int(u)} FLAGS () BODY[] {{{len(raw)}}}'.encode(), raw), b')']
            return 'OK', out
        return 'OK', [b'']

    def list(self):
        return 'OK', []

    def logout(self):
        pass


class FakeIMAPClient:
    """Заміна класу IMAPClient: справжні методи клієнта поверх FakeConn (connect не йде в мережу)."""

    def __init__(self, conn):
        self.conn = conn

    def __call__(self, account):
        client = RealIMAPClient(account)
        client.conn = self.conn
        client.connect = lambda: client
        client.disconnect = lambda: None
        return client


class SyncSavesDocumentsTests(AttachmentLinkTests.__bases__[0]):
    """Щохвилинна синхронізація зберігала лист без файлів вкладень → PDF не читався, а порожній результат
    запам'ятовувався назавжди. Тепер документи — на диск одразу, порожній кеш без файлу — читається знову."""

    setUp = AttachmentLinkTests.setUp
    _order = AttachmentLinkTests._order
    _file = AttachmentLinkTests._file
    _abd = AttachmentLinkTests._abd
    _abd_pdf = AttachmentLinkTests._abd_pdf

    def _pdf(self):
        return pdf_bytes('EUROPAEISCHE UNION - AUSGANGSVERMERK', 'LRN [12 09] 187472-1',
                         'Referenznummer/UCR [12 08] 102497269', 'Unterlage [N380] 102497269')

    def test_cron_sync_saves_the_pdf_and_links_the_order(self):
        conn = FakeConn({7: avm_raw(self._pdf())})
        with patch('email_assistant.imap_client.IMAPClient', FakeIMAPClient(conn)):
            call_command('sync_email', stdout=StringIO())
        msg = EmailMessage.objects.get(imap_uid=7, imap_folder_name='INBOX')   # фейк віддає лист і з «Надісланих»
        pdf, logo = msg.attachments
        self.assertTrue(pdf['file_path'] and os.path.isfile(os.path.join(self.media, pdf['file_path'])))
        self.assertNotIn('file_path', logo)                             # картинка — лише на вимогу, як раніше
        self.assertIn('102497269', pdf['tokens'])
        ev = CustomerTimeline.objects.get(email_message=msg)
        self.assertEqual((ev.customer, ev.related_order_id), (self.diwakar, self.order.pk))

    def _saved_without_file(self, **att):
        msg = self._abd({'name': PDF_NAME, 'size': 3000, 'content_type': 'application/pdf', **att})
        self.assertFalse(CustomerTimeline.objects.filter(email_message=msg).exists())
        return msg

    def test_no_file_no_cache(self):
        from email_assistant.links import attachment_tokens
        msg = self._saved_without_file()
        self.assertEqual(attachment_tokens(msg), {})
        msg.refresh_from_db()
        self.assertNotIn('tokens', msg.attachments[0])                  # «нічого не знайдено» не запам'ятовано

    def test_preview_fetches_the_pdf_and_links_the_order(self):
        from email_assistant.links import PREVIEW_FETCH, allow_fetch, find_links
        msg = self._saved_without_file(tokens=[])                       # старий порожній кеш
        conn = FakeConn({7: avm_raw(self._pdf())})
        with patch('email_assistant.imap_client.IMAPClient', FakeIMAPClient(conn)):
            self.assertEqual(find_links([msg.subject], messages=[msg]), [])   # без дозволу — IMAP не питаємо
            self.assertEqual(conn.fetched, 0)
            with allow_fetch(PREVIEW_FETCH):
                links = find_links([msg.subject], messages=[msg])
        self.assertEqual((links[0]['order'], links[0]['reason']), (self.order, 'номер замовлення у вкладенні'))
        self.assertEqual(conn.fetched, 1)
        msg.refresh_from_db()
        self.assertTrue(msg.attachments[0]['file_path'] and msg.attachments[0]['tokens_read'])
        ev = CustomerTimeline.objects.get(email_message=msg)            # і в хронології клієнта — з замовленням
        self.assertEqual((ev.customer, ev.related_order_id), (self.diwakar, self.order.pk))
        with patch('email_assistant.imap_client.IMAPClient', FakeIMAPClient(conn)), allow_fetch(PREVIEW_FETCH):
            find_links([msg.subject], messages=[msg])
        self.assertEqual(conn.fetched, 1)                               # прочитано раз — більше не завантажується
        self.assertEqual(CustomerTimeline.objects.filter(email_message=msg).count(), 1)

    def test_preview_view_reads_the_pdf(self):
        """Прев'ю листа в пошті (views._mail_links) бере файл з IMAP саме."""
        from email_assistant.views import _mail_links
        msg = self._saved_without_file(tokens=[])
        conn = FakeConn({7: avm_raw(self._pdf())})
        with patch('email_assistant.imap_client.IMAPClient', FakeIMAPClient(conn)):
            links = _mail_links([msg])
        self.assertEqual([lk['order'] for lk in links], [self.order])
        self.assertEqual(CustomerTimeline.objects.get(email_message=msg).related_order_id, self.order.pk)

    def test_old_empty_cache_is_reread_when_the_file_appeared(self):
        """PDF відкривали в Minerva (файл з'явився), а порожній кеш лишався — тепер читається."""
        from email_assistant.links import order_in_attachments
        att = self._abd_pdf('102497269')
        msg = self._saved_without_file()
        EmailMessage.objects.filter(pk=msg.pk).update(attachments=[{**att, 'tokens': []}])
        msg.refresh_from_db()
        self.assertEqual(order_in_attachments(msg), self.order)

    def test_read_empty_result_is_kept(self):
        from email_assistant.links import attachment_tokens
        msg = self._abd(self._file('note.pdf', pdf_bytes('Danke fuer Ihre Bestellung')))
        msg.refresh_from_db()
        self.assertEqual((msg.attachments[0]['tokens'], msg.attachments[0]['tokens_read']), ([], True))
        with patch('email_assistant.reply_brief.attachment_text', side_effect=AssertionError('read again')):
            attachment_tokens(msg)

    def test_relink_command_fetches_missing_pdf(self):
        msg = self._saved_without_file()
        conn = FakeConn({7: avm_raw(self._pdf())})
        with patch('email_assistant.imap_client.IMAPClient', FakeIMAPClient(conn)):
            call_command('relink_email_crm', stdout=StringIO())
        ev = CustomerTimeline.objects.get(email_message=msg)
        self.assertEqual(ev.related_order_id, self.order.pk)

    def test_fetch_messages_keeps_only_small_documents(self):
        from email_assistant import imap_client
        conn = FakeConn({7: avm_raw(self._pdf())})
        client = FakeIMAPClient(conn)(self.account)
        kept = client.fetch_messages('INBOX', keep_documents=True)[0]['attachments']
        self.assertEqual([bool(a.get('_data')) for a in kept], [True, False])
        plain = client.fetch_messages('INBOX')[0]['attachments']
        self.assertFalse(any(a.get('_data') for a in plain))
        with patch.object(imap_client, 'KEEP_DOCUMENT_MB', 0):
            self.assertFalse(client.fetch_messages('INBOX', keep_documents=True)[0]['attachments'][0].get('_data'))
