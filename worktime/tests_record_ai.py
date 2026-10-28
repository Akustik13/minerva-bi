"""✍️ Запис askDANTE з календаря: опис AI, заміна після видалення в askDANTE, новий запис.
python manage.py test worktime --settings=tabele.settings_test"""
import datetime
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from email_assistant.models import EmailAccount, EmailMessage, MailCategory
from worktime import record_ai
from worktime.client import DanteError
from worktime.models import Booking, DanteActivity, DanteProject, MailCategoryTime, TimeRecord
from worktime.tests import MON, Base


def rec(rid, start, stop, desc="Messungen", project="77"):
    return {"id": rid, "person_id": "1108", "day": MON, "duration": 180, "start": start, "stop": stop,
            "description": desc, "project_id": project, "project_name": "LTE/BT-Transponder", "item_id": "",
            "item_name": "", "activity_id": "", "activity_name": "Entwicklung", "hourly_rate": None, "cost_center": ""}


class RecordBase(Base):
    def setUp(self):
        super().setUp()
        self.dp = DanteProject.objects.create(dante_id="77", name="LTE/BT-Transponder")
        DanteActivity.objects.create(dante_id="190", name="Entwicklung")
        acc = EmailAccount.objects.create(user=self.user, email_address="anna@example.com", imap_username="a",
                                          imap_password="x", smtp_username="a", smtp_password="x")
        self.cat = MailCategory.objects.create(name="Transponder", senders="@acme.example")
        MailCategoryTime.objects.create(category=self.cat, dante_project=self.dp)
        at = timezone.make_aware(datetime.datetime.combine(MON, datetime.time(9, 30)))
        EmailMessage.objects.create(account=acc, imap_uid=1, subject="Messwerte LTE-Board",
                                    from_email="max@acme.example", to_emails=["anna@example.com"], is_read=True,
                                    body_text="Die S11-Messung zeigt -12 dB bei 868 MHz.\n\n> alte Mail", sent_at=at,
                                    work_category=self.cat)
        EmailMessage.objects.create(account=acc, imap_uid=2, subject="Urlaub", from_email="hr@other.example",
                                    to_emails=["anna@example.com"], is_read=True, body_text="privat", sent_at=at)
        self.fake.records = [rec("r1", "08:00", "11:00")]
        from worktime import services
        services.sync_records(self.st, MON, MON)


class MaterialTests(RecordBase):
    def test_only_project_mails_without_quotes(self):
        data = record_ai.material(self.du, MON, self.dp)
        self.assertEqual(len(data), 1)
        self.assertIn("S11-Messung", data[0])
        self.assertNotIn("alte Mail", data[0])

    def test_describe_uses_data_hint_and_language(self):
        with mock.patch("email_assistant.ai_helper._call_ai_direct",
                        return_value="Messungen LTE-Board\n- S11 bei 868 MHz gemessen (-12 dB) 🟥\n\n- Ergebnis an Kunden") as ai:
            r = record_ai.describe(self.du, MON, self.dp, datetime.time(8), datetime.time(11), "Messungen",
                                   "Antenne abgeglichen")
        prompt = ai.call_args.args[0]
        self.assertIn("German", prompt)
        self.assertIn("Antenne abgeglichen", prompt)
        self.assertIn("S11-Messung", prompt)
        self.assertEqual(ai.call_args.kwargs["model"], record_ai.AI_MODEL)
        self.assertEqual(r["text"], "Messungen LTE-Board\n- S11 bei 868 MHz gemessen (-12 dB)\n- Ergebnis an Kunden")
        with mock.patch("email_assistant.ai_helper._call_ai_direct", return_value=""):
            with self.assertRaises(DanteError):
                record_ai.describe(self.du, MON, self.dp)


class ReplaceTests(RecordBase):
    def test_refuses_while_old_record_exists_then_writes(self):
        old = TimeRecord.objects.get(dante_id="r1")
        with self.assertRaises(DanteError) as err:
            record_ai.replace(self.du, old, self.dp, datetime.time(8), datetime.time(11), 0, None, "Neu")
        self.assertIn("ще є в askDANTE", err.exception.message)
        self.assertEqual([c for c in self.fake.calls if "projectData" in c], [])
        self.fake.records = []   # видалили в askDANTE
        b = record_ai.replace(self.du, old, self.dp, datetime.time(8), datetime.time(11), 0, None,
                              "Messungen LTE-Board\n- S11 gemessen")
        self.assertEqual((b.status, b.start, b.stop), (Booking.SENT, datetime.time(8), datetime.time(11)))
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"], "Messungen LTE-Board\n- S11 gemessen")


class PageTests(RecordBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)

    def test_calendar_chip_opens_panel_and_endpoints(self):
        page = self.client.get(reverse("admin:worktime_workday_changelist"), {"u": self.du.pk, "m": f"{MON:%Y-%m}"})
        r1 = TimeRecord.objects.get(dante_id="r1")
        self.assertContains(page, f'data-rec="{r1.pk}"')
        self.assertContains(page, 'id="wt-rec-data"')
        self.assertContains(page, "Описати детально (AI)")
        with mock.patch("email_assistant.ai_helper._call_ai_direct", return_value="Messungen\n- S11"):
            d = self.client.post(reverse("admin:worktime_workday_record_describe"),
                                 {"u": self.du.pk, "date": MON.isoformat(), "project": self.dp.pk}).json()
        self.assertEqual((d["ok"], d["text"], d["sources"]), (True, "Messungen\n- S11", 1))
        save = reverse("admin:worktime_workday_record_save")
        r = self.client.post(save, {"u": self.du.pk, "mode": "replace", "rec": r1.pk, "project": self.dp.pk,
                                    "start": "08:00", "stop": "11:00", "comment": "Neu"})
        self.assertEqual([c for c in self.fake.calls if "projectData" in c], [])   # старий ще в askDANTE
        self.assertIn(f"rec={r1.pk}", r["Location"])   # вікно відкриється знову з чернеткою
        self.client.post(save, {"u": self.du.pk, "mode": "new", "date": MON.isoformat(), "project": self.dp.pk,
                                "start": "13:00", "stop": "14:00", "comment": "Bericht"})
        self.assertEqual(self.fake.calls[-1]["start"], "13:00")
        other = self.du.__class__.objects.create(dante_id="9", first_name="Bob")
        r = self.client.post(save, {"u": other.pk, "mode": "new", "date": MON.isoformat(), "project": self.dp.pk,
                                    "start": "15:00", "stop": "16:00"})
        self.assertEqual(r.status_code, 302)   # адміністратор може й за іншого — у users_for
