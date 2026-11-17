"""Запрошення з календаря Minerva (calendar_app/outgoing_invites.py): iCalendar REQUEST / CANCEL зі скриньки людини,
оновлення (SEQUENCE), нові й прибрані учасники, видалення події → «скасовано», відповіді REPLY / COUNTER з листів.
SMTP підмінено, відповіді — синтетичні ics.

python manage.py test calendar_app.tests_outgoing --settings=tabele.settings_test
"""
import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from calendar_app import outgoing_invites as oi
from calendar_app.models import CalendarEvent, InviteAttendee, MeetingInvite, OutgoingInvite
from email_assistant.models import EmailAccount, EmailMessage

ORG = "organizer@sevskiy.example"
TEAMS = "https://teams.live.com/meet/9345?p=AbCdEf"


def unfold(ics: str) -> str:
    return ics.replace("\r\n ", "")


def reply_ics(uid, attendee, partstat="ACCEPTED", seq=0, method="REPLY", extra=""):
    return ("BEGIN:VCALENDAR\r\nPRODID:Microsoft Exchange Server 2010\r\nVERSION:2.0\r\nMETHOD:" + method + "\r\n"
            f"BEGIN:VEVENT\r\nUID:{uid}\r\nSEQUENCE:{seq}\r\n"
            f"ATTENDEE;PARTSTAT={partstat};CN=Someone:mailto:{attendee}\r\n"
            f"ORGANIZER:mailto:{ORG}\r\nDTSTAMP:20261117T120000Z\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n")


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(user=self.user, email_address=ORG, display_name='Slava Organizer')
        start = (timezone.localtime(timezone.now()) + datetime.timedelta(days=3)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        self.event = CalendarEvent.objects.create(user=self.user, title='Abstimmung Antenne', start_at=start,
                                                  end_at=start + datetime.timedelta(hours=1),
                                                  event_type=CalendarEvent.TYPE_MEETING)
        self.start = start

    def smtp(self, ok=True):
        return mock.patch('email_assistant.smtp_client.SMTPClient.send',
                          return_value={'ok': ok, 'error': '' if ok else 'SMTP недоступний'})

    def invite(self, **data):
        base = {'attendees': 'Anna Muster <anna@acme.example>, bob@beta.example', 'join_url': TEAMS}
        base.update(data)
        with self.smtp() as send:
            out = oi.send(self.user, self.event, base)
        self.inv = OutgoingInvite.objects.get(event=self.event)
        return out, send

    def mail(self, ics, folder='inbox', sender='anna@acme.example', when=None):
        return EmailMessage.objects.create(account=self.account, folder=folder, imap_folder_name='INBOX',
                                           imap_uid=EmailMessage.objects.count() + 100, subject='Accepted: Abstimmung',
                                           body_text=ics, from_email=sender, sent_at=when or timezone.now())


class SendTests(Base):
    def test_first_invite_is_request_with_attendees(self):
        out, send = self.invite()
        self.assertEqual((out['kind'], sorted(out['to'])), ('new', ['anna@acme.example', 'bob@beta.example']))
        args, kw = send.call_args
        self.assertEqual(sorted(args[0]), ['anna@acme.example', 'bob@beta.example'])
        self.assertTrue(args[1].startswith('Einladung / Invitation: Abstimmung Antenne'))
        self.assertIn(TEAMS, args[2])                                     # посилання в тексті листа
        method, ics = kw['calendar']
        self.assertEqual(method, 'REQUEST')
        self.assertTrue(all(len(x.encode()) <= 75 for x in ics.split('\r\n')))
        ics = unfold(ics)
        self.assertIn('METHOD:REQUEST', ics)
        self.assertIn(f'UID:{self.inv.uid}', ics)
        self.assertTrue(self.inv.uid.startswith('minerva-') and self.inv.uid.endswith('@sevskiy.example'))
        self.assertIn('SEQUENCE:0', ics)
        self.assertIn('BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin', ics)
        self.assertIn(f'DTSTART;TZID=Europe/Berlin:{self.start:%Y%m%dT%H%M%S}', ics)
        self.assertIn(f'ORGANIZER;CN=Slava Organizer:mailto:{ORG}', ics)
        self.assertIn('ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;'
                      'CN=Anna Muster:mailto:anna@acme.example', ics)
        self.assertIn(f'X-MICROSOFT-SKYPETEAMSMEETINGURL:{TEAMS}', ics)
        self.assertIn(f'LOCATION:{TEAMS}', ics)
        self.assertEqual(self.inv.status, OutgoingInvite.SENT)
        self.assertTrue(all(a.invited_at for a in self.inv.attendees.all()))

    def test_preview_saves_and_sends_nothing(self):
        with self.smtp() as send:
            p = oi.preview(self.user, None, {'title': 'Neu', 'start': '2030-01-10T09:00',
                                             'attendees': ['x@y.example']})
        send.assert_not_called()
        self.assertEqual((p['kind'], p['request']), ('new', ['x@y.example']))
        self.assertFalse(CalendarEvent.objects.filter(title='Neu').exists())
        self.assertFalse(OutgoingInvite.objects.exists())

    def test_time_change_sends_update_and_resets_answers(self):
        self.invite()
        self.inv.attendees.filter(email='anna@acme.example').update(partstat=InviteAttendee.ACCEPTED)
        new = self.start + datetime.timedelta(hours=2)
        p = oi.preview(self.user, self.event, {'start': f'{new:%Y-%m-%dT%H:%M}', 'end': f'{new:%Y-%m-%d}T13:00'})
        self.assertEqual((p['kind'], p['time_changed'], p['sequence']), ('update', True, 1))
        with self.smtp() as send:
            out = oi.send(self.user, self.event, {'start': f'{new:%Y-%m-%dT%H:%M}', 'end': f'{new:%Y-%m-%d}T13:00',
                                                  'expect': p['request'] + p['cancel']})
        self.assertEqual(out['kind'], 'update')
        args, kw = send.call_args
        self.assertTrue(args[1].startswith('Aktualisierte Einladung / Updated invitation'))
        ics = unfold(kw['calendar'][1])
        self.assertIn('SEQUENCE:1', ics)
        self.assertIn(f'DTSTART;TZID=Europe/Berlin:{new:%Y%m%dT%H%M%S}', ics)
        self.assertNotIn('PARTSTAT=ACCEPTED', ics)                       # новий час — відповіді заново
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.sequence, self.inv.rsvp_sequence), (1, 1))
        self.assertEqual(self.inv.attendees.get(email='anna@acme.example').partstat, InviteAttendee.NEEDS)
        self.assertEqual(oi.changed_keys(self.inv), [])

    def test_location_change_keeps_answers(self):
        self.invite()
        self.inv.attendees.filter(email='anna@acme.example').update(partstat=InviteAttendee.ACCEPTED)
        with self.smtp() as send:
            oi.send(self.user, self.event, {'location': 'Raum 2, Kiel'})
        ics = unfold(send.call_args[1]['calendar'][1])
        self.assertIn('SEQUENCE:1', ics)
        self.assertIn('LOCATION:Raum 2\\, Kiel', ics)                     # кома екранована
        self.assertIn('PARTSTAT=ACCEPTED', ics)
        self.assertEqual(self.inv.attendees.get(email='anna@acme.example').partstat, InviteAttendee.ACCEPTED)

    def test_added_attendee_gets_only_his_invite(self):
        self.invite()
        with self.smtp() as send:
            out = oi.send(self.user, self.event,
                          {'attendees': 'anna@acme.example, bob@beta.example, carol@gamma.example'})
        self.assertEqual((out['kind'], out['to']), ('added', ['carol@gamma.example']))
        self.assertEqual(send.call_args[0][0], ['carol@gamma.example'])
        ics = unfold(send.call_args[1]['calendar'][1])
        self.assertIn('SEQUENCE:0', ics)
        self.assertIn('mailto:anna@acme.example', ics)                   # у запрошенні — усі учасники

    def test_removed_attendee_gets_cancel(self):
        self.invite()
        p = oi.preview(self.user, self.event, {'attendees': ['anna@acme.example']})
        self.assertEqual((p['kind'], p['request'], p['cancel']), ('none', [], ['bob@beta.example']))
        with self.smtp() as send:
            out = oi.send(self.user, self.event, {'attendees': ['anna@acme.example']})
        self.assertEqual(out['cancelled'], ['bob@beta.example'])
        self.assertEqual(send.call_count, 1)
        args, kw = send.call_args
        self.assertEqual(args[0], ['bob@beta.example'])
        self.assertTrue(args[1].startswith('Abgesagt / Cancelled'))
        ics = unfold(kw['calendar'][1])
        self.assertEqual(kw['calendar'][0], 'CANCEL')
        self.assertIn('STATUS:CANCELLED', ics)
        self.assertIn('SEQUENCE:1', ics)
        self.assertNotIn('anna@acme.example', ics)
        self.assertEqual(list(self.inv.attendees.values_list('email', flat=True)), ['anna@acme.example'])

    def test_nothing_changed_needs_resend(self):
        self.invite()
        with self.smtp() as send, self.assertRaises(oi.InviteError):
            oi.send(self.user, self.event, {})
        send.assert_not_called()
        with self.smtp() as send:
            out = oi.send(self.user, self.event, {'resend': True})
        self.assertEqual(out['kind'], 'resend')
        self.assertIn('SEQUENCE:0', unfold(send.call_args[1]['calendar'][1]))

    def test_confirmed_recipients_must_match(self):
        with self.smtp() as send, self.assertRaisesMessage(oi.InviteError, 'список адресатів змінився'):
            oi.send(self.user, self.event, {'attendees': 'anna@acme.example, bob@beta.example',
                                            'expect': ['anna@acme.example']})
        send.assert_not_called()

    def test_smtp_failure_keeps_draft(self):
        with self.smtp(ok=False), self.assertRaisesMessage(oi.InviteError, 'SMTP недоступний'):
            oi.send(self.user, self.event, {'attendees': 'anna@acme.example'})
        inv = OutgoingInvite.objects.get(event=self.event)
        self.assertEqual(inv.status, OutgoingInvite.DRAFT)
        self.assertIn('SMTP недоступний', inv.last_error)
        self.assertIsNone(inv.attendees.get().invited_at)

    def test_all_day_invite_uses_dates(self):
        self.event.all_day = True
        self.event.end_at = None
        self.event.save()
        _out, send = self.invite()
        ics = unfold(send.call_args[1]['calendar'][1])
        d = self.start.date()
        self.assertIn(f'DTSTART;VALUE=DATE:{d:%Y%m%d}', ics)
        self.assertIn(f'DTEND;VALUE=DATE:{d + datetime.timedelta(days=1):%Y%m%d}', ics)
        self.assertNotIn('VTIMEZONE', ics)

    def test_bad_input_refused(self):
        with self.assertRaisesMessage(oi.InviteError, 'незрозуміла адреса'):
            oi.send(self.user, self.event, {'attendees': 'not-an-email'})
        with self.assertRaisesMessage(oi.InviteError, 'https://'):
            oi.send(self.user, self.event, {'attendees': 'a@b.example', 'join_url': 'javascript:alert(1)'})
        past = timezone.now() - datetime.timedelta(days=2)
        with self.smtp() as send, self.assertRaisesMessage(oi.InviteError, 'уже минула'):
            oi.send(self.user, self.event, {'attendees': 'a@b.example', 'start': f'{timezone.localtime(past):%Y-%m-%dT%H:%M}',
                                            'end': ''})
        send.assert_not_called()

    def test_organizer_is_not_an_attendee(self):
        out, _send = self.invite(attendees=f'anna@acme.example, {ORG}')
        self.assertEqual(out['to'], ['anna@acme.example'])

    def test_foreign_meeting_and_foreign_mailbox_refused(self):
        MeetingInvite.objects.create(user=self.user, uid='x@outlook', title='Fremd', start_at=self.start,
                                     organizer='Max Muster', event=self.event, status=MeetingInvite.ADDED)
        with self.assertRaisesMessage(oi.InviteError, 'Max Muster'):
            oi.send(self.user, self.event, {'attendees': 'a@b.example'})
        other = User.objects.create_user('other', 'o@x.y', 'p')
        acc = EmailAccount.objects.create(user=other, email_address='other@x.example')
        MeetingInvite.objects.all().delete()
        with self.assertRaisesMessage(oi.InviteError, 'не ваша скринька'):
            oi.send(self.user, self.event, {'attendees': 'a@b.example', 'account': acc.pk})


