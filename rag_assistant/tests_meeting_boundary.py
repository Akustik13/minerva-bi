"""🎙 Розшифровки нарад і хмарний AI: частини розшифровки (KnowledgeItem kind «meeting», приватні) людина бачить у
пошуку й картці, але жоден AI-шлях їх не отримує — retrieval.verify_hits позначає фрагменти (ai_blocked), for_ai /
facts_text їх відкидають, «🧭 / ❓» картки не бере текст; посилання /rag/knowledge/<pk>/ веде на сторінку наради;
у «Документах бази знань» адмінки — лише свої, лише для читання.

    python manage.py test rag_assistant.tests_meeting_boundary --settings=tabele.settings_test
"""
from unittest.mock import patch

from django.test import override_settings

from rag_assistant import knowledge, knowledge_items, retrieval
from rag_assistant.models import KnowledgeDoc, KnowledgeItem, KnowledgeSettings, RetrievalJob
from rag_assistant.tests_entity_help import EntityHelpBase
from rag_assistant.tests_knowledge_items import ItemsMixin, staff

SECRET = "GEHEIMES-TRANSKRIPT Rabatt 17 Prozent"


class MeetingBoundaryBase(ItemsMixin, EntityHelpBase):
    def setUp(self):
        super().setUp()
        self.inline_fetch()

    def meeting_item(self, owner=None, n=1, **links):
        return KnowledgeItem.objects.create(
            kind=KnowledgeItem.MEETING, title=f"🎙 Нарада · {n}/1", text=f"[00:01:02] Я: {SECRET}",
            visibility=KnowledgeItem.PRIVATE, owner=owner or self.user, auto=True, source_key=f"meeting:7:{n}",
            **links)

    def fed(self, item):
        ks = KnowledgeSettings.get()
        ks.enabled = True
        ks.save()
        knowledge_items.touch([item.pk])
        return KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk)

    def hit(self, item, doc, gen, text=SECRET):
        return {"source_instance_id": "test-suite", "generation": gen, "document_id": f"document:{item.pk}",
                "document_type": "document", "document_revision": doc.revision, "content_version": doc.feed_version,
                "chunk": 0, "score": 0.9, "text": text, "subject": item.title, "sent_at": None, "from_email": "",
                "thread_id": None, "mailbox_id": None, "customer_id": self.client_c.pk, "project_ids": [],
                "attachment": None, "text_truncated": False}


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class VerifyHitsTests(MeetingBoundaryBase):
    def test_meeting_hits_kept_for_owner_but_blocked_for_ai(self):
        meeting = self.meeting_item(customer=self.client_c)
        upload = self.upload(name="Spec.txt", data=b"Normaler Text", ctype="text/plain",
                             links={"customer": self.client_c})
        gen = str(knowledge.get_scope().generation)
        result = {"source_instance_id": "test-suite", "generation": gen, "user_id": self.user.pk,
                  "hits": [self.hit(meeting, self.fed(meeting), gen),
                           self.hit(upload, self.fed(upload), gen, text="Normaler Text")]}
        kept, dropped = retrieval.verify_hits(self.user, result)
        self.assertEqual((len(kept), dropped), (2, 0))                    # людина бачить обидва
        by_id = {h["document_id"]: h for h in kept}
        self.assertTrue(by_id[meeting.document_id]["ai_blocked"])
        self.assertNotIn("ai_blocked", by_id[upload.document_id])
        self.assertEqual([h["document_id"] for h in retrieval.for_ai(kept)], [upload.document_id])
        job = RetrievalJob.objects.create(user=self.user, idempotency_key="minerva:test:mt", request={"query": "x"},
                                          status=RetrievalJob.COMPLETED, hits=kept)
        facts = retrieval.facts_text(job)
        self.assertIn("Normaler Text", facts)
        self.assertNotIn(SECRET, facts)
        only = RetrievalJob.objects.create(user=self.user, idempotency_key="minerva:test:mt2", request={"query": "x"},
                                           status=RetrievalJob.COMPLETED, hits=[by_id[meeting.document_id]])
        self.assertEqual(retrieval.facts_text(only), "")                  # лише нарада — AI нічого не отримує
        other = staff("mila", "view_customer")                           # чужа розшифровка — не видно взагалі
        self.assertEqual(retrieval.verify_hits(other, dict(result, user_id=other.pk))[0][0]["document_id"],
                         upload.document_id)


