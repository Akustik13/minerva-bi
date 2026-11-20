"""📊 Статистика запиту, 💶 сума постачальника, 💬 листування з одним постачальником (🧭 Коротко, відповідь у гілку),
↪ пересилання вибраних колезі, 📨 запит у прев'ю листа з прив'язаної гілки.

python manage.py test suppliers.tests_rfq_talk --settings=tabele.settings_test
"""
import datetime
import json
from decimal import Decimal
from unittest import mock

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from email_assistant.models import EmailMessage, EmailThread
from inventory.models import Supplier
from suppliers.models import SupplierEvent, SupplierReplyFile, SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import ai, rfq_compare, rfq_stats, rfq_talk
from suppliers.tests import ME, Base


class TalkBase(Base):
    def setUp(self):
        super().setUp()
        self.t0 = timezone.now() - datetime.timedelta(days=5)
        self.req = SupplierRequest.objects.create(
            title="PCBA TM270402-04H", template="pcba", quantities="5; 2500", language="en", owner=self.user,
            account=self.account, status=SupplierRequest.ANSWERED, sent_at=self.t0, body="{greeting}\n\nPlease quote.")
        self.jy = Supplier.objects.create(name="JY Electronics", email="charles@jypcba.com")
        self.th = EmailThread.objects.create(account=self.account, subject="RFQ")
        self.sent = self.mail(frm=ME, subject=f"[{self.req.code}] RFQ", folder="sent", thread=self.th, when=self.t0,
                              to=("charles@jypcba.com",), body="Please quote PCBA.")
        self.r = R.objects.create(
            request=self.req, supplier=self.jy, email="charles@jypcba.com", state=R.QUOTED, thread=self.th,
            sent_at=self.t0, replied_at=self.t0 + datetime.timedelta(hours=3), sent_message=self.sent, currency="EUR",
            includes=["assembly"], tiers=[{"qty": 5, "unit_price": "130"}, {"qty": 2500, "unit_price": "10"}],
            one_time=[{"name": "Stencil", "amount": "30", "qty": 5}])
        self.ans = self.mail(frm="charles@jypcba.com", subject="Re: RFQ", thread=self.th,
                             when=self.t0 + datetime.timedelta(hours=3), name="Charles",
                             body="Can you share the BOM? We will quote by Friday.")
        self.hi = Supplier.objects.create(name="HITECH CIRCUITS", email="sales8@hitechpcb.com")
        self.r2 = R.objects.create(
            request=self.req, supplier=self.hi, email="sales8@hitechpcb.com", state=R.QUOTED, sent_at=self.t0,
            replied_at=self.t0 + datetime.timedelta(days=2), currency="EUR", includes=["assembly"],
            tiers=[{"qty": 5, "unit_price": "200"}, {"qty": 2500, "unit_price": "12"}],
            thread=EmailThread.objects.create(account=self.account, subject="RFQ hi"))
        self.quiet = Supplier.objects.create(name="Quiet PCB", email="info@quiet.example")
        self.r3 = R.objects.create(request=self.req, supplier=self.quiet, email="info@quiet.example", state=R.SENT,
                                   sent_at=self.t0)
        self.act = reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk])

    def chat(self, r=None):
        return self.client.get(reverse("admin:suppliers_supplierrequest_chat", args=[self.req.pk, (r or self.r).pk]))

    def frank(self):
        """Відповідь JY окремим листом з іншої адреси — прив'язана гілка (rfq_match.link)."""
        th = EmailThread.objects.create(account=self.account, subject="TM270402-04H Quotation")
        m = self.mail(frm="frank@bld-pcb.com", name="frank", subject="TM270402-04H Quotation --JY Electronics",
                      body="Please find our quotations attached. Lead time 18 days.", thread=th,
                      when=self.t0 + datetime.timedelta(days=4))
        self.r.extra_threads.add(th)
        return m