class CancelTests(Base):
    def test_cancel_meeting_sends_cancel_to_all(self):
        self.invite()
        with self.smtp() as send:
            out = oi.cancel(self.user, self.inv)
        self.assertEqual(sorted(out['to']), ['anna@acme.example', 'bob@beta.example'])
        method, ics = send.call_args[1]['calendar']
        ics = unfold(ics)
        self.assertEqual(method, 'CANCEL')
        self.assertIn('METHOD:CANCEL', ics)
        self.assertIn('SEQUENCE:1', ics)
        self.assertIn(f'UID:{self.inv.uid}', ics)
        self.event.refresh_from_db()
        self.assertTrue(self.event.title.startswith('✖ Скасовано: '))
        self.assertTrue(self.event.is_done)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, OutgoingInvite.CANCELLED)

    def test_reinvite_after_cancel(self):
        self.invite()
        with self.smtp():
            oi.cancel(self.user, self.inv)
        with self.smtp() as send:
            out = oi.send(self.user, self.event, {'title': 'Abstimmung Antenne'})
        self.assertEqual(out['kind'], 'new')
        self.assertIn('SEQUENCE:2', unfold(send.call_args[1]['calendar'][1]))
        self.event.refresh_from_db()
        self.assertFalse(self.event.is_done)

    def test_deleting_event_sends_cancel(self):
        self.invite()
        with self.smtp() as send, self.captureOnCommitCallbacks(execute=True):
            CalendarEvent.objects.filter(pk=self.event.pk).delete()
        self.assertEqual(send.call_args[1]['calendar'][0], 'CANCEL')
        self.assertIn('DTSTART;TZID=Europe/Berlin', unfold(send.call_args[1]['calendar'][1]))
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.status, self.inv.cancel_pending, self.inv.event_id),
                         (OutgoingInvite.CANCELLED, False, None))

    def test_failed_cancel_waits_for_retry(self):
        self.invite()
        with self.smtp(ok=False), self.captureOnCommitCallbacks(execute=True):
            self.event.delete()
        self.inv.refresh_from_db()
        self.assertTrue(self.inv.cancel_pending)
        self.assertEqual([i.pk for i in oi.pending_cancels(self.user)], [self.inv.pk])
        self.client.force_login(self.user)
        with self.smtp() as send:
            r = self.client.post(f'/calendar/outgoing/{self.inv.pk}/retry/')
        self.assertTrue(r.json()['ok'])
        send.assert_called_once()
        self.assertEqual(oi.pending_cancels(self.user), [])

    def test_past_meeting_deleted_without_mail(self):
        self.invite()
        past = timezone.now() - datetime.timedelta(days=5)
        CalendarEvent.objects.filter(pk=self.event.pk).update(start_at=past, end_at=past + datetime.timedelta(hours=1))
        with self.smtp() as send, self.captureOnCommitCallbacks(execute=True):
            CalendarEvent.objects.get(pk=self.event.pk).delete()
        send.assert_not_called()


