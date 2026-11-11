"""🎙 Розшифровка наради в базі знань (meetings/knowledge.py): python manage.py test meetings --settings=tabele.settings_test

Приватні частини ≤ TEXT_MAX, вид для RAG — «link» (документ проходить схему контракту), доступ — лише власник,
хмарний AI їх не отримує, видалення → tombstone у стрічці й копії тексту прибрано.
"""
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from meetings import knowledge, notify, services
from meetings.models import Meeting, MeetingSettings, MeetingTranscript
from meetings.tests_services import SEGMENTS
from projects.models import Project
from rag_assistant import knowledge_items
from rag_assistant.models import KnowledgeItem, RetrievalJob
from rag_assistant.tests_knowledge import KnowledgeBase


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class MeetingKnowledgeTests(KnowledgeBase):
    def setUp(self):
        super().setUp()
        notify.BACKGROUND = False
        self.addCleanup(setattr, notify, "BACKGROUND", True)
        s = MeetingSettings.load()
        s.enabled = True
        s.save()
        self.user.first_name, self.user.last_name = "Slava", "P"
        self.user.save()
        self.project = Project.objects.create(name="Antenne", customer=self.client_co, owner=self.user)
        self.other = User.objects.create_superuser("boss", "boss@x.y", "p")

    def meeting(self, segments=SEGMENTS, **kw):
        m = services.create(self.user, {"title": "Geheime Abstimmung", "project": self.project.pk})
        Meeting.objects.filter(pk=m.pk).update(
            status=Meeting.REVIEW, transcript_at=timezone.now(), capture_mode=kw.get("capture_mode", "both"),
            started_at=datetime(2026, 11, 3, 9, 30, tzinfo=dt_tz.utc))
        MeetingTranscript.objects.create(meeting=m, segments=segments, at=timezone.now())
        return Meeting.objects.get(pk=m.pk)

    def test_publish_private_parts_and_contract(self):
        m = self.meeting()
        pks = knowledge.publish(m)
        self.assertEqual(len(pks), 1)
        item = KnowledgeItem.objects.get(pk=pks[0])
        self.assertEqual((item.kind, item.auto, item.visibility, item.owner, item.project, item.customer),
                         (KnowledgeItem.MEETING, True, KnowledgeItem.PRIVATE, self.user, self.project, self.client_co))
        self.assertEqual(item.source_key, f"meeting:{m.pk}:1")
        self.assertEqual(item.title, f"🎙 Нарада {self.project.code} · 03.11.2026 · 1/1")
        self.assertNotIn("Geheime", item.title)
        self.assertEqual(item.created_at, m.started_at)
        self.assertTrue(item.text.startswith("🎙 Нарада 03.11.2026 · частина 1 з 1. «Я» — Slava P"))
        self.assertIn("[00:02:00] Співрозмовники: Wir liefern die Antennen bis Freitag nach Köln.", item.text)
        self.assertIn("[00:02:11] Я: Ich schicke die Zeichnung bis Montag.", item.text)
        self.assertIsNotNone(Meeting.objects.get(pk=m.pk).approved_at)
        self.assertTrue(knowledge_items.ai_blocked(item))
        self.assertFalse(knowledge_items.can_view(self.other, item))       # суперкористувач — ні
        self.assertTrue(knowledge_items.can_view(self.user, item))
        self.assertFalse(knowledge_items.can_change(self.user, item))      # частини веде Minerva
        data = self.changes(types="email,document")
        docs = [i["document"] for i in data["items"] if i["document_type"] == "document"]
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual((doc["kind"], doc["attachments"], doc["source_url"], doc["text_truncated"]),
                         ("link", [], None, False))
        self.assertEqual(doc["access"], {"scope": "users", "user_ids": [self.user.pk]})
        self.assertEqual(doc["projects"][0]["code"], self.project.code)
        got = self.api("get", f"/api/v1/knowledge/documents/document:{item.pk}/").json()
        self.assertSchema("document", got)

    def test_parts_split_under_limit_and_republish_in_place(self):
        long = [{"n": i, "start": i * 10.0, "end": i * 10.0 + 9, "ch": "R" if i % 2 else "L",
                 "text": f"Satz {i} " + "x" * 300} for i in range(1, 41)]
        m = self.meeting(long)
        with patch("rag_assistant.knowledge.TEXT_MAX", 4000):
            pks = knowledge.publish(m)
            self.assertGreater(len(pks), 2)
            items = knowledge.parts(m)
            self.assertTrue(all(len(i.text) <= 4000 for i in items))
            self.assertEqual(items[0].title.split(" · ")[-1], f"1/{len(pks)}")
            joined = "\n".join(i.text for i in items)
            self.assertTrue(all(f"Satz {n} " in joined for n in range(1, 41)))
            MeetingTranscript.objects.filter(meeting=m).update(segments=long[:3])
            again = knowledge.publish(m)
        self.assertEqual(again, pks[:1])                                    # той самий документ оновлено
        self.assertEqual(len(knowledge.parts(m)), 1)                       # зайві частини прибрано

    def test_split_hard_cuts_long_line(self):
        parts = knowledge.split(["a" * 25, "bb", "c" * 5], 10)
        self.assertEqual(parts, ["a" * 10, "a" * 10, "aaaaa\nbb", "ccccc"])

    def test_labels_by_capture_mode(self):
        m = self.meeting(capture_mode="mic")
        knowledge.publish(m)
        text = knowledge.parts(m)[0].text
        self.assertIn("мовців не розділено", text)
        self.assertIn("] У кімнаті (мікрофон): Ich schicke", text)

    def test_unpublish_prefix_tombstones_and_copies(self):
        m = self.meeting()
        pk = knowledge.publish(m)[0]
        neighbour = KnowledgeItem.objects.create(title="n", text="n", source_key=f"meeting:{m.pk}0:1",
                                                 kind=KnowledgeItem.MEETING, auto=True, owner=self.user,
                                                 visibility=KnowledgeItem.PRIVATE)
        job = RetrievalJob.objects.create(user=self.user, idempotency_key="k1", hits=[
            {"document_id": f"document:{pk}", "text": "Wir liefern", "url": f"/rag/knowledge/{pk}/"},
            {"document_id": "email:5", "text": "other"}])
        start = self.changes(types="email,document")["next_after"]
        self.assertEqual(knowledge.unpublish(m), 1)
        self.assertTrue(KnowledgeItem.objects.filter(pk=neighbour.pk).exists())   # нарада 120 ≠ 12
        self.assertFalse(KnowledgeItem.objects.filter(pk=pk).exists())
        job.refresh_from_db()
        self.assertEqual(job.hits, [{"document_id": "email:5", "text": "other"}])
        last = self.changes(after=start, types="email,document")["items"]
        self.assertIn((f"document:{pk}", "delete"), [(i["document_id"], i["action"]) for i in last])

    def test_deleting_meeting_removes_parts(self):
        m = self.meeting()
        pk = knowledge.publish(m)[0]
        services.delete_meeting(self.user, m)
        self.assertFalse(KnowledgeItem.objects.filter(pk=pk).exists())

    def test_publish_needs_transcript_and_parts_state_owner_only(self):
        m = services.create(self.user, {"title": "x"})
        with self.assertRaises(services.MeetingError):
            knowledge.publish(m)
        m = self.meeting()
        knowledge.publish(m)
        state = knowledge.parts_state(m, self.user)
        self.assertTrue(state["enabled"])
        self.assertEqual([p["n"] for p in state["parts"]], [1])
        self.assertTrue(state["parts"][0]["rag"])
        self.assertEqual(knowledge.parts_state(m, self.other)["parts"], [])

    def test_wire_kind_and_ai_blocked(self):
        item = KnowledgeItem(kind=KnowledgeItem.MEETING, title="t")
        self.assertEqual(knowledge_items.wire_kind(item), "link")
        self.assertEqual(knowledge_items.wire_kind(KnowledgeItem(kind=KnowledgeItem.OFFER)), "offer")
        self.assertTrue(knowledge_items.ai_blocked(item))
        self.assertTrue(knowledge_items.ai_blocked({"ai_blocked": True}))
        self.assertFalse(knowledge_items.ai_blocked({"document_id": "document:1"}))
        self.assertFalse(knowledge_items.ai_blocked(KnowledgeItem(kind=KnowledgeItem.UPLOAD)))
        self.assertFalse(knowledge_items.ai_blocked(None))