class StatsTests(TalkBase):
    def test_counts_reply_times_and_prices(self):
        t = rfq_compare.table(self.req)
        for row in t["rows"]:
            row["timing"] = rfq_stats.timing(row["recipient"])
        st = rfq_stats.stats(self.req, t)
        self.assertEqual((st["asked"], st["replied"], st["priced"], st["silent"]), (3, 2, 2, 1))
        self.assertEqual(st["replied_pct"], 67)
        self.assertEqual(st["reply"]["fast"], "3 год")
        self.assertEqual(st["reply"]["fast_who"], "JY Electronics")
        self.assertEqual(st["reply"]["slow"], "2 дн")
        five = st["per_qty"][0]
        self.assertEqual((five["qty"], five["min_who"], five["max_who"]), (5, "JY Electronics", "HITECH CIRCUITS"))
        self.assertEqual(five["min"], "136,00")                      # 130 + 30 трафарет ÷ 5
        self.assertEqual(five["spread"], "×1,5")

    def test_page_numbers_rows_and_shows_reply_time(self):
        self.frank()
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("📊 Статистика", page)
        self.assertIn('class="rw-pick" value="%d"> 1' % self.r.pk, page)
        self.assertIn("· за 3 год", page)
        self.assertIn("↓ останній", page)                            # лист frank новіший за першу відповідь
        self.assertIn('data-rw-chat="%d"' % self.r.pk, page)
        self.assertIn("↪ Переслати колезі", page)
        self.assertIn("✉️ Написати кожному", page)
        self.assertIn('id="rwe-ccall"', page)                         # 👥 копія всім листам

    def test_human(self):
        self.assertEqual(rfq_stats.human(30), "1 хв")
        self.assertEqual(rfq_stats.human(3 * 3600 + 20 * 60), "3 год 20 хв")
        self.assertEqual(rfq_stats.human(2 * 86400 + 4 * 3600), "2 дн 4 год")


class SupplierTotalTests(TalkBase):
    def test_ai_unit_price_recomputed_from_supplier_sum(self):
        stencil = [{"name": "Stencil", "amount": "30.31", "qty": 5}]
        # AI написав «(сума − трафарет) ÷ кількість», а дав суму ÷ кількість — Minerva рахує з суми сама
        t = ai.tier_from_ai({"qty": 5, "unit_price": 136.61, "total": 683.31, "total_has_one_time": True}, stencil)
        self.assertEqual((t["unit_price"], t["fixed"], t["total"], t["total_ot"]), ("130.6000", True, "683.31", True))
        ok = ai.tier_from_ai({"qty": 2500, "unit_price": 10.545, "total": 26362.5, "total_has_one_time": False}, stencil)
        self.assertNotIn("fixed", ok)
        self.assertEqual(ok["unit_price"], "10.5450")
        self.assertEqual(ai.tier_from_ai({"qty": 5, "unit_price": 2, "total": None}, [])["unit_price"], "2.0000")
        self.assertIsNone(ai.tier_from_ai({"qty": 0, "unit_price": 2}, []))

    def test_table_shows_supplier_sum_and_mismatch(self):
        R.objects.filter(pk=self.r.pk).update(
            one_time=[{"name": "Stencil", "amount": "30.31", "qty": 5}],
            tiers=[{"qty": 5, "unit_price": "136.61", "total": "683.31", "total_ot": True},
                   {"qty": 2500, "unit_price": "10.545", "total": "26362.50", "total_ot": False}])
        row = next(x for x in rfq_compare.table(self.req)["rows"] if x["recipient"].pk == self.r.pk)
        self.assertTrue(row["cells"][0]["sum_mismatch"])              # 136,61 × 5 + 30,31 ≠ 683,31
        self.assertFalse(row["cells"][1]["sum_mismatch"])
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("⚠️ не сходиться", page)
        self.assertRegex(page, r"у постачальника 26.362,50 EUR без разових ✓")


