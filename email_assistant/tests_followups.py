"""❗ Важливо / ‼️ Дуже важливо (email_assistant/importance.py) і ⏰ «немає реакції» з 📬 запитом на прочитання
(email_assistant/followups.py): заголовки, надсилання, підтвердження прочитання (MDN), відповідь, нагадування.
SMTP і повідомлення підмінено.

python manage.py test email_assistant.tests_followups --settings=tabele.settings_test
"""
import datetime
import email
import io
import json
from email.message import EmailMessage as MimeMessage
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from email_assistant import followups, importance
from email_assistant.imap_client import IMAPClient
from email_assistant.models import EmailAccount, EmailMessage, MailFollowup, ScheduledEmail
from email_assistant.sync import save_message

OUR_ID = '<abc.123@sevskiy.de>'


def raw_mail(subject='Angebot', frm='anna@kunde.de', headers=None, body='Hallo'):
    m = MimeMessage()
    m['From'], m['To'], m['Subject'] = frm, 'slava@sevskiy.de', subject
    m['Message-ID'] = '<in.%s@kunde.de>' % abs(hash(subject + frm))
    m['Date'] = email.utils.format_datetime(timezone.now())
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


def raw_mdn(original=OUR_ID, disposition='manual-action/MDN-sent-manually; displayed'):
    """Підтвердження прочитання, як його шле Outlook / Thunderbird (RFC 8098)."""
    return (
        'From: anna@kunde.de\r\nTo: slava@sevskiy.de\r\nSubject: Gelesen: Angebot A-2026-001\r\n'
        'Message-ID: <mdn.1@kunde.de>\r\nDate: ' + email.utils.format_datetime(timezone.now()) + '\r\n'
        'MIME-Version: 1.0\r\n'
        'Content-Type: multipart/report; report-type=disposition-notification; boundary="b1"\r\n\r\n'
        '--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nIhre Nachricht wurde gelesen.\r\n'
        '--b1\r\nContent-Type: message/disposition-notification\r\n\r\n'
        'Reporting-UA: Thunderbird\r\nFinal-Recipient: rfc822;anna@kunde.de\r\n'
        f'Original-Message-ID: {original}\r\nDisposition: {disposition}\r\n'
        '--b1--\r\n').encode()


def parsed(raw, uid=1):
    return IMAPClient.__new__(IMAPClient)._parse_one_message(b'1 (UID %d FLAGS ())' % uid, raw)


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de',
                                                   imap_folder_inbox='INBOX')
        self.client.force_login(self.user)
        self.uid = 100

    def sent(self, subject='Angebot A-2026-001', msg_id=OUR_ID, days_ago=0):
        when = timezone.now() - datetime.timedelta(days=days_ago)
        return EmailMessage.objects.create(account=self.account, folder='sent', imap_folder_name='Sent',
                                           message_id=msg_id, subject=subject, from_email='slava@sevskiy.de',
                                           to_emails=['anna@kunde.de'], body_text='Anbei unser Angebot.',
                                           sent_at=when, is_read=True)

    def incoming(self, raw):
        self.uid += 1
        return save_message(self.account, parsed(raw, self.uid), 'INBOX')

    def deliver(self):
        return mock.patch('agent.notify.deliver', return_value='🔔 Повідомлено: пошта')


