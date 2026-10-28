"""Позначки бази знань у пошті: що передано в RAG, що ні й чому; «🧠 Передати в базу знань», «🔄 Передати ще раз».

python manage.py test rag_assistant --settings=tabele.settings_test
"""
import json

from django.test import override_settings

from email_assistant.models import EmailThread
from rag_assistant import knowledge, knowledge_items
from rag_assistant.models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings
from rag_assistant.tests_knowledge import KnowledgeBase


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class MailMarksTests(KnowledgeBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def state(self, msg):
        r = self.client.get(f"/email/message/{msg.pk}/knowledge/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        return r.json()

    def act(self, msg, action):
        return self.client.post(f"/email/message/{msg.pk}/knowledge/", json.dumps({"action": action}),
                                content_type="application/json")

    def indexed(self, msg, state=KnowledgeDoc.INDEXED):
        doc = KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk)
        KnowledgeDoc.objects.filter(pk=doc.pk).update(state=state, status_revision=doc.revision)

    def test_newsletter_out_of_rules_then_sent_explicitly(self):
        news = self.mail("Weekly deals", from_email="news@shop.example", list_unsubscribe=True)
        s = self.state(news)
        self.assertEqual((s["status"], s["can_send"]), ("out", True))
        self.assertIn("розсилки", s["text"])
        r = self.act(news, "send")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["status"], "wait")                 # передано, RAG ще не обробив
        self.assertTrue(EmailThread.objects.get(pk=news.thread_id).knowledge_forced)
        self.assertTrue(KnowledgeDoc.objects.get(doc_type="email", obj_id=news.pk).fed)
        self.indexed(news)
        self.assertEqual(self.state(news)["status"], "in")
        knowledge.refresh_all()                                      # нічна звірка не прибирає примусово передане
        self.assertTrue(KnowledgeDoc.objects.get(doc_type="email", obj_id=news.pk).fed)

    def test_private_wins_over_forced(self):
        news = self.mail("Weekly deals", from_email="news@shop.example", list_unsubscribe=True)
        self.act(news, "send")
        self.client.post(f"/email/thread/{news.thread_id}/knowledge/", json.dumps({"excluded": True}),
                         content_type="application/json")
        thread = EmailThread.objects.get(pk=news.thread_id)
        self.assertEqual((thread.knowledge_excluded, thread.knowledge_forced), (True, False))
        self.assertFalse(KnowledgeDoc.objects.get(doc_type="email", obj_id=news.pk).fed)
        self.assertIn("приватна", self.state(news)["text"])

    def test_failed_is_resent_as_new_revision(self):
        msg = self.mail()
        self.indexed(msg, KnowledgeDoc.FAILED)
        s = self.state(msg)
        self.assertEqual((s["status"], s["counts"]["fail"]), ("fail", 1))
        before = KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk).revision
        last = KnowledgeChange.objects.latest("id").id
        r = self.act(msg, "resend")
        self.assertEqual(r.json()["status"], "wait")
        self.assertEqual(KnowledgeDoc.objects.get(doc_type="email", obj_id=msg.pk).revision, before + 1)
        self.assertTrue(KnowledgeChange.objects.filter(id__gt=last, obj_id=msg.pk, action="upsert").exists())

    def test_mailbox_not_selected_cannot_be_forced_and_disabled_refuses(self):
        from email_assistant.models import EmailAccount
        msg = self.mail()
        other = EmailAccount.objects.create(user=self.user, email_address="info@sevskiy.de", imap_username="info@sevskiy.de",
                                            imap_password="x", smtp_username="info@sevskiy.de", smtp_password="x")
        ks = KnowledgeSettings.get()
        ks.accounts.set([other])
        ks.save()
        knowledge.touch_messages([msg.pk])
        s = self.state(msg)
        self.assertEqual((s["status"], s["can_send"]), ("out", False))
        self.assertIn("скринька не вибрана", s["text"])
        ks.accounts.clear()
        ks.enabled = False
        ks.save()
        r = self.act(msg, "send")
        self.assertEqual(r.status_code, 400)

    def test_inbox_rows_show_kb_tags(self):
        a = self.mail("Antenna quote")
        b = self.mail("Cable order", from_email="other@client.example")
        self.indexed(a)
        self.indexed(b, KnowledgeDoc.FAILED)
        page = self.client.get("/email/").content.decode()
        self.assertIn("У базі знань RAG — знаходиться пошуком", page)
        self.assertIn("⚠️🧠", page)
        self.assertIn("🧠 Передати в базу знань", page)             # пункт меню правого кліку

    def test_document_resend_from_card(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rag_assistant.models import KnowledgeActivity
        KnowledgeActivity.objects.update_or_create(pk=1, defaults={"rag_types": ["email", "document"]})
        item = knowledge_items.add_file(self.user, SimpleUploadedFile("spec.txt", b"spec"),
                                        links={"customer": self.client_co})
        doc = KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk)
        r = self.client.post(f"/rag/api/knowledge/{item.pk}/resend/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(KnowledgeDoc.objects.get(pk=doc.pk).revision, doc.revision + 1)
        self.assertTrue(r.json()["item"]["can_resend"])
