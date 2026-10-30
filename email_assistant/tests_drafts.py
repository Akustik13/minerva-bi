"""Чернетки листів, вкладення запланованих листів і «↩ Відновити» з Кошика:
python manage.py test email_assistant.tests_drafts --settings=tabele.settings_test

Пошта не надсилається: SMTPClient.send підмінено, файли — у тимчасовій MEDIA_ROOT."""
import datetime
import json
import os
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from email_assistant import attachments, drafts
from email_assistant.models import EmailAccount, EmailDraft, EmailMessage, EmailRule, ScheduledEmail

ME = 'sales@sevskiy.example'
SEND = 'email_assistant.smtp_client.SMTPClient.send'


class Base(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp(prefix='minerva-drafts-')
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=self.media)
        media.enable()
        self.addCleanup(media.disable)
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password='x', smtp_username=ME,
            smtp_password='x', imap_folder_inbox='INBOX', imap_folder_sent='Gesendete Objekte', is_primary=True)
        self.other = User.objects.create_superuser('olena', 'o@x.y', 'p')
        self.other_account = EmailAccount.objects.create(
            user=self.other, email_address='olena@sevskiy.example', imap_username='o', imap_password='x',
            smtp_username='o', smtp_password='x', is_primary=True)
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.client.force_login(self.user)
        self.uid = 0

    def message(self, folder=EmailMessage.FOLDER_INBOX, imap_folder='INBOX', **kw):
        self.uid += 1
        data = dict(account=self.account, imap_uid=self.uid, imap_folder_name=imap_folder, folder=folder,
                    message_id=f'<m{self.uid}@x>', subject='Anfrage', from_email='buyer@client.example',
                    to_emails=[ME], body_text='Hallo', is_read=True,
                    sent_at=datetime.datetime(2026, 10, 20, 9, self.uid, tzinfo=datetime.timezone.utc))
        data.update(kw)
        return EmailMessage.objects.create(**data)

    def order_doc(self, name='Invoice-1.pdf', content=b'%PDF-order'):
        from sales.models import SalesOrder
        order = SalesOrder.objects.create(source='digikey', order_number='99823894', affects_stock=False)
        folder = attachments.order_dir(order)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'wb') as fh:
            fh.write(content)
        return f'order:{order.pk}:{name}'

    def save_draft(self, **data):
        files = data.pop('files', None)
        post = {'to': 'buyer@client.example', 'cc': '', 'subject': 'Angebot', 'body': 'Text',
                'body_html': '<p>Text</p>', **data}
        if files:
            post['attachments'] = files
        return self.client.post('/email/draft/save/', post).json()

    def outbox_files(self):
        root = os.path.join(self.media, attachments.OUTBOX_DIR)
        return sorted(os.path.join(d, f) for d, _s, fs in os.walk(root) for f in fs)


class DraftSaveTests(Base):
    def test_save_does_not_send_and_updates_the_same_draft(self):
        with patch(SEND) as send:
            d = self.save_draft()
            self.assertTrue(d['ok'], d)
            again = self.save_draft(draft_pk=d['draft_pk'], subject='Angebot A-2026-001', cc='boss@client.example')
        send.assert_not_called()                                   # «💾 Зберегти» нічого не надсилає
        self.assertEqual(again['draft_pk'], d['draft_pk'])
        self.assertEqual(EmailDraft.objects.count(), 1)            # та сама чернетка, не нова
        draft = EmailDraft.objects.get()
        self.assertEqual((draft.subject, draft.to_emails, draft.cc_emails, draft.body_html, draft.source),
                         ('Angebot A-2026-001', ['buyer@client.example'], ['boss@client.example'], '<p>Text</p>',
                          EmailDraft.SOURCE_MANUAL))
        self.assertFalse(EmailMessage.objects.filter(folder=EmailMessage.FOLDER_SENT).exists())

    def test_compose_save_button_posts_to_draft_endpoint(self):
        html = self.client.get('/email/compose/').content.decode()
        save = html[html.index('async function saveDraft'):]
        save = save[:save.index('\n}\n')]
        self.assertIn("fetch('/email/draft/save/'", save)
        self.assertNotIn('/email/send/', save)

    def test_empty_draft_is_refused(self):
        d = self.save_draft(to='', subject='', body='', body_html='')
        self.assertFalse(d['ok'])
        self.assertFalse(EmailDraft.objects.exists())

    def test_files_are_kept_in_draft_and_removed_when_dropped(self):
        d = self.save_draft(files=[SimpleUploadedFile('Spec.pdf', b'%PDF-1', content_type='application/pdf')])
        ref = f'draft:{d["draft_pk"]}:0'
        self.assertEqual(d['attachments'], [{'ref': ref, 'name': 'Spec.pdf', 'size': 6}])
        files = self.outbox_files()
        self.assertEqual(len(files), 1)
        # ще раз зберегли з тим самим файлом (посилання draft:) і документом замовлення — файл той самий
        doc = self.order_doc()
        d2 = self.save_draft(draft_pk=d['draft_pk'], att_ref=[ref, doc], att_ref_name=['Spec.pdf', 'Invoice-1.pdf'])
        self.assertEqual(self.outbox_files(), files)
        self.assertEqual([a['name'] for a in d2['attachments']], ['Spec.pdf', 'Invoice-1.pdf'])
        self.assertEqual(EmailDraft.objects.get().attachments[1], {'ref': doc, 'name': 'Invoice-1.pdf'})
        # файл прибрали з листа (✕) — і з диска
        self.save_draft(draft_pk=d['draft_pk'], att_ref=[doc], att_ref_name=['Invoice-1.pdf'])
        self.assertEqual(self.outbox_files(), [])

    def test_other_users_draft_files_are_not_reachable(self):
        foreign = EmailDraft.objects.create(account=self.other_account, subject='privat', attachments=[
            attachments.store_file('Geheim.pdf', b'secret')])
        self.assertIsNone(attachments.load_ref(self.account, f'draft:{foreign.pk}:0'))
        d = self.save_draft(att_ref=[f'draft:{foreign.pk}:0'], att_ref_name=['Geheim.pdf'])
        self.assertFalse(d['ok'])
        self.assertIn('Вкладення недоступне', d['error'])