class ImportanceTests(Base):
    def test_sender_headers(self):
        self.assertEqual(parsed(raw_mail(headers={'X-Priority': '1 (Highest)'}))['importance'], 'important')
        self.assertEqual(parsed(raw_mail(headers={'Importance': 'High'}))['importance'], 'important')
        self.assertEqual(parsed(raw_mail(headers={'X-Priority': '3'}))['importance'], '')
        self.assertEqual(parsed(raw_mail(headers={'Importance': 'low'}))['importance'], '')
        em = self.incoming(raw_mail(headers={'Importance': 'high'}))
        self.assertEqual((em.importance, em.importance_by), ('important', 'sender'))

    def test_mark_by_hand_and_starred_folder(self):
        em = self.incoming(raw_mail())
        plain = self.incoming(raw_mail(subject='Newsletter'))
        r = self.client.post(f'/email/message/{em.pk}/importance/', json.dumps({'level': 'urgent'}),
                             content_type='application/json').json()
        self.assertEqual((r['ok'], r['level'], r['icon']), (True, 'urgent', '‼️'))
        em.refresh_from_db()
        self.assertEqual((em.importance, em.importance_by), ('urgent', 'me'))
        page = self.client.get('/email/?folder=starred')
        self.assertContains(page, f'id="row-{em.pk}"')
        self.assertNotContains(page, f'id="row-{plain.pk}"')
        self.assertContains(page, '‼️</span>')
        self.assertEqual(self.client.post(f'/email/message/{em.pk}/importance/', json.dumps({'level': 'x'}),
                                          content_type='application/json').status_code, 400)
        other = User.objects.create_superuser('olga', 'o@x.y', 'p')
        EmailAccount.objects.create(user=other, email_address='olga@sevskiy.de')
        self.client.force_login(other)
        self.assertEqual(self.client.post(f'/email/message/{em.pk}/importance/', json.dumps({'level': ''}),
                                          content_type='application/json').status_code, 404)

    def test_agent_does_not_override_human(self):
        em = self.incoming(raw_mail())
        importance.set_level([em], '')
        em.refresh_from_db()
        self.assertFalse(importance.raise_level(em, 'urgent'))
        em2 = self.incoming(raw_mail(subject='Zweite'))
        self.assertTrue(importance.raise_level(em2, 'important'))
        self.assertFalse(importance.raise_level(em2, 'important'))
        self.assertTrue(importance.raise_level(em2, 'urgent'))


