"""Зустрічі → askDANTE за період: python manage.py test worktime --settings=tabele.settings_test (askDANTE імітується)."""
import datetime

from django.urls import reverse
from django.utils import timezone

from calendar_app.models import CalendarEvent, CalendarFeed, ExternalEvent
from crm.models import Customer
from worktime import meetings
from worktime.models import Booking, DanteProject, ProjectMap
from worktime.tests import MON, Base, at


def rec(rid, day, start, stop, desc="Abstimmung", project="51"):
    return {"id": rid, "person_id": "1108", "day": day, "duration": 60, "start": start, "stop": stop,
            "description": desc, "project_id": project, "project_name": "ACME", "item_id": "", "item_name": "",
            "activity_id": "", "activity_name": "", "hourly_rate": None, "cost_center": ""}


class MeetingsBase(Base):
    def setUp(self):
        super().setUp()
        self.acme = Customer.objects.create(name="ACME GmbH", email="max@acme.example")
        self.dp = DanteProject.objects.create(dante_id="51", name="ACME Support", crm_customer=self.acme)
        self.other = DanteProject.objects.create(dante_id="52", name="Antennen-Entwicklung")

    def meeting(self, title, day=MON, h=10, minutes=60, **kw):
        return CalendarEvent.objects.create(user=self.user, title=title, event_type=CalendarEvent.TYPE_MEETING,
                                            start_at=at(day, h), end_at=at(day, h) + datetime.timedelta(minutes=minutes),
                                            **kw)

    def rows(self, a=MON, b=MON + datetime.timedelta(days=6)):
        return {r["title"]: r for r in meetings.plan(self.du, a, b)}


class PlanTests(MeetingsBase):
    def test_hints_states_and_dedup(self):
        self.meeting("ACME Abstimmung", crm_customer=self.acme)
        self.meeting("Weekly Antennen-Entwicklung", h=13, minutes=30)
        self.meeting("Klausur", h=8, minutes=14 * 60)
        self.meeting("Morgen", day=timezone.localdate() + datetime.timedelta(days=1))
        CalendarEvent.objects.create(user=self.user, title="Messe", event_type=CalendarEvent.TYPE_MEETING,
                                     start_at=at(MON, 0), all_day=True)
        feed = CalendarFeed.objects.create(user=self.user, name="Outlook", url="https://x.example/a.ics")
        ExternalEvent.objects.create(feed=feed, uid="u1", title="ACME Abstimmung", start_at=at(MON, 10),
                                     end_at=at(MON, 11))   # та сама зустріч в Outlook — один рядок
        ExternalEvent.objects.create(feed=feed, uid="u2", title="Zahnarzt", start_at=at(MON, 16), end_at=at(MON, 17))
        rows = self.rows(MON, MON)
        self.assertEqual(set(rows), {"ACME Abstimmung", "Weekly Antennen-Entwicklung", "Klausur", "Zahnarzt"})
        a = rows["ACME Abstimmung"]
        self.assertEqual((a["dp"], a["reason"], a["state"], a["checked"], a["where"]),
                         (self.dp, "клієнт CRM зустрічі", "ready", True, "Minerva"))
        self.assertEqual((a["start"], a["stop"], a["minutes"]), (datetime.time(10), datetime.time(11), 60))
        w = rows["Weekly Antennen-Entwicklung"]
        self.assertEqual((w["dp"], w["reason"]), (self.other, "назва проєкту askDANTE у назві зустрічі"))
        self.assertEqual(rows["Klausur"]["state"], "skip")
        z = rows["Zahnarzt"]
        self.assertEqual((z["dp"], z["checked"], z["where"]), (None, False, "Outlook"))   # без проєкту — без галочки

    def test_project_code_and_previous_booking(self):
        from projects.models import Project
        project = Project.objects.create(name="Gehäuse")
        ProjectMap.objects.create(project=project, dante_project=self.other)
        self.meeting(f"Review {project.code}")
        self.meeting("Daily", h=9, minutes=15)
        Booking.objects.create(dante_user=self.du, date=MON - datetime.timedelta(days=1), kind=Booking.PROJECT,
                               source=Booking.MEETING, duration=15, dante_project=self.dp, comment="Daily")
        rows = self.rows(MON, MON)
        self.assertEqual(rows[f"Review {project.code}"]["dp"], self.other)
        self.assertEqual((rows["Daily"]["dp"], rows["Daily"]["reason"]), (self.dp, "ця зустріч уже записувалась сюди"))

    def test_booked_and_overlap(self):
        from worktime.models import TimeRecord
        self.meeting("ACME Abstimmung", crm_customer=self.acme)
        self.meeting("Call", h=14)
        self.meeting("Kickoff", h=16)
        TimeRecord.objects.create(dante_id="t1", dante_user=self.du, day=MON, start="14:30", stop="15:30",
                                  project_name="Intern")
        TimeRecord.objects.create(dante_id="t2", dante_user=self.du, day=MON, start="", stop="", duration=60,
                                  description="Kickoff")
        Booking.objects.create(dante_user=self.du, date=MON, kind=Booking.PROJECT, source=Booking.MEETING,
                               start=datetime.time(10), stop=datetime.time(11), dante_project=self.dp)
        rows = self.rows(MON, MON)
        self.assertEqual(rows["ACME Abstimmung"]["state"], "booked")
        self.assertEqual(rows["Call"]["state"], "overlap")
        self.assertIn("14:30–15:30 Intern", rows["Call"]["note"])
        self.assertEqual(rows["Kickoff"]["state"], "booked")


