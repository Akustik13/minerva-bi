"""«Робочий день сам» і нагадування про відмітку askDANTE без «Gehen»:
python manage.py test worktime.tests_auto_day --settings=tabele.settings_test"""
import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from worktime import services
from worktime.models import Booking, DanteProject, DanteSettings, DanteUser
from worktime.tests import MON, Base, at

T = datetime.time


@override_settings(WORKTIME_MAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AutoDayTests(Base):
    def setUp(self):
        super().setUp()
        DanteUser.objects.filter(pk=self.du.pk).update(autofill=True)
        self.du.refresh_from_db()
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_host, ns.email_from = "smtp.example.com", "minerva@example.com"
        ns.save()

    def at_time(self, day, h, m=0):
        now = at(day, h, m)
        return mock.patch("django.utils.timezone.now", return_value=now), \
            mock.patch("worktime.services.local_today", return_value=day)

    def cron(self, day, h, m=0):
        """Один цикл cron (worktime_autofill --if-due): відкрити → «Пішов» автоматично → кінець дня."""
        a, b = self.at_time(day, h, m)
        with a, b:
            out = services.auto_day(), services.auto_clock_out(), services.day_end()
        self.du.refresh_from_db()
        return out

    def test_empty_workday_opens_at_arrival_and_closes_at_departure(self):
        self.cron(MON, 7, 30)
        self.assertIsNone(self.du.open_since)                                  # ще до стандартного приходу
        self.cron(MON, 8, 3)
        self.assertEqual((self.du.open_since, self.du.day_note), (at(MON, 8, 0), services.DAY_OPENED))
        self.assertEqual(self.cron(MON, 8, 4)[0], [])                           # раз на день, без читань щохвилини
        self.assertEqual(self.fake.calls, [])                                   # в askDANTE ще нічого
        _, closed, _ = self.cron(MON, 16, 31)
        self.assertEqual([(b.start, b.stop, b.source, b.status) for b in closed],
                         [(T(8, 0), T(16, 30), Booking.AUTO, Booking.SENT)])
        self.assertEqual(self.fake.calls, [{"date": MON.isoformat(), "start": "08:00", "stop": "16:30"}])
        self.assertIsNone(self.du.open_since)
        self.assertEqual(len(mail.outbox), 0)                                   # усе закрито — нагадувати нема про що

    def test_manual_entries_weekend_and_absence_are_left_alone(self):
        sat = MON + datetime.timedelta(days=5)
        self.cron(sat, 9, 0)
        self.assertEqual((self.du.open_since, self.du.day_note), (None, "вихідний"))
        tue = MON + datetime.timedelta(days=1)
        self.fake.att[tue] = {"planned": "BUSINESS_DAY", "absences": [{"type": "VACATION", "ratio": 1}]}
        self.cron(tue, 8, 5)
        self.assertIsNone(self.du.open_since)
        self.assertNotEqual(self.du.day_note, services.DAY_OPENED)
        self.fake.times[MON] = [{"coming": "07:55", "leaving": None, "source": "APP", "minutes": 0}]   # «Kommen»
        self.cron(MON, 8, 5)
        self.assertIsNone(self.du.open_since)
        self.assertIn(services.DAY_MANUAL, self.du.day_note)
        self.cron(MON, 16, 31)
        self.assertEqual(self.fake.calls, [])                                   # Minerva нічого не пише
        self.assertEqual(len(mail.outbox), 1)                                   # «Gehen» забули — нагадування
        self.assertIn("07.09.2026 з 07:55", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["anna@example.com"])
        self.assertIn("лише готові записи", mail.outbox[0].body)
        self.cron(MON, 18, 0)
        self.cron(tue, 17, 0)                                                   # наступного дня — про вчорашню не вдруге
        self.assertEqual(len(mail.outbox), 1)

    def test_manual_attendance_during_auto_day_wins_quietly(self):
        self.cron(MON, 8, 3)
        self.fake.times[MON] = [{"coming": "09:00", "leaving": "12:00", "source": "WEB", "minutes": 180}]
        _, closed, _ = self.cron(MON, 16, 31)
        self.assertEqual(closed, [])
        self.assertFalse(Booking.objects.filter(status=Booking.FAILED).exists())   # не помилка — людина внесла сама
        self.assertIsNone(self.du.open_since)
        self.assertTrue(self.du.day_note.startswith(services.DAY_MANUAL))
        self.assertEqual(self.fake.calls, [])

    def test_project_time_on_open_day_writes_attendance_first(self):
        DanteSettings.objects.update(attendance_required=True)
        dp = DanteProject.objects.create(dante_id="77", name="DigiKey")
        self.cron(MON, 8, 3)
        a, b = self.at_time(MON, 11, 0)
        with a, b:
            booking = services.book_project(self.du, MON, dp, start=T(9, 0), stop=T(10, 0))
        self.assertEqual(booking.status, Booking.SENT)
        self.assertEqual(self.fake.calls[0], {"date": MON.isoformat(), "start": "08:00", "stop": "11:00"})
        _, closed, _ = self.cron(MON, 16, 31)
        self.assertEqual([(b.start, b.stop) for b in closed], [(T(11, 0), T(16, 30))])   # решта дня

    def test_off_switches_and_calendar_page(self):
        DanteUser.objects.filter(pk=self.du.pk).update(autofill=False)
        self.cron(MON, 8, 3)
        self.assertIsNone(self.du.open_since)
        st = DanteSettings.get()
        self.assertIn("Автозаповнення", services.auto_day_state(st, self.du)["why_off"])
        DanteUser.objects.filter(pk=self.du.pk).update(autofill=True)
        DanteSettings.objects.update(auto_day=False)
        self.cron(MON, 8, 3)
        self.assertIsNone(self.du.open_since)
        DanteSettings.objects.update(auto_day=True)
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(get_user_model().objects.create_superuser("boss", "boss@example.com", "x"))
        self.fake.times[MON] = [{"coming": "07:55", "leaving": None, "source": "APP", "minutes": 0}]
        r = self.client.get(reverse("admin:worktime_workday_changelist"), {"u": self.du.pk, "m": "2026-09"})
        self.assertContains(r, "Робочий день сам")
        self.assertContains(r, "відкривається о 08:00 і записується в askDANTE о 16:30")
        self.assertContains(r, "відкрита відмітка «Kommen» 07.09.2026 з 07:55 без «Gehen»")
        r = self.client.get(reverse("admin:worktime_workday_day"), {"u": self.du.pk, "d": MON.isoformat()})
        self.assertContains(r, "відкрита відмітка «Kommen» з 07:55 без «Gehen»")
