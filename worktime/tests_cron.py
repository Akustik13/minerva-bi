"""Щоденний запуск через контейнер cron: python manage.py test worktime --settings=tabele.settings_test"""
import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from worktime import services
from worktime.models import DanteSettings
from worktime.tests import Base


class CronTests(Base):
    def setUp(self):
        super().setUp()
        self.st.autofill_mode, self.st.autofill_hour = DanteSettings.MODE_FIXED, 7
        self.st.save()

    def at(self, h, day=28):
        return timezone.make_aware(datetime.datetime(2026, 10, day, h, 0))

    def test_no_cron_yet_warns(self):
        plan = services.autofill_schedule(self.st, self.at(9))
        self.assertIn("ще жодного разу", plan["warning"])
        self.assertIn("docker-compose restart cron", plan["warning"])

    def test_cron_call_marks_alive(self):
        with mock.patch("worktime.services.autofill_due", return_value=False), \
                mock.patch("worktime.services.autofill") as run:
            call_command("worktime_autofill", "--if-due", stdout=StringIO())
        run.assert_not_called()   # не час — не запускає, але відмітку «cron живий» ставить
        self.st.refresh_from_db()
        self.assertIsNotNone(self.st.cron_seen_at)
        plan = services.autofill_schedule(self.st, timezone.now())
        self.assertEqual(plan["warning"], "")

    def test_stale_cron_and_next_run_text(self):
        self.st.cron_seen_at = self.at(6)
        self.st.last_autofill_at = self.at(13, day=27)
        plan = services.autofill_schedule(self.st, self.at(9))
        self.assertIn("не звертався з 28.10.2026 06:00", plan["warning"])
        self.assertIn("зараз", plan["next"])
        self.st.cron_seen_at = self.at(8, day=28) + datetime.timedelta(minutes=55)
        self.assertEqual(services.autofill_schedule(self.st, self.at(9))["warning"], "")
        self.assertIn("сьогодні після 07:00", services.autofill_schedule(self.st, self.at(6))["next"])
        self.st.last_autofill_at = self.at(8)
        self.assertIn("завтра", services.autofill_schedule(self.st, self.at(9))["next"])

    def test_settings_page_shows_warning(self):
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        page = self.client.get(f"/admin/worktime/dantesettings/{self.st.pk}/change/")
        self.assertContains(page, "Наступний щоденний запуск")
        self.assertContains(page, "docker-compose restart cron")
