"""
Допомога в листі: пересланий колегою лист (справжній відправник, його листування й замовлення), склад за артикулами,
пошук за виділеним текстом, читабельні фрагменти RAG, «❓ Запитати про лист», «🧭 Що обіцяли / зробили».
python manage.py test email_assistant.tests_mail_help --settings=tabele.settings_test
"""
import json
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

from email_assistant import quotes, reply_brief
from email_assistant.context import build_context, state_lines
from email_assistant.tests_reply_brief import ReplyBriefBase

FW_BODY = (
    "fyi\n\n"
    "-------- Ursprüngliche Nachricht --------\n"
    "Von: Denys Repetynskyi Module ETP LLC <procurement10@moduletp.example>\n"
    "Datum: 28.10.26 14:23 (GMT+01:00)\n"
    "An: \"Dr. Sergey Sevskiy\" <sergey@sevskiy.de>\n"
    "Betreff: Denys SL Tire\n"
    "Good afternoon, my name is Denys, and I'm the procurement manager at SL Tire. We've purchased filters from you\n"
    "in the past, and now we'd like to place a larger order.\n\n"
    "Filter FT220407.\n"
    "Please provide pricing and production lead times for 35 units and 100 units.\n\n"
    "-- \nBest regard,\nDenys Repetynskyi - supply manager Module ETP LLC\n"
)
SIGNATURE = ("Mit freundlichen Grüßen\nDr. Sergey Sevskiy\n=====================\nSevskiy GmbH\nSchatzbogen 43\n"
             "81829 München\n")


class QuotesTests(ReplyBriefBase):
    def test_strip_signature_and_tidy(self):
        self.assertEqual(quotes.strip_signature("Bitte Preis.\n\nMit freundlichen Grüßen / Best regards\nMax"),
                         "Bitte Preis.")
        text = ("Re: Filter > Wie viele FT220407 haben wir?\n" + "> " + SIGNATURE.replace("\n", "\n> ")
                + "\n> Von: Denys <procurement10@moduletp.example>\n> 12 pcs: €19 per unit <mailto:a@b.example>")
        tidy = quotes.tidy(text)
        self.assertNotIn("Schatzbogen", tidy)
        self.assertNotIn("mailto", tidy)
        self.assertIn("Wie viele FT220407", tidy)
        self.assertIn("12 pcs: €19 per unit", tidy)
        self.assertEqual(quotes.terms_of("Denys SL Tire · FT220407 · price"), ["FT220407", "Denys", "price"])


