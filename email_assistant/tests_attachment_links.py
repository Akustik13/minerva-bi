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

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from crm.models import Customer, CustomerTimeline
from email_assistant.models import EmailAccount, EmailMessage
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
