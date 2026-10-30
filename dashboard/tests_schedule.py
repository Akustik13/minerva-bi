"""Звіти й попередження за розкладом: надсилаються в налаштований час і не повторюються.

    python manage.py test dashboard --settings=tabele.settings_test
"""
import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from config.models import NotificationSettings
from core.shared_cache import cache

# Пт, 30.10.2026 (weekday 4), за часом Берліна
FRIDAY = datetime.date(2026, 10, 30)


def at(h, m=0, day=30):
    return timezone.make_aware(datetime.datetime(2026, 10, day, h, m))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        ns = NotificationSettings.get()
        ns.email_enabled = True
        ns.email_to = "team@firma.example"
        ns.telegram_enabled = False
        ns.digest_email = True
        ns.digest_telegram = False
        ns.save()
        self.ns = ns

    def cmd(self, now, *args):
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=now), \
                mock.patch("dashboard.notifications._send_event_email") as send:
            call_command(*args, stdout=out)
        return send, out.getvalue()


class DigestScheduleTests(Base):
    def setUp(self):
        super().setUp()
        ns = self.ns
        ns.digest_enabled = True
        ns.digest_send_time = datetime.time(8, 0)
        ns.weekly_digest_enabled = True
        ns.weekly_digest_day = FRIDAY.weekday()
        ns.weekly_digest_time = datetime.time(9, 0)
        ns.monthly_digest_enabled = True
        ns.monthly_digest_day = FRIDAY.day
        ns.monthly_digest_time = datetime.time(10, 0)
        ns.save()

    def subjects(self, send):
        return [c.args[1].split(" · ")[0] for c in send.call_args_list]

    def test_each_digest_once_at_its_time(self):
        send, _ = self.cmd(at(7, 59), "send_digest", "--period", "all", "--verbosity", "0")
        send.assert_not_called()
        send, _ = self.cmd(at(8, 0), "send_digest", "--period", "all", "--verbosity", "0")
        self.assertEqual(self.subjects(send), ["📊 Minerva — Щоденний звіт"])
        send, _ = self.cmd(at(9, 5), "send_digest", "--period", "all", "--verbosity", "0")
        self.assertEqual(self.subjects(send), ["📊 Minerva — Тижневий звіт"])
        send, _ = self.cmd(at(10, 1), "send_digest", "--period", "all", "--verbosity", "0")
        self.assertEqual(self.subjects(send), ["📊 Minerva — Місячний звіт"])
        for h in (10, 14, 23):                        # cron щохвилини до кінця дня — повторів немає
            send, out = self.cmd(at(h, 30), "send_digest", "--period", "all", "--verbosity", "0")
            send.assert_not_called()
            self.assertEqual(out, "")                  # тихо: у журнал лише надіслане й помилки

    def test_weekly_and_monthly_wait_for_their_day(self):
        send, _ = self.cmd(at(9, 30, day=29), "send_digest", "--period", "weekly")
        send.assert_not_called()
        send, _ = self.cmd(at(10, 30, day=31), "send_digest", "--period", "monthly")
        send.assert_not_called()

    def test_broken_schedule_check_does_not_send(self):
        ns = NotificationSettings.get()
        with mock.patch("config.models.NotificationSettings.get", return_value=ns), \
                mock.patch.object(type(ns), "weekly_digest_time", new_callable=mock.PropertyMock,
                                  side_effect=ValueError("bad time"), create=True):
            send, _ = self.cmd(at(9, 30), "send_digest", "--period", "weekly")
        send.assert_not_called()

    def test_failed_send_pauses_retries(self):
        with mock.patch("django.utils.timezone.now", return_value=at(8, 1)), \
                mock.patch("dashboard.notifications._send_event_email", side_effect=OSError("SMTP down")) as send:
            out = StringIO()
            call_command("send_digest", "--period", "daily", "--verbosity", "0", stdout=out)
            self.assertEqual(send.call_count, 1)
            self.assertIn("SMTP down", out.getvalue())
            call_command("send_digest", "--period", "daily", "--verbosity", "0", stdout=StringIO())
            self.assertEqual(send.call_count, 1)        # одразу — не повторює
        self.assertIsNone(NotificationSettings.get().digest_last_sent)
        with mock.patch("time.time", return_value=__import__("time").time() + 31 * 60):
            send, _ = self.cmd(at(8, 40), "send_digest", "--period", "daily", "--verbosity", "0")
        send.assert_called_once()                       # через 30 хв — ще одна спроба, успішна

    def test_force_sends_even_if_already_sent(self):
        self.cmd(at(8, 0), "send_digest")
        send, _ = self.cmd(at(8, 5), "send_digest", "--force")
        send.assert_called_once()


class AlertScheduleTests(Base):
    def setUp(self):
        super().setUp()
        self.ns.stock_alerts_enabled = True
        self.ns.deadline_alerts_enabled = False
        self.ns.alert_min_interval_hours = 12
        self.ns.save()
        p = mock.patch("dashboard.notifications._get_critical_stock", return_value=[{"sku": "FT1"}])
        p.start()
        self.addCleanup(p.stop)

    def alerts(self, now, side_effect=None):
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=now), \
                mock.patch("dashboard.notifications._send_email", side_effect=side_effect) as send:
            call_command("send_alerts", "--if-due", stdout=out)
        return send, out.getvalue()

    def test_sent_once_per_interval(self):
        send, out = self.alerts(at(8))
        send.assert_called_once()
        self.assertIn("Alert sent", out)
        for h in (8, 12, 19):
            send, out = self.alerts(at(h, 15))
            send.assert_not_called()
            self.assertEqual(out, "")
        send, _ = self.alerts(at(20, 1))             # минуло 12 год
        send.assert_called_once()

    def test_zero_interval_means_one_hour(self):
        self.ns.alert_min_interval_hours = 0
        self.ns.save()
        self.alerts(at(8))
        send, _ = self.alerts(at(8, 15))
        send.assert_not_called()
        send, _ = self.alerts(at(9, 1))
        send.assert_called_once()

    def test_nothing_to_report_is_quiet(self):
        with mock.patch("dashboard.notifications._get_critical_stock", return_value=[]):
            send, out = self.alerts(at(8))
        send.assert_not_called()
        self.assertEqual(out, "")

    def test_failure_pauses_and_stops_after_three(self):
        send, out = self.alerts(at(8), side_effect=OSError("SMTP down"))
        self.assertIn("SMTP down", out)
        send, _ = self.alerts(at(8, 15), side_effect=OSError("SMTP down"))
        send.assert_not_called()                     # пауза 30 хв
        import time as _time
        base = _time.time()
        for i in (1, 2):
            with mock.patch("time.time", return_value=base + i * 31 * 60):
                send, _ = self.alerts(at(9, i), side_effect=OSError("SMTP down"))
            send.assert_called_once()
        with mock.patch("time.time", return_value=base + 4 * 31 * 60):
            send, _ = self.alerts(at(11), side_effect=OSError("SMTP down"))
        send.assert_not_called()                     # 3 невдачі за день — до завтра