class ReplyTests(Base):
    def test_outlook_accept_and_google_decline(self):
        self.invite()
        msg = self.mail(reply_ics(self.inv.uid, 'anna@acme.example', 'ACCEPTED'))
        anna = self.inv.attendees.get(email='anna@acme.example')
        self.assertEqual((anna.partstat, anna.reply_message_id), (InviteAttendee.ACCEPTED, msg.pk))
        self.mail(reply_ics(self.inv.uid, 'Bob@Beta.example', 'DECLINED',
                            extra='COMMENT:Leider im Urlaub\r\n').replace('mailto:', 'MAILTO:'),
                  sender='bob@beta.example')
        bob = self.inv.attendees.get(email='bob@beta.example')
        self.assertEqual((bob.partstat, bob.comment), (InviteAttendee.DECLINED, 'Leider im Urlaub'))
        self.assertEqual(oi.counts(self.inv)['accepted'], 1)
        self.assertFalse(MeetingInvite.objects.exists())                  # відповідь — не запрошення

    def test_older_reply_does_not_override_newer(self):
        self.invite()
        now = timezone.now()
        self.mail(reply_ics(self.inv.uid, 'anna@acme.example', 'DECLINED'), when=now)
        self.mail(reply_ics(self.inv.uid, 'anna@acme.example', 'ACCEPTED'), when=now - datetime.timedelta(hours=1))
        self.assertEqual(self.inv.attendees.get(email='anna@acme.example').partstat, InviteAttendee.DECLINED)

    def test_reply_to_old_time_ignored(self):
        self.invite()
        new = self.start + datetime.timedelta(days=1)
        with self.smtp():
            oi.send(self.user, self.event, {'start': f'{new:%Y-%m-%dT%H:%M}', 'end': f'{new:%Y-%m-%d}T12:00'})
        self.mail(reply_ics(self.inv.uid, 'anna@acme.example', 'ACCEPTED', seq=0))
        self.assertEqual(self.inv.attendees.get(email='anna@acme.example').partstat, InviteAttendee.NEEDS)
        self.mail(reply_ics(self.inv.uid, 'anna@acme.example', 'ACCEPTED', seq=1))
        self.assertEqual(self.inv.attendees.get(email='anna@acme.example').partstat, InviteAttendee.ACCEPTED)

    def test_counter_proposal_noted(self):
        self.invite()
        self.mail(reply_ics(self.inv.uid, 'anna@acme.example', 'TENTATIVE', method='COUNTER',
                            extra='DTSTART:20301120T130000Z\r\nDTEND:20301120T140000Z\r\n'))
        anna = self.inv.attendees.get(email='anna@acme.example')
        self.assertEqual(anna.partstat, InviteAttendee.TENTATIVE)
        self.assertEqual(anna.comment, 'пропонує інший час: 20.11.2030 14:00–15:00')

    def test_other_users_reply_not_counted(self):
        self.invite()
        other = User.objects.create_user('other', 'o@x.y', 'p')
        acc = EmailAccount.objects.create(user=other, email_address='other@x.example')
        EmailMessage.objects.create(account=acc, folder='inbox', imap_folder_name='INBOX', imap_uid=7, subject='x',
                                    body_text=reply_ics(self.inv.uid, 'anna@acme.example'), from_email='a@b.c',
                                    sent_at=timezone.now())
        self.assertEqual(self.inv.attendees.get(email='anna@acme.example').partstat, InviteAttendee.NEEDS)

    def test_own_sent_copy_is_not_an_incoming_invite(self):
        _out, send = self.invite()
        self.mail(send.call_args[1]['calendar'][1], folder='sent', sender=ORG)
        self.assertFalse(MeetingInvite.objects.exists())

    def test_check_replies_finds_saved_mail(self):
        self.invite()
        EmailMessage.objects.bulk_create([EmailMessage(        # без сигналу — як лист, що прийшов до оновлення
            account=self.account, folder='inbox', imap_folder_name='INBOX', imap_uid=55, subject='Zugesagt',
            body_text=reply_ics(self.inv.uid, 'bob@beta.example', 'TENTATIVE'), from_email='bob@beta.example',
            sent_at=timezone.now())])
        self.assertEqual(oi.check_replies(self.inv), 1)
        self.assertEqual(self.inv.attendees.get(email='bob@beta.example').partstat, InviteAttendee.TENTATIVE)


class ViewTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def post(self, pk, **data):
        return self.client.post(f'/calendar/event/{pk}/invite/', data=json.dumps(data),
                                content_type='application/json').json()

    def test_dialog_flow_preview_then_send(self):
        st = self.client.get(f'/calendar/event/{self.event.pk}/invite/').json()['state']
        self.assertTrue(st['can'])
        self.assertEqual(st['accounts'][0]['email'], ORG)
        form = {'title': 'Abstimmung Antenne', 'start': f'{self.start:%Y-%m-%dT%H:%M}',
                'end': f'{self.start:%Y-%m-%d}T11:00', 'attendees': [{'email': 'anna@acme.example', 'name': 'Anna'}],
                'join_url': TEAMS, 'account': self.account.pk, 'lang': 'en'}
        with self.smtp() as send:
            d = self.post(self.event.pk, action='preview', **form)
            send.assert_not_called()
            self.assertEqual(d['plan']['request'], ['anna@acme.example'])
            d = self.post(self.event.pk, action='send', expect=d['plan']['request'], **form)
        self.assertTrue(d['ok'], d)
        self.assertTrue(send.call_args[0][1].startswith('Invitation: Abstimmung Antenne'))
        self.assertEqual(d['state']['attendees'][0]['icon'], '⏳')
        detail = self.client.get(f'/calendar/event/{self.event.pk}/').json()
        self.assertEqual(detail['invite']['attendees'][0]['email'], 'anna@acme.example')
        page = self.client.get('/calendar/?year=%d&month=%d' % (self.start.year, self.start.month))
        self.assertContains(page, 'data-invited="anna@acme.example"')
        self.assertContains(page, '👥 1 · ⏳ 1')

    def test_new_meeting_from_calendar(self):
        with self.smtp():
            d = self.post(0, action='send', title='Kick-off', start='2030-03-04T09:30', end='2030-03-04T10:00',
                          attendees=['x@y.example'])
        self.assertTrue(d['ok'], d)
        ev = CalendarEvent.objects.get(pk=d['event'])
        self.assertEqual((ev.title, ev.event_type, ev.user_id), ('Kick-off', 'meeting', self.user.pk))

    def test_foreign_event_not_found(self):
        other = User.objects.create_user('other', 'o@x.y', 'p')
        ev = CalendarEvent.objects.create(user=other, title='x', start_at=self.start)
        self.assertEqual(self.client.get(f'/calendar/event/{ev.pk}/invite/').status_code, 404)
        r = self.client.post(f'/calendar/event/{ev.pk}/invite/', data='{"action": "send"}',
                             content_type='application/json')
        self.assertEqual(r.status_code, 404)

    def test_error_keeps_state(self):
        d = self.post(self.event.pk, action='send', attendees='bad')
        self.assertFalse(d['ok'])
        self.assertIn('незрозуміла адреса', d['error'])
