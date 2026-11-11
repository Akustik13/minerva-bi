"""🎙 Наради в проєкті: «🎙 Записати нараду» в пульті, «🎙 Мої наради», доріжка шкали, «🎙 з наради DD.MM» біля
прийнятих моментів / ризиків / кроків, приватність (лише свої — і для суперкористувача), спільний AI-огляд проєкту без
«🔒 лише я» документів і розшифровок нарад.

    python manage.py test projects.tests_meetings_ui --settings=tabele.settings_test
"""
import json
import xml.etree.ElementTree as ET
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.utils import timezone

from meetings.models import Meeting, MeetingItem, MeetingSettings
from projects import cockpit, overview, timeline
from projects.models import ProjectHighlight, ProjectRisk, ProjectStep
from projects.tests_cockpit import CockpitBase


def enable_recording(on=True):
    s = MeetingSettings.load()
    s.enabled = on
    s.texts_reviewed_at = timezone.now() if on else None
    s.save()


class MeetingsBase(CockpitBase):
    def setUp(self):
        super().setUp()
        enable_recording()
        self.bob = User.objects.create_user("bob", "bob@x.y", "p", is_staff=True)
        self.bob.profile.role = "manager"
        self.bob.profile.save()
        self.bob = User.objects.get(pk=self.bob.pk)

    def meeting(self, owner=None, title="Kickoff Antenne", status=Meeting.REVIEW, pending=0, accepted=0, **kw):
        m = Meeting.objects.create(owner=owner or self.user, title=title, project=self.project, status=status,
                                   started_at=kw.pop("started_at", self.ago(2, hour=10)), duration_s=2700, **kw)
        for n in range(pending + accepted):
            MeetingItem.objects.create(meeting=m, key=f"k{m.pk}-{n}", kind=MeetingItem.DECISION, title=f"Punkt {n}",
                                       quote=f"Zitat {n}", start_s=60 * n,
                                       status=MeetingItem.PENDING if n < pending else MeetingItem.ACCEPTED)
        return m

    def html(self, user=None):
        return cockpit.render(self.project, user or self.user)

    def page_url(self, m, anchor=""):
        return f"/admin/meetings/meeting/{m.pk}/page/{anchor}"


