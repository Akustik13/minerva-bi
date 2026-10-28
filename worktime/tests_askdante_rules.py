"""askDANTE без тривалості й з обов'язковим видом роботи: python manage.py test worktime --settings=tabele.settings_test"""
import datetime

from worktime import services
from worktime.client import DanteError
from worktime.models import Booking, DanteActivity, DanteProject, DanteSettings, TimeRecord, WorkDay
from worktime.tests import MON, Base, FakeClient


class StrictDante(FakeClient):
    """Як askDANTE фірми: «Die Verwendung einer Dauer ist nicht erlaubt» і «activityId: Eine Eingabe ist erforderlich»."""

    def track(self, dante_id, record):
        fields = {}
        if "duration" in record:
            fields["duration"] = "Die Verwendung einer Dauer ist nicht erlaubt."
        if "projectData" in record and not record["projectData"].get("activityId"):
            fields["projectData.activityId"] = "Eine Eingabe ist erforderlich."
        self.tried = getattr(self, "tried", []) + [record]
        if fields:
            raise DanteError("bad_request", "askDANTE не прийняв запит: " + "; ".join(f"{k}: {v}" for k, v in
                                                                                    fields.items()), 400, fields)
        return super().track(dante_id, record)


class RulesTests(Base):
    def setUp(self):
        super().setUp()
        self.fake.__class__ = StrictDante   # той самий об'єкт, що підставлено в services.client
        self.dp = DanteProject.objects.create(dante_id="77", name="DigiKey Marketplace")
        WorkDay.objects.create(dante_user=self.du, date=MON, times=[{"coming": "08:00", "leaving": "16:30"}])

    def test_first_refusal_teaches_and_retries_once(self):
        DanteActivity.objects.create(dante_id="190", name="Kommunikation")
        b = services.book_project(self.du, MON, self.dp, minutes=30, prefer=datetime.time(9, 12))
        self.assertEqual((b.status, b.start, b.stop, b.activity.name),
                         (Booking.SENT, datetime.time(9, 10), datetime.time(9, 40), "Kommunikation"))
        self.assertEqual(len(self.fake.tried), 2)   # перша спроба — тривалістю, друга — «з … по …»
        self.assertEqual({k: v for k, v in self.fake.tried[1].items() if k != "projectData"},
                         {"date": MON.isoformat(), "start": "09:10", "stop": "09:40"})
        st = DanteSettings.get()
        self.assertEqual((st.durations_allowed, st.activity_required), (False, True))
        self.fake.tried = []
        services.book_project(self.du, MON, self.dp, minutes=15, comment="ще")
        self.assertEqual(len(self.fake.tried), 1)    # далі — одразу «з … по …», без зайвої відмови
        self.assertNotIn("duration", self.fake.tried[0])

    def test_activity_required_without_choice_explains(self):
        DanteActivity.objects.create(dante_id="190", name="Kommunikation")
        DanteActivity.objects.create(dante_id="191", name="Entwicklung")
        b = services.book_project(self.du, MON, self.dp, minutes=30)
        self.assertEqual(b.status, Booking.FAILED)
        self.assertIn("Вид роботи за замовчуванням", b.error)
        before = len(self.fake.tried)
        with self.assertRaises(DanteError):   # уже відомо — без запиту в askDANTE
            services.book_project(self.du, MON, self.dp, minutes=30, comment="2")
        self.assertEqual(len(self.fake.tried), before)
        st = DanteSettings.get()
        st.default_activity = DanteActivity.objects.get(name="Entwicklung")
        st.save()
        b = services.book_project(self.du, MON, self.dp, minutes=30, comment="3")
        self.assertEqual((b.status, b.activity.name), (Booking.SENT, "Entwicklung"))

    def test_find_slot_inside_attendance_and_free(self):
        TimeRecord.objects.create(dante_id="t1", dante_user=self.du, day=MON, start="09:00", stop="10:00",
                                  project_name="Intern")
        slot = services.find_slot(self.du, MON, 30, datetime.time(9, 12))
        self.assertEqual(slot, (datetime.time(10, 0), datetime.time(10, 30)))   # після запису, що перекривається
        self.assertEqual(services.find_slot(self.du, MON, 30), (datetime.time(8, 0), datetime.time(8, 30)))
        self.assertEqual(services.find_slot(self.du, MON, 60, datetime.time(16, 0)),
                         (datetime.time(8, 0), datetime.time(9, 0)))   # після 16:00 не влізе — з початку дня
        TimeRecord.objects.create(dante_id="t2", dante_user=self.du, day=MON, start="08:00", stop="16:30")
        with self.assertRaises(DanteError):
            services.find_slot(self.du, MON, 30)

    def test_mail_time_prefers_first_mail(self):
        from email_assistant.models import EmailAccount, EmailMessage, MailCategory
        from worktime import mail_time
        from worktime.models import MailCategoryTime
        DanteActivity.objects.create(dante_id="190", name="Kommunikation")
        acc = EmailAccount.objects.create(user=self.user, email_address="anna@example.com", imap_username="a",
                                          imap_password="x", smtp_username="a", smtp_password="x")
        cat = MailCategory.objects.create(name="DigiKey", senders="digikey")
        MailCategoryTime.objects.create(category=cat, dante_project=self.dp, reply_minutes=10, read_minutes=3)
        from django.utils import timezone
        EmailMessage.objects.create(account=acc, imap_uid=1, folder="sent", subject="Re: order",
                                    from_email="anna@example.com", to_emails=["orders@digikey.com"], is_read=True,
                                    work_category=cat,
                                    sent_at=timezone.make_aware(datetime.datetime.combine(MON, datetime.time(11, 7))))
        b = mail_time.book(self.du, MON, self.dp.pk)
        self.assertEqual((b.status, b.start, b.stop), (Booking.SENT, datetime.time(11, 5), datetime.time(11, 20)))


