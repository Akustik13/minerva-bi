"""Стан кожного вкладення в RAG: звіт RAG зберігається (KnowledgeDoc.attachments) і показується в пошті.

python manage.py test rag_assistant --settings=tabele.settings_test
"""
import json
import os

from django.test import override_settings

from rag_assistant import knowledge
from rag_assistant.models import KnowledgeDoc
from rag_assistant.tests_knowledge import KnowledgeBase


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class AttachmentStatesTests(KnowledgeBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def stored(self, name, data=b"%PDF-1.4 synthetic"):
        os.makedirs(os.path.join(self.media, "email_attachments"), exist_ok=True)
        with open(os.path.join(self.media, "email_attachments", name), "wb") as fh:
            fh.write(data)
        return {"name": name, "content_type": "application/pdf", "size": len(data),
                "file_path": f"email_attachments/{name}"}

    def report(self, msg, attachments, state="indexed"):
        doc = KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk)
        item = {"document_id": f"email:{msg.pk}", "document_revision": doc.revision, "attempt_id": doc.attempt_id + 1,
                "state": state, "content_version": doc.feed_version, "reported_at": "2026-10-28T10:00:00Z"}
        if state == "indexed":
            item.update(chunks=3, attachments=attachments)
        r = self.api("post", "/api/v1/knowledge/status/", data={
            "source_instance_id": "test-suite", "generation": str(knowledge.get_scope().generation), "items": [item]})
        self.assertEqual(r.json()["results"][0]["result"], "accepted", r.content[:300])

    def states(self, msg):
        return self.client.get(f"/email/message/{msg.pk}/knowledge/").json()["attachments"][str(msg.pk)]

    def test_report_is_stored_and_shown_per_attachment(self):
        atts = [self.stored("Spec.pdf"), self.stored("Scan.pdf"),
                {"name": "photo.jpg", "content_type": "image/jpeg", "size": 10},           # тип не індексується
                {"name": "remote.pdf", "content_type": "application/pdf", "size": 10}]     # лише на сервері
        msg = self.mail(attachments=atts)
        s = self.states(msg)
        self.assertEqual((s["0"]["state"], s["1"]["state"]), ("wait", "wait"))            # RAG ще не звітував
        self.assertEqual((s["2"]["state"], s["3"]["state"]), ("skip", "skip"))
        self.assertIn("тип файлу", s["2"]["label"])
        self.assertIn("не збережено", s["3"]["label"])
        self.report(msg, [{"index": 0, "state": "indexed"},
                          {"index": 1, "state": "skipped", "reason": "no_extractable_text"}])
        doc = KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk)
        self.assertEqual(doc.attachments[1], {"index": 1, "state": "skipped", "reason": "no_extractable_text"})
        s = self.states(msg)
        self.assertEqual((s["0"]["state"], s["1"]["state"]), ("in", "skip"))
        self.assertIn("скан", s["1"]["label"])

    def test_indexed_before_reports_is_unknown_and_new_revision_waits(self):
        msg = self.mail(attachments=[self.stored("Spec.pdf")])
        self.report(msg, None)                                       # старий звіт без вкладень
        doc = KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk)
        KnowledgeDoc.objects.filter(pk=doc.pk).update(attachments=None)
        self.assertEqual(self.states(msg)["0"]["state"], "unknown")
        knowledge.resend("email", [msg.pk])                          # нова ревізія — чекає звіту RAG
        self.assertEqual(self.states(msg)["0"]["state"], "wait")

    def test_preview_has_attachment_slots(self):
        msg = self.mail(attachments=[self.stored("Spec.pdf")])
        page = self.client.get(f"/email/message/{msg.pk}/preview/").content.decode()
        self.assertIn(f'data-kb-msg="{msg.pk}" data-kb-idx="0"', page)
        self.assertIn('class="em-att-kb"', page)

    def test_bad_attachment_report_is_ignored(self):
        msg = self.mail(attachments=[self.stored("Spec.pdf")])
        self.report(msg, [{"index": "x", "state": "indexed"}, {"index": 0, "state": "weird"}])
        self.assertEqual(KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk).attachments, [])
        self.assertEqual(json.dumps(self.states(msg)["0"]["state"]), '"wait"')   # звіт є, про це вкладення — ні
