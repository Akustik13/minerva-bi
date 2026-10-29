"""«📊 Огляд бази знань» (rag_assistant/knowledge_overview.py): що є, що в RAG, що чекає, що не передано і чому.

python manage.py test rag_assistant --settings=tabele.settings_test
"""
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from django.test import override_settings

from email_assistant.models import EmailMessage, EmailThread
from rag_assistant import knowledge_items, knowledge_overview
from rag_assistant.models import KnowledgeDoc, KnowledgeItem, KnowledgeSettings
from rag_assistant.tests_knowledge import KnowledgeBase

URL = "/admin/rag_assistant/knowledgesettings/overview/"


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class OverviewTests(KnowledgeBase):
    def setUp(self):
        super().setUp()
        ks = KnowledgeSettings.get()
        ks.categories = ["customer"]
        ks.save()
        self.ok = self.mail(subject="Angebot Antennen")
        self.spam = self.mail(subject="Gewinn")
        self.private = self.mail(subject="Gehalt")
        self.supplier = self.mail(subject="Lieferung")
        EmailMessage.objects.filter(pk__in=[self.ok.pk, self.spam.pk, self.private.pk]).update(category="customer")
        EmailMessage.objects.filter(pk=self.spam.pk).update(is_spam=True)
        EmailMessage.objects.filter(pk=self.supplier.pk).update(category="supplier")
        EmailThread.objects.filter(pk=self.private.thread_id).update(knowledge_excluded=True)
        from rag_assistant import knowledge
        knowledge.refresh_all()
        KnowledgeDoc.objects.filter(doc_type="email", obj_id=self.ok.pk).update(state="indexed",
                                                                                status_revision=F("revision"))

    def test_mail_flow_and_reasons(self):
        m = knowledge_overview.mail(knowledge_overview.knowledge.get_scope())
        self.assertEqual((m["total"], m["admitted"], m["fed"], m["indexed"]), (4, 1, 1, 1))
        reasons = {r["label"]: r["count"] for r in m["not_admitted"]}
        self.assertEqual(reasons["спам і чернетки"], 1)
        self.assertEqual(reasons["🔒 приватна переписка"], 1)
        self.assertEqual(reasons["категорія «постачальники» не вибрана"], 1)
        self.assertEqual(m["total"], m["admitted"] + sum(reasons.values()))           # кожен лист пояснено
        account = m["by_account"][0]
        self.assertEqual((account["total"], account["admitted"], account["indexed"], account["pct"]), (4, 1, 1, 100))
        self.assertEqual(m["by_category"][0]["label"], "клієнти")

        EmailThread.objects.filter(pk=self.supplier.thread_id).update(knowledge_forced=True)   # «🧠 Передати»
        m = knowledge_overview.mail(knowledge_overview.knowledge.get_scope())
        self.assertEqual(m["admitted"], 2)
        self.assertNotIn("категорія «постачальники» не вибрана", {r["label"] for r in m["not_admitted"]})

    def test_documents_kinds_and_reasons(self):
        doc = knowledge_items.add_file(self.user, SimpleUploadedFile("Pruefbericht.pdf", b"%PDF-1.4\n", "application/pdf"))
        KnowledgeItem.objects.create(kind=KnowledgeItem.DATASHEET, auto=True, title="FT-100", owner=self.user,
                                     source_key="datasheet:1")
        d = knowledge_overview.documents(knowledge_overview.knowledge.get_scope())
        self.assertEqual((d["total"], d["fed"]), (2, 1))
        self.assertEqual({k["label"]: (k["total"], k["fed"]) for k in d["kinds"]},
                         {"📄 файл": (1, 1), "📑 даташит": (1, 0)})
        self.assertIn("галочка «даташити» на «🧠 База знань» вимкнена", {r["label"] for r in d["not_fed"]})
        self.assertTrue(KnowledgeDoc.objects.filter(doc_type="document", obj_id=doc.pk, fed=True).exists())

    def test_page_and_failures_privacy(self):
        KnowledgeDoc.objects.filter(doc_type="email", obj_id=self.ok.pk).update(state="failed", error="embedding timeout")
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 302)                                           # без входу — ні
        self.client.force_login(self.user)
        r = self.client.get(URL)
        self.assertContains(r, "📊 Огляд бази знань")
        self.assertContains(r, "За скриньками")
        self.assertContains(r, "приватна переписка")
        self.assertContains(r, "Angebot Antennen")                                     # свій лист — з темою
        self.assertContains(r, "embedding timeout")
        self.assertContains(self.client.get("/admin/rag_assistant/knowledgesettings/1/change/"), URL)

        other = User.objects.create_user("kolega", "k@x.y", "p", is_staff=True)
        other.user_permissions.add(Permission.objects.get(codename="view_knowledgesettings"))
        other.profile.role = "admin"
        other.profile.save()
        self.client.force_login(other)
        r = self.client.get(URL)
        self.assertContains(r, f"лист #{self.ok.pk}")                                  # чужий лист — без теми
        self.assertNotContains(r, "Angebot Antennen")