class ForwardedOriginTests(ReplyBriefBase):
    def setUp(self):
        super().setUp()
        from inventory.models import Location, Product
        from inventory.services.stock import TX, create_movement
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.filter = Product.objects.create(sku="FT220407", name="Filter with SMA connectors")
        create_movement(product=self.filter, tx_type=TX.INCOMING, qty=14)

    def fw(self, body=FW_BODY + SIGNATURE, day=28):
        return self.mail(subject="FW: Denys SL Tire", from_email="sergey@sevskiy.de", body=body, day=day)

    def test_origin_history_orders_and_stock(self):
        from sales.models import SalesOrder
        earlier = self.mail(subject="WG: Denys", from_email="sergey@sevskiy.de", day=5,
                            body="Славик, вышли 12 FT220407 по адресу SL-TIRE B.V.\n procurement10@moduletp.example")
        SalesOrder.objects.create(source="manual", order_number="SL-0921", email="buyer@moduletp.example",
                                  order_date=date(2026, 9, 28))
        msg = self.fw()
        ctx = build_context(msg)
        self.assertEqual((ctx["origin"]["from"], ctx["origin"]["domain"]), ("procurement10@moduletp.example", "moduletp.example"))
        self.assertIn(earlier.thread_id, [t["thread"].pk for t in ctx["other_threads"]])
        self.assertEqual([o.order_number for o in ctx["origin_orders"]], ["SL-0921"])
        state = "\n".join(state_lines(ctx))
        self.assertIn("Переслано колегою: лист від Denys Repetynskyi Module ETP LLC <procurement10@moduletp.example>", state)
        self.assertIn("📦 FT220407: на складі 14, резерв 0, вільно 14 · просять 35 / 100 → бракує 21 / 86", state)
        self.assertIn("Раніше замовляв: manual:SL-0921", state)

    def test_query_is_the_forwarded_letter_not_the_colleague_signature(self):
        from rag_assistant import retrieval
        ctx = build_context(self.fw())
        q = retrieval.query_for(ctx)
        self.assertIn("Denys SL Tire", q)
        self.assertIn("35 units and 100 units", q)
        self.assertIn("FT220407", q)
        self.assertNotIn("Schatzbogen", q)
        self.assertNotIn("supply manager", q)                       # підпис Денис теж не в запиті
        self.assertIsNone(retrieval.customer_filter(ctx))
        from crm.models import Customer
        Customer.objects.create(name="Module ETP", email="sales@moduletp.example")   # клієнт лише за доменом
        ctx = build_context(self.fw(day=29))
        self.assertEqual((ctx["customer"].name, ctx["customer_from_origin"]), ("Module ETP", True))
        self.assertIsNone(retrieval.customer_filter(ctx))           # пересилання колег з ним не пов'язані

    def test_reply_to_forward_prefers_dossier_customer(self):
        from email_assistant.views import _reply_recipient
        Customer = __import__("crm.models", fromlist=["Customer"]).Customer
        msg = self.fw()
        ctx = dict(build_context(msg), customer=Customer.objects.create(name="Kunde", email="kunde@firma.example"),
                   customer_from_origin=False)                       # клієнт із замовлення в листі (як лист UPS)
        self.assertEqual(_reply_recipient(msg, ctx)[0], "kunde@firma.example")

    def test_build_query_standard_plus_wish(self):
        from rag_assistant import retrieval
        ctx = build_context(self.fw())
        self.assertEqual(retrieval.build_query(ctx, "  "), retrieval.query_for(ctx))     # без побажання — стандартний
        q = retrieval.build_query(ctx, "які були проблеми з цим фільтром?")
        self.assertTrue(q.startswith("які були проблеми з цим фільтром? · Denys SL Tire"), q)
        self.assertIn("FT220407", q)
        self.assertLess(len(q), len(retrieval.query_for(ctx)) + 60)                       # лист коротший, не заглушує

    def test_ai_reply_instruction_goes_into_rag_query(self):
        from email_assistant.models import EmailSettings
        from rag_assistant.models import RetrievalJob
        es = EmailSettings.get_for_user(self.user)
        es.rag_mode = EmailSettings.RAG_EXTENDED
        es.save()
        job = RetrievalJob(status=RetrievalJob.UNAVAILABLE, message="x")
        with patch("rag_assistant.retrieval.available", return_value=True), \
                patch("rag_assistant.retrieval.start", return_value=job) as start:
            self.client.get(f"/email/message/{self.fw().pk}/ai-reply/?prompt=" + "назви ціну на 100 шт.")
        self.assertTrue(start.call_args.args[1].startswith("назви ціну на 100 шт. · Denys SL Tire"))

    def test_reply_goes_to_original_sender(self):
        from email_assistant.views import _reply_recipient
        msg = self.fw()
        self.assertEqual(_reply_recipient(msg, build_context(msg))[0], "procurement10@moduletp.example")

    def test_client_letter_with_our_quote_is_not_a_forward(self):
        msg = self.mail(subject="AW: Angebot", body="Danke!\n\nVon: Viacheslav <viacheslav.pryimak@sevskiy.de>\n"
                                                    "Betreff: Angebot\nAnbei unser Angebot.")
        self.assertIsNone(build_context(msg)["origin"])


class SelectionSearchTests(ReplyBriefBase):
    def test_selection_is_the_whole_query_without_customer_filter(self):
        from rag_assistant.models import RetrievalJob
        msg = self.mail()
        job = RetrievalJob.objects.create(
            user=self.user, idempotency_key="minerva:test:1", request={"query": "FT220407 SL Tire"},
            status=RetrievalJob.COMPLETED,
            hits=[{"document_id": "email:1", "url": "/email/message/1/", "subject": "Re: Filter",
                   "text": "> FT220407:\n" + SIGNATURE + "> Von: x <x@y.example>\n> 14 chips in stock"}])
        with patch("rag_assistant.retrieval.start", return_value=job) as start:
            r = self.client.post(f"/email/message/{msg.pk}/rag-search/", json.dumps({"query": "  FT220407\n SL Tire "}),
                                 content_type="application/json")
        q = start.call_args.args[1]
        self.assertTrue(q.startswith("FT220407 SL Tire · Anfrage ANT-1"), q)   # своє першим, далі стандартна частина
        self.assertIsNone(start.call_args.kwargs["customer_id"])                # зі своїм запитом — у всій пошті
        self.assertEqual(r.json()["scope"], "у всій вашій пошті")
        with patch("rag_assistant.retrieval.start", return_value=job) as start:
            r = self.client.post(f"/email/message/{msg.pk}/rag-search/", json.dumps({}), content_type="application/json")
        self.assertTrue(start.call_args.args[1].startswith("Anfrage ANT-1"))     # нічого не вказали — лише стандартна
        self.assertEqual(start.call_args.kwargs["customer_id"], self.client_c.pk)
        self.assertEqual(r.json()["scope"], f"по клієнту {self.client_c.name}")
        hit = r.json()["hits"][0]
        self.assertIn("14 chips in stock", hit["snippet"])
        self.assertNotIn("Schatzbogen", hit["snippet"])
        self.assertEqual(r.json()["terms"][0], "FT220407")