class RecordLinkTests(MeetingsBase):
    def test_record_link_only_when_recording_is_possible(self):
        html = self.html()
        self.assertIn("🎙 Записати нараду", html)
        self.assertIn(f"/admin/meetings/meeting/new/?project={self.project.pk}", html)
        self.assertNotIn("<form", html)                           # пульт — усередині форми картки
        enable_recording(False)                                   # записування ще не ввімкнено — кнопки немає
        self.assertNotIn("/admin/meetings/meeting/new/", self.html())

    def test_viewer_without_meetings_right_has_no_link(self):
        viewer = User.objects.create_user("viewer", "v@x.y", "p", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_project"))
        viewer.profile.role = "readonly"
        viewer.profile.module_operations = {"projects": ["view"]}
        viewer.profile.save()
        viewer = User.objects.get(pk=viewer.pk)
        self.assertNotIn("/admin/meetings/meeting/new/", self.html(viewer))

    def test_change_page_shows_link(self):
        r = self.client.get(f"/admin/projects/project/{self.project.pk}/change/")
        self.assertContains(r, "🎙 Записати нараду")
        self.assertContains(r, "projects/pj_timeline.js")


class MyMeetingsBlockTests(MeetingsBase):
    def test_block_lists_only_own_meetings_even_for_superuser(self):
        own = self.meeting(pending=2, accepted=1)
        self.meeting(owner=self.bob, title="Bobs geheime Runde")
        html = self.html()                                        # self.user — суперкористувач
        self.assertIn("🎙 Мої наради", html)
        self.assertIn("Kickoff Antenne", html)
        self.assertIn("чекають ✓ / ✕: 2", html)
        self.assertIn(self.page_url(own), html)
        self.assertNotIn("Bobs geheime Runde", html)              # чужа нарада — нікому, і адміністратору
        bob_html = self.html(self.bob)
        self.assertIn("Bobs geheime Runde", bob_html)
        self.assertNotIn("Kickoff Antenne", bob_html)

    def test_no_block_without_meetings_and_limit(self):
        self.assertNotIn("🎙 Мої наради", self.html())
        for n in range(cockpit.MEETINGS_SHOWN + 2):
            self.meeting(title=f"Runde {n}", started_at=self.ago(n + 1))
        ctx = cockpit.context(self.project, self.user)["meetings"]
        self.assertEqual((len(ctx["rows"]), ctx["total"]), (cockpit.MEETINGS_SHOWN, cockpit.MEETINGS_SHOWN + 2))
        self.assertEqual(ctx["rows"][0]["title"], "Runde 0")      # новіші першими
        self.assertIn("/admin/meetings/meeting/", ctx["all_url"])

    def test_list_query_does_not_load_protocol_or_transcript(self):
        self.meeting(consent_text="Sehr langer Text " * 50)
        m = timeline.own_meetings(self.project, self.user)[0]
        self.assertIn("consent_text", m.get_deferred_fields())
        self.assertIn("participants", m.get_deferred_fields())


class TimelineLaneTests(MeetingsBase):
    def test_lane_shows_only_viewers_meetings(self):
        own = self.meeting(pending=1)
        theirs = self.meeting(owner=self.bob, title="Bobs geheime Runde")
        facts = overview.collect(self.project)
        evs = timeline.events(self.project, facts, self.user)
        lane = [e for e in evs if e["lane"] == "meetings"]
        self.assertEqual(len(lane), 1)
        self.assertEqual(lane[0]["url"], self.page_url(own))
        self.assertEqual(lane[0]["cls"], "warn")                  # розшифровка готова, пункти чекають ✓ / ✕
        self.assertIn("1 чекають ✓ / ✕", lane[0]["tip"])
        svg = timeline.render(self.project, facts, self.user)
        ET.fromstring(svg)
        self.assertIn("🎙 Наради", svg)
        self.assertNotIn("Bobs geheime Runde", svg)
        data = timeline.payload(self.project, facts, self.user)
        self.assertIn(["meetings", "🎙 Наради"], [list(x) for x in data["lanes"]])
        self.assertNotIn(self.page_url(theirs), json.dumps(data))
        items = timeline.mobile_items(self.project, facts, self.user)
        self.assertIn("🎙", [i["icon"] for i in items])

    def test_lane_status_classes(self):
        cases = {Meeting.READY: "plan", Meeting.RECORDING: "open", Meeting.PROCESSING: "open", Meeting.FAILED: "bad"}
        for status, cls in cases.items():
            m = self.meeting(status=status, title=f"M {status}")
            m.pending = 0
            self.assertEqual(timeline.meeting_cls(m), cls)
        done = self.meeting(status=Meeting.REVIEW, accepted=1)
        done.pending = 0
        self.assertEqual(timeline.meeting_cls(done), "ok")

    def test_calendar_meeting_offers_record_and_open(self):
        from calendar_app.models import CalendarEvent
        ev = CalendarEvent.objects.create(user=self.user, title="Telko Antenne", start_at=self.ago(-1, hour=11),
                                          email_message=self.ask, event_type="meeting")
        cal = next(e["cal"] for e in timeline.events(self.project, overview.collect(self.project), self.user)
                   if e["lane"] == "calendar")
        self.assertEqual(cal["record"], f"/admin/meetings/meeting/new/?project={self.project.pk}&event={ev.pk}")
        self.assertNotIn("rec", cal)
        m = self.meeting(calendar_event=ev)
        self.meeting(owner=self.bob, calendar_event=ev, title="x")             # чужа (новіша) нарада з події — ні
        cal = next(e["cal"] for e in timeline.events(self.project, overview.collect(self.project), self.user)
                   if e["lane"] == "calendar")
        self.assertEqual(cal["rec"], self.page_url(m))
        enable_recording(False)
        cal = next(e["cal"] for e in timeline.events(self.project, overview.collect(self.project), self.user)
                   if e["lane"] == "calendar")
        self.assertNotIn("record", cal)


class FromMeetingTests(MeetingsBase):
    def test_moment_from_meeting_links_only_for_recorder(self):
        m = self.meeting(started_at=self.ago(3, hour=10))
        ProjectHighlight.objects.create(project=self.project, date=self.today, title="Muster freigegeben",
                                        kind=ProjectHighlight.APPROVAL, quote="Das Muster ist freigegeben",
                                        meeting=m, meeting_second=754, source="meeting", created_by=self.user)
        label = f"🎙 з наради {self.ago(3).strftime('%d.%m')}"
        html = self.html()
        self.assertIn(label, html)
        self.assertIn(self.page_url(m, "#t=754"), html)
        tl = timeline.events(self.project, overview.collect(self.project), self.user)
        flag = next(e for e in tl if e["lane"] == "moments")
        self.assertEqual(flag["url"], self.page_url(m, "#t=754"))
        self.assertIn(label, flag["tip"])
        bob_html = self.html(self.bob)                           # момент — усім, місце в записі — лише тому, хто записав
        self.assertIn("Muster freigegeben", bob_html)
        self.assertIn(label, bob_html)
        self.assertNotIn("#t=754", bob_html)
        self.assertNotIn("Kickoff Antenne", bob_html)             # назву наради не видно

    def test_risk_from_meeting_in_register(self):
        m = self.meeting()
        ProjectRisk.objects.create(project=self.project, title="Lieferverzug Steckverbinder", probability=3, impact=3,
                                   source="meeting", meeting=m, meeting_second=95, evidence="Lieferung verzögert sich")
        html = self.html()
        self.assertIn("Lieferverzug Steckverbinder", html)
        self.assertIn(self.page_url(m, "#t=95"), html)
        self.assertNotIn("#t=95", self.html(self.bob))

    def test_step_from_meeting_in_plan_dialog_data(self):
        m = self.meeting()
        step = ProjectStep.objects.create(project=self.project, title="Muster an Kunden senden", start=self.today,
                                          end=self.today + timedelta(days=3), meeting=m, meeting_second=1203,
                                          source="meeting")
        data = cockpit.context(self.project, self.user)["plan"]["data"]
        s = next(x for x in data["steps"] if x["id"] == step.pk)
        self.assertEqual(s["meeting"]["url"], self.page_url(m, "#t=1203"))
        self.assertTrue(s["meeting"]["label"].startswith("🎙 з наради"))
        bob_data = cockpit.context(self.project, self.bob)["plan"]["data"]        # менеджер: може змінювати проєкт
        s = next(x for x in bob_data["steps"] if x["id"] == step.pk)
        self.assertEqual(s["meeting"]["url"], "")                                  # місце в записі — лише тому, хто записав

    def test_deleted_meeting_leaves_item_without_mark(self):
        m = self.meeting()
        h = ProjectHighlight.objects.create(project=self.project, date=self.today, title="Termin bestätigt",
                                            meeting=m, meeting_second=10, source="meeting")
        m.delete()
        h.refresh_from_db()
        self.assertIsNone(h.meeting_id)
        html = self.html()
        self.assertIn("Termin bestätigt", html)
        self.assertNotIn("з наради", html)


class OverviewPrivacyTests(MeetingsBase):
    """Спільний AI-огляд проєкту (бачать усі, хто бачить проєкт): без «🔒 лише я» і без розшифровок нарад."""

    def item(self, **kw):
        from rag_assistant.models import KnowledgeItem
        return KnowledgeItem.objects.create(project=self.project, owner=self.user, **kw)

    def test_prompt_has_team_document_but_not_private_or_meeting(self):
        from rag_assistant.models import KnowledgeItem
        team = self.item(title="Spezifikation", text="TEAM-DOKUMENT 2,45 GHz", visibility=KnowledgeItem.TEAM)
        private = self.item(title="Notiz", text="PRIVAT-NOTIZ nur ich", visibility=KnowledgeItem.PRIVATE)
        meeting = self.item(title="🎙 Нарада 1/1", text="[00:01:02] Я: GEHEIMES-TRANSKRIPT", kind="meeting",
                            visibility=KnowledgeItem.PRIVATE, auto=True, source_key="meeting:1:1")
        calls = []
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=lambda p, **kw: calls.append(p) or ""):
            overview.generate(self.project, self.user)
        prompt = calls[-1]
        self.assertIn("TEAM-DOKUMENT", prompt)
        self.assertNotIn("PRIVAT-NOTIZ", prompt)
        self.assertNotIn("GEHEIMES-TRANSKRIPT", prompt)
        text, ids = overview._documents_block(self.project, self.user, [])
        self.assertEqual(ids, {team.document_id, private.document_id, meeting.document_id})   # пропуск для RAG
        self.assertNotIn("Notiz", text)
        self.assertEqual(overview._private_documents(self.user), {private.document_id, meeting.document_id})

    def test_rag_block_skips_private_and_meeting_hits(self):
        from rag_assistant.models import KnowledgeItem, RetrievalJob
        meeting = self.item(title="🎙 Нарада 1/1", text="x", kind="meeting", visibility=KnowledgeItem.PRIVATE,
                            auto=True, source_key="meeting:1:1")
        private = self.item(title="Notiz", text="y", visibility=KnowledgeItem.PRIVATE)
        hits = [{"document_id": meeting.document_id, "kind": "document", "text": "GEHEIMES-TRANSKRIPT",
                 "url": f"/rag/knowledge/{meeting.pk}/", "subject": "🎙", "ai_blocked": True},
                {"document_id": private.document_id, "kind": "document", "text": "PRIVAT-TREFFER",
                 "url": f"/rag/knowledge/{private.pk}/", "subject": "Notiz"},
                {"document_id": "email:999", "kind": "email", "message_pk": 999, "text": "Kunde will 500 Stück",
                 "url": "/email/message/999/", "subject": "Anfrage", "from_email": "buyer@client.example"}]
        job = RetrievalJob.objects.create(user=self.user, idempotency_key="minerva:test:ov", request={"query": "x"},
                                          status=RetrievalJob.COMPLETED, hits=hits)
        with patch("rag_assistant.retrieval.available_for", return_value=True), \
                patch("rag_assistant.retrieval.start", return_value=job):
            text = overview._rag_block(self.project, self.user, overview.collect(self.project), [])
        self.assertIn("Kunde will 500 Stück", text)
        self.assertNotIn("GEHEIMES-TRANSKRIPT", text)
        self.assertNotIn("PRIVAT-TREFFER", text)
