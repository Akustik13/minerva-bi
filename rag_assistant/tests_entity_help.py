"""«🧭 Огляд» і «❓ Запитати» в картках проєкту, клієнта, замовлення й товару (rag_assistant/entity_help.py).

python manage.py test rag_assistant --settings=tabele.settings_test   (AI і RAG підмінено, без мережі)
"""
import json
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import override_settings
from django.utils import timezone

from email_assistant.tests_reply_brief import ReplyBriefBase
from rag_assistant import entity_help
from rag_assistant.models import EntityHelp, RetrievalJob


class EntityHelpBase(ReplyBriefBase):
    def setUp(self):
        super().setUp()
        from inventory.models import Location, Product, PurchaseOrder, PurchaseOrderLine, Supplier
        from inventory.services.stock import TX, create_movement
        from sales.models import SalesOrder, SalesOrderLine
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.filter = Product.objects.create(sku="FT220407", name="Filter with SMA connectors", sale_price=D("19.50"))
        create_movement(product=self.filter, tx_type=TX.INCOMING, qty=14)
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.get(name="Polecoz"), status="ordered",
                                          expected_date=date(2026, 11, 20))
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.filter, qty_ordered=100, qty_received=0)
        self.order = SalesOrder.objects.create(source="manual", order_number="SL-0921", email="buyer@client.com",
                                               client="Client Co", order_date=date(2026, 9, 28),
                                               tracking_number="1Z999AA10123456784")
        SalesOrderLine.objects.create(order=self.order, product=self.filter, qty=D("35"), total_price=D("682.50"),
                                      currency="EUR")
        self.hits = []
        self.rag = []   # виклики retrieval.start

    def rag_job(self, *args, **kwargs):
        self.rag.append((args, kwargs))
        return RetrievalJob.objects.create(user=self.user, idempotency_key=f"minerva:test:{len(self.rag)}",
                                           request={"query": args[1]}, status=RetrievalJob.COMPLETED, hits=self.hits)

    def run_help(self, kind, obj, mode, question="", rag=True):
        with patch("rag_assistant.retrieval.available", return_value=rag), \
                patch("rag_assistant.retrieval.start", side_effect=self.rag_job):
            r = self.client.post(f"/rag/api/entity/{kind}/{obj.pk}/{mode}/", json.dumps({"question": question}),
                                 content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return self.client.get(f"/rag/api/entity/help/{r.json()['id']}/").json()

    def prompt(self):
        return self.ai.calls[-1]["prompt"]


class CustomerTests(EntityHelpBase):
    def test_review_gathers_card_orders_notes_mail_and_rag_by_customer(self):
        from crm.models import CustomerNote
        CustomerNote.objects.create(customer=self.client_c, subject="Скарга", body="Фільтри прийшли з подряпинами")
        mail = self.mail(subject="Lieferzeit FT220407", body="Wann kommt die Ware?")
        self.hits = [{"document_id": "email:999", "message_pk": 999, "thread_id": 777, "url": "/email/message/999/",
                      "subject": "Reklamation", "from_email": "buyer@client.com", "sent_at": "2026-05-02",
                      "text": "Kratzer auf dem Gehäuse"}]
        self.ai.letter = json.dumps({
            "summary": "Клієнт чекає поставку.",
            "promises": [{"text": "Відправити 35 шт.", "by": "we", "date": "2026-11-01", "ref": "#3", "status": "open"}],
            "done": [], "open_questions": [], "next_action": {"text": "Підтвердити строк", "due": None, "why": "питає"}})
        b = self.run_help("customer", self.client_c, "review")
        self.assertEqual((b["status"], b["mode"]), ("done", "review"), b)
        kinds = [s["kind"] for s in b["sources"]]
        self.assertEqual(kinds[:2], ["minerva", "minerva"])            # картка CRM, замовлення
        self.assertIn("mail", kinds)
        self.assertIn("rag", kinds)
        text = "\n".join(s["text"] for s in b["sources"])
        self.assertIn("FT220407 × 35", text)
        self.assertIn("подряпинами", text)
        self.assertIn("Kratzer", text)
        mail_src = next(s for s in b["sources"] if s["kind"] == "mail")
        self.assertEqual(mail_src["url"], f"/email/message/{mail.pk}/")
        self.assertEqual(b["review"]["promises"][0]["ref"]["label"], "#3")
        kw = self.rag[-1][1]
        self.assertEqual((kw["customer_id"], kw["purpose"]), (self.client_c.pk, "entity"))
        self.assertIn(mail.thread_id, kw["exclude_thread_ids"])         # листи вже в джерелах — RAG їх не повторює
        self.assertIn("Огляд для: картку клієнта", self.prompt())
        self.assertEqual(EntityHelp.objects.get(pk=b["id"]).user, self.user)

    def test_latest_and_stale(self):
        self.ai.letter = json.dumps({"summary": "ok", "promises": [], "done": [], "open_questions": []})
        self.assertTrue(self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/latest/?mode=review").json()["none"])
        b = self.run_help("customer", self.client_c, "review", rag=False)
        latest = self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/latest/?mode=review").json()
        self.assertEqual((latest["id"], latest["stale"]), (b["id"], False))
        self.order.status = "shipped"
        self.order.save()
        self.assertTrue(self.client.get(f"/rag/api/entity/customer/{self.client_c.pk}/latest/?mode=review").json()["stale"])
        rag_stage = next(s for s in b["stages"] if s["key"] == "rag")
        self.assertEqual((rag_stage["status"], rag_stage["detail"]), ("skipped", "RAG не налаштований"))


class OrderTests(EntityHelpBase):
    def test_answer_shows_shortage_mail_with_number_and_rag_in_all_mail(self):
        mail = self.mail(subject="Re: Bestellung", body="Danke!\n\n> Ihre Bestellung SL-0921 ist unterwegs.")
        self.ai.letter = "Бракує 21 шт. [#1]."
        b = self.run_help("order", self.order, "answer", question="чого не вистачає?")
        self.assertEqual((b["status"], b["answer"], b["question"]), ("done", "Бракує 21 шт. [#1].", "чого не вистачає?"))
        card = b["sources"][0]["text"]
        self.assertIn("FT220407 × 35", card)
        self.assertIn("вільно -21", card)                                # замовлення вже списало 35 із 14
        self.assertIn("очікується 100", card)
        self.assertIn("під це замовлення вже враховано 35 → бракує 21", card)
        mail_src = next(s for s in b["sources"] if s["kind"] == "mail")
        self.assertEqual(mail_src["url"], f"/email/message/{mail.pk}/")
        self.assertIn("SL-0921 ist unterwegs", mail_src["text"])       # згадка лише в цитаті — фрагмент навколо неї
        args, kw = self.rag[-1]
        self.assertIsNone(kw["customer_id"])                            # замовлення — уся пошта
        self.assertTrue(args[1].startswith("чого не вистачає? · SL-0921 · 1Z999AA10123456784"), args[1])
        self.assertIn("ЗАПИТАННЯ МЕНЕДЖЕРА: чого не вистачає?", self.prompt())
        self.assertIn("Менеджер дивиться картку замовлення", self.prompt())

    def test_empty_question_and_project_review_are_refused(self):
        r = self.client.post(f"/rag/api/entity/order/{self.order.pk}/answer/", json.dumps({"question": " "}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        from projects.services import create_project
        project = create_project(name="Antennen", customer=self.client_c)
        r = self.client.post(f"/rag/api/entity/project/{project.pk}/review/", json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post(f"/rag/api/entity/order/{self.order.pk}/nope/", "{}",
                                          content_type="application/json").status_code, 404)
        self.assertEqual(self.client.post("/rag/api/entity/unknown/1/answer/", "{}",
                                          content_type="application/json").status_code, 404)


class ProductTests(EntityHelpBase):
    def test_answer_has_stock_purchases_sales_and_mail_with_sku(self):
        self.mail(subject="FT220407 broken", body="Two FT220407 arrived broken.")
        self.mail(subject="Other", body="Nothing about filters.")
        self.ai.letter = "Скарга на пошкодження [#5]."
        b = self.run_help("product", self.filter, "answer", question="які були проблеми?")
        labels = [s["label"] for s in b["sources"]]
        self.assertIn("Очікується (відкриті закупівлі)", labels)
        self.assertIn("Останні продажі", labels)
        mails = [s for s in b["sources"] if s["kind"] == "mail"]
        self.assertEqual(len(mails), 1)
        self.assertIn("arrived broken", mails[0]["text"])
        self.assertTrue(self.rag[-1][0][1].startswith("які були проблеми? · FT220407 · Filter with SMA connectors"))

    def test_build_query_without_question_is_standard_part(self):
        self.assertEqual(entity_help.build_query("product", self.filter), "FT220407 · Filter with SMA connectors")
        self.assertEqual(entity_help.build_query("product", self.filter, "  ціна \n"),
                         "ціна · FT220407 · Filter with SMA connectors")


class AccessTests(EntityHelpBase):
    def test_permissions_and_owner_only(self):
        self.ai.letter = "ok"
        b = self.run_help("product", self.filter, "answer", question="?", rag=False)
        other = User.objects.create_user("mila", "m@x.y", "p", is_staff=True)
        other.profile.role = "readonly"                     # роль без складу й продажів (лише дашборд і FAQ)
        other.profile.save()
        self.client.force_login(other)
        self.assertEqual(self.client.get(f"/rag/api/entity/help/{b['id']}/").status_code, 404)        # чужий запис
        self.assertEqual(self.client.get(f"/rag/api/entity/product/{self.filter.pk}/latest/").status_code, 404)   # без права
        other.user_permissions.add(Permission.objects.get(codename="view_product"))
        other = User.objects.get(pk=other.pk)
        self.client.force_login(other)
        self.assertTrue(self.client.get(f"/rag/api/entity/product/{self.filter.pk}/latest/?mode=answer").json()["none"])
        self.assertEqual(self.client.get(f"/rag/api/entity/order/{self.order.pk}/latest/").status_code, 404)

    def test_mail_only_from_own_mailboxes(self):
        from email_assistant.models import EmailAccount, EmailMessage
        other = User.objects.create_user("mila", "m@x.y", "p", is_staff=True)
        acc = EmailAccount.objects.create(user=other, email_address="mila@sevskiy.de", imap_username="mila@sevskiy.de",
                                          imap_password="x", smtp_username="mila@sevskiy.de", smtp_password="x")
        EmailMessage.objects.create(account=acc, folder="inbox", imap_folder_name="INBOX", imap_uid=1, to_emails=[],
                                    cc_emails=[], from_email="x@y.example", subject="FT220407 secret", body_text="secret",
                                    sent_at=timezone.now())
        self.ai.letter = "ok"
        b = self.run_help("product", self.filter, "answer", question="?", rag=False)
        self.assertNotIn("secret", json.dumps(b["sources"]))


class ProjectTests(EntityHelpBase):
    def test_overview_generate_adds_rag_with_continued_refs(self):
        from projects import overview
        from projects.services import create_project
        msg = self.mail(subject="Projekt Antennen", body="Bitte 35 FT220407 bis Ende November.")
        project = create_project(name="Antennen 2026", customer=self.client_c, threads=[msg.thread])
        self.hits = [{"document_id": "email:999", "message_pk": 999, "thread_id": 777, "url": "/email/message/999/",
                      "subject": "Rahmenvertrag", "from_email": "buyer@client.com", "sent_at": "2026-03-02",
                      "text": "Rahmenvertrag 500 Stück"}]
        self.ai.letter = json.dumps({"summary": "s", "promises": [], "open_questions": [],
                                     "done": [{"text": "Rahmenvertrag", "ref": "#2"}, {"text": "Anfrage", "ref": "#1"}]})
        with patch("rag_assistant.retrieval.available", return_value=True), \
                patch("rag_assistant.retrieval.start", side_effect=self.rag_job):
            ov, error = overview.generate(project, user=self.user)
        self.assertEqual(error, "")
        prompt = self.prompt()
        self.assertIn("ЗНАЙДЕНО В БАЗІ ЗНАНЬ", prompt)
        self.assertIn("[#2] RAG ·", prompt)
        done = ov.data["done"]
        self.assertEqual(done[0]["ref"], {"label": "#2", "url": "/email/message/999/"})
        self.assertEqual(done[1]["ref"]["url"], f"/email/message/{msg.pk}/")
        kw = self.rag[-1][1]
        self.assertEqual((kw["customer_id"], kw["purpose"]), (self.client_c.pk, "overview"))
        self.assertIn(msg.thread_id, kw["exclude_thread_ids"])

    def test_overview_without_rag_and_ask_about_project(self):
        from projects import overview
        from projects.services import create_project
        msg = self.mail(subject="Projekt Antennen", body="Bitte Muster schicken.")
        project = create_project(name="Antennen 2026", customer=self.client_c, threads=[msg.thread])
        self.ai.letter = json.dumps({"summary": "s", "promises": [], "done": [], "open_questions": []})
        ov, error = overview.generate(project, user=self.user)     # RAG не налаштований — огляд без нього
        self.assertEqual(error, "")
        self.assertNotIn("ЗНАЙДЕНО В БАЗІ ЗНАНЬ", self.prompt())
        self.ai.letter = "Зразки ще не надіслали [#4]."
        b = self.run_help("project", project, "answer", question="що ще не надіслали?", rag=False)
        self.assertEqual(b["status"], "done", b)
        self.assertIn(project.code, b["sources"][0]["text"])
        self.assertTrue(any(s["kind"] == "overview" for s in b["sources"]))
        self.assertTrue(any(s["kind"] == "mail" and s["url"] == f"/email/message/{msg.pk}/" for s in b["sources"]))


class PagesTests(EntityHelpBase):
    def test_widget_on_cards(self):
        from pathlib import Path
        media = override_settings(MEDIA_ROOT=Path(self.media))   # документи замовлення: MEDIA_ROOT / 'orders'
        media.enable()
        self.addCleanup(media.disable)
        from projects.services import create_project
        project = create_project(name="Antennen 2026", customer=self.client_c)
        pages = {"customer": f"/admin/crm/customer/{self.client_c.pk}/change/",
                 "order": f"/admin/sales/salesorder/{self.order.pk}/change/",
                 "product": f"/admin/inventory/product/{self.filter.pk}/change/",
                 "project": f"/admin/projects/project/{project.pk}/change/"}
        objs = {"customer": self.client_c, "order": self.order, "product": self.filter, "project": project}
        for kind, url in pages.items():
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, (kind, r.status_code))
            self.assertContains(r, f'id="ehw-{kind}-{objs[kind].pk}"')
        self.assertNotContains(self.client.get(pages["project"]), "ehw-review\">")   # проєкт — лише «❓ Запитати»
        self.assertNotContains(self.client.get("/admin/crm/customer/add/"), 'class="ehw"')
