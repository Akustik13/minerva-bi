"""📅 Сторінка дня робочого часу: python manage.py test worktime --settings=tabele.settings_test (askDANTE імітується)."""
import datetime
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog
from worktime import services
from worktime.models import DanteProject, DanteSettings, TimeRecord
from worktime.tests import MON, Base, at
from worktime.tests_record_ai import rec


class DayPageTests(Base):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        self.dp = DanteProject.objects.create(dante_id="77", name="LTE/BT-Transponder")
        self.fake.records = [rec("r1", "10:00", "13:00")]
        self.url = reverse("admin:worktime_workday_day")
        for h, m in ((7, 52), (12, 0), (16, 41)):
            a = AuditLog.objects.create(user=self.user, action="update", object_repr="Замовлення 101121975")
            AuditLog.objects.filter(pk=a.pk).update(timestamp=at(MON, h, m))

    def get(self, day=MON):
        with mock.patch("worktime.services.local_today", return_value=MON + datetime.timedelta(days=1)):
            return self.client.get(self.url, {"u": self.du.pk, "d": day.isoformat()})

    def test_everything_of_the_day(self):
        self.fake.times[MON] = [{"coming": "08:00", "leaving": "17:30", "source": "MANUAL", "minutes": 570}]
        page = self.get()
        self.assertContains(page, "08:00–17:30")                       # прихід / відхід з askDANTE
        r1 = TimeRecord.objects.get(dante_id="r1")
        self.assertContains(page, f'data-rec="{r1.pk}"')               # запис на проєкт — у вікно ✍️
        self.assertContains(page, "Messungen")
        self.assertContains(page, "Замовлення 101121975")              # 👣 що робили
        self.assertContains(page, 'id="wt-rm"')                        # спільне вікно запису
        self.assertContains(page, f'name="next" value="{self.url}?u={self.du.pk}&amp;d={MON}"')   # вікно повертає сюди

    def test_footprints_proposal_and_booking_attendance(self):
        self.st.round_minutes = 15
        self.st.min_activity_minutes = 30
        self.st.save()
        page = self.get()
        self.assertContains(page, "З активності в Minerva")
        self.assertContains(page, "07:45–16:45")                       # округлено за налаштуваннями
        with mock.patch("worktime.services.local_today", return_value=MON + datetime.timedelta(days=1)):
            r = self.client.post(self.url, {"u": self.du.pk, "d": MON.isoformat(), "start": "07:45", "stop": "16:45"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.fake.calls[-1]["start"], self.fake.calls[-1]["stop"]), ("07:45", "16:45"))

    def test_calendar_date_links_to_day(self):
        page = self.client.get(reverse("admin:worktime_workday_changelist"), {"u": self.du.pk, "m": f"{MON:%Y-%m}"})
        self.assertContains(page, f'href="{self.url}?u={self.du.pk}&d={MON}"')

    def test_record_save_returns_to_day(self):
        nxt = f"{self.url}?u={self.du.pk}&d={MON}"
        with mock.patch("worktime.services.local_today", return_value=MON + datetime.timedelta(days=1)):
            r = self.client.post(reverse("admin:worktime_workday_record_save"),
                                 {"u": self.du.pk, "mode": "new", "date": MON.isoformat(), "project": self.dp.pk,
                                  "start": "14:00", "stop": "15:00", "comment": "Bericht", "next": nxt})
        self.assertEqual(r["Location"], nxt)
        r = self.client.post(reverse("admin:worktime_workday_record_save"),
                             {"u": self.du.pk, "mode": "new", "date": MON.isoformat(), "project": self.dp.pk,
                              "start": "15:00", "stop": "15:30", "next": "https://evil.example/"})
        self.assertNotIn("evil", r["Location"])                        # лише сторінка дня Minerva

    def test_activity_log_includes_sent_mail(self):
        from email_assistant.models import EmailAccount, EmailMessage
        acc = EmailAccount.objects.create(user=self.user, email_address="anna@example.com", imap_username="a",
                                          imap_password="x", smtp_username="a", smtp_password="x")
        EmailMessage.objects.create(account=acc, imap_uid=1, folder="sent", subject="Re: Messwerte",
                                    from_email="anna@example.com", to_emails=["max@acme.example"],
                                    sent_at=at(MON, 9, 15))
        log = services.activity_log(self.user, MON)
        self.assertEqual([x["icon"] for x in log], ["🖊", "📤", "🖊", "🖊"])
        self.assertEqual(log[1]["text"], "Re: Messwerte")