class EntityHelpBoundaryTests(MeetingBoundaryBase):
    def test_card_question_never_sends_meeting_text_to_ai(self):
        meeting = self.meeting_item(customer=self.client_c)
        self.upload(name="Reklamation.txt", data=b"Kratzer auf dem Gehaeuse", ctype="text/plain",
                    links={"customer": self.client_c})
        # фрагмент наради без позначки ai_blocked (старий) — відкидається, бо id розшифровки картка дає в пропуск RAG
        self.hits = [{"document_id": meeting.document_id, "kind": "document", "text": SECRET,
                      "url": f"/rag/knowledge/{meeting.pk}/", "subject": meeting.title, "document_kind": "🎙 нарада"},
                     {"document_id": "document:9999", "kind": "document", "text": SECRET + " (2)", "ai_blocked": True,
                      "url": "/rag/knowledge/9999/", "subject": "🎙", "document_kind": "🎙 нарада"},
                     {"document_id": "email:999", "message_pk": 999, "thread_id": 777, "url": "/email/message/999/",
                      "subject": "Reklamation", "from_email": "buyer@client.com", "sent_at": "2026-05-02",
                      "text": "Kunde beschwert sich"}]
        self.ai.letter = "Скарга на подряпини [#3]."
        b = self.run_help("customer", self.client_c, "answer", question="які були скарги?")
        self.assertEqual(b["status"], "done", b)
        prompt = self.prompt()
        self.assertIn("Kratzer", prompt)                                   # звичайний документ — так
        self.assertIn("Kunde beschwert sich", prompt)                      # звичайний фрагмент RAG — так
        self.assertNotIn(SECRET, prompt)                                   # розшифровка — ні, ні з картки, ні з RAG
        self.assertNotIn(SECRET, str(b["sources"]))
        stage = next(s for s in b["stages"] if s["key"] == "documents")
        self.assertIn("розшифровок нарад 1", stage["detail"])


class PageAndAdminTests(MeetingBoundaryBase):
    def test_knowledge_link_opens_meeting_page_for_owner_only(self):
        item = self.meeting_item()
        r = self.client.get(f"/rag/knowledge/{item.pk}/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/admin/meetings/meeting/7/page/#kb")
        self.client.force_login(staff("mila", "view_customer"))
        self.assertEqual(self.client.get(f"/rag/knowledge/{item.pk}/").status_code, 404)

    def test_admin_lists_only_own_meeting_parts_and_read_only(self):
        mine = self.meeting_item()
        other = staff("bob", role="admin")
        theirs = self.meeting_item(owner=other, n=2)
        r = self.client.get("/admin/rag_assistant/knowledgeitem/")          # self.user — суперкористувач
        self.assertEqual(r.status_code, 200)
        listed = {o.pk for o in r.context["cl"].result_list}
        self.assertIn(mine.pk, listed)
        self.assertNotIn(theirs.pk, listed)                                 # чужа розшифровка — і суперкористувачу ні
        self.assertEqual(self.client.get(f"/admin/rag_assistant/knowledgeitem/{theirs.pk}/change/").status_code, 302)
        r = self.client.post(f"/admin/rag_assistant/knowledgeitem/{mine.pk}/change/",
                             {"kind": "upload", "title": "x", "visibility": "team"})
        self.assertEqual(r.status_code, 403)                                # змінює лише сторінка наради
        self.assertEqual(self.client.post(f"/admin/rag_assistant/knowledgeitem/{mine.pk}/delete/",
                                          {"post": "yes"}).status_code, 403)
        mine.refresh_from_db()
        self.assertEqual((mine.visibility, mine.kind), (KnowledgeItem.PRIVATE, KnowledgeItem.MEETING))
        with patch.object(knowledge_items, "can_change", return_value=True):     # навіть якщо щось дозволить
            self.assertFalse(self.client.get(f"/admin/rag_assistant/knowledgeitem/{mine.pk}/change/")
                             .context["has_change_permission"])