class ChatTests(TalkBase):
    def test_only_own_letters_and_reply_goes_to_last_supplier_sender(self):
        frank = self.frank()
        oth = EmailThread.objects.create(account=self.other_account, subject="JY private")
        self.mail(frm="charles@jypcba.com", subject="Secret price talk", body="SECRET 99", thread=oth,
                  account=self.other_account)
        self.r.extra_threads.add(oth)
        page = self.chat().content.decode()
        self.assertIn("Can you share the BOM?", page)
        self.assertIn("Please find our quotations attached", page)
        self.assertNotIn("SECRET 99", page)
        self.assertNotIn("Secret price talk", page)
        self.assertIn("🔒 Лист у скриньці колеги", page)
        target = rfq_talk.target(self.req, self.r, rfq_talk.letters(self.r, self.user), self.user)
        self.assertEqual((target["to"], target["cc"], target["reply_pk"]),
                         ("frank@bld-pcb.com", "charles@jypcba.com", frank.pk))
        self.assertEqual(target["subject"], "Re: TM270402-04H Quotation --JY Electronics")

    def test_reply_in_thread_with_copy_and_event(self):
        frank = self.frank()
        calls = []

        def fake_send(client, to_emails, subject, body_text, body_html="", **kw):
            calls.append({"to": to_emails, "subject": subject, "text": body_text, "reply": kw.get("reply_to_message"),
                          "cc": kw.get("cc_emails")})
            return {"ok": True, "message_id": f"<reply{len(calls)}@sevskiy.de>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            j = self.client.post(self.act, {"do": "chat_send", "rid": self.r.pk, "to": "frank@bld-pcb.com",
                                            "cc": "charles@jypcba.com", "subject": "Re: TM270402-04H Quotation",
                                            "body": "Thank you, BOM attached soon.", "reply": frank.pk}).json()
        self.assertTrue(j["ok"])
        self.assertEqual(calls[0]["to"], ["frank@bld-pcb.com"])
        self.assertEqual(calls[0]["cc"], ["charles@jypcba.com"])
        self.assertEqual(calls[0]["reply"].pk, frank.pk)
        self.assertIn("> Please find our quotations attached", calls[0]["text"])
        copy = EmailMessage.objects.get(message_id="<reply1@sevskiy.de>")
        self.assertEqual((copy.thread_id, copy.folder), (frank.thread_id, "sent"))
        self.assertTrue(SupplierEvent.objects.filter(request=self.req, supplier=self.jy, text__contains="Відповіли").exists())
        self.assertIn("Thank you, BOM attached soon.", self.chat().content.decode())
        # чужий лист як «на який відповідаємо» не береться; порожній лист — помилка
        foreign = self.mail(frm="x@y.z", account=self.other_account,
                            thread=EmailThread.objects.create(account=self.other_account, subject="o"))
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            self.client.post(self.act, {"do": "chat_send", "rid": self.r.pk, "to": "frank@bld-pcb.com",
                                        "body": "Hi", "reply": foreign.pk})
        self.assertIsNone(calls[-1]["reply"])
        bad = self.client.post(self.act, {"do": "chat_send", "rid": self.r.pk, "to": "frank@bld-pcb.com", "body": " "})
        self.assertEqual(bad.status_code, 400)

    def test_brief_per_user_from_own_letters(self):
        oth = EmailThread.objects.create(account=self.other_account, subject="JY private")
        self.mail(frm="charles@jypcba.com", body="SECRET 99", thread=oth, account=self.other_account)
        self.r.extra_threads.add(oth)
        answer = json.dumps({"status": "Чекають BOM", "they_ask": ["Надіслати BOM"], "they_promised": ["Ціна до п'ятниці"],
                             "we_promised": [], "to_answer": ["Надіслати BOM"], "lang": "en",
                             "draft": "Dear Charles,\n\nplease find the BOM attached.\n\nBest regards,"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.01"))) as call:
            j = self.client.post(self.act, {"do": "chat_brief", "rid": self.r.pk}).json()
        self.assertIn("job", j)
        sent_text = call.call_args[0][1]
        self.assertIn("Can you share the BOM?", sent_text)
        self.assertNotIn("SECRET 99", sent_text)                       # чужа скринька — AI не читає
        page = self.chat().content.decode()
        self.assertIn("❓ Питають у нас", page)
        self.assertIn("Надіслати BOM", page)
        self.assertIn("please find the BOM attached.", page)
        self.assertIsNone(rfq_talk.brief_cached(self.other, self.r, rfq_talk.letters(self.r, self.other)))
        self.mail(frm="charles@jypcba.com", subject="Re: RFQ", thread=self.th, body="Any news?")
        self.assertIn("змінилось листування", self.chat().content.decode())


class ForwardTests(TalkBase):
    def test_forward_selected_with_table_letters_and_files(self):
        SupplierReplyFile.objects.create(recipient=self.r, message=self.ans, index=0, name="JY Quotation.pdf",
                                         size=8, sha256="a" * 64, received_at=timezone.now(),
                                         file=ContentFile(b"%PDF-1.4", name="JY Quotation.pdf"))
        oth = EmailThread.objects.create(account=self.other_account, subject="JY private")
        self.mail(frm="charles@jypcba.com", body="SECRET 99", thread=oth, account=self.other_account)
        self.r.extra_threads.add(oth)
        plan = self.client.post(self.act, {"do": "forward_plan", "rid": [self.r.pk, self.r2.pk]}).json()
        self.assertEqual([x["name"] for x in plan["rows"]], ["JY Electronics", "HITECH CIRCUITS"])
        self.assertEqual(plan["rows"][0]["hidden"], 1)
        self.assertEqual(plan["files"][0]["name"], "JY Electronics · JY Quotation.pdf")
        calls = []

        def fake_send(client, to_emails, subject, body_text, body_html="", **kw):
            calls.append({"to": to_emails, "subject": subject, "text": body_text, "html": body_html,
                          "files": [a["name"] for a in kw.get("attachments") or []]})
            return {"ok": True, "message_id": "<fwd1@sevskiy.de>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            j = self.client.post(self.act, {"do": "forward_send", "rid": [self.r.pk, self.r2.pk],
                                            "to": "olga@sevskiy.de", "note": "Глянь, кого беремо?",
                                            "part": ["table", "mail", "files"]}).json()
        self.assertTrue(j["ok"])
        c = calls[0]
        self.assertEqual(c["to"], ["olga@sevskiy.de"])
        self.assertIn("JY Electronics", c["subject"])
        self.assertIn("Глянь, кого беремо?", c["html"])
        self.assertIn("HITECH CIRCUITS", c["html"])
        self.assertIn("136,00 EUR", c["html"])                          # таблиця: ціна з разовими
        self.assertIn("Can you share the BOM?", c["html"])
        self.assertNotIn("SECRET 99", c["html"] + c["text"])
        self.assertIn("🔒 Ще листів у скриньках колег: 1", c["html"])
        self.assertEqual(c["files"], ["JY Electronics - JY Quotation.pdf"])
        self.assertEqual(SupplierEvent.objects.filter(request=self.req, text__contains="Переслано колезі").count(), 2)
        empty = self.client.post(self.act, {"do": "forward_plan"})
        self.assertEqual(empty.status_code, 400)


class MailPreviewTests(TalkBase):
    def test_supplier_and_rfq_shown_for_linked_thread(self):
        from suppliers.services import mail_link
        frank = self.frank()
        out = mail_link.for_messages([frank], frank.thread, self.user)
        jy = next(x for x in out if x["name"] == "JY Electronics")
        self.assertEqual(jy["rfq"][0]["code"], self.req.code)


class EachTests(TalkBase):
    """✉️ Написати кожному: свій лист кожному вибраному, відповіддю в його гілці, інші не бачать."""

    def test_template_fills_greeting_and_skips_not_sent(self):
        frank = self.frank()
        planned = R.objects.create(request=self.req, supplier=Supplier.objects.create(name="Later Ltd", email="a@later.example"),
                                   email="a@later.example", state=R.PLANNED)
        out = rfq_talk.compose_each(self.req, [self.r.pk, self.r2.pk, planned.pk], self.user,
                                    "{greeting}\n\nThank you, {company}! Price with ENIG for {code}?", mode="template")
        self.assertEqual([x["name"] for x in out], ["JY Electronics", "HITECH CIRCUITS"])   # не надісланим — ні
        jy, hi = out
        self.assertEqual((jy["to"], jy["cc"], jy["reply"]), ("frank@bld-pcb.com", "charles@jypcba.com", frank.pk))
        self.assertTrue(jy["body"].startswith("Dear Frank,\n\nThank you, JY Electronics! Price with ENIG for RFQ-"))
        self.assertTrue(hi["body"].startswith("Dear Sir or Madam,"))                 # листів немає — без імені
        self.assertEqual((hi["to"], hi["cc"]), ("sales8@hitechpcb.com", ""))
        self.assertIn("новою гілкою", hi["note"])
        self.assertNotIn("колеги", hi["note"])

    def test_ai_letter_per_supplier_from_own_letters_only(self):
        oth = EmailThread.objects.create(account=self.other_account, subject="JY private")
        self.mail(frm="charles@jypcba.com", body="SECRET 99", thread=oth, account=self.other_account)
        self.r.extra_threads.add(oth)
        answer = json.dumps({"body": "Dear Charles,\n\nthank you. What is the price with ENIG?\n\nBest regards,"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.01"))) as call:
            j = self.client.post(self.act, {"do": "each_compose", "rids": f"{self.r.pk},{self.r2.pk}",
                                            "wish": "подякуй і запитай ціну з позолотою контактів", "mode": "ai"}).json()
        res = self.client.get(f"/admin/inventory/supplier/job/{j['job']}/").json()["result"]
        self.assertEqual(len(res["letters"]), 2)
        self.assertIn("What is the price with ENIG?", res["letters"][0]["body"])
        first = call.call_args_list[0][0][1]
        self.assertTrue(first.startswith("ПОБАЖАННЯ МЕНЕДЖЕРА — що написати: подякуй"))   # довірене — поза ДАНИМИ
        self.assertIn("Can you share the BOM?", first)
        self.assertNotIn("SECRET 99", first)
        self.assertNotIn("<<<ДАНІ", call.call_args_list[1][0][1])                   # HITECH: листів немає
        empty = self.client.post(self.act, {"do": "each_compose", "rids": str(self.r.pk), "wish": " "})
        self.assertEqual(empty.status_code, 400)

    def test_send_each_isolated_into_own_threads(self):
        frank = self.frank()
        calls = []

        def fake_send(client, to_emails, subject, body_text, body_html="", **kw):
            calls.append({"to": to_emails, "cc": kw.get("cc_emails"), "reply": kw.get("reply_to_message"),
                          "text": body_text})
            return {"ok": True, "message_id": f"<each{len(calls)}@sevskiy.de>"}
        other_req = SupplierRequest.objects.create(title="Other", owner=self.user, account=self.account)
        foreign_row = R.objects.create(request=other_req, supplier=self.quiet, email="x@quiet.example", state=R.SENT)
        letters = [
            {"rid": self.r.pk, "to": "frank@bld-pcb.com", "cc": "charles@jypcba.com", "subject": "Re: TM270402-04H",
             "body": "Dear Frank, ENIG price please.", "reply": frank.pk},
            {"rid": self.r2.pk, "to": "sales8@hitechpcb.com", "cc": "", "subject": "Re: RFQ",
             "body": "Dear Sir or Madam, ENIG price please.", "reply": ""},
            {"rid": foreign_row.pk, "to": "x@quiet.example", "body": "Hi"},
        ]
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            j = self.client.post(self.act, {"do": "each_send", "letters": json.dumps(letters)}).json()
        res = self.client.get(f"/admin/inventory/supplier/job/{j['job']}/").json()["result"]
        self.assertEqual(res["sent"], ["JY Electronics", "HITECH CIRCUITS"])
        self.assertEqual(res["failed"], [["?", "постачальника немає в цьому запиті"]])
        self.assertEqual(len(calls), 2)                                              # кожному — окремий лист
        self.assertEqual((calls[0]["to"], calls[0]["cc"]), (["frank@bld-pcb.com"], ["charles@jypcba.com"]))
        self.assertEqual(calls[0]["reply"].pk, frank.pk)                             # у його гілку
        self.assertEqual((calls[1]["to"], calls[1]["cc"]), (["sales8@hitechpcb.com"], []))
        self.assertNotIn("frank", calls[1]["text"])                                  # інші не бачать
        self.assertEqual(EmailMessage.objects.get(message_id="<each1@sevskiy.de>").thread_id, frank.thread_id)
        self.assertEqual(SupplierEvent.objects.filter(request=self.req, text__contains="Відповіли").count(), 2)


class ScopePerQuantityTests(TalkBase):
    """🧩 Ціна лише за плати для частини кількостей не змагається з PCBA: що входить — для кожної кількості."""

    def test_parts_from_ai_decide_scope_and_sum(self):
        # HK Victory: PCBA лише на 5 шт (компоненти + плата + складання 0), на решту — лише голі плати
        pcba = ai.tier_from_ai({"qty": 5, "unit_price": 53.584, "includes": ["pcb", "assembly", "components"],
                                "parts": {"pcb": 0.574, "assembly": 0, "components": 53.01}}, [])
        self.assertEqual((pcba["unit_price"], pcba["includes"]), ("53.5840", ["pcb", "assembly", "components"]))
        bare = ai.tier_from_ai({"qty": 2500, "unit_price": 0.455, "includes": ["pcb", "assembly", "components"],
                                "parts": {"pcb": 0.455}}, [])
        self.assertEqual(bare["includes"], ["pcb"])                     # AI сказав «усе», а ціна — лише плата
        summed = ai.tier_from_ai({"qty": 10000, "parts": {"pcb": 0.4, "assembly": 0.3, "components": 1.2}}, [])
        self.assertEqual((summed["unit_price"], summed["includes"]), ("1.9000", ["pcb", "assembly", "components"]))
        bundled = ai.tier_from_ai({"qty": 20000, "unit_price": 5, "includes": ["pcb", "assembly"],
                                   "parts": {"pcb": 0.4}}, [])          # складова лише частина ціни — includes AI
        self.assertEqual(bundled["includes"], ["pcb", "assembly"])

    def test_bare_board_cells_excluded_from_medal_and_stats(self):
        # запит PCBA (needed: монтаж); JY — PCBA для обох кількостей, HITECH — PCBA лише на 5 шт, на 2500 — плати
        R.objects.filter(pk=self.r2.pk).update(
            includes=["pcb", "assembly", "components"],
            tiers=[{"qty": 5, "unit_price": "100", "includes": ["pcb", "assembly", "components"]},
                   {"qty": 2500, "unit_price": "0.5", "includes": ["pcb"]}])
        t = rfq_compare.table(self.req)
        hi = next(x for x in t["rows"] if x["recipient"].pk == self.r2.pk)
        jy = next(x for x in t["rows"] if x["recipient"].pk == self.r.pk)
        self.assertTrue(hi["comparable"])                              # 5 шт — повна
        self.assertTrue(hi["cells"][0]["ok"])
        self.assertEqual(hi["cells"][1]["lacks"], ["монтаж"])
        self.assertFalse(hi["cells"][1].get("best"))                   # 0,5 € за голу плату — не 🥇
        self.assertTrue(jy["cells"][1]["best"])                        # 10 € PCBA — найдешевше з повних
        self.assertIn("у ціні немає: монтаж — для 2 500 шт", hi["gaps"])
        for row in t["rows"]:
            row["timing"] = rfq_stats.timing(row["recipient"])
        st = rfq_stats.stats(self.req, t)
        q2500 = next(p for p in st["per_qty"] if p["qty"] == 2500)
        self.assertEqual((q2500["n"], q2500["min_who"]), (1, "JY Electronics"))
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("⚠️ немає: монтаж", page)

    def test_reparse_refreshes_ai_fields_keeps_manual(self):
        old_ai = {"v": 2, "summary": "old", "tiers": [{"qty": 5, "unit_price": "0.5"}], "includes": ["pcb"]}
        R.objects.filter(pk=self.r2.pk).update(ai=old_ai, tiers=old_ai["tiers"], includes=["pcb"])
        self.r2.refresh_from_db()
        new = {"v": ai.QUOTE_VERSION, "summary": "new", "currency": "EUR", "includes": ["pcb", "assembly"],
               "tiers": [{"qty": 5, "unit_price": "90.0000", "includes": ["pcb", "assembly"]}], "one_time": []}
        from suppliers.services import rfq
        rfq.apply_ai(self.r2, new)                                     # без «перезаписати»: поле AI — оновлюється
        self.r2.refresh_from_db()
        self.assertEqual((self.r2.tiers, self.r2.includes, self.r2.price), (new["tiers"], ["pcb", "assembly"],
                                                                             Decimal("90.0000")))
        manual = [{"qty": 5, "unit_price": "77"}]                      # людина виправила ✏️ — не чіпаємо
        R.objects.filter(pk=self.r2.pk).update(tiers=manual)
        self.r2.refresh_from_db()
        rfq.apply_ai(self.r2, {**new, "tiers": [{"qty": 5, "unit_price": "95.0000"}]})
        self.r2.refresh_from_db()
        self.assertEqual(self.r2.tiers, manual)


class EachRetryTests(TalkBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, rfq_talk, "EACH_PAUSE", rfq_talk.EACH_PAUSE)
        self.addCleanup(setattr, rfq_talk, "EACH_RETRY", rfq_talk.EACH_RETRY)
        rfq_talk.EACH_PAUSE, rfq_talk.EACH_RETRY = 0, (0, 0)

    def test_temporary_smtp_refusal_is_retried_permanent_reported(self):
        calls = []

        def fake_send(client, to_emails, subject, body_text, body_html="", **kw):
            calls.append(to_emails[0])
            if to_emails[0] == "sales8@hitechpcb.com" and calls.count("sales8@hitechpcb.com") == 1:
                return {"ok": False, "error": "(421, b'4.7.0 Too many connections, try again later')"}
            if to_emails[0] == "info@quiet.example":
                return {"ok": False, "error": "(550, b'5.1.1 Mailbox unavailable')"}
            return {"ok": True, "message_id": f"<retry{len(calls)}@sevskiy.de>"}
        letters = [{"rid": self.r2.pk, "to": "sales8@hitechpcb.com", "body": "ENIG?"},
                   {"rid": self.r3.pk, "to": "info@quiet.example", "body": "ENIG?"}]
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            out = rfq_talk.send_each(self.req, self.user, letters)
        self.assertEqual(out["sent"], ["HITECH CIRCUITS"])                    # 421 — повтор, пішло
        self.assertEqual(calls.count("sales8@hitechpcb.com"), 2)
        self.assertEqual(calls.count("info@quiet.example"), 1)                # 550 — без повтору
        self.assertEqual(out["failed"][0][0], "Quiet PCB")
        self.assertIn("Mailbox unavailable", out["failed"][0][1])


class FoldedHeaderTests(TalkBase):
    """Тема / Message-ID з чужої пошти з переносом рядка («Re:\n RFQ…») — лист однаково йде (раніше: «Header values
    may not contain linefeed or carriage return characters» у трьох постачальників із тринадцяти)."""

    def test_reply_with_folded_subject_and_message_id_goes_out(self):
        hr = Supplier.objects.create(name="HongRong (shenzhen) Electronics Co.,Ltd", email="anduin@hrpcba.com")
        th = EmailThread.objects.create(account=self.account, subject="RFQ hr")
        r = R.objects.create(request=self.req, supplier=hr, email="anduin@hrpcba.com", state=R.REPLIED, thread=th,
                             sent_at=self.t0)
        last = self.mail(frm="anduin@hrpcba.com", name="Anduin", thread=th, body="We are working on it.",
                         subject="Re:\r\n RFQ RFQ-2026-001: PCBA manufacturing")
        type(last).objects.filter(pk=last.pk).update(message_id="<abc123@hrpcba.com>\r\n")
        items = rfq_talk.letters(r, self.user)
        tg = rfq_talk.target(self.req, r, items, self.user)
        self.assertEqual(tg["subject"], "Re: RFQ RFQ-2026-001: PCBA manufacturing")
        sent = []

        class Conn:
            def sendmail(self, frm, rcpt, data):
                sent.append((rcpt, data))

            def quit(self):
                pass
        with mock.patch("email_assistant.smtp_client.SMTPClient._open", return_value=Conn()), \
                mock.patch("core.smtp_utf8.smtp_login"), \
                mock.patch("email_assistant.imap_client.IMAPClient", side_effect=OSError("no imap")):
            out = rfq_talk.send_each(self.req, self.user, [{
                "rid": r.pk, "to": "anduin@hrpcba.com", "cc": "sergey@sevskiy.de",
                "subject": "Re:\n RFQ RFQ-2026-001: PCBA manufacturing", "body": "Dear Anduin, ENIG?", "reply": last.pk}])
        self.assertEqual((out["sent"], out["failed"]), (["HongRong (shenzhen) Electronics Co.,Ltd"], []))
        rcpt, data = sent[0]
        self.assertEqual(rcpt, ["anduin@hrpcba.com", "sergey@sevskiy.de"])
        self.assertIn(b"Subject: Re: RFQ RFQ-2026-001: PCBA manufacturing\r\n", data)
        self.assertIn(b"In-Reply-To: <abc123@hrpcba.com>\r\n", data)


class SavedLettersTests(TalkBase):
    """📝 Складені листи зберігаються: відкрити й дослати невідправлене — без нового складання AI."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, rfq_talk, "EACH_PAUSE", rfq_talk.EACH_PAUSE)
        rfq_talk.EACH_PAUSE = 0

    def test_compose_saves_edit_resume_send(self):
        from suppliers.models import SupplierRequestLetter
        rfq_talk.compose_each(self.req, [self.r.pk, self.r2.pk], self.user, "{greeting}\n\nENIG price?", mode="template")
        self.assertEqual(SupplierRequestLetter.objects.filter(request=self.req, user=self.user, sent_at=None).count(), 2)
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("📝 Збережені листи «Написати кожному» — не надіслано: <b>2</b>", page)
        # ✏️ правка — автозбереження; «відкрити» — уже виправлене
        self.client.post(self.act, {"do": "each_save", "letters": json.dumps([
            {"rid": self.r2.pk, "to": "sales8@hitechpcb.com", "cc": "sergey@sevskiy.de", "subject": "Re: RFQ",
             "body": "Dear Susie, ENIG price please.", "on": True}])})
        saved = self.client.post(self.act, {"do": "each_load"}).json()
        hi = next(x for x in saved["letters"] if x["rid"] == self.r2.pk)
        self.assertEqual((hi["body"], hi["cc"]), ("Dear Susie, ENIG price please.", "sergey@sevskiy.de"))
        self.assertIn("Dear Susie, ENIG price please.", self.chat(self.r2).content.decode())   # 💬 — та сама чернетка
        # чужі збережені листи не видно
        self.assertEqual(rfq_talk.saved_letters(self.req, self.other)["letters"], [])

        def fake_send(client, to_emails, subject, body_text, body_html="", **kw):
            if to_emails[0] == "sales8@hitechpcb.com":
                return {"ok": False, "error": "(550, b'Mailbox unavailable')"}
            return {"ok": True, "message_id": f"<saved{to_emails[0]}>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            out = rfq_talk.send_each(self.req, self.user, saved["letters"])
        self.assertEqual(out["sent"], ["JY Electronics"])
        left = rfq_talk.saved_letters(self.req, self.user)["letters"]
        self.assertEqual([x["rid"] for x in left], [self.r2.pk])               # невдалий — лишився, з причиною
        self.assertIn("Mailbox unavailable", left[0]["error"])
        with mock.patch("email_assistant.smtp_client.SMTPClient.send",
                        lambda *a, **k: {"ok": True, "message_id": "<chat1@x>"}):
            self.client.post(self.act, {"do": "chat_send", "rid": self.r2.pk, "to": "sales8@hitechpcb.com",
                                        "body": "Dear Susie, ENIG price please."})
        self.assertEqual(rfq_talk.saved_letters(self.req, self.user)["letters"], [])   # 💬 надіслали — позначено