class DraftListTests(Base):
    def test_drafts_folder_lists_own_drafts_with_count(self):
        mine = EmailDraft.objects.create(account=self.account, subject='Meine Antwort', to_emails=['a@b.c'],
                                         body='Hallo Anna', source=EmailDraft.SOURCE_AUTO_REPLY)
        EmailDraft.objects.create(account=self.other_account, subject='Fremde Antwort')
        html = self.client.get('/email/').content.decode()
        self.assertIn('id="folder-link-drafts"', html)                 # «📝 Чернетки» у лівій колонці
        badge = html[html.index('id="badge-drafts"'):]
        self.assertTrue(badge[badge.index('>') + 1:].startswith('1<'))  # лічильник — лише свої
        page = self.client.get('/email/?folder=drafts').content.decode()
        self.assertIn('Meine Antwort', page)
        self.assertIn(f'?draft={mine.pk}', page)
        self.assertIn('🤖 AI-автовідповідь', page)
        self.assertNotIn('Fremde Antwort', page)
        self.assertIn('Meine Antwort', self.client.get('/email/?folder=drafts&q=antwort').content.decode())
        self.assertNotIn('Meine Antwort', self.client.get('/email/?folder=drafts&q=zzz').content.decode())
        self.assertEqual(self.client.get('/email/unread-count/').json()['drafts'], 1)

    def test_open_saved_draft_in_compose(self):
        doc = self.order_doc()
        reply_to = self.message(subject='Anfrage FT220407')
        d = self.save_draft(reply_to_pk=reply_to.pk, body_html='<p>Gern <b>senden</b> wir</p>',
                            att_ref=[doc], att_ref_name=['Invoice-1.pdf'],
                            files=[SimpleUploadedFile('Spec.pdf', b'%PDF-1', content_type='application/pdf')])
        html = self.client.get(f'/email/compose/?draft={d["draft_pk"]}').content.decode()
        self.assertIn(f'name="draft_pk" id="draft-pk" value="{d["draft_pk"]}"', html)
        self.assertIn(f'name="reply_to_pk" value="{reply_to.pk}"', html)
        self.assertIn('value="buyer@client.example"', html)
        self.assertIn('value="Angebot"', html)
        self.assertIn('<p>Gern <b>senden</b> wir</p>', html)        # текст із форми — як був (з підписом)
        self.assertIn(f'name="att_ref" value="{doc}"', html)
        self.assertIn(f'name="att_ref" value="draft:{d["draft_pk"]}:1"', html)

    def test_ai_draft_opens_as_reply_with_text_and_quote(self):
        original = self.message(subject='Lieferzeit?', body_text='Wann liefern Sie <FT220407>?')
        draft = EmailDraft.objects.create(account=self.account, reply_to=original, subject='Re: Lieferzeit?',
                                          to_emails=['buyer@client.example'], body='Guten Tag,\n<2 Wochen>',
                                          source=EmailDraft.SOURCE_AUTO_REPLY)
        html = self.client.get(f'/email/compose/?draft={draft.pk}').content.decode()
        self.assertIn('Guten Tag,<br>&lt;2 Wochen&gt;', html)       # текст AI — екранований
        self.assertIn('Оригінальний лист від buyer@client.example', html)
        self.assertIn(f'name="reply_to_pk" value="{original.pk}"', html)

    def test_foreign_draft_is_404_and_delete(self):
        foreign = EmailDraft.objects.create(account=self.other_account, subject='privat')
        self.assertEqual(self.client.get(f'/email/compose/?draft={foreign.pk}').status_code, 404)
        self.assertEqual(self.client.post(f'/email/draft/{foreign.pk}/delete/').status_code, 404)
        self.assertTrue(EmailDraft.objects.filter(pk=foreign.pk).exists())
        d = self.save_draft(files=[SimpleUploadedFile('Spec.pdf', b'%PDF-1', content_type='application/pdf')])
        self.assertEqual(len(self.outbox_files()), 1)
        self.assertTrue(self.client.post(f'/email/draft/{d["draft_pk"]}/delete/').json()['ok'])
        self.assertFalse(EmailDraft.objects.filter(pk=d['draft_pk']).exists())
        self.assertEqual(self.outbox_files(), [])                   # файли чернетки — теж

    def test_sent_draft_is_removed_with_its_files(self):
        d = self.save_draft(files=[SimpleUploadedFile('Spec.pdf', b'%PDF-1', content_type='application/pdf')])
        form = {'to': 'buyer@client.example', 'subject': 'Angebot', 'body': 'Text', 'draft_pk': d['draft_pk'],
                'att_ref': [f'draft:{d["draft_pk"]}:0']}
        with patch(SEND, return_value={'ok': False, 'error': '535 auth'}):
            self.client.post('/email/send/', form)
        self.assertTrue(EmailDraft.objects.filter(pk=d['draft_pk']).exists())   # не пішов — чернетка лишається
        with patch(SEND, return_value={'ok': True, 'message_id': '<s1@x>'}) as send:
            self.assertTrue(self.client.post('/email/send/', form).json()['ok'])
        sent = send.call_args.kwargs['attachments']
        self.assertEqual([(a['name'], a['content']) for a in sent], [('Spec.pdf', b'%PDF-1')])
        self.assertFalse(EmailDraft.objects.filter(pk=d['draft_pk']).exists())
        self.assertEqual(self.outbox_files(), [])

    def test_auto_reply_draft_is_marked(self):
        from email_assistant.management.commands.sync_email import Command
        from email_assistant.models import EmailSettings
        em = self.message(subject='Frage')
        es = EmailSettings.get_for_user(self.user)
        es.auto_reply_mode = 'draft'
        with patch('email_assistant.ai_helper.generate_reply', return_value='Danke!'):
            Command()._auto_reply(em, self.account, es, None)
        draft = EmailDraft.objects.get()
        self.assertEqual((draft.source, draft.reply_to, draft.subject), (EmailDraft.SOURCE_AUTO_REPLY, em, 'Re: Frage'))
        es.auto_reply_mode = 'send'                                  # «Надіслати одразу» — у ту саму гілку
        with patch('email_assistant.ai_helper.generate_reply', return_value='Danke!'):
            Command()._auto_reply(em, self.account, es, None)
        self.assertEqual(ScheduledEmail.objects.get().reply_to, em)

    def test_missing_draft_file_on_send_names_the_file(self):
        d = self.save_draft(files=[SimpleUploadedFile('Spec.pdf', b'%PDF-1', content_type='application/pdf')])
        for path in self.outbox_files():
            os.remove(path)
        with patch(SEND) as send:
            r = self.client.post('/email/send/', {'to': 'a@b.c', 'subject': 'S', 'draft_pk': d['draft_pk'],
                                                  'att_ref': [f'draft:{d["draft_pk"]}:0'],
                                                  'att_ref_name': ['Spec.pdf']}).json()
        send.assert_not_called()
        self.assertEqual(r, {'ok': False, 'error': 'Вкладення недоступне: Spec.pdf'})
        self.assertTrue(EmailDraft.objects.filter(pk=d['draft_pk']).exists())


