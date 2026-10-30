"""Документи й посилання бази знань (KnowledgeItem, тип document): стрічка для RAG, доступ, картки, пошук.

python manage.py test rag_assistant --settings=tabele.settings_test   (RAG, AI і мережа підмінені)
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from rag_assistant import knowledge, knowledge_items, retrieval
from rag_assistant.models import KnowledgeActivity, KnowledgeDoc, KnowledgeItem, RetrievalJob
from rag_assistant.tests_entity_help import EntityHelpBase
from rag_assistant.tests_knowledge import KnowledgeBase

PDF = b"%PDF-1.4\n% synthetic\n"
HTML = (b"<html><head><title>Application note SMA</title></head><body><h1>SMA</h1>"
        b"<p>Torque for SMA connectors: 0.56 Nm.</p></body></html>")


def staff(username, *perms, role=None):
    """Співробітник з правами Django perms; role — роль Minerva (без неї — менеджер, як створює core.signals).
    «Без права бачити картку» — роль без модуля (readonly: лише дашборд і FAQ)."""
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=perms))
    if role:
        user.profile.role = role
        user.profile.save()
    return User.objects.get(pk=user.pk)


class ItemsMixin:
    def inline_fetch(self):
        p = patch("rag_assistant.knowledge_items._start_background", lambda fn: fn())
        p.start()
        self.addCleanup(p.stop)

    def upload(self, user=None, name="Pruefbericht.pdf", data=PDF, ctype="application/pdf", **kw):
        return knowledge_items.add_file(user or self.user, SimpleUploadedFile(name, data, ctype), **kw)


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class DocumentFeedTests(ItemsMixin, KnowledgeBase):
    def setUp(self):
        super().setUp()
        self.inline_fetch()

    def test_old_rag_without_types_sees_only_email(self):
        self.mail()
        self.upload(links={"customer": self.client_co})
        data = self.changes()
        self.assertEqual({i["document_type"] for i in data["items"]}, {"email"})
        snap = self.api("post", "/api/v1/knowledge/manifest/snapshots/").json()
        self.assertEqual(snap["total"], 1)
        self.assertEqual(self.api("get", "/api/v1/knowledge/summary/").json()["documents"]["eligible"], 1)
        self.assertEqual(KnowledgeActivity.get().rag_types, ["email"])
        self.assertFalse(retrieval.documents_supported())

    def test_new_rag_gets_document_with_file_access_and_bytes(self):
        viewer = staff("mila", "view_customer")
        staff("nobody", role="readonly")                    # без права бачити клієнта — не в access
        item = self.upload(links={"customer": self.client_co}, note="Messprotokoll")
        data = self.changes(types="email,document")
        events = [i for i in data["items"] if i["document_type"] == "document"]
        self.assertEqual(len(events), 1)
        doc = events[0]["document"]
        self.assertEqual((doc["document_id"], doc["url"], doc["kind"]), (f"document:{item.pk}", f"/rag/knowledge/{item.pk}/", "upload"))
        self.assertEqual(doc["access"], {"scope": "users", "user_ids": sorted([self.user.pk, viewer.pk])})
        self.assertEqual(doc["customer"]["id"], self.client_co.pk)
        self.assertIn("Messprotokoll", doc["text"])
        att = doc["attachments"][0]
        self.assertTrue(att["indexable"])
        r = self.client.get(att["url"], HTTP_AUTHORIZATION=f"Token {self.key.key}")
        self.assertEqual((r.status_code, r.content), (200, PDF))
        self.assertEqual("sha256:" + r["X-Content-SHA256"], att["content_hash"])
        self.assertEqual(KnowledgeActivity.get().rag_types, ["email", "document"])
        self.assertTrue(retrieval.documents_supported())
        snap = self.api("post", "/api/v1/knowledge/manifest/snapshots/", data={"types": ["email", "document"]}).json()
        page = self.api("get", f"/api/v1/knowledge/manifest/snapshots/{snap['snapshot_id']}/").json()
        self.assertIn(f"document:{item.pk}", [i["document_id"] for i in page["items"]])
        got = self.api("get", f"/api/v1/knowledge/documents/document:{item.pk}/").json()
        self.assertSchema("document", got)
        summary = self.api("get", "/api/v1/knowledge/summary/", data={"types": "email,document"}).json()
        self.assertEqual(summary["documents"]["eligible"], 1)

    def test_private_status_report_and_delete_tombstone(self):
        item = self.upload(private=True)
        data = self.changes(types="email,document")
        self.assertEqual(data["items"][-1]["document"]["access"]["user_ids"], [self.user.pk])
        doc = KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk)
        payload = {"source_instance_id": "test-suite", "generation": str(knowledge.get_scope().generation),
                   "items": [{"document_id": f"document:{item.pk}", "document_revision": doc.revision, "attempt_id": 1,
                              "state": "indexed", "content_version": doc.feed_version, "chunks": 2,
                              "reported_at": "2026-10-28T10:00:00Z"}]}
        r = self.api("post", "/api/v1/knowledge/status/", data=payload)
        self.assertEqual(r.json()["results"][0]["result"], "accepted")
        pk = item.pk
        knowledge_items.delete(item)
        last = self.changes(after=data["next_after"], types="email,document")["items"]
        self.assertEqual((last[-1]["document_id"], last[-1]["action"]), (f"document:{pk}", "delete"))

    def test_bad_types_refused(self):
        r = self.api("get", "/api/v1/knowledge/changes/", data={"after": 0, "types": "email,project"})
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_request"))

    def test_link_page_text_title_and_pdf(self):
        with patch("rag_assistant.knowledge_items._download",
                   return_value=("https://example.com/appnote", HTML, "text/html")):
            item = knowledge_items.add_link(self.user, "https://example.com/appnote", links={"customer": self.client_co})
        item.refresh_from_db()
        self.assertEqual(item.title, "Application note SMA")
        self.assertIn("0.56 Nm", item.text)
        self.assertEqual(item.fetch_error, "")
        doc = [i for i in self.changes(types="email,document")["items"] if i["document_type"] == "document"][-1]["document"]
        self.assertEqual((doc["kind"], doc["source_url"], doc["attachments"]), ("link", "https://example.com/appnote", []))
        self.assertIn("0.56 Nm", doc["text"])
        with patch("rag_assistant.knowledge_items._download", return_value=("https://example.com/ds.pdf", PDF, "application/pdf")):
            pdf = knowledge_items.add_link(self.user, "https://example.com/ds.pdf", title="Datenblatt")
        pdf.refresh_from_db()
        self.assertTrue(pdf.file.name.endswith("ds.pdf"))
        self.assertEqual((pdf.title, pdf.text, pdf.file_size), ("Datenblatt", "", len(PDF)))

    def test_link_to_local_network_is_refused(self):
        with patch("rag_assistant.knowledge_items._public", return_value=False):
            item = knowledge_items.add_link(self.user, "http://192.168.2.10/secret")
        item.refresh_from_db()
        self.assertIn("локальної мережі", item.fetch_error)
        self.assertEqual(item.text, "")
        self.assertFalse(KnowledgeDoc.objects.filter(doc_type="document", obj_id=item.pk, fed=True).exists())
        with self.assertRaises(knowledge_items.ItemError):
            knowledge_items.add_link(self.user, "ftp://example.com/x")


class CardDocumentsTests(ItemsMixin, EntityHelpBase):
    def setUp(self):
        super().setUp()
        self.inline_fetch()

    def post_doc(self, kind, obj, **data):
        return self.client.post(f"/rag/api/entity/{kind}/{obj.pk}/documents/", data)

    def test_add_list_toggle_and_delete_from_card(self):
        r = self.post_doc("order", self.order, file=SimpleUploadedFile("Lieferschein.txt", b"Lieferung 35 Stueck FT220407"),
                          note="vom Kunden")
        self.assertEqual(r.status_code, 200, r.content[:300])
        items = r.json()["items"]
        self.assertEqual((len(items), items[0]["kind"], items[0]["private"]), (1, "upload", False))
        item = KnowledgeItem.objects.get()
        self.assertEqual((item.order_id, item.customer_id), (self.order.pk, self.client_c.pk))   # і до клієнта
        self.assertEqual(len(self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/documents/").json()["items"]), 1)
        r = self.client.post(f"/rag/api/knowledge/{item.pk}/visibility/")
        self.assertTrue(r.json()["item"]["private"])
        page = self.client.get(f"/rag/knowledge/{item.pk}/")
        self.assertEqual((page.status_code, b"".join(page.streaming_content)), (200, b"Lieferung 35 Stueck FT220407"))
        self.assertEqual(self.client.post(f"/rag/api/knowledge/{item.pk}/delete/").json()["deleted"], True)
        self.assertFalse(KnowledgeItem.objects.exists())
        self.assertEqual(self.post_doc("order", self.order).status_code, 400)      # ні файлу, ні посилання

    def test_access_private_team_and_permissions(self):
        team = self.upload(links={"customer": self.client_c})
        private = self.upload(name="mine.pdf", private=True, links={"customer": self.client_c})
        viewer = staff("mila", "view_customer")
        self.client.force_login(viewer)
        ids = [d["id"] for d in self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/documents/").json()["items"]]
        self.assertEqual(ids, [team.pk])
        self.assertEqual(self.client.get(f"/rag/knowledge/{private.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/rag/api/knowledge/{team.pk}/delete/").status_code, 403)   # не власник
        self.client.force_login(staff("nobody", role="readonly"))
        self.assertEqual(self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/documents/").status_code, 404)
        self.assertEqual(self.client.get(f"/rag/knowledge/{team.pk}/").status_code, 404)

    def test_card_answer_uses_document_without_rag(self):
        self.upload(name="Reklamation.txt", data=b"Kratzer auf dem Gehaeuse bei 3 Filtern", ctype="text/plain",
                    links={"customer": self.client_c})
        self.ai.letter = "Скарга на подряпини [#4]."
        b = self.run_help("customer", self.client_c, "answer", question="які були скарги?", rag=False)
        doc = next(s for s in b["sources"] if s["kind"] == "document")
        self.assertIn("Kratzer", doc["text"])
        self.assertTrue(doc["url"].startswith("/rag/knowledge/"))
        stage = next(s for s in b["stages"] if s["key"] == "documents")
        self.assertEqual((stage["status"], stage["detail"]), ("done", "документів 1"))

    def test_rag_types_and_document_hits_verified(self):
        item = self.upload(links={"customer": self.client_c})
        KnowledgeActivity.objects.update_or_create(pk=1, defaults={"rag_types": ["email", "document"]})
        doc = KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk) if KnowledgeDoc.objects.filter(
            doc_type="document", obj_id=item.pk).exists() else None
        if doc is None:                                     # стрічка вимкнена в цій базі — увімкнути й передати
            from rag_assistant.models import KnowledgeSettings
            ks = KnowledgeSettings.get()
            ks.enabled = True
            ks.save()
            knowledge_items.touch([item.pk])
            doc = KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk)
        with override_settings(MINERVA_INSTANCE_ID="test-suite"):
            gen = str(knowledge.get_scope().generation)
            hit = {"source_instance_id": "test-suite", "generation": gen, "document_id": f"document:{item.pk}",
                   "document_type": "document", "document_revision": doc.revision, "content_version": doc.feed_version,
                   "chunk": 0, "score": 0.9, "text": "Messprotokoll 2,45 GHz", "subject": item.title, "sent_at": None,
                   "from_email": "", "thread_id": None, "mailbox_id": None, "customer_id": self.client_c.pk,
                   "project_ids": [], "attachment": {"index": 0, "name": "Pruefbericht.pdf", "page": 1},
                   "text_truncated": False}
            result = {"source_instance_id": "test-suite", "generation": gen, "user_id": self.user.pk, "hits": [hit]}
            kept, dropped = retrieval.verify_hits(self.user, result)
            self.assertEqual((len(kept), dropped, kept[0]["kind"], kept[0]["url"]), (1, 0, "document", f"/rag/knowledge/{item.pk}/"))
            other = staff("nobody", role="readonly")
            self.assertEqual(retrieval.verify_hits(other, dict(result, user_id=other.pk)), ([], 1))
            with patch("rag_assistant.retrieval._submit"):
                job = retrieval.start(self.user, "Prüfbericht", customer_id=self.client_c.pk)
            self.assertEqual(job.request["types"], ["email", "document"])
        from email_assistant.reply_brief import Sources, rag_sources
        job = RetrievalJob.objects.create(user=self.user, idempotency_key="minerva:test:doc", request={"query": "x"},
                                          status=RetrievalJob.COMPLETED, hits=kept)
        src = Sources()
        with patch("rag_assistant.retrieval.start", return_value=job):
            rag_sources(src, self.user, "x", scope="test")
        self.assertTrue(src.items[0]["label"].startswith("RAG · 📄 файл"))
        src = Sources()
        with patch("rag_assistant.retrieval.start", return_value=job):
            rag_sources(src, self.user, "x", scope="test", seen_documents={f"document:{item.pk}"})
        self.assertEqual(src.items, [])                      # документ уже серед джерел картки

    def test_widget_has_documents_block(self):
        r = self.client.get(f"/admin/crm/customer/{self.client_c.pk}/change/")
        self.assertContains(r, "ehw-docs")
        self.assertContains(r, "📥 Додати в базу знань")
        self.assertEqual(self.client.get("/admin/rag_assistant/knowledgeitem/").status_code, 200)
