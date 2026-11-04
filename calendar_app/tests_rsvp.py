"""✅ Буду / ❔ Можливо / ✕ Відхилити — відповідь організатору на запрошення з пошти (calendar_app/invites.py respond):
iCalendar METHOD:REPLY з UID / SEQUENCE / ORGANIZER оригіналу, лист зі скриньки, календар Minerva. SMTP підмінено.

python manage.py test calendar_app.tests_rsvp --settings=tabele.settings_test
"""
import email
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from calendar_app import invites
from calendar_app.models import CalendarEvent, MeetingInvite

ICS = ('BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:040000008200E00074C5B7101A82E008\r\nSEQUENCE:2\r\n'
       'DTSTART;TZID="W. Europe Standard Time":20261120T140000\r\nDTEND;TZID="W. Europe Standard Time":20261120T150000\r\n'
       'SUMMARY:Online meeting with Dr. Sergey Sevskiy\r\nORGANIZER;CN="Sevskiy, Sergey":mailto:sergey@sevskiy.de\r\n'
       'ATTENDEE;CN=Slava;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:Viacheslav.Pryimak@sevskiy.de\r\n'
       'ATTENDEE;CN=Pieter;PARTSTAT=ACCEPTED:mailto:pieter@customer.example\r\n'
       'LOCATION:MS Teams\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')


class RsvpTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from email_assistant.models import EmailAccount, EmailMessage
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(user=self.user, email_address='viacheslav.pryimak@sevskiy.de',
                                                   display_name='Viacheslav Pryimak')
        self.msg = EmailMessage.objects.create(account=self.account, folder='inbox', imap_folder_name='INBOX',
                                               imap_uid=1, subject='Einladung: Online meeting', body_text=ICS,
                                               from_email='sergey@sevskiy.de', sent_at=timezone.now())
        MeetingInvite.objects.filter(user=self.user).delete()          # сигнал міг уже створити — беремо свіже
        self.inv = invites.detect(self.msg)[0]

    def send(self, ok=True):
        return mock.patch('email_assistant.smtp_client.SMTPClient.send',
                          return_value={'ok': ok, 'error': '' if ok else 'SMTP недоступний'})

    def test_accept_sends_reply_and_adds_event(self):
        with self.send() as send:
            out = invites.respond(self.inv, self.user, 'accepted')
        self.assertEqual(out['to'], 'sergey@sevskiy.de')
        args, kw = send.call_args
        self.assertEqual(args[0], ['sergey@sevskiy.de'])
        self.assertTrue(args[1].startswith('Accepted / Angenommen: Online meeting'))
        method, ics = kw['calendar']
        self.assertTrue(all(len(x.encode()) <= 75 for x in ics.split('\r\n')))   # рядки згорнуто
        ics = ics.replace('\r\n ', '')
        self.assertEqual(method, 'REPLY')
        self.assertIn('METHOD:REPLY', ics)
        self.assertIn('UID:040000008200E00074C5B7101A82E008', ics)
        self.assertIn('SEQUENCE:2', ics)
        self.assertIn('ORGANIZER;CN="Sevskiy, Sergey":mailto:sergey@sevskiy.de', ics)
        self.assertIn('ATTENDEE;PARTSTAT=ACCEPTED;CN=Viacheslav Pryimak:mailto:viacheslav.pryimak@sevskiy.de', ics)
        self.assertNotIn('pieter@customer.example', ics)                   # лише наша відповідь
        self.assertNotIn('DTSTART;TZID', ics)                              # без VTIMEZONE — без поясу
        self.assertEqual(kw['reply_to_message'], self.msg)
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.status, self.inv.response), (MeetingInvite.ADDED, 'accepted'))
        self.assertTrue(CalendarEvent.objects.filter(pk=self.inv.event_id).exists())

    def test_tentative_then_decline(self):
        with self.send():
            invites.respond(self.inv, self.user, 'tentative')
        self.inv.refresh_from_db()
        self.assertTrue(self.inv.event.title.startswith('❔ '))
        with self.send() as send:
            invites.respond(self.inv, self.user, 'declined')
        self.assertIn('PARTSTAT=DECLINED', send.call_args[1]['calendar'][1])
        self.inv.refresh_from_db()
        event = CalendarEvent.objects.get(pk=self.inv.event_id)
        self.assertEqual((self.inv.status, self.inv.response), (MeetingInvite.DISMISSED, 'declined'))
        self.assertTrue(event.title.startswith('✕ Відхилено: '))
        self.assertTrue(event.is_done)

    def test_failure_changes_nothing(self):
        with self.send(ok=False):
            with self.assertRaises(invites.RespondError):
                invites.respond(self.inv, self.user, 'accepted')
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.status, self.inv.response, self.inv.event_id), (MeetingInvite.NEW, '', None))
        MeetingInvite.objects.filter(pk=self.inv.pk).update(status=MeetingInvite.CANCEL)
        self.inv.refresh_from_db()
        with self.send() as send, self.assertRaises(invites.RespondError):
            invites.respond(self.inv, self.user, 'accepted')
        send.assert_not_called()

    def test_view_and_preview(self):
        self.client.force_login(self.user)
        with self.send():
            d = self.client.post(f'/calendar/invites/{self.inv.pk}/yes/').json()
        self.assertEqual((d['ok'], d['to'], d['response']), (True, 'sergey@sevskiy.de', 'accepted'))
        page = self.client.get(f'/email/message/{self.msg.pk}/preview/')
        self.assertContains(page, '📅 <b>Запрошення</b>')
        self.assertContains(page, 'ваша відповідь: ✅ Буду')
        other = User.objects.create_superuser('olga', 'o@x.y', 'p')
        self.client.force_login(other)
        self.assertEqual(self.client.post(f'/calendar/invites/{self.inv.pk}/yes/').status_code, 404)

    def test_smtp_adds_calendar_part(self):
        from email_assistant.smtp_client import SMTPClient
        sent = {}

        class Conn:
            def login(self, *a):
                pass

            def sendmail(self, frm, to, data):
                sent['data'] = data

            def quit(self):
                pass

        ics = invites.build_reply(invites._raw_event(ICS, self.inv.uid), 'viacheslav.pryimak@sevskiy.de', 'V P',
                                  'accepted')
        with mock.patch.object(SMTPClient, '_open', return_value=Conn()), \
                mock.patch('email_assistant.imap_client.IMAPClient', side_effect=OSError('no imap')):
            r = SMTPClient(self.account).send(['sergey@sevskiy.de'], 'Accepted: x', 'body', calendar=('REPLY', ics))
        self.assertTrue(r['ok'], r)
        parsed = email.message_from_bytes(sent['data'])
        parts = {p.get_content_type(): p for p in parsed.walk()}
        self.assertEqual(parts['text/calendar'].get_param('method'), 'REPLY')
        self.assertIn('invite.ics', [p.get_filename() for p in parsed.walk() if p.get_filename()])
