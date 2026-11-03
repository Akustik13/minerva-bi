"""🔔 Агент закінчив завдання — лист і Telegram власнику (agent/notify.py). Нічого не надсилається: пошту й Telegram
підмінено.

python manage.py test agent.tests_notify --settings=tabele.settings_test
"""
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from agent import notify, tasks
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base, resp, text, tool
from config.models import NotificationSettings
from core.models import UserProfile


class NotifyTests(Base):
    def setUp(self):
        super().setUp()
        self.mail = mock.patch('worktime.reports.deliver', return_value='').start()
        self.tg = mock.patch('dashboard.notifications._send_telegram').start()
        self.addCleanup(mock.patch.stopall)

    def running(self, minutes=5, **kw):
        task = self.task(**kw)
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.RUNNING,
                                                    started_at=timezone.now() - timedelta(minutes=minutes))
        task.refresh_from_db()
        return task

    def sent(self):
        args = self.mail.call_args[0]
        return {'to': args[1], 'subject': args[2], 'text': args[3], 'html': args[4]}

    def test_long_run_with_proposals(self):
        task = self.running()
        AgentProposal.objects.create(task=task, kind=AgentProposal.FIELD, product=self.ant, field='net_weight_g',
                                     new_value='45')
        task.summary = 'Знайдено вагу AN220213-01C.'
        tasks._finish(task)
        m = self.sent()
        self.assertEqual(m['to'], ['s@x.y'])
        self.assertIn(task.title, m['subject'])
        self.assertIn('1 пропозицій чекають вашої перевірки', m['text'])
        self.assertIn(f'/admin/agent/agenttask/{task.pk}/review/', m['text'])
        self.assertIn('Знайдено вагу', m['html'])
        task.refresh_from_db()
        self.assertEqual(task.status, AgentTask.REVIEW)
        self.assertIn('🔔 Повідомлено: пошта', task.log[-1]['text'])
        self.tg.assert_not_called()                                # Telegram ID не вписано

    def test_telegram_too(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.telegram_id = 12345
        profile.save()
        ns = NotificationSettings.get()
        ns.telegram_bot_token = 'x'
        ns.save()
        task = self.running()
        tasks._finish(task)
        self.assertEqual(self.tg.call_args.kwargs['chat_id_override'], 12345)
        self.assertIn('нових пропозицій немає', self.tg.call_args[0][1])
        task.refresh_from_db()
        self.assertIn('пошта, Telegram', task.log[-1]['text'])

    def test_short_run_stays_quiet(self):
        task = self.running(minutes=0)
        tasks._finish(task)
        self.mail.assert_not_called()

    def test_stopped_by_person_quiet(self):
        task = self.running()
        tasks._finish(task, note='зупинено людиною', cancelled=True)
        self.mail.assert_not_called()

    def test_limit_and_error(self):
        task = self.running()
        tasks._finish(task, note='досягнуто ліміту 2 $')
        self.assertIn('⏸', self.sent()['subject'])
        self.assertIn('▶ Продовжити', self.sent()['text'])
        task = self.running()
        tasks._finish(task, error='немає ключа AI')
        self.assertIn('⚠️', self.sent()['subject'])
        self.assertIn('немає ключа AI', self.sent()['text'])

    def test_mcp_task_never(self):
        task = self.running(mcp_session='abc12345')
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.REVIEW, finished_at=timezone.now())
        self.assertEqual(notify.task_finished(task.pk), '')
        self.mail.assert_not_called()

    def test_failed_send_only_logged(self):
        self.mail.return_value = 'не задано SMTP'
        task = self.running()
        tasks._finish(task)
        task.refresh_from_db()
        self.assertEqual(task.status, AgentTask.DONE)
        self.assertIn('🔕 Не вдалося повідомити — пошта: не задано SMTP', task.log[-1]['text'])

    def test_whole_run(self):
        task = self.task()
        with mock.patch.object(notify, 'NOTIFY_AFTER_SECONDS', 0):
            self.go(task, [
                resp('tool_use', tool('t1', 'read_datasheet', sku='AN220213-01C')),
                resp('tool_use', tool('t2', 'propose_change', sku='AN220213-01C', field='net_weight_g', value='45',
                                      evidence='Weight: 45 g', source='даташит, стор. 1')),
                resp('end_turn', text('Вага знайдена.')),
            ])
        self.assertEqual(task.status, AgentTask.REVIEW)
        self.assertIn('Вага знайдена.', self.sent()['text'])
