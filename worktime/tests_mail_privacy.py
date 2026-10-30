"""Пошта працівника — лише йому: адміністратор бачить у «Часі на пошту», на сторінці дня й в описі AI лише кількості й
хвилини; дії з листами й опис AI з листів — відмова. І подвійний запис тривалості в askDANTE.
python manage.py test worktime.tests_mail_privacy --settings=tabele.settings_test (askDANTE імітується)."""
import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse

from email_assistant.models import EmailMessage
from worktime import mail_time, services
from worktime.client import DanteError
from worktime.models import Booking, DanteProject, DanteSettings, MailTimeIgnore, WorkDay
from worktime.tests import MON, Base
from worktime.tests_mail_time import MailTimeBase, TUE
from worktime.tests_record_ai import RecordBase

AI = "email_assistant.ai_helper._call_ai_direct"


def login_boss(test):
    from config.models import SystemSettings
    SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
    boss = get_user_model().objects.create_superuser("boss", "boss@example.com", "x")
    test.client.force_login(boss)
    return boss


class MailTimePageTests(MailTimeBase):
    def setUp(self):
        super().setUp()
        self.loose = self.msg(cat=False, sender="info@hrpcba.example", subject="Geheime Materialliste")
        self.mail = self.msg(subject="Order 4711 vertraulich")
        self.url = reverse("admin:worktime_workday_mailtime")
        self.q = {"u": self.du.pk, "from": MON.isoformat(), "to": TUE.isoformat()}
        self.boss = login_boss(self)

    def test_manager_sees_counts_not_mail(self):
        page = self.client.get(self.url, {**self.q, "show": "all"})
        html = page.content.decode()
        for secret in ("Geheime Materialliste", "info@hrpcba.example", "Order 4711 vertraulich", "orders@digikey.com"):
            self.assertNotIn(secret, html)
        self.assertContains(page, "DigiKey Marketplace")                       # рядок і хвилини є
        self.assertContains(page, "Без категорії / проєкту askDANTE: 1")
        self.assertContains(page, "бачить і змінює лише він сам")
        self.assertNotContains(page, 'value="describe"')
        self.assertContains(page, 'value="E-Mails DigiKey (1 E-Mail)"')        # опис для запису — лише кількості

    def test_manager_cannot_act_on_mail(self):
        self.client.post(self.url, {**self.q, "show": "all", "action": "ignore", "mail": [self.loose.pk],
                                    "ignore_scope": "message"})
        self.assertFalse(MailTimeIgnore.objects.exists())
        self.client.post(self.url, {**self.q, "show": "all", "action": "assign", "mail": [self.loose.pk],
                                    "wcat": "new", "wcat_name": "Spy", "scope": "sender"})
        self.loose.refresh_from_db()
        self.assertIsNone(self.loose.work_category_id)
        with mock.patch(AI, return_value="Geheim") as ai:
            self.client.post(self.url, {**self.q, "action": "describe"})
        ai.assert_not_called()

    def test_manager_booking_uses_counts_only(self):
        self.st.comment_ai = True
        self.st.save()
        with mock.patch(AI, return_value="Geheime Zusammenfassung") as ai:
            b = mail_time.book(self.du, MON, self.dp.pk, user=self.boss, st=self.st)
        ai.assert_not_called()
        self.assertEqual(b.status, Booking.SENT)
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"], "E-Mails DigiKey (1 E-Mail)")

    def test_owner_still_sees_everything(self):
        self.client.force_login(self.user)
        self.user.is_superuser = True
        self.user.save()
        page = self.client.get(self.url, {**self.q, "show": "all"})
        self.assertContains(page, "Geheime Materialliste")
        self.assertContains(page, 'value="describe"')