class ScheduledAttachmentTests(Base):
    def schedule(self, **data):
        when = (timezone.localtime() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        files = data.pop('files', None)
        post = {'to': 'buyer@client.example', 'subject': 'Angebot', 'body': 'Text', 'body_html': '<p>Text</p>',
                'scheduled_at': when, **data}
        if files:
            post['attachments'] = files
        return self.client.post('/email/schedule/', post).json()

    def run_due(self):
        ScheduledEmail.objects.update(scheduled_at=timezone.now() - timedelta(minutes=1))
        call_command('send_scheduled', stdout=StringIO())

    def test_scheduled_letter_sends_files_and_documents(self):
        doc = self.order_doc(content=b'%PDF-order')
        original = self.message(subject='Anfrage')
        d = self.save_draft(files=[SimpleUploadedFile('Notes.txt', b'draft-file', content_type='text/plain')])
        r = self.schedule(reply_to_pk=original.pk, draft_pk=d['draft_pk'],
                          att_ref=[doc, f'draft:{d["draft_pk"]}:0'], att_ref_name=['Invoice-1.pdf', 'Notes.txt'],
                          files=[SimpleUploadedFile('Spec.pdf', b'%PDF-spec', content_type='application/pdf')])
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['attachments'], 3)
        self.assertFalse(EmailDraft.objects.exists())               # чернетку заплановано — її більше немає
        mail = ScheduledEmail.objects.get()
        self.assertEqual(mail.reply_to, original)
        self.assertEqual(mail.attachments[0], {'ref': doc, 'name': 'Invoice-1.pdf'})   # документ — посиланням
        self.assertEqual(len(self.outbox_files()), 2)                # файли — свої копії листа
        with patch(SEND, return_value={'ok': True, 'message_id': '<s2@x>'}) as send:
            self.run_due()
        kw = send.call_args.kwargs
        self.assertEqual([(a['name'], a['content']) for a in kw['attachments']],
                         [('Invoice-1.pdf', b'%PDF-order'), ('Notes.txt', b'draft-file'), ('Spec.pdf', b'%PDF-spec')])
        self.assertEqual(kw['reply_to_message'], original)
        mail.refresh_from_db()
        self.assertEqual(mail.status, ScheduledEmail.STATUS_SENT)
        self.assertEqual(self.outbox_files(), [])                   # надіслано — файли з диска прибрано

    def test_missing_document_is_reported_now_and_file_missing_later_fails(self):
        r = self.schedule(att_ref=['order:999999:Gone.pdf'], att_ref_name=['Gone.pdf'])
        self.assertEqual((r['ok'], r['error']), (False, 'Вкладення недоступне: Gone.pdf'))
        self.assertFalse(ScheduledEmail.objects.exists())
        r = self.schedule(files=[SimpleUploadedFile('Spec.pdf', b'%PDF-spec', content_type='application/pdf')])
        self.assertTrue(r['ok'])
        for path in self.outbox_files():
            os.remove(path)                                          # файл зник до відправки
        with patch(SEND) as send:
            self.run_due()
        send.assert_not_called()                                     # без вкладення лист не йде
        mail = ScheduledEmail.objects.get()
        self.assertEqual((mail.status, mail.error_msg, mail.sent_at),
                         (ScheduledEmail.STATUS_FAILED, 'Вкладення недоступне: Spec.pdf', None))

    def test_offer_document_marked_sent_after_scheduled_send(self):
        att = {'name': 'Angebot A-2026-007.pdf', 'content': b'%PDF-offer', 'content_type': 'application/pdf'}
        with patch('offers.services.revision_attachment', return_value=att):
            self.assertTrue(self.schedule(att_ref=['offer:7'], att_ref_name=['Angebot.pdf'])['ok'])
            self.assertEqual(ScheduledEmail.objects.get().attachments, [{'ref': 'offer:7', 'name': att['name']}])
            with patch(SEND, return_value={'ok': True, 'message_id': '<s3@x>'}) as send, \
                    patch('offers.services.mark_sent_refs') as mark:
                self.run_due()
        self.assertEqual(send.call_args.kwargs['attachments'][0]['content'], b'%PDF-offer')
        mark.assert_called_once_with(['offer:7'], '<s3@x>')

    def test_json_schedule_still_works(self):
        when = (timezone.localtime() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        r = self.client.post('/email/schedule/', json.dumps({'to': 'a@b.c', 'subject': 'S', 'body': 'B',
                                                             'scheduled_at': when}), content_type='application/json')
        self.assertTrue(r.json()['ok'])
        self.assertEqual(ScheduledEmail.objects.get().attachments, [])

    def test_compose_schedules_with_the_form(self):
        html = self.client.get('/email/compose/').content.decode()
        fn = html[html.index('async function scheduleEmail'):]
        fn = fn[:fn.index('\n}\n')]
        self.assertIn("new FormData(document.getElementById('compose-form'))", fn)
        self.assertNotIn('JSON.stringify', fn)

    def test_deleted_scheduled_letter_removes_files(self):
        self.assertTrue(self.schedule(files=[SimpleUploadedFile('a.txt', b'x', content_type='text/plain')])['ok'])
        self.assertEqual(len(self.outbox_files()), 1)
        ScheduledEmail.objects.get().delete()
        self.assertEqual(self.outbox_files(), [])


class RestoreFromTrashTests(Base):
    def test_delete_then_restore_returns_to_original_folder(self):
        inbox = self.message()
        sent = self.message(folder=EmailMessage.FOLDER_SENT, imap_folder='Gesendete Objekte', from_email=ME,
                            to_emails=['buyer@client.example'])
        with patch('email_assistant.views._imap_bg'), patch('email_assistant.views._imap_bg_many'):
            self.client.post(f'/email/message/{inbox.pk}/delete/')
            self.client.post('/email/messages/bulk/', json.dumps({'action': 'delete', 'pks': [sent.pk]}),
                             content_type='application/json')
        inbox.refresh_from_db()
        sent.refresh_from_db()
        self.assertEqual((inbox.folder, inbox.trashed_from), ('trash', 'inbox'))
        self.assertEqual((sent.folder, sent.trashed_from), ('trash', 'sent'))
        page = self.client.get('/email/?folder=trash').content.decode()
        self.assertIn(f'rowRestore(event, {inbox.pk})', page)       # ↩ у рядку
        self.assertIn('data-k="restore"', page)                     # і в меню правого кліку
        self.assertIn("bulkAction('restore')", page)                # і для вибраних
        self.assertIn('↩ 📤 Надіслані', page)                       # куди повернеться
        self.assertNotIn('data-k="restore"', self.client.get('/email/').content.decode())
        d = self.client.post(f'/email/message/{inbox.pk}/restore/').json()
        self.assertEqual((d['restored'], d['folders'][0]['folder']), (1, 'inbox'))
        d = self.client.post('/email/messages/bulk/', json.dumps({'action': 'restore', 'pks': [sent.pk]}),
                             content_type='application/json').json()
        self.assertEqual(d['folders'], [{'folder': 'sent', 'label': '📤 Надіслані', 'n': 1}])
        inbox.refresh_from_db()
        sent.refresh_from_db()
        self.assertEqual((inbox.folder, inbox.trashed_from, sent.folder), ('inbox', '', 'sent'))

    def test_old_trash_letters_restore_by_server_folder(self):
        sent = self.message(folder='trash', imap_folder='Gesendete Objekte', from_email=ME)
        local = self.message(folder='trash', imap_folder='minerva:<x@y>', imap_uid=0, from_email=ME)
        custom = self.message(folder='trash', imap_folder='Meine Order')
        spam = self.message(folder='trash', is_spam=True)
        from email_assistant.sync import restore_from_trash
        restored = restore_from_trash(self.account, [sent, local, custom, spam])
        self.assertEqual(restored, {'sent': 2, 'inbox': 1, 'spam': 1})
        self.assertEqual(EmailMessage.objects.get(pk=custom.pk).folder, 'inbox')   # папка сервера лишається
        self.assertEqual(EmailMessage.objects.get(pk=custom.pk).imap_folder_name, 'Meine Order')

    def test_restore_ignores_letters_outside_trash_and_foreign(self):
        inbox = self.message()
        gone = self.message(folder='trash', is_deleted=True)
        d = self.client.post('/email/messages/bulk/', json.dumps({'action': 'restore', 'pks': [inbox.pk, gone.pk]}),
                             content_type='application/json').json()
        self.assertEqual(d['restored'], 0)
        self.assertTrue(EmailMessage.objects.get(pk=gone.pk).is_deleted)   # видалений назавжди — не повертається
        self.uid += 1
        foreign = EmailMessage.objects.create(account=self.other_account, folder='trash', imap_uid=self.uid,
                                              from_email='x@y.z', trashed_from='inbox')
        self.assertEqual(self.client.post(f'/email/message/{foreign.pk}/restore/').status_code, 404)

    def test_rule_trash_remembers_folder(self):
        rule = EmailRule(account=self.account, name='weg', condition_value='x', action=EmailRule.ACTION_TRASH)
        msg = self.message()
        rule.apply_to(msg)
        msg.refresh_from_db()
        self.assertEqual((msg.folder, msg.trashed_from), ('trash', 'inbox'))