class AskAndReviewTests(ReplyBriefBase):
    def test_answer_mode_answers_with_sources_and_stays_apart_from_reply(self):
        msg = self.mail(body="Bitte Preis für 500 Stück ANT-1.")
        self.ai.letter = "Склад: вільно 0 [#1]. Раніше проблем не було [#9]."   # підміна AI: не-бриф промпти → letter
        r = self.client.post(f"/email/message/{msg.pk}/ai-ask/", json.dumps({"question": "чого не вистачає?"}),
                             content_type="application/json")
        b = self.client.get(f"/email/brief/{r.json()['id']}/").json()
        self.assertEqual((b["status"], b["mode"], b["wish"]), ("done", "answer", "чого не вистачає?"))
        self.assertIn("вільно 0 [#1]", b["brief"])
        self.assertEqual({s["key"] for s in b["stages"]} & {"brief", "letter", "review"}, set())
        self.assertIn("ЗАПИТАННЯ МЕНЕДЖЕРА: чого не вистачає?", self.ai.calls[-1]["prompt"])
        self.assertIsNone(self.client.get(f"/email/message/{msg.pk}/brief/").json()["brief"])   # не підміняє бриф листа
        self.assertEqual(self.client.get(f"/email/message/{msg.pk}/brief/?mode=answer").json()["brief"]["id"], b["id"])
        self.assertEqual(self.client.post(f"/email/message/{msg.pk}/ai-ask/", json.dumps({"question": " "}),
                                          content_type="application/json").status_code, 400)

    def test_review_is_validated_and_refs_point_to_sources(self):
        msg = self.mail(body="Bitte liefern Sie bis 30.10.")
        self.ai.letter = json.dumps({
            "summary": "Клієнт чекає поставку.",
            "promises": [{"text": "Відправити 12 шт.", "by": "we", "date": "2026-10-30", "ref": "#1", "status": "open",
                          "evidence": "лист клієнта"}, {"text": "", "ref": "#1"}],
            "done": [], "open_questions": [{"text": "Ціна на 100 шт.?", "asked_by": "customer", "ref": "#99"}],
            "next_action": {"text": "Підтвердити строк", "due": None, "why": "клієнт питає"}})
        r = self.client.post(f"/email/message/{msg.pk}/ai-review/", json.dumps({}), content_type="application/json")
        b = self.client.get(f"/email/brief/{r.json()['id']}/").json()
        self.assertEqual((b["status"], b["mode"]), ("done", "review"))
        p = b["review"]["promises"]
        self.assertEqual(len(p), 1)                                           # порожній пункт відкинуто
        self.assertEqual((p[0]["status_icon"], p[0]["who"], p[0]["ref"]["url"]), ("⏳", "ми", f"/email/message/{msg.pk}/"))
        self.assertEqual(b["review"]["open_questions"][0]["ref"], {"label": "", "url": ""})   # #99 — немає джерела
        self.assertEqual(b["review"]["next_action"]["text"], "Підтвердити строк")
        self.assertEqual(self.client.get(f"/email/message/{msg.pk}/brief/?mode=review").json()["brief"]["id"], b["id"])

    def test_answer_searches_rag_in_all_mail_with_question(self):
        msg = self.mail()
        job = SimpleNamespace(status="completed", COMPLETED="completed", is_terminal=True, hits=[], message="", poll_after_ms=0)
        with patch("rag_assistant.retrieval.available", return_value=True), \
                patch("rag_assistant.retrieval.start", return_value=job) as start:
            self.client.post(f"/email/message/{msg.pk}/ai-ask/", json.dumps({"question": "які були проблеми з фільтром?"}),
                             content_type="application/json")
        kw = start.call_args.kwargs
        self.assertEqual((kw["customer_id"], kw["project_id"]), (None, None))
        self.assertTrue(start.call_args.args[1].startswith("які були проблеми з фільтром?"))


class StockLineTests(ReplyBriefBase):
    def test_enough_and_unknown_quantity(self):
        from email_assistant.context import stock_line
        item = {"sku": "ANT-1", "on_hand": D(50), "reserved": D(5), "available": D(45), "incoming": D(10), "requested": [D(20)]}
        self.assertEqual(stock_line(item), "📦 ANT-1: на складі 50, резерв 5, вільно 45, очікується 10 · просять 20 → вистачає")
        self.assertEqual(stock_line(dict(item, requested=[])), "📦 ANT-1: на складі 50, резерв 5, вільно 45, очікується 10")
        self.assertTrue(reply_brief.REPLY_MODES)