class SendTests(Base):
    def test_smtp_headers(self):
        from email_assistant.smtp_client import SMTPClient
        sent = {}

        class Conn:
            def login(self, *a):
                pass

            def sendmail(self, frm, to, data):
                sent['data'] = data

            def quit(self):
                pass

        with mock.patch.object(SMTPClient, '_open', return_value=Conn()), \
                mock.patch('email_assistant.imap_client.IMAPClient', side_effect=OSError('no imap')):
            r = SMTPClient(self.account).send(['anna@kunde.de'], 'Angebot', 'Text', importance='important',
                                              read_receipt=True)
        self.assertTrue(r['ok'], r)
        msg = email.message_from_bytes(sent['data'])
        self.assertEqual(msg['Importance'], 'high')
        self.assertTrue(msg['X-Priority'].startswith('1'))
        self.assertEqual(msg['Disposition-Notification-To'], 'slava@sevskiy.de')

    def test_send_creates_followup(self):
        with mock.patch('email_assistant.smtp_client.SMTPClient.send',
                        return_value={'ok': True, 'message_id': OUR_ID}) as send:
            d = self.client.post('/email/send/', {'to': 'anna@kunde.de', 'subject': 'Angebot', 'body': 'Text',
                                                  'opt_important': '1', 'opt_receipt': '1', 'opt_remind': '1',
                                                  'remind_days': '5'}).json()
        self.assertTrue(d['ok'], d)
        self.assertEqual((send.call_args.kwargs['importance'], send.call_args.kwargs['read_receipt']),
                         ('important', True))
        f = MailFollowup.objects.get(msg_id=OUR_ID)
        copy = EmailMessage.objects.get(message_id=OUR_ID)
        self.assertEqual((f.message_id, f.recipients, f.read_receipt), (copy.pk, ['anna@kunde.de'], True))
        self.assertEqual(copy.importance, 'important')
        self.assertAlmostEqual((f.remind_at - f.sent_at).total_seconds(), 5 * 86400, delta=5)
        self.assertIn('нагадаю', d['followup'])

    def test_plain_send_no_followup(self):
        with mock.patch('email_assistant.smtp_client.SMTPClient.send',
                        return_value={'ok': True, 'message_id': OUR_ID}) as send:
            self.client.post('/email/send/', {'to': 'anna@kunde.de', 'subject': 'Angebot', 'body': 'Text'})
        self.assertEqual((send.call_args.kwargs['importance'], send.call_args.kwargs['read_receipt']), ('', False))
        self.assertFalse(MailFollowup.objects.exists())

    def test_scheduled_keeps_options(self):
        from django.core.management import call_command
        when = (timezone.localtime() + datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        d = self.client.post('/email/schedule/', {'to': 'anna@kunde.de', 'subject': 'Angebot', 'body': 'Text',
                                                  'scheduled_at': when, 'opt_receipt': '1', 'opt_remind': '1',
                                                  'remind_days': '2', 'remind_until': 'reply'}).json()
        self.assertTrue(d['ok'], d)
        s = ScheduledEmail.objects.get(pk=d['id'])
        self.assertEqual((s.send_options['read_receipt'], s.send_options['remind_days'], s.send_options['until']),
                         (True, 2, 'reply'))
        ScheduledEmail.objects.filter(pk=s.pk).update(scheduled_at=timezone.now() - datetime.timedelta(minutes=1))
        with mock.patch('email_assistant.smtp_client.SMTPClient.send',
                        return_value={'ok': True, 'message_id': OUR_ID}) as send:
            call_command('send_scheduled', stdout=io.StringIO())
        self.assertTrue(send.call_args.kwargs['read_receipt'])
        f = MailFollowup.objects.get(msg_id=OUR_ID)
        self.assertEqual((f.until, f.message_id), ('reply', None))
        copy = self.incoming_sent()                     # копія з «Надісланих» — прив'язується
        f.refresh_from_db()
        self.assertEqual(f.message_id, copy.pk)

    def incoming_sent(self):
        m = MimeMessage()
        m['From'], m['To'], m['Subject'], m['Message-ID'] = 'slava@sevskiy.de', 'anna@kunde.de', 'Angebot', OUR_ID
        m['Date'] = email.utils.format_datetime(timezone.now())
        m.set_content('Text')
        return save_message(self.account, parsed(m.as_bytes(), 900), 'Sent', folder_type='sent')


class ReactionTests(Base):
    def setUp(self):
        super().setUp()
        self.msg = self.sent()
        self.f = followups.track(self.account, msg_id=OUR_ID, subject=self.msg.subject, recipients=['anna@kunde.de'],
                                 message=self.msg, read_receipt=True, remind_days=3)

    def test_parse_mdn(self):
        mdn = email.message_from_bytes(raw_mdn())
        self.assertEqual(followups.parse_mdn(mdn), {'for': OUR_ID, 'disposition': 'displayed'})
        self.assertIsNone(followups.parse_mdn(email.message_from_bytes(raw_mail())))

    def test_read_receipt_then_no_reminder(self):
        with self.deliver() as deliver:
            self.incoming(raw_mdn())
        deliver.assert_not_called()
        self.f.refresh_from_db()
        self.assertEqual((self.f.status, self.f.receipt, self.f.read_by), (MailFollowup.READ, 'displayed',
                                                                          'anna@kunde.de'))
        MailFollowup.objects.filter(pk=self.f.pk).update(remind_at=timezone.now() - datetime.timedelta(minutes=1))
        with self.deliver() as deliver:
            self.assertEqual(followups.run_due()['notified'], 0)    # «реакція» — прочитано
        deliver.assert_not_called()

    def test_read_but_waiting_for_reply(self):
        MailFollowup.objects.filter(pk=self.f.pk).update(until=MailFollowup.UNTIL_REPLY)
        self.incoming(raw_mdn())
        MailFollowup.objects.filter(pk=self.f.pk).update(remind_at=timezone.now() - datetime.timedelta(minutes=1))
        with self.deliver() as deliver:
            self.assertEqual(followups.run_due()['notified'], 1)
        m = deliver.call_args.args[1]
        self.assertIn('прочитано', m['head'])
        self.assertIn('ручне втручання', m['head'])
        self.assertIn(f'/email/?msg={self.msg.pk}', m['link'])

    def test_deleted_unread_notifies_now(self):
        with self.deliver() as deliver:
            self.incoming(raw_mdn(disposition='manual-action/MDN-sent-manually; deleted'))
        deliver.assert_called_once()
        self.f.refresh_from_db()
        self.assertEqual((self.f.status, self.f.receipt), (MailFollowup.NO_REACTION, 'deleted'))
        self.assertIn('Повідомлено', self.f.notify_result)

    def test_reply_by_in_reply_to(self):
        self.incoming(raw_mail(subject='Out of Office: Angebot', headers={'In-Reply-To': OUR_ID}))
        self.f.refresh_from_db()
        self.assertEqual(self.f.status, MailFollowup.WAITING)        # автовідповідь — не реакція
        reply = self.incoming(raw_mail(subject='AW: Angebot', headers={'In-Reply-To': OUR_ID}))
        self.f.refresh_from_db()
        self.assertEqual((self.f.status, self.f.reply_id), (MailFollowup.REPLIED, reply.pk))

    def test_no_reaction_notifies_once(self):
        MailFollowup.objects.filter(pk=self.f.pk).update(remind_at=timezone.now() - datetime.timedelta(minutes=1))
        with self.deliver() as deliver:
            self.assertEqual(followups.run_due()['notified'], 1)
            self.assertEqual(followups.run_due()['notified'], 0)
        self.assertEqual(deliver.call_count, 1)
        self.assertEqual(deliver.call_args.args[0], self.user)
        self.assertIn('не підтверджено прочитання й немає відповіді', deliver.call_args.args[1]['head'])
        self.f.refresh_from_db()
        self.assertEqual(self.f.status, MailFollowup.NO_REACTION)
        page = self.client.get('/email/?fu=1')
        self.assertContains(page, f'id="row-{self.msg.pk}"')
        self.assertContains(page, '⚠️ без реакції')
        prev = self.client.get(f'/email/message/{self.msg.pk}/preview/')
        self.assertContains(prev, 'Потрібне ручне втручання')
        # ⏰ +2 дні → знову чекаємо; ✓ Закрити → більше не стежимо
        d = self.client.post(f'/email/followup/{self.f.pk}/', json.dumps({'action': 'snooze', 'days': 2}),
                             content_type='application/json').json()
        self.assertEqual(d['status'], MailFollowup.WAITING)
        d = self.client.post(f'/email/followup/{self.f.pk}/', json.dumps({'action': 'done'}),
                             content_type='application/json').json()
        self.assertEqual(d['status'], MailFollowup.DONE)
        self.assertNotContains(self.client.get('/email/?fu=1'), f'id="row-{self.msg.pk}"')


class RemindExistingTests(Base):
    def test_remind_on_sent_mail(self):
        msg = self.sent(days_ago=10)
        d = self.client.post(f'/email/message/{msg.pk}/followup/', json.dumps({'days': 3}),
                             content_type='application/json').json()
        self.assertTrue(d['ok'], d)
        f = MailFollowup.objects.get(message=msg)
        self.assertGreater(f.remind_at, timezone.now() + datetime.timedelta(days=2))   # давній лист — від сьогодні
        inbox = self.incoming(raw_mail())
        r = self.client.post(f'/email/message/{inbox.pk}/followup/', json.dumps({'days': 3}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_already_replied(self):
        from email_assistant.sync import get_or_create_thread
        msg = self.sent(days_ago=2)
        thread = get_or_create_thread(self.account, {'subject': msg.subject, 'from_email': msg.from_email,
                                                     'to_emails': msg.to_emails, 'message_id': OUR_ID,
                                                     'sent_at': msg.sent_at})
        EmailMessage.objects.filter(pk=msg.pk).update(thread=thread)
        msg.refresh_from_db()
        EmailMessage.objects.create(account=self.account, thread=thread, folder='inbox', imap_folder_name='INBOX',
                                    imap_uid=5, subject='AW: Angebot', from_email='anna@kunde.de',
                                    in_reply_to=OUR_ID, sent_at=timezone.now())
        f = followups.track_message(msg, 3)
        self.assertEqual(f.status, MailFollowup.REPLIED)
