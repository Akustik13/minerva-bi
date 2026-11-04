"""⏰ Регулярні завдання агенту (agent/schedules.py): розклад, запуск раз на день, звіт у Telegram / пошту
(agent/notify.report), інструменти пошти й календаря (agent/inbox_tools.py), сторінка в адмінці. Claude — сценарій,
повідомлення підмінено.

python manage.py test agent.tests_schedules --settings=tabele.settings_test
"""
import datetime
import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone

from agent import inbox_tools, notify, schedules, tools
from agent.models import AgentSchedule, AgentTask
from agent.tests_tasks import Base, FakeClient, resp, text, tool


def at(day, hh, mm=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hh, mm)))


class ScheduleTests(Base):
    def setUp(self):
        super().setUp()
        self.s = AgentSchedule.objects.create(owner=self.user, name='📦 Що не відправлено й чому',
                                              instruction='Перевір, що не відправлено й чому.',
                                              time=datetime.time(8, 0), days=[0, 1, 2, 3, 4])
        self.monday = datetime.date(2026, 11, 2)

    def test_due_window_weekdays_and_once_a_day(self):
        s = self.s
        self.assertFalse(schedules.due(s, at(self.monday, 7, 59)))
        self.assertTrue(schedules.due(s, at(self.monday, 8, 0)))
        self.assertTrue(schedules.due(s, at(self.monday, 10, 30)))
        self.assertFalse(schedules.due(s, at(self.monday, 11, 30)))         # пізніше за 3 год — уже не сьогодні
        self.assertFalse(schedules.due(s, at(self.monday + datetime.timedelta(days=5), 8, 30)))   # субота
        self.assertEqual(schedules.days_text(s), 'у будні')
        self.assertEqual(schedules.run_due(at(self.monday, 8, 5)), 1)
        self.assertEqual(schedules.run_due(at(self.monday, 8, 6)), 0)       # раз на день
        task = AgentTask.objects.get(schedule=s)
        self.assertEqual((task.status, task.model, task.owner), (AgentTask.QUEUED, s.model, self.user))
        self.assertIn('Перевір, що не відправлено й чому.', task.instruction)
        self.assertIn('звіт людині', task.instruction)
        s.refresh_from_db()
        self.assertEqual(schedules.next_run(s, at(self.monday, 9, 0)), at(self.monday + datetime.timedelta(days=1), 8))
        s.enabled = False
        self.assertIsNone(schedules.next_run(s))

    def test_cron_runs_task_and_sends_report(self):
        fake = FakeClient([resp('end_turn', text('1. Замовлення 102572339 — немає на складі AN220201-04A (бракує 2).\n'
                                                 '2. Замовлення 102497269 — можна відправити сьогодні.'))])
        now = at(timezone.localdate(), 8, 1)
        with mock.patch('agent.schedules.timezone.now', return_value=now), \
                mock.patch('agent.tasks._client', return_value=fake), \
                mock.patch('agent.notify.deliver', return_value='🔔 Повідомлено: пошта, Telegram') as deliver:
            AgentSchedule.objects.filter(pk=self.s.pk).update(days=[])
            call_command('agent_runs', stdout=io.StringIO())
        task = AgentTask.objects.get(schedule=self.s)
        self.assertEqual(task.status, AgentTask.DONE)
        user, m = deliver.call_args.args
        self.assertEqual(user, self.user)
        self.assertEqual(deliver.call_args.kwargs, {'mail': True, 'telegram': True})
        self.assertTrue(m['head'].startswith('🤖 Звіт агента — «📦 Що не відправлено й чому»'))
        self.assertIn('немає на складі AN220201-04A', m['summary'])
        self.assertEqual(m['tg_max'], notify.REPORT_TG_MAX)

    def test_report_channels_follow_schedule(self):
        AgentSchedule.objects.filter(pk=self.s.pk).update(notify_email=False)
        task = schedules.make_task(AgentSchedule.objects.get(pk=self.s.pk))
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.DONE, summary='Усе відправлено.')
        with mock.patch('agent.notify._send_mail', return_value='') as mail, \
                mock.patch('agent.notify._send_telegram', return_value='') as tg:
            from core.models import UserProfile
            UserProfile.objects.filter(user=self.user).update(telegram_id=12345)
            out = notify.task_finished(task.pk)
        mail.assert_not_called()
        tg.assert_called_once()
        self.assertIn('Telegram', out)


class InboxToolTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.acc = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        other = User.objects.create_user('olga', 'o@x.y', 'p', is_staff=True)
        self.other_acc = EmailAccount.objects.create(user=other, email_address='olga@sevskiy.de')
        self.task = AgentTask.objects.create(owner=self.user, title='Ранковий огляд', instruction='Що в пошті?')
        self.ctx = tools.Context(self.task)

    def mail(self, acc, subject, **kw):
        from email_assistant.models import EmailMessage
        n = EmailMessage.objects.filter(account=acc).count() + 1
        return EmailMessage.objects.create(account=acc, folder='inbox', imap_folder_name='INBOX', imap_uid=n,
                                           subject=subject, from_email='anna@kunde.de', body_text='Bitte Angebot.',
                                           sent_at=timezone.now() - datetime.timedelta(hours=1), **kw)

    def test_list_and_open_only_own_mail(self):
        from email_assistant.models import MailFollowup
        mine = self.mail(self.acc, 'Anfrage AN220213', importance='urgent')
        theirs = self.mail(self.other_acc, 'Geheim')
        old = self.mail(self.acc, 'Alt')                       # старіше за 24 год — не показується
        type(old).objects.filter(pk=old.pk).update(sent_at=timezone.now() - datetime.timedelta(days=3))
        MailFollowup.objects.create(account=self.acc, msg_id='<x@y>', subject='Angebot A-1', recipients=['b@c.de'],
                                    sent_at=timezone.now(), status=MailFollowup.NO_REACTION)
        out, err = tools.run(self.ctx, 'list_mail', {'hours': 24})
        self.assertFalse(err)
        self.assertIn(f'#{mine.pk}', out)
        self.assertIn('‼️', out)
        self.assertNotIn('Geheim', out)
        self.assertNotIn('Alt', out)
        self.assertIn('без реакції', out)
        self.assertIn('Angebot A-1', out)
        out, err = tools.run(self.ctx, 'open_mail', {'id': theirs.pk})
        self.assertTrue(err)
        out, err = tools.run(self.ctx, 'open_mail', {'id': mine.pk})
        self.assertIn('Bitte Angebot.', out)

    def test_calendar_and_definitions(self):
        from calendar_app.models import CalendarEvent
        today = timezone.localdate()
        CalendarEvent.objects.create(user=self.user, title='Call Sergey', event_type=CalendarEvent.TYPE_MEETING,
                                     start_at=at(today, 15))
        CalendarEvent.objects.create(user=self.user, title='Angebot senden', event_type=CalendarEvent.TYPE_DEADLINE,
                                     start_at=at(today - datetime.timedelta(days=2), 9))
        out, _ = tools.run(self.ctx, 'calendar_days', {'days': 0})
        self.assertIn('Call Sergey', out)
        self.assertIn('Прострочені невиконані дедлайни', out)
        self.assertIn('Angebot senden', out)
        names = {t['name'] for t in tools.definitions(self.task) if 'name' in t}
        self.assertTrue({'list_mail', 'open_mail', 'calendar_days'} <= names)
        nobody = AgentTask.objects.create(owner=User.objects.create_user('ivan', 'i@x.y', 'p', is_staff=True),
                                          title='x', instruction='x')
        self.assertFalse(inbox_tools.can_read(nobody.owner))
        self.assertNotIn('list_mail', {t.get('name') for t in tools.definitions(nobody)})


class ScheduleAdminTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_preset_add_and_run_now(self):
        page = self.client.get('/admin/agent/agentschedule/add/?preset=morning').content.decode()
        self.assertIn('☀️ Ранковий огляд пошти й дня', page)
        self.assertIn('list_mail', page)
        r = self.client.post('/admin/agent/agentschedule/add/', {
            'name': 'Ранок', 'enabled': 'on', 'instruction': 'Що в пошті?', 'time': '08:00', 'days': ['0', '2'],
            'model': 'claude-haiku-5-5', 'budget_usd': '0.30', 'notify_telegram': 'on'})
        self.assertEqual(r.status_code, 302, r.content[:500])
        s = AgentSchedule.objects.get()
        self.assertEqual((s.owner, s.days, s.notify_email), (self.user, [0, 2], False))
        self.assertIn('Ср', self.client.get('/admin/agent/agentschedule/').content.decode())
        with mock.patch('agent.tasks.start') as start:
            r = self.client.post(f'/admin/agent/agentschedule/{s.pk}/run/')
        start.assert_called_once()
        task = AgentTask.objects.get(schedule=s)
        self.assertEqual(r['Location'], f'/admin/agent/agenttask/{task.pk}/review/')
        s.refresh_from_db()
        self.assertIsNone(s.last_run_at)                      # ручний запуск розклад не змінює
        other = User.objects.create_superuser('olga', 'o@x.y', 'p')
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/admin/agent/agentschedule/{s.pk}/change/').status_code, 302)
        self.assertEqual(self.client.post(f'/admin/agent/agentschedule/{s.pk}/run/').status_code, 404)


class PreviewTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.s = AgentSchedule.objects.create(owner=self.user, name='☀️ Ранок', instruction='Що в пошті?',
                                              time=datetime.time(8, 0), days=[])
        self.client.force_login(self.user)

    def test_preview_lists_access_and_report(self):
        info = schedules.preview(self.s)
        see = ' '.join(text for _ok, text in info['see'])
        self.assertIn('📦 Замовлення й відправлення', see)
        self.assertIn('📬 Ваша пошта — за останню добу 0 листів', see)
        report = dict((text.split(' — ')[0], ok) for ok, text in info['report'])
        self.assertFalse(report['💬 Telegram'])                       # немає Telegram ID
        self.assertTrue(report['✉️ Пошта'])
        self.assertEqual(info['when'][0], 'щодня о 08:00')
        page = self.client.get(f'/admin/agent/agentschedule/{self.s.pk}/change/').content.decode()
        self.assertIn('👀 Що агент бачитиме', page)
        self.assertIn('🧪 Спробувати без звіту', page)
        self.assertIn('не вказано Telegram ID', page)

    def test_quiet_run_sends_nothing(self):
        fake = FakeClient([resp('end_turn', text('Пошта: 2 важливі листи.'))])
        with mock.patch('agent.tasks._client', return_value=fake), \
                mock.patch('agent.notify.deliver') as deliver:
            r = self.client.post(f'/admin/agent/agentschedule/{self.s.pk}/run/?quiet=1')
        deliver.assert_not_called()
        task = AgentTask.objects.get(schedule=self.s)
        self.assertTrue(task.quiet)
        self.assertTrue(task.title.startswith('🧪 '))
        self.assertEqual((task.status, task.summary), (AgentTask.DONE, 'Пошта: 2 важливі листи.'))
        self.assertEqual(r['Location'], f'/admin/agent/agenttask/{task.pk}/review/')
        page = self.client.get(r['Location']).content.decode()
        self.assertIn('пробний запуск — звіт нікуди не надсилається', page)
        self.s.refresh_from_db()
        self.assertIsNone(self.s.last_run_at)
        self.assertIn('пробний, без звіту',
                      self.client.get(f'/admin/agent/agentschedule/{self.s.pk}/change/').content.decode())