class DescribeTests(RecordBase):
    def setUp(self):
        super().setUp()
        self.boss = login_boss(self)
        self.url = reverse("admin:worktime_workday_record_describe")

    def test_manager_gets_no_mail_in_ai(self):
        with mock.patch(AI, return_value="Messungen") as ai:
            d = self.client.post(self.url, {"u": self.du.pk, "date": MON.isoformat(), "project": self.dp.pk}).json()
        ai.assert_not_called()
        self.assertFalse(d["ok"])
        self.assertIn("лише він сам", d["error"])
        with mock.patch(AI, return_value="Messungen am Board") as ai:
            d = self.client.post(self.url, {"u": self.du.pk, "date": MON.isoformat(), "project": self.dp.pk,
                                            "hint": "Board gemessen"}).json()
        self.assertTrue(d["ok"])
        prompt = ai.call_args.args[0]
        self.assertIn("Board gemessen", prompt)
        self.assertNotIn("S11-Messung", prompt)                               # текст листа працівника — ні
        self.assertNotIn("Messwerte LTE-Board", prompt)

    def test_owner_material_has_mail(self):
        from worktime import record_ai
        data = record_ai.material(self.du, MON, self.dp)
        self.assertEqual(len(data), 1)
        self.assertIn("S11-Messung", data[0])
        self.assertEqual(record_ai.material(self.du, MON, self.dp, with_mail=False), [])


class DayPageTests(Base):
    def test_activity_log_hides_subjects_for_others(self):
        from email_assistant.models import EmailAccount
        acc = EmailAccount.objects.create(user=self.user, email_address="anna@example.com", imap_username="a",
                                          imap_password="x", smtp_username="a", smtp_password="x")
        EmailMessage.objects.create(account=acc, imap_uid=1, folder="sent", subject="Re: Kündigung privat",
                                    from_email="anna@example.com", to_emails=["max@acme.example"],
                                    sent_at=datetime.datetime(2026, 9, 7, 9, 15, tzinfo=datetime.timezone.utc))
        self.assertEqual(services.activity_log(self.user, MON)[0]["text"], "Re: Kündigung privat")
        boss = login_boss(self)
        log = services.activity_log(self.user, MON, viewer=boss)
        self.assertEqual(log[0]["text"], "надісланий лист")
        with mock.patch("worktime.services.local_today", return_value=MON + datetime.timedelta(days=1)):
            page = self.client.get(reverse("admin:worktime_workday_day"), {"u": self.du.pk, "d": MON.isoformat()})
        self.assertNotContains(page, "Kündigung")


# ── 13. Подвійний запис тривалості ───────────────────────────────────────────
class DuplicateDurationTests(Base):
    def setUp(self):
        super().setUp()
        self.dp = DanteProject.objects.create(dante_id="77", name="DigiKey Marketplace")
        WorkDay.objects.create(dante_user=self.du, date=MON, times=[{"coming": "08:00", "leaving": "16:30"}])

    def test_duration_written_as_slot_is_not_written_twice(self):
        DanteSettings.objects.filter(pk=self.st.pk).update(durations_allowed=False)   # askDANTE без тривалостей
        st = DanteSettings.get()
        b = services.book_project(self.du, MON, self.dp, minutes=30, comment="Mails", st=st)
        self.assertEqual(b.status, Booking.SENT)
        self.assertIsNotNone(b.start)                                          # записано як «з … по …»
        with self.assertRaises(DanteError) as err:
            services.book_project(self.du, MON, self.dp, minutes=30, comment="Mails", st=st)
        self.assertIn("щойно вже пішов", err.exception.message)
        self.assertEqual(len([c for c in self.fake.calls if "projectData" in c]), 1)

    def test_plain_duration_and_other_comment(self):
        services.book_project(self.du, MON, self.dp, minutes=30, comment="A")
        with self.assertRaises(DanteError):
            services.book_project(self.du, MON, self.dp, minutes=30, comment="A")
        services.book_project(self.du, MON, self.dp, minutes=30, comment="B")   # інший опис — це інший запис
        self.assertEqual(len([c for c in self.fake.calls if "projectData" in c]), 2)
