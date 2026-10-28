"""Етап 3 — автоматичні документи бази знань (knowledge_auto): Angebote, документи замовлень, даташити.

python manage.py test rag_assistant --settings=tabele.settings_test
"""
import os
from datetime import date
from decimal import Decimal as D

from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone

from rag_assistant import knowledge, knowledge_auto, knowledge_items
from rag_assistant.models import KnowledgeItem, KnowledgeSettings
from rag_assistant.tests_entity_help import EntityHelpBase

PDF = b"%PDF-1.4\n% synthetic offer\n"


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class AutoDocumentsTests(EntityHelpBase):
    def setUp(self):
        super().setUp()
        self.ks = KnowledgeSettings.get()
        self.ks.enabled = True
        self.ks.save()

    def toggle(self, **values):
        KnowledgeSettings.objects.filter(pk=1).update(**values)
        return knowledge_auto.sync_all()

    def sent_offer(self):
        from offers.models import OfferRevision
        from offers.services import add_line, new_offer, snapshot
        from projects.services import create_project
        project = create_project(name="Filter 2026", customer=self.client_c)
        offer = new_offer(customer=self.client_c, user=self.user, project=project)
        add_line(offer, sku="FT220407", qty=35, unit_price="18.90", lead_time="3 Wochen")
        offer.refresh_from_db()
        rev = OfferRevision(offer=offer, revision=1, snapshot=snapshot(offer), sent_at=timezone.now())
        rev.pdf_file.save("Angebot.pdf", ContentFile(PDF), save=False)
        rev.save()
        return offer, rev, project

    def feed_documents(self):
        data = knowledge.read_changes(types=("email", "document"))
        return {i["document_id"]: i for i in data["items"] if i["document_type"] == "document"}

    def test_off_by_default_nothing_added(self):
        self.sent_offer()
        self.assertEqual(knowledge_auto.sync_all()["offer"], {"created": 0, "updated": 0, "removed": 0})
        self.assertFalse(KnowledgeItem.objects.exists())

    def test_sent_offer_becomes_document_and_goes_away_when_switched_off(self):
        offer, rev, project = self.sent_offer()
        result = self.toggle(auto_offers=True)
        self.assertEqual(result["offer"]["created"], 1)
        item = KnowledgeItem.objects.get(auto=True, kind="offer")
        self.assertEqual((item.file.name, item.offer_id, item.customer_id, item.project_id),
                         (rev.pdf_file.name, offer.pk, self.client_c.pk, project.pk))
        self.assertIn("FT220407 × 35", item.text)
        self.assertEqual(knowledge_auto.sync_all()["offer"], {"created": 0, "updated": 0, "removed": 0})   # без змін
        doc = self.feed_documents()[f"document:{item.pk}"]["document"]
        self.assertEqual((doc["kind"], doc["attachments"][0]["indexable"]), ("offer", True))
        self.assertIn("3 Wochen", knowledge_items.local_text(item))
        r = self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/documents/").json()["items"]
        self.assertEqual((r[0]["kind"], r[0]["auto"], r[0]["can_change"]), ("offer", True, False))
        self.assertEqual(self.client.post(f"/rag/api/knowledge/{item.pk}/delete/").status_code, 403)
        self.assertEqual(self.toggle(auto_offers=False)["offer"]["removed"], 1)
        self.assertFalse(KnowledgeItem.objects.exists())
        self.assertTrue(os.path.isfile(rev.pdf_file.path))                      # файл Angebot лишається
        self.assertEqual(self.feed_documents()[f"document:{item.pk}"]["action"], "delete")

    def test_sending_a_new_version_adds_it_at_once(self):
        from offers.models import OfferRevision
        offer, rev, _ = self.sent_offer()
        self.toggle(auto_offers=True)
        with self.captureOnCommitCallbacks(execute=True):
            OfferRevision.objects.create(offer=offer, revision=2, snapshot=rev.snapshot, sent_at=timezone.now())
        self.assertEqual(KnowledgeItem.objects.filter(auto=True, kind="offer").count(), 2)

    def test_order_folder_files(self):
        from email_assistant.attachments import order_dir
        from sales.models import SalesOrder
        folder = order_dir(self.order)
        os.makedirs(folder, exist_ok=True)
        for name, data in (("Rechnung.pdf", PDF), ("Foto.png", b"\x89PNG"), ("Packliste.txt", b"35 x FT220407")):
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(data)
        old = SalesOrder.objects.create(source="manual", order_number="OLD-1", order_date=date(2025, 1, 5))
        os.makedirs(order_dir(old), exist_ok=True)
        with open(os.path.join(order_dir(old), "alt.pdf"), "wb") as fh:
            fh.write(PDF)
        self.ks.index_from = date(2026, 1, 1)
        self.ks.save()
        result = self.toggle(auto_order_files=True)
        items = list(KnowledgeItem.objects.filter(auto=True, kind="order_file").order_by("title"))
        self.assertEqual([i.file_name for i in items], ["Packliste.txt", "Rechnung.pdf"])   # png і старе замовлення — ні
        self.assertEqual(result["order_file"]["created"], 2)
        self.assertEqual({(i.order_id, i.customer_id) for i in items}, {(self.order.pk, self.client_c.pk)})
        os.remove(os.path.join(folder, "Rechnung.pdf"))
        self.assertEqual(knowledge_auto.sync_all()["order_file"]["removed"], 1)
        self.ai.letter = "ok"
        b = self.run_help("order", self.order, "answer", question="що в пакувальному листі?", rag=False)
        doc = next(s for s in b["sources"] if s["kind"] == "document")
        self.assertIn("35 x FT220407", doc["text"])

    def test_product_datasheet(self):
        self.filter.datasheet_file.save("FT220407.pdf", ContentFile(PDF), save=False)
        self.filter.save()
        self.toggle(auto_datasheets=True)
        item = KnowledgeItem.objects.get(auto=True, kind="datasheet")
        self.assertEqual((item.product_id, item.file.name), (self.filter.pk, self.filter.datasheet_file.name))
        with self.captureOnCommitCallbacks(execute=True):
            self.filter.datasheet_file.save("FT220407-v2.pdf", ContentFile(PDF + b"v2"), save=False)
            self.filter.save()
        item.refresh_from_db()
        self.assertTrue(item.file.name.endswith("FT220407-v2.pdf"))
        self.assertEqual(item.file_size, len(PDF) + 2)
        knowledge_items.delete(item)                                          # адміністратор видалив запис
        self.assertTrue(os.path.isfile(self.filter.datasheet_file.path))      # даташит товару лишається
