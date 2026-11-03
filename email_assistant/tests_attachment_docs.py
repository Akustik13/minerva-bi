"""📎 Вкладення листів → документи замовлення й проєкту (email_assistant/attachment_docs.py).

ABD від митного брокера (номер замовлення лише в PDF) прив'язується до замовлення — його PDF має сам опинитись у
«Документах замовлення»; той самий файл, що вже є, — не дублюється.

python manage.py test email_assistant.tests_attachment_docs --settings=tabele.settings_test
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone as dt_tz

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from crm.models import Customer
from email_assistant import attachment_docs
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, EmailThread
from email_assistant.tests_attachment_links import SIGNATURE, SUBJECT, pdf_bytes
from sales.models import SalesOrder

ABD_NAME = 'EXP-1-DE2211955-0000-DE007602_00000004293271922-A#20261102-150123658-411.pdf'


class AttachmentDocsTests(TestCase):
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
        self.order = SalesOrder.objects.create(source='digikey', order_number='102497269',
                                               customer_key=self.diwakar.external_key,
                                               email=self.diwakar.email, affects_stock=False)
        self.folder = os.path.join(self.media, 'orders', 'digikey', '102497269')
        self.abd = pdf_bytes('AUSFUHRBEGLEITDOKUMENT', 'Referenznummer/UCR [12 08] 102497269')
        self.uid = 7

    def _mode(self, mode):
        es = EmailSettings.get_for_user(self.user)
        es.attachments_to_docs = mode
        es.save()

    def _att(self, name, data, ctype='application/pdf', **extra):
        rel = os.path.join('email_attachments', str(self.account.pk), f'{self.uid}_{name}'.replace('#', '_'))
        os.makedirs(os.path.join(self.media, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(self.media, rel), 'wb') as fh:
            fh.write(data)
        return {'name': name, 'file_path': rel, 'size': len(data), 'content_type': ctype, **extra}

    def _mail(self, *attachments, subject=SUBJECT, thread=None):
        self.uid += 1
        with self.captureOnCommitCallbacks(execute=True):
            return EmailMessage.objects.create(
                account=self.account, thread=thread, folder=EmailMessage.FOLDER_INBOX, imap_folder_name='INBOX',
                imap_uid=self.uid, subject=subject, from_email='noreply@dakosy.de',
                to_emails=['viacheslav.pryimak@sevskiy.de'], cc_emails=[], body_text=SIGNATURE,
                attachments=list(attachments), sent_at=datetime(2026, 11, 2, 14, 1, tzinfo=dt_tz.utc))

    def _files(self):
        return sorted(os.listdir(self.folder)) if os.path.isdir(self.folder) else []

    def test_off_by_default(self):
        self._mail(self._att(ABD_NAME, self.abd))
        self.assertEqual(self._files(), [])

    def test_abd_pdf_goes_to_order_documents(self):
        self._mode(attachment_docs.ORDERS)
        msg = self._mail(self._att(ABD_NAME, self.abd))
        self.assertEqual(self._files(), [ABD_NAME])
        with open(os.path.join(self.folder, ABD_NAME), 'rb') as fh:
            self.assertEqual(fh.read(), self.abd)
        msg.refresh_from_db()
        self.assertEqual(msg.attachments[0]['saved'], [f'order:{self.order.pk}'])

    def test_same_document_already_there_is_not_saved(self):
        self._mode(attachment_docs.ORDERS)
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, 'ABD_102497269.pdf'), 'wb') as fh:
            fh.write(self.abd)                                       # той самий вміст під іншою назвою
        msg = self._mail(self._att(ABD_NAME, self.abd))
        self.assertEqual(self._files(), ['ABD_102497269.pdf'])
        msg.refresh_from_db()
        self.assertEqual(msg.attachments[0]['saved'], [f'order:{self.order.pk}'])

    def test_same_name_other_content_gets_a_number(self):
        self._mode(attachment_docs.ORDERS)
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, 'Invoice.pdf'), 'wb') as fh:
            fh.write(pdf_bytes('Invoice old'))
        self._mail(self._att('Invoice.pdf', pdf_bytes('Invoice new 102497269')), subject='Invoice 102497269')
        self.assertEqual(self._files(), ['Invoice (2).pdf', 'Invoice.pdf'])

    def test_pictures_and_inline_files_are_skipped(self):
        self._mode(attachment_docs.ORDERS)
        self._mail(self._att('image001.png', b'\x89PNG\r\n\x1a\nlogo', 'image/png'),
                   self._att('scan.pdf', pdf_bytes('inline'), is_inline=True),
                   self._att('Lieferschein.docx', b'PK\x03\x04docx', 'application/octet-stream'),
                   subject='Auftrag 102497269')
        self.assertEqual(self._files(), ['Lieferschein.docx'])

    def test_deleted_by_a_person_is_not_restored(self):
        self._mode(attachment_docs.ORDERS)
        msg = self._mail(self._att(ABD_NAME, self.abd))
        os.remove(os.path.join(self.folder, ABD_NAME))
        msg.refresh_from_db()
        self.assertEqual(attachment_docs.save_for_message(msg), {'orders': 0, 'projects': 0, 'already': 0})
        self.assertEqual(self._files(), [])

    def test_spam_and_unlinked_mail_are_skipped(self):
        self._mode(attachment_docs.ORDERS)
        self._mail(self._att('Price list.pdf', pdf_bytes('no order here')), subject='Newsletter')
        self.assertFalse(os.path.isdir(os.path.join(self.media, 'orders')))
        msg = self._mail(self._att(ABD_NAME, self.abd))
        EmailMessage.objects.filter(pk=msg.pk).update(is_spam=True, attachments=[self._att('x.pdf', b'%PDF-1.4 x')])
        msg.refresh_from_db()
        self.assertEqual(attachment_docs.save_for_message(msg)['orders'], 0)

    def test_project_documents_for_all(self):
        from projects.models import ProjectLink
        from projects.services import create_project
        from rag_assistant.models import KnowledgeItem
        self._mode(attachment_docs.ALL)
        thread = EmailThread.objects.create(account=self.account, subject='Antenna project')
        project = create_project(name='Antenna', user=self.user, customer=self.diwakar)
        with self.captureOnCommitCallbacks(execute=True):
            ProjectLink.objects.create(project=project, thread=thread, status=ProjectLink.CONFIRMED)
        spec = pdf_bytes('Specification antenna 2.4 GHz')
        self._mail(self._att('Spec.pdf', spec), subject='Spec', thread=thread)
        item = KnowledgeItem.objects.get(project=project)
        self.assertEqual((item.title, item.owner, item.customer), ('Spec.pdf', self.user, self.diwakar))
        self.assertIn('noreply@dakosy.de', item.note)
        # друга копія того самого файлу (інший лист гілки) — не дублюється
        self._mail(self._att('Spec copy.pdf', spec), subject='Re: Spec', thread=thread)
        self.assertEqual(KnowledgeItem.objects.filter(project=project).count(), 1)

    def test_confirmed_thread_saves_earlier_mails(self):
        from projects.models import ProjectLink
        from projects.services import create_project
        from rag_assistant.models import KnowledgeItem
        self._mode(attachment_docs.ALL)
        thread = EmailThread.objects.create(account=self.account, subject='Filter')
        self._mail(self._att('Drawing.pdf', pdf_bytes('drawing')), subject='Filter', thread=thread)
        project = create_project(name='Filter', user=self.user)
        with self.captureOnCommitCallbacks(execute=True):
            ProjectLink.objects.create(project=project, thread=thread, status=ProjectLink.CONFIRMED)
        self.assertEqual(list(KnowledgeItem.objects.filter(project=project).values_list('title', flat=True)),
                         ['Drawing.pdf'])

    def test_orders_mode_does_not_touch_projects(self):
        from projects.models import ProjectLink
        from projects.services import create_project
        from rag_assistant.models import KnowledgeItem
        self._mode(attachment_docs.ORDERS)
        thread = EmailThread.objects.create(account=self.account, subject='Filter')
        project = create_project(name='Filter', user=self.user)
        ProjectLink.objects.create(project=project, thread=thread, status=ProjectLink.CONFIRMED)
        self._mail(self._att('Drawing.pdf', pdf_bytes('drawing')), subject='Filter', thread=thread)
        self.assertFalse(KnowledgeItem.objects.filter(project=project).exists())

    def _recent(self):
        """«📥 Зберегти вкладення за 30 днів» (у тестах — без фонового потоку) → стан після завершення."""
        from config.models import SystemSettings
        from core.shared_cache import cache
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        cache.delete(attachment_docs._key(self.user.pk))
        self.client.force_login(self.user)
        old, attachment_docs.BACKGROUND = attachment_docs.BACKGROUND, False
        try:
            with self.captureOnCommitCallbacks(execute=True):
                started = self.client.post('/email/attachments-to-docs/', {'days': '30'}).json()
        finally:
            attachment_docs.BACKGROUND = old
        if not started['ok']:
            return started
        return self.client.get('/email/attachments-to-docs/').json()

    def test_save_recent_and_views(self):
        msg = self._mail(self._att(ABD_NAME, self.abd))              # прийшов, коли було вимкнено
        EmailMessage.objects.filter(pk=msg.pk).update(sent_at=datetime.now(dt_tz.utc))
        self.assertFalse(self._recent()['ok'])
        r = self.client.post('/email/settings/toggle/', json.dumps({'field': 'attachments_to_docs',
                                                                    'value': 'orders'}),
                             content_type='application/json')
        self.assertTrue(r.json()['ok'])
        d = self._recent()
        self.assertEqual((d['ok'], d['running'], d['total'], d['orders'], d['already'], d['linked']),
                         (True, False, 1, 1, 0, 0))
        self.assertEqual(self._files(), [ABD_NAME])
        d = self._recent()
        self.assertEqual((d['orders'], d['already']), (0, 0))       # уже позначено «saved»

    def test_mail_from_before_the_update_is_linked_and_saved(self):
        """Як на сервері: ABD прийшов до оновлення — у хронології CRM його немає, кнопка прив'язує й зберігає."""
        from crm.models import CustomerTimeline
        msg = self._mail(self._att(ABD_NAME, self.abd))
        EmailMessage.objects.filter(pk=msg.pk).update(sent_at=datetime.now(dt_tz.utc))
        CustomerTimeline.objects.filter(email_message=msg).delete()
        self._mode(attachment_docs.ORDERS)
        d = self._recent()
        self.assertEqual((d['total'], d['linked'], d['orders']), (1, 1, 1))
        self.assertEqual(self._files(), [ABD_NAME])
        self.assertEqual(CustomerTimeline.objects.get(email_message=msg).related_order_id, self.order.pk)

    def test_order_without_crm_customer(self):
        """Замовлення без клієнта CRM — у хронологію лист не йде, а документ у замовлення — так (номер у PDF)."""
        from crm.models import CustomerTimeline
        self.order.customer_key, self.order.email = '', ''
        self.order.save()
        self.diwakar.delete()
        self._mode(attachment_docs.ORDERS)
        msg = self._mail(self._att(ABD_NAME, self.abd))
        self.assertFalse(CustomerTimeline.objects.filter(email_message=msg).exists())
        self.assertEqual(self._files(), [ABD_NAME])

    def test_safe_name(self):
        self.assertEqual(attachment_docs.safe_name('..\\..\\C:\\x\\Rechnung..pdf'), 'Rechnung.pdf')
        self.assertEqual(attachment_docs.safe_name('a/b/<c>.pdf'), '_c_.pdf')
        self.assertEqual(attachment_docs.safe_name(''), 'document')
        long = attachment_docs.safe_name('x' * 300 + '.pdf')
        self.assertTrue(long.endswith('.pdf') and len(long) <= attachment_docs.NAME_MAX)