class BookTests(MeetingsBase):
    def test_book_writes_from_to_and_refuses_not_ready(self):
        ok = self.meeting("ACME Abstimmung", crm_customer=self.acme)
        long = self.meeting("Klausur", h=8, minutes=14 * 60)
        r = meetings.book(self.du, {f"e{ok.pk}": self.dp.pk, f"e{long.pk}": self.dp.pk}, MON, MON, user=self.user)
        self.assertEqual((r["written"], r["minutes"]), (1, 60))
        self.assertIn("довше 12 годин", r["errors"][0])
        call = self.fake.calls[-1]
        self.assertEqual((call["date"], call["start"], call["stop"], call["projectData"]["projectId"],
                          call["projectData"]["comment"]), (MON.isoformat(), "10:00", "11:00", "51", "ACME Abstimmung"))
        b = Booking.objects.get(source=Booking.MEETING, status=Booking.SENT)
        self.assertEqual((b.start, b.stop), (datetime.time(10), datetime.time(11)))
        self.assertEqual(self.rows(MON, MON)["ACME Abstimmung"]["state"], "booked")
        again = meetings.book(self.du, {f"e{ok.pk}": self.dp.pk}, MON, MON)
        self.assertEqual(again["written"], 0)   # уже записано — повторно не пише

    def test_overlap_from_fresh_records_is_not_written(self):
        m = self.meeting("Call", h=14)
        self.fake.records = [rec("r1", MON, "14:30", "15:30", desc="Intern")]
        r = meetings.book(self.du, {f"e{m.pk}": self.dp.pk}, MON, MON)
        self.assertEqual(r["written"], 0)
        self.assertIn("перекривається", r["errors"][0])
        self.assertEqual(self.fake.calls, [])


class PageTests(MeetingsBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)

    def test_page_and_post(self):
        m = self.meeting("ACME Abstimmung", crm_customer=self.acme)
        url = reverse("admin:worktime_workday_meetings")
        q = {"u": self.du.pk, "from": MON.isoformat(), "to": MON.isoformat()}
        page = self.client.get(url, q)
        self.assertContains(page, "ACME Abstimmung")
        self.assertContains(page, f'name="p_e{m.pk}"')
        self.assertContains(page, "клієнт CRM зустрічі")
        self.client.post(url, {**q, "pick": [f"e{m.pk}"], f"p_e{m.pk}": self.dp.pk})
        self.assertEqual(Booking.objects.filter(source=Booking.MEETING, status=Booking.SENT).count(), 1)
        self.assertContains(self.client.get(url, q), "уже записано з Minerva")

    def test_last_month_preset(self):
        page = self.client.get(reverse("admin:worktime_workday_meetings"), {"preset": "last_month"})
        first = timezone.localdate().replace(day=1)
        last = first - datetime.timedelta(days=1)
        self.assertEqual((page.context["date_from"], page.context["date_to"]), (last.replace(day=1), last))
