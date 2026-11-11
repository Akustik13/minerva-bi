"""Пункти наради в проєкті (📌 момент, ⚠️ ризик, 📅 крок з meeting і секундою): python manage.py test projects
--settings=tabele.settings_test"""
from datetime import date, datetime, timedelta, timezone as dt_tz

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from meetings.models import Meeting
from projects import plan, risks, services
from projects.models import (Project, ProjectHighlight, ProjectPhase, ProjectRisk, ProjectState, ProjectStep)


class MeetingRefsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("rec", "rec@x.y", "p")
        self.project = Project.objects.create(name="Antenne", owner=self.user)
        self.meeting = Meeting.objects.create(owner=self.user, title="t", project=self.project,
                                              started_at=datetime(2026, 11, 3, 23, 30, tzinfo=dt_tz.utc))
        ProjectState.objects.create(project=self.project, dirty=False)

    def test_highlight_from_meeting(self):
        h = services.add_highlight(self.project, title="Rabatt  5 %", quote=" «5 % ab 500» ", user=self.user,
                                   source=ProjectHighlight.MEETING, meeting=self.meeting, meeting_second=754.9)
        self.assertEqual((h.source, h.meeting, h.meeting_second, h.title, h.quote),
                         ("meeting", self.meeting, 754, "Rabatt 5 %", "«5 % ab 500»"))
        self.assertEqual(h.date, date(2026, 11, 4))                       # 23:30 UTC = 00:30 у Берліні
        old = services.add_highlight(self.project, title="x", source="whatever")
        self.assertEqual((old.source, old.meeting, old.meeting_second, old.date),
                         ("manual", None, None, timezone.localdate()))
        self.assertEqual(services.add_highlight(self.project, title="y", source="ai").source, "ai")

    def test_risk_from_meeting(self):
        r = risks.save(self.project, {"title": "Zoll", "quote": "könnte sich verzögern", "review_on": "2026-11-20"},
                       user=self.user, source="meeting", meeting=self.meeting, meeting_second=12)
        self.assertEqual((r.source, r.meeting, r.meeting_second, r.evidence, r.owner),
                         ("meeting", self.meeting, 12, "könnte sich verzögern", self.user))
        self.assertTrue(ProjectState.objects.get(project=self.project).dirty)
        bare = risks.save(self.project, {"title": "Ohne Zitat"}, source="meeting", meeting=self.meeting)
        self.assertEqual(bare.evidence, "🎙 Нарада 04.11.2026")
        manual = risks.save(self.project, {"title": "Hand"})
        self.assertEqual((manual.source, manual.meeting), (ProjectRisk.MANUAL, None))
        edited = risks.save(self.project, {"id": r.pk, "title": "Zoll neu"}, source="meeting", meeting=self.meeting)
        self.assertEqual((edited.evidence, edited.title), ("könnte sich verzögern", "Zoll neu"))

    def test_meeting_step(self):
        p1 = ProjectPhase.objects.create(project=self.project, name="A", order=1)
        ProjectStep.objects.create(project=self.project, phase=p1, title="done", status=ProjectStep.DONE, order=5)
        p2 = ProjectPhase.objects.create(project=self.project, name="B", order=2)
        ProjectStep.objects.create(project=self.project, phase=p2, title="open", order=7)
        due = timezone.localdate() + timedelta(days=4)
        s = plan.meeting_step(self.project, "Zeichnung  schicken", assignee=self.user.pk, due=due, side="customer",
                              quote="schicke die\nZeichnung", meeting=self.meeting, meeting_second=131, user=self.user)
        self.assertEqual((s.phase, s.order, s.side, s.assignee, s.source, s.meeting, s.meeting_second),
                         (p2, 8, "customer", self.user, ProjectStep.MEETING, self.meeting, 131))
        self.assertEqual((s.start, s.end, s.duration_days), (timezone.localdate(), due, 5))
        self.assertEqual(s.notes, "🎙 з наради від 04.11.2026: «schicke die Zeichnung»")
        self.assertTrue(ProjectState.objects.get(project=self.project).dirty)
        past = plan.meeting_step(self.project, "Alt", due=timezone.localdate() - timedelta(days=2), side="nobody")
        self.assertEqual((past.side, past.duration_days, past.assignee), (ProjectStep.WE, 1, None))
        off = User.objects.create_user("off", "o@x.y", "p", is_staff=True, is_active=False)
        with self.assertRaisesMessage(plan.PlanError, "Виконавця"):
            plan.meeting_step(self.project, "x", assignee=off)
        with self.assertRaisesMessage(plan.PlanError, "назву"):
            plan.meeting_step(self.project, "  ")

    def test_meeting_fk_set_null_on_delete(self):
        h = services.add_highlight(self.project, title="x", source="meeting", meeting=self.meeting)
        self.meeting.delete()
        h.refresh_from_db()
        self.assertEqual((h.meeting, h.source), (None, "meeting"))