class NeedsWorktime(FakeClient):
    """askDANTE фірми: час на проєкт — лише в день з приходом / відходом («Es wurden keine Arbeitszeiten erfasst»)."""

    def track(self, dante_id, record):
        d = datetime.date.fromisoformat(record["date"])
        self.tried = getattr(self, "tried", []) + [record]
        if "projectData" in record and not self.times.get(d):
            raise DanteError("bad_request", "askDANTE не прийняв запит: start: Es wurden keine Arbeitszeiten erfasst.",
                             400, {"start": "Es wurden keine Arbeitszeiten erfasst."})
        return super().track(dante_id, record)


class WorktimeRequiredTests(Base):
    """Неділя без приходу / відходу: перша відмова вчить, далі — пояснення без запиту, дні не позначено."""

    def setUp(self):
        super().setUp()
        self.fake.__class__ = NeedsWorktime
        self.dp = DanteProject.objects.create(dante_id="77", name="DigiKey Marketplace")
        self.sun = MON - datetime.timedelta(days=1)
        self.fake.times[MON] = [{"coming": "08:00", "leaving": "16:30", "minutes": 510}]

    def test_first_refusal_teaches_then_precheck(self):
        b = services.book_project(self.du, self.sun, self.dp, start=datetime.time(10, 0), stop=datetime.time(10, 30))
        self.assertEqual(b.status, Booking.FAILED)
        self.assertIn("немає робочого часу", b.error)
        self.assertIn("прихід і відхід", b.error)
        self.assertTrue(DanteSettings.get().attendance_required)
        self.assertEqual(len(self.fake.tried), 1)   # повтор не допоможе — без другої спроби
        with self.assertRaises(DanteError) as e:     # далі — без запиту в askDANTE
            services.book_project(self.du, self.sun, self.dp, start=datetime.time(11, 0), stop=datetime.time(11, 30))
        self.assertIn(f"{self.sun:%d.%m}", e.exception.message)
        self.assertEqual(len(self.fake.tried), 1)
        ok = services.book_project(self.du, MON, self.dp, start=datetime.time(9, 0), stop=datetime.time(9, 30))
        self.assertEqual(ok.status, Booking.SENT)   # робочий день — як завжди

    def test_has_worktime(self):
        self.assertIsNone(services.has_worktime(self.du, self.sun))   # ще не читали
        services.refresh_days(self.du, self.sun, MON, self.fake)
        self.assertFalse(services.has_worktime(self.du, self.sun))
        self.assertTrue(services.has_worktime(self.du, MON))

    def test_attendance_first_when_chosen(self):
        st = DanteSettings.get()
        st.attendance_required = True
        st.save()
        tue = MON + datetime.timedelta(days=1)
        b = services.book_project(self.du, tue, self.dp, start=datetime.time(10, 0), stop=datetime.time(10, 30),
                                  attendance=DanteSettings.MODE_FIXED)
        self.assertEqual(b.status, Booking.SENT)
        kinds = ["project" if "projectData" in r else "attendance" for r in self.fake.calls if r["date"] == tue.isoformat()]
        self.assertEqual(kinds, ["attendance", "project"])   # спершу прихід / відхід, потім час на проєкт
        self.assertTrue(services.has_worktime(self.du, tue))
        with self.assertRaises(DanteError) as e:   # неділя — вихідний: прихід автоматично не ставиться
            services.book_project(self.du, self.sun, self.dp, start=datetime.time(10, 0), stop=datetime.time(10, 30),
                                  attendance=DanteSettings.MODE_FIXED)
        self.assertIn("не записано", e.exception.message)
