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


class ForwardBase(TalkBase):
    """Пересилання колезі з оригіналами з сервера пошти: відповідь JY є на сервері (FakeIMAP), запит — лише в
    Minerva; файл відповіді, файл без листа, лист у скриньці колеги."""

    def setUp(self):
        super().setUp()
        from email_assistant.tests_originals import FakeIMAP, raw_letter
        self.FakeIMAP = FakeIMAP
        FakeIMAP.store, FakeIMAP.calls = {}, []
        self.raw = raw_letter(message_id=self.ans.message_id, subject="Re: RFQ")
        FakeIMAP.store[("INBOX", self.ans.imap_uid)] = self.raw          # відповідь JY — є на сервері
        SupplierReplyFile.objects.create(recipient=self.r, message=self.ans, index=0, name="JY Quotation.pdf",
                                         size=8, sha256="a" * 64, received_at=timezone.now(),
                                         file=ContentFile(b"%PDF-1.4", name="JY Quotation.pdf"))
        SupplierReplyFile.objects.create(recipient=self.r, index=-1, name="Datasheet.pdf", size=9, sha256="b" * 64,
                                         received_at=timezone.now(), file=ContentFile(b"%PDF data", name="Datasheet.pdf"))
        oth = EmailThread.objects.create(account=self.other_account, subject="JY private")
        self.mail(frm="charles@jypcba.com", body="SECRET 99", thread=oth, account=self.other_account)
        self.r.extra_threads.add(oth)
        self.calls = []
        rfq_talk.EACH_PAUSE, pause = 0, rfq_talk.EACH_PAUSE
        self.addCleanup(setattr, rfq_talk, "EACH_PAUSE", pause)

    def record(self, to_emails, subject, body_text, body_html="", **kw):
        self.calls.append({"to": to_emails, "cc": kw.get("cc_emails"), "subject": subject, "text": body_text,
                           "html": body_html, "atts": kw.get("attachments") or []})
        return {"ok": True, "message_id": f"<fwd{len(self.calls)}@sevskiy.de>"}

    def send(self, **extra):
        from suppliers.services import jobs
        data = {"do": self.DO, "rid": [self.r.pk, self.r2.pk], "to": "olga@sevskiy.de",
                "cc": "boss@sevskiy.de", "note": "Глянь, кого беремо?", "table": "1", **extra}

        def fake_send(client, *args, **kw):
            return self.record(*args, **kw)
        with mock.patch("email_assistant.imap_client.IMAPClient", self.FakeIMAP), \
                mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            j = self.client.post(self.act, data).json()
        return j if "job" not in j else jobs.state(j["job"], self.user)


class ForwardThreadsTests(ForwardBase):
    """↪ «Переслати гілки» (Weiterleiten): кожна гілка — окремим листом «Fwd: …», текст листів у листі (найновіший —
    повністю, раніші — новий текст), файли листів з оригіналів і файли без листа — вкладеннями."""
    DO = "forward_threads"

    def test_plan_lists_threads_with_subjects(self):
        g = self.client.post(self.act, {"do": "forward_plan", "rid": [self.r.pk, self.r2.pk]}).json()["threads"]
        jy = g["rows"][0]
        self.assertEqual((jy["threads"], jy["letters"], jy["hidden"]), (1, 2, 1))
        self.assertEqual(g["rows"][1]["threads"], 0)
        self.assertEqual([s["subject"] for s in g["subjects"]], ["Fwd: Re: RFQ"])
        self.assertEqual((g["subjects"][0]["files"], g["parts"], g["local"]), (1, 1, 0))

    def test_thread_forwarded_inline_with_its_files(self):
        type(self.ans).objects.filter(pk=self.ans.pk).update(
            body_html='<p>Price <b>10 EUR</b><img src="cid:logo1"></p><script>alert(1)</script>')
        s = self.send()
        self.assertEqual(s["status"], "done", s)
        self.assertEqual(len(self.calls), 1)
        c = self.calls[0]
        self.assertEqual((c["subject"], c["to"], c["cc"]), ("Fwd: Re: RFQ", ["olga@sevskiy.de"], ["boss@sevskiy.de"]))
        self.assertEqual([a["name"] for a in c["atts"]], ["HR-031 Quotation.pdf", "Datasheet.pdf"])
        self.assertEqual(c["atts"][0]["content"], b"%PDF-1.4 quote")           # файл з оригіналу на сервері
        self.assertFalse(any(a["content_type"] == "message/rfc822" for a in c["atts"]))
        html = c["html"]
        self.assertIn("Глянь, кого беремо?", html)
        self.assertIn("136,00 EUR", html)                                     # таблиця — у першому листі
        self.assertEqual(html.count("-------- Forwarded Message --------"), 2)
        self.assertIn("<b>10 EUR</b>", html)                                  # найновіший — з форматуванням
        self.assertNotIn("cid:", html)
        self.assertNotIn("<script", html)
        self.assertLess(html.index("10 EUR"), html.index("Please quote PCBA."))   # найновіший — угорі
        self.assertIn("Раніші листи гілки", html)
        self.assertIn("<b>Attachments:</b> HR-031 Quotation.pdf", html)
        self.assertIn("копії немає на сервері пошти", html)                   # запит — лише в Minerva
        self.assertIn("Ще файли постачальника (не з цих листів): Datasheet.pdf", html)
        self.assertIn("🔒 Ще листів у скриньках колег: 1", html)
        self.assertNotIn("SECRET 99", html + c["text"])
        self.assertIn("From: Charles <charles@jypcba.com>", c["text"])
        self.assertIn("гілок 1", s["result"]["message"])
        self.assertEqual(SupplierEvent.objects.filter(request=self.req, text__contains="гілок листування: 1").count(), 1)

    def test_files_over_the_limit_go_in_next_letters(self):
        from suppliers.services import rfq
        with mock.patch.object(rfq, "ATTACH_MAX_MB", 0.0005):
            s = self.send(table="")
        self.assertEqual(s["status"], "done", s)
        self.assertEqual([c["subject"] for c in self.calls], ["Fwd: Re: RFQ (файли 1/2)", "Fwd: Re: RFQ (файли 2/2)"])
        self.assertEqual(self.calls[0]["atts"], [])
        self.assertEqual([a["name"] for a in self.calls[1]["atts"]], ["HR-031 Quotation.pdf", "Datasheet.pdf"])
        self.assertIn("Продовження — файли гілки", self.calls[1]["html"])
        self.assertNotIn("136,00 EUR", self.calls[0]["html"])
        self.calls.clear()
        with mock.patch.object(rfq, "ATTACH_MAX_MB", 0.0005), mock.patch.object(rfq_talk, "THREADS_MAX", 1):
            s = self.send()
        self.assertEqual((s["status"], self.calls), ("failed", []))
        self.assertIn("за раз — до 1", s["error"])


class ForwardMailsTests(ForwardBase):
    """↪ «📨 Листи файлами .eml»: кожен лист — вкладенням .eml (оригінал із сервера пошти або зібраний з Minerva),
    файли постачальника без листа — звичайними вкладеннями, понад ліміт — кількома листами колезі."""
    DO = "forward_mails"

    def test_plan_counts_letters_files_and_hidden(self):
        o = self.client.post(self.act, {"do": "forward_plan", "rid": [self.r.pk, self.r2.pk]}).json()["originals"]
        jy = o["rows"][0]
        self.assertEqual((jy["name"], jy["letters"], jy["files"], jy["hidden"]), ("JY Electronics", 2, 1, 1))
        self.assertEqual(o["rows"][1]["letters"], 0)
        self.assertEqual((o["parts"], o["local"]), (1, 0))

    def test_every_letter_as_eml_in_one_mail(self):
        s = self.send()
        self.assertEqual(s["status"], "done", s)
        self.assertEqual(len(self.calls), 1)
        c = self.calls[0]
        self.assertEqual((c["to"], c["cc"]), (["olga@sevskiy.de"], ["boss@sevskiy.de"]))
        names = [a["name"] for a in c["atts"]]
        self.assertEqual(len(names), 3, names)
        self.assertRegex(names[0], rf"^JY Electronics - \d{{4}}-\d\d-\d\d \d\d-\d\d Anna Li - \[{self.req.code}\] RFQ\.eml$")
        self.assertRegex(names[1], r"^JY Electronics - \d{4}-\d\d-\d\d \d\d-\d\d Charles - Re RFQ\.eml$")
        self.assertEqual(names[2], "JY Electronics - Datasheet.pdf")         # файл без листа; з листа — у .eml
        ans = c["atts"][1]
        self.assertEqual((ans["content"], ans["content_type"]), (self.raw, "message/rfc822"))   # як на сервері
        self.assertIn(b"Please quote PCBA.", c["atts"][0]["content"])         # на сервері немає — з Minerva
        self.assertIn("Глянь, кого беремо?", c["html"])
        self.assertIn("136,00 EUR", c["html"])                                # таблиця
        self.assertIn("📨 У вкладенні листів: 2", c["html"])
        self.assertIn("🔒 Ще листів у скриньках колег: 1", c["html"])
        self.assertIn("копії немає на сервері пошти", c["html"])
        self.assertNotIn(b"SECRET 99", b"".join(a["content"] for a in c["atts"]))
        self.assertNotIn("SECRET 99", c["html"] + c["text"])
        self.assertIn("листів 2, файлів 1", s["result"]["message"])
        self.assertEqual(SupplierEvent.objects.filter(request=self.req, text__contains="листи як є (2)").count(), 1)
        copy = type(self.ans).objects.get(message_id="<fwd1@sevskiy.de>")
        self.assertTrue(all(a.get("file_path") for a in copy.attachments))    # копія — з файлами на диску

    def test_without_table_and_too_big_splits_into_parts(self):
        from suppliers.services import rfq
        with mock.patch.object(rfq, "ATTACH_MAX_MB", 0.0005):                # ~ 500 байт — кожне вкладення окремо
            s = self.send(table="")
        self.assertEqual(s["status"], "done", s)
        self.assertEqual([c["subject"][-5:] for c in self.calls], ["(1/3)", "(2/3)", "(3/3)"])
        self.assertNotIn("136,00 EUR", self.calls[0]["html"])
        self.assertIn("Глянь, кого беремо?", self.calls[0]["html"])
        self.assertNotIn("Глянь, кого беремо?", self.calls[1]["html"])
        self.assertIn("Лист 2 з 3", self.calls[1]["html"])
        self.assertIn("🔒 Ще листів у скриньках колег: 1", self.calls[2]["html"])
        self.calls.clear()
        with mock.patch.object(rfq, "ATTACH_MAX_MB", 0.0005), mock.patch.object(rfq_talk, "ORIGINALS_PARTS_MAX", 2):
            s = self.send()
        self.assertEqual(s["status"], "failed")
        self.assertIn("за раз — до 2", s["error"])
        self.assertEqual(self.calls, [])

    def test_letter_without_server_copy_takes_its_files_from_the_request(self):
        from suppliers.models import SupplierRequestFile
        SupplierRequestFile.objects.create(request=self.req, name="Gerber.zip", size=5,
                                           file=ContentFile(b"PK\x03\x04z", name="Gerber.zip"))
        type(self.sent).objects.filter(pk=self.sent.pk).update(attachments=[{"name": "Gerber.zip", "size": 5}])
        type(self.ans).objects.filter(pk=self.ans.pk).update(attachments=[
            {"name": "JY Quotation.pdf", "size": 8}, {"name": "lost.step", "size": 3}])
        self.FakeIMAP.store.clear()                                          # на сервері пошти — нічого
        s = self.send()
        self.assertEqual(s["status"], "done", s)
        c = self.calls[0]
        names = [a["name"] for a in c["atts"]]
        self.assertEqual(names[1], "JY Electronics - Gerber.zip")            # файл запиту — під листом запиту
        self.assertEqual(names[3], "JY Electronics - JY Quotation.pdf")      # файл відповіді — під відповіддю
        self.assertEqual(names[4], "JY Electronics - Datasheet.pdf")
        self.assertEqual(len(names), 5, names)
        self.assertIn("файл цього листа (збережений у запиті): JY Quotation.pdf", c["html"])
        self.assertIn("без файлів: lost.step", c["html"])
        self.assertNotIn("без файлів: JY Quotation.pdf", c["html"])

    def test_needs_address_and_a_row(self):
        self.assertEqual(self.client.post(self.act, {"do": "forward_mails", "rid": [self.r.pk]}).status_code, 400)
        self.assertEqual(self.client.post(self.act, {"do": "forward_mails", "to": "olga@sevskiy.de"}).status_code,
                         400)


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


class StatsScopeTests(TalkBase):
    """📊 Біля найнижчої / найвищої — що в ціні; ціна далеко від медіани — ⚠️ (голі плати серед PCBA)."""

    def test_scope_shown_and_outlier_flagged(self):
        R.objects.filter(pk=self.r3.pk).update(               # «усе в ціні» за словами AI, а ціна — як за голу плату
            state=R.QUOTED, currency="EUR", includes=["pcb", "assembly", "components"],
            tiers=[{"qty": 5, "unit_price": "150"}, {"qty": 2500, "unit_price": "0.49"}])
        t = rfq_compare.table(self.req)
        for row in t["rows"]:
            row["timing"] = rfq_stats.timing(row["recipient"])
        st = rfq_stats.stats(self.req, t)
        q = next(p for p in st["per_qty"] if p["qty"] == 2500)
        self.assertEqual((q["min_who"], q["min_incl"]), ("Quiet PCB", "🟩 плати · 🔧 монтаж · 🔩 компоненти"))
        self.assertIn("нижча за медіану", q["min_warn"])
        self.assertEqual(q["max_warn"], "")
        quiet = next(x for x in t["rows"] if x["recipient"].pk == self.r3.pk)
        self.assertIn("перевірте, що входить", quiet["cells"][1]["suspect"])
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("Найнижча — за 1 шт.", page)
        self.assertIn("раза нижча за медіану", page)
        self.assertIn("Просили: 🔧 монтаж", page)


class AIUsageTests(TalkBase):
    """🧠 Смуга AI: яка модель, скільки витрачено на запит (по моделях і діях), модель у рядку."""

    def test_spend_recorded_and_shown(self):
        answer = json.dumps({"currency": "EUR", "tiers": [{"qty": 5, "unit_price": 120}], "includes": ["assembly"],
                             "summary": "PCBA 120"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.0123"))):
            self.client.post(self.act, {"do": "ai_quote", "rid": self.r.pk, "model": "claude-haiku-5-5"})
        brief = json.dumps({"status": "ok", "they_ask": [], "to_answer": [], "they_promised": [], "we_promised": [],
                            "draft": "Dear Charles,"})
        with mock.patch.object(ai, "call", return_value=(brief, Decimal("0.002"))):
            self.client.post(self.act, {"do": "chat_brief", "rid": self.r.pk, "model": "claude-sonnet-5-5"})
        self.req.refresh_from_db()
        u = self.req.ai_usage
        self.assertEqual((u["calls"], round(u["usd"], 4)), (2, 0.0143))
        self.assertEqual(u["models"]["claude-haiku-5-5"]["calls"], 1)
        self.assertEqual(set(u["kinds"]), {"parse", "brief"})
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("витрачено на запит: <b>$0.014</b>", page)
        self.assertIn("розбір відповідей $0.012", page)
        self.assertIn('id="rw-model"', page)
        self.assertIn('🧠 <span translate="no">Haiku 5.5</span> · $0.012', page)      # модель під рядком
        self.assertIn("Sonnet 5.5", self.chat().content.decode())                        # «🧭 Коротко» — модель


class ChatAITests(TalkBase):
    """💬 AI у полі відповіді: ✍️ написати за побажанням, ✨ покращити мій текст — лише з власних листів."""

    def test_write_and_improve(self):
        oth = EmailThread.objects.create(account=self.other_account, subject="JY private")
        self.mail(frm="charles@jypcba.com", body="SECRET 99", thread=oth, account=self.other_account)
        self.r.extra_threads.add(oth)
        answer = json.dumps({"body": "Dear Charles,\n\nthank you. What is the price with ENIG?\n\nBest regards,"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.004"))) as call:
            j = self.client.post(self.act, {"do": "chat_ai", "rid": self.r.pk, "mode": "write",
                                            "wish": "подякуй і запитай ціну з позолотою"}).json()
        res = self.client.get(f"/admin/inventory/supplier/job/{j['job']}/").json()["result"]
        self.assertIn("price with ENIG", res["body"])
        sent = call.call_args[0][1]
        self.assertTrue(sent.startswith("ПОБАЖАННЯ МЕНЕДЖЕРА — що написати: подякуй"))
        self.assertIn("Can you share the BOM?", sent)
        self.assertNotIn("SECRET 99", sent)
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.004"))) as call:
            self.client.post(self.act, {"do": "chat_ai", "rid": self.r.pk, "mode": "improve",
                                        "text": "pls price with gold contacts thx"})
        self.assertIn("МІЙ ТЕКСТ:\npls price with gold contacts thx", call.call_args[0][1])
        self.req.refresh_from_db()
        self.assertEqual(self.req.ai_usage["kinds"]["reply"]["calls"], 2)
        self.assertEqual(self.client.post(self.act, {"do": "chat_ai", "rid": self.r.pk, "mode": "write",
                                                     "wish": " "}).status_code, 400)
        self.assertIn('data-rwc-ai="write"', self.chat().content.decode())


class ChatFilesTests(TalkBase):
    """📎 Вкладення в 💬 — посилання: свій лист — через сторінку запиту, збережений у запиті файл — його адреса."""

    def test_attachment_links(self):
        import os
        path = os.path.join(self.media, "email_attachments", "q.pdf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 quote")
        EmailMessage.objects.filter(pk=self.ans.pk).update(attachments=[
            {"name": "logo.png", "content_type": "image/png", "size": 900, "is_inline": True},
            {"name": "Quotation.pdf", "content_type": "application/pdf", "size": 14, "file_path": "email_attachments/q.pdf"}])
        page = self.chat().content.decode()
        url = reverse("admin:suppliers_supplierrequest_chat_file", args=[self.req.pk, self.r.pk, self.ans.pk, 1])
        self.assertIn(f'href="{url}"', page)
        self.assertContains(self.client.get(url), 'class="fp-frame"')                 # 👁 сторінка перегляду
        resp = self.client.get(url + "?raw=1")
        self.assertEqual((resp.status_code, resp["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(b"".join(resp.streaming_content if resp.streaming else [resp.content]).startswith(b"%PDF"))
        # чужий лист / лист не цього рядка — ні
        foreign = self.mail(frm="charles@jypcba.com", account=self.other_account,
                            thread=EmailThread.objects.create(account=self.other_account, subject="o"))
        self.assertEqual(self.client.get(reverse("admin:suppliers_supplierrequest_chat_file",
                                                 args=[self.req.pk, self.r.pk, foreign.pk, 0])).status_code, 404)


class RowMarksTests(TalkBase):
    """🏷 Позначки рядків (правий клік): колір «№», наліпки, свій текст — одному або всім вибраним; ↔ ширина колонок."""

    def test_apply_and_view(self):
        from suppliers.services import rfq_marks
        out = rfq_marks.apply(self.req, [self.r.pk, self.r2.pk], self.user, {"color": "green"})
        self.assertEqual(set(out), {self.r.pk, self.r2.pk})
        self.assertEqual((out[self.r.pk]["color"], out[self.r.pk]["hex"]), ("green", "#43a047"))
        rfq_marks.apply(self.req, [self.r.pk], self.user, {"sticker": "ask", "on": True})
        rfq_marks.apply(self.req, [self.r.pk], self.user, {"sticker": "star", "on": True})
        v = rfq_marks.apply(self.req, [self.r.pk], self.user, {"text": "  ENIG   ціна  " + "x" * 80})[self.r.pk]
        self.assertEqual([s["key"] for s in v["stickers"]], ["star", "ask"])           # порядок — як у довіднику
        self.assertTrue(v["text"].startswith("ENIG ціна x") and len(v["text"]) == rfq_marks.TEXT_MAX)
        self.assertTrue(v["by"] and v["at"])
        v = rfq_marks.apply(self.req, [self.r.pk], self.user, {"sticker": "ask", "on": False})[self.r.pk]
        self.assertEqual([s["key"] for s in v["stickers"]], ["star"])
        rfq_marks.apply(self.req, [self.r.pk], self.user, {"color": "nonsense"})
        self.r.refresh_from_db()
        self.assertEqual(self.r.label["color"], "")
        rfq_marks.apply(self.req, [self.r.pk], self.user, {"clear": True})
        self.r.refresh_from_db()
        self.assertEqual(self.r.label, {})                                               # порожньо — без «хто / коли»
        other = SupplierRequest.objects.create(title="інший", owner=self.user, account=self.account)
        self.assertEqual(rfq_marks.apply(other, [self.r.pk], self.user, {"color": "red"}), {})   # рядок іншого запиту

    def test_action_and_page(self):
        j = self.client.post(self.act, {"do": "mark", "rids": f"{self.r.pk},{self.r2.pk}", "color": "red"}).json()
        self.assertEqual(j["labels"][str(self.r2.pk)]["color"], "red")
        j = self.client.post(self.act, {"do": "mark", "rids": str(self.r.pk), "sticker": "talk", "on": "1"}).json()
        self.assertEqual(j["labels"][str(self.r.pk)]["stickers"][0]["icon"], "💬")
        self.client.post(self.act, {"do": "mark", "rids": str(self.r.pk), "text": "<b>x</b>"})
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn('class="rw-n rw-lab" style="--rwl:#e53935" data-color="red" data-stickers="talk"', page)
        self.assertIn('<span class="rw-lab-t" translate="no">&lt;b&gt;x&lt;/b&gt;</span>', page)
        self.assertIn('data-col="sup"', page)
        self.assertIn("rw-colw", page)                                                    # ↔ ширина колонок
        self.assertIn('"key": "sample"', page)                                            # довідник наліпок для меню
        self.other.profile.role = "readonly"
        self.other.profile.save()
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.post(self.act, {"do": "mark", "rids": str(self.r.pk), "clear": "1"})
                            .status_code, 200)
        self.r.refresh_from_db()
        self.assertEqual(self.r.label["color"], "red")


class FilePreviewTests(TalkBase):
    """👁 Файли запиту відкриваються в Minerva: Excel таблицею, архів — списком і файлом усередині; чужий запит — ні."""

    def _xlsx(self):
        from core.tests_file_preview import _xlsx
        return _xlsx()

    def test_reply_file_views(self):
        from core.tests_file_preview import _zip
        from suppliers.models import SupplierReplyFile
        f = SupplierReplyFile.objects.create(recipient=self.r, message=self.ans, index=0, name="BOM_TM.xlsx",
                                             file=ContentFile(self._xlsx(), name="BOM_TM.xlsx"), sha256="a" * 64)
        url = reverse("admin:suppliers_supplierrequest_file", args=[self.req.pk, "reply", f.pk])
        page = self.client.get(url).content.decode()
        self.assertIn("<td title=\"STM32G0\">STM32G0</td>", page)
        self.assertIn('<td title="&lt;b&gt;bold&lt;/b&gt;">&lt;b&gt;bold&lt;/b&gt;</td>', page)   # екрановано
        self.assertIn(self.req.code, page)                                                 # назад до запиту
        dl = self.client.get(url + "?download=1")
        self.assertTrue(dl["Content-Disposition"].startswith("attachment"))
        z = SupplierReplyFile.objects.create(recipient=self.r, name="Gerber.zip", sha256="b" * 64,
                                             file=ContentFile(_zip({"pcb/top.gtl": b"G04 TOP*", "BOM.xlsx": self._xlsx()}),
                                                              name="Gerber.zip"))
        zurl = reverse("admin:suppliers_supplierrequest_file", args=[self.req.pk, "reply", z.pk])
        page = self.client.get(zurl).content.decode()
        self.assertIn("?e=pcb%2Ftop.gtl", page)
        self.assertIn("G04 TOP*", self.client.get(zurl + "?e=pcb/top.gtl").content.decode())
        self.assertIn("STM32G0", self.client.get(zurl + "?e=BOM.xlsx").content.decode())
        self.assertIn("немає", self.client.get(zurl + "?e=nope.txt").content.decode())
        # панель праворуч і 💬 ведуть на перегляд
        panel = self.client.get(reverse("admin:suppliers_supplierrequest_panel", args=[self.req.pk, self.r.pk]))
        self.assertContains(panel, f'href="{url}"')
        EmailMessage.objects.filter(pk=self.ans.pk).update(attachments=[{"name": "BOM_TM.xlsx", "size": 900}])
        self.assertIn(f'href="{url}"', self.chat().content.decode())

    def test_request_file_and_rights(self):
        from suppliers.models import SupplierRequestFile
        f = SupplierRequestFile.objects.create(request=self.req, name="spec.txt", file=ContentFile(b"4 layers", name="spec.txt"))
        url = reverse("admin:suppliers_supplierrequest_file", args=[self.req.pk, "req", f.pk])
        self.assertContains(self.client.get(url), "4 layers")
        other = SupplierRequest.objects.create(title="інший", owner=self.user, account=self.account)
        self.assertEqual(self.client.get(reverse("admin:suppliers_supplierrequest_file",
                                                 args=[other.pk, "req", f.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("admin:suppliers_supplierrequest_file",
                                                 args=[self.req.pk, "bogus", f.pk])).status_code, 404)
        self.other.profile.role = "readonly"
        self.other.profile.save()
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.get(url).status_code, 200)


class PinnedPlaceTests(TalkBase):
    """📌 Закріплене місце: 1, 2, 3… поспіль, вставка зсуває інших, «відкріпити» стискає; «Зняти все» місця не чіпає."""

    def test_set_rank(self):
        from suppliers.services import rfq_marks
        rfq_marks.set_rank(self.req, self.r.pk, 1, self.user)
        out = rfq_marks.set_rank(self.req, self.r2.pk, 1, self.user)              # r2 — перше, r — друге
        self.assertEqual((out[self.r2.pk]["rank"], out[self.r.pk]["rank"]), (1, 2))
        rfq_marks.set_rank(self.req, self.r3.pk, 9, self.user)                     # місце 9 → у кінець закріплених: 3
        for x in (self.r, self.r2, self.r3):
            x.refresh_from_db()
        self.assertEqual([x.label.get("rank") for x in (self.r2, self.r, self.r3)], [1, 2, 3])
        out = rfq_marks.set_rank(self.req, self.r2.pk, 0, self.user)               # відкріпити — решта стискаються
        self.assertEqual((out[self.r2.pk]["rank"], out[self.r.pk]["rank"], out[self.r3.pk]["rank"]), (0, 1, 2))
        rfq_marks.apply(self.req, [self.r.pk], self.user, {"color": "red"})
        rfq_marks.apply(self.req, [self.r.pk], self.user, {"clear": True})
        self.r.refresh_from_db()
        self.assertEqual((self.r.label.get("rank"), self.r.label.get("color")), (1, None))
        self.r2.refresh_from_db()
        self.assertEqual(self.r2.label.get("rank"), None)
        self.assertEqual(rfq_marks.set_rank(self.req, 999999, 1, self.user), {})

    def test_action_and_page(self):
        j = self.client.post(self.act, {"do": "mark", "rids": str(self.r3.pk), "rank": "1"}).json()
        self.assertEqual(j["labels"][str(self.r3.pk)]["rank"], 1)
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn('data-rank="1"', page)
        self.assertIn("📌 1</div>", page)


class CompareViewTests(TalkBase):
    """💶 Таблиця: одна кількість (ціна за шт., сума, «враховано в сумі», строк, умови, діє до) і «⇔ Усі кількості»."""

    def test_cells_and_page(self):
        cmp = rfq_compare.table(self.req)
        row = next(x for x in cmp["rows"] if x["recipient"].pk == self.r.pk)
        c5 = row["cells"][0]
        self.assertEqual([f["state"] for f in c5["flags"]], ["n", "y", "n", "y", "u"])   # лише монтаж; stencil; доставки немає
        self.assertIn("Stencil", c5["detail"])
        self.assertTrue(c5["unit_best"] and c5["bar"] == 65)                                 # 130 проти 200 за шт.
        R.objects.filter(pk=self.r2.pk).update(lead_days=12)
        R.objects.filter(pk=self.r.pk).update(lead_days=20)
        fast = {x["recipient"].pk: x["cells"][0].get("fast") for x in rfq_compare.table(self.req)["rows"]}
        self.assertTrue(fast[self.r2.pk])                                                   # 12 дн. — найшвидше
        self.assertFalse(fast[self.r.pk])
        self.assertEqual(rfq_compare.valid_state(datetime.date(2026, 1, 1), datetime.date(2026, 1, 2)), "expired")
        self.assertEqual(rfq_compare.valid_state(datetime.date(2026, 1, 3), datetime.date(2026, 1, 2)), "soon")
        self.assertEqual(rfq_compare.valid_state(datetime.date(2026, 1, 9), datetime.date(2026, 1, 2)), "")
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn('data-rw-qi="all"', page)                                              # ⇔ Усі кількості
        self.assertIn('data-rw-qi="2"', page)
        self.assertIn('class="rw-chip n" role="button" tabindex="0" data-rw-count="pcb" title="Плата: У ціні немає',
                      page)                                                                    # ⚖ клік — рахувати чи ні
        self.assertIn('class="rw-note"', page)                                                # коментар під рядком
        self.assertIn('data-rw-sort="total"', page)
        self.assertIn("rw-qv rw-qi2", page)


class CompareSelectedTests(TalkBase):
    """⚖ Порівняння вибраних: складові × кількість, разові, доставка, разом — у валюті Minerva й постачальника."""

    def test_breakdown(self):
        R.objects.filter(pk=self.r.pk).update(
            shipping=Decimal("35"), includes=["pcb", "assembly", "components"],
            tiers=[{"qty": 5, "unit_price": "130", "parts": {"pcb": "22.528", "assembly": "40.746", "components": "67.326"}},
                   {"qty": 2500, "unit_price": "10"}])
        rows = rfq_compare.breakdown(rfq_compare.table(self.req))
        jy = next(x for x in rows if x["rid"] == self.r.pk)
        c5 = jy["cells"][0]
        self.assertEqual(c5["parts"]["pcb"]["t"], "112,64 EUR")                  # 22,528 × 5
        self.assertEqual(c5["parts"]["components"]["v"], 336.63)
        self.assertEqual(c5["one_time"][0]["name"], "Stencil")
        self.assertEqual((c5["ship"]["v"], c5["total"]["v"], c5["total_ship"]["v"]), (35.0, 680.0, 715.0))
        self.assertEqual(jy["cells"][1]["parts"]["pcb"], {"state": "y"})        # без розбивки — лише «у ціні»
        self.assertNotIn(self.r3.pk, [x["rid"] for x in rows])                   # без цін — немає
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn('id="rw-cmp-btn"', page)
        self.assertIn('id="rw-cmpd"', page)
        self.assertIn('"rid": %d' % self.r.pk, page)


class FileBankTests(TalkBase):
    """📂 Банк файлів запиту: по постачальниках, тип для фільтра, перегляд у вікні (?embed=1), «← Назад» на сторінці."""

    def test_bank_and_embed(self):
        from core.tests_file_preview import _xlsx, _zip
        from suppliers.models import SupplierReplyFile, SupplierRequestFile
        from suppliers.services import rfq_bank
        f1 = SupplierReplyFile.objects.create(recipient=self.r, name="BOM.xlsx", sha256="a" * 64, size=900,
                                              received_at=timezone.now(), file=ContentFile(_xlsx(), name="BOM.xlsx"))
        z = SupplierReplyFile.objects.create(recipient=self.r2, name="Gerber.zip", sha256="b" * 64,
                                             file=ContentFile(_zip({"top.gtl": b"G04*"}), name="Gerber.zip"))
        ours = SupplierRequestFile.objects.create(request=self.req, name="spec.pdf", file=ContentFile(b"%PDF-1.4", name="spec.pdf"))
        b = rfq_bank.bank(self.req, [self.r, self.r2, self.r3])
        self.assertEqual(b["count"], 3)
        self.assertEqual([g["name"] for g in b["groups"]], ["JY Electronics", "HITECH CIRCUITS", "📤 Файли нашого запиту"])
        self.assertEqual((b["groups"][0]["n"], b["groups"][0]["files"][0]["id"], b["groups"][0]["files"][0]["kind"]),
                         (1, f"reply:{f1.pk}", "sheet"))
        self.assertEqual(b["groups"][1]["files"][0]["kind"], "archive")
        self.assertTrue(b["groups"][2]["files"][0]["embed"].endswith("?embed=1"))
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("📂 Файли (3)", page)
        self.assertIn(f'data-rw-files="{self.r.pk}"', page)                            # 📎 N у таблиці
        self.assertIn(f'data-rw-file="req:{ours.pk}"', page)
        self.assertIn('id="rw-bank"', page)
        url = reverse("admin:suppliers_supplierrequest_file", args=[self.req.pk, "reply", z.pk])
        emb = self.client.get(url + "?embed=1")
        self.assertEqual(emb["X-Frame-Options"], "SAMEORIGIN")
        html = emb.content.decode()
        self.assertNotIn('id="nav-sidebar"', html)                                      # без меню Minerva
        self.assertIn("?e=top.gtl&amp;embed=1", html)                                    # файл в архіві — у вікні ж
        full = self.client.get(url + "?e=top.gtl").content.decode()
        self.assertIn('id="fp-back"', full)
        self.assertIn(f'href="{url}"', full)                                             # ← Назад — до архіву
        self.assertIn(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk]),
                      self.client.get(url).content.decode())                             # з архіву — до запиту


class CustomColumnsTests(TalkBase):
    """➕ Свої колонки («Сума з ENIG», «Покриття»): додати / змінити / прибрати, вписати вручну, AI — лише не вписане людиною."""

    def test_columns_values_and_ai(self):
        from suppliers.services import rfq_columns
        enig = rfq_columns.add(self.req, "Сума з ENIG", "price", "ENIG 1u замість HASL")
        cov = rfq_columns.add(self.req, "Покриття", "text")
        self.assertEqual((enig["key"], cov["key"]), ("c1", "c2"))
        with self.assertRaises(rfq_columns.ColumnError):
            rfq_columns.add(self.req, "сума з enig", "price")                      # така вже є
        rfq_columns.set_value(self.r, "c1", 5, "1.291,69", self.user)
        rfq_columns.set_value(self.r2, "c1", 5, "800", self.user)
        rfq_columns.set_value(self.r, "c2", None, "  ENIG   1u ", self.user)
        with self.assertRaises(rfq_columns.ColumnError):
            rfq_columns.set_value(self.r, "c1", 5, "abc", self.user)
        self.r.refresh_from_db()
        self.assertEqual(self.r.extra["c1"]["v"], {"5": "1291.69"})
        self.assertEqual((self.r.extra["c2"]["v"], self.r.extra["c2"]["src"]), ("ENIG 1u", "user"))
        cmp = rfq_compare.table(self.req)
        cells = rfq_columns.cells(cmp, self.req)
        self.assertEqual(cells[self.r.pk][0]["qs"][0]["main"], "1 291,69")
        self.assertTrue(cells[self.r2.pk][0]["qs"][0]["best"])                       # 800 < 1291,69
        # 🧠 AI: у промпті — колонки; вписане людиною лишається, порожнє — заповнюється
        note, js = rfq_columns.ai_prompt(self.req)
        self.assertIn("«Сума з ENIG» (ENIG 1u замість HASL)", note)
        self.assertIn('"extra": {"c1"', js)
        x = rfq_columns.clean_ai(self.req, {"extra": {"c1": {"5": 999, "2500": "26672.70", "x": 1}, "c2": "HASL",
                                                     "c9": "чужа"}})
        self.assertEqual(x, {"c1": {"5": "999", "2500": "26672.7"}, "c2": "HASL"})
        rfq_columns.apply_ai(self.r, x)
        rfq_columns.apply_ai(self.r3, x)
        self.r.refresh_from_db()
        self.r3.refresh_from_db()
        self.assertEqual(self.r.extra["c1"]["v"], {"5": "1291.69"})                  # вручну — не змінено
        self.assertEqual((self.r3.extra["c1"]["src"], self.r3.extra["c2"]["v"]), ("ai", "HASL"))
        rfq_columns.edit(self.req, "c1", name="ENIG total")
        rfq_columns.delete(self.req, "c2")
        self.req.refresh_from_db()
        self.r.refresh_from_db()
        self.assertEqual([c["name"] for c in rfq_columns.columns(self.req)], ["ENIG total"])
        self.assertNotIn("c2", self.r.extra)

    def test_quote_prompt_and_page(self):
        from suppliers.services import rfq_columns
        rfq_columns.add(self.req, "Сума з ENIG", "price")
        answer = json.dumps({"currency": "EUR", "tiers": [{"qty": 5, "unit_price": 120}], "includes": ["assembly"],
                             "summary": "PCBA", "extra": {"c1": {"5": 711.46}}})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.01"))) as call:
            self.client.post(self.act, {"do": "ai_quote", "rid": self.r.pk})
        self.assertIn("«Сума з ENIG»", call.call_args[0][0])
        self.r.refresh_from_db()
        self.assertEqual(self.r.extra["c1"]["v"], {"5": "711.46"})
        j = self.client.post(self.act, {"do": "col_add", "name": "Покриття", "kind": "text"}).json()
        self.assertEqual(j["column"]["key"], "c2")
        j = self.client.post(self.act, {"do": "col_set", "rid": self.r2.pk, "key": "c1", "qty": "5", "value": "x"})
        self.assertEqual(j.status_code, 400)
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn('data-rw-sort="x:c1"', page)
        self.assertIn('<span translate="no">Сума з ENIG</span>', page)
        self.assertIn('id="rw-cols-btn"', page)
        self.assertIn('id="rw-colnew"', page)
        self.assertIn('data-x-c1-1="', page)
        self.other.profile.role = "readonly"
        self.other.profile.save()
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.post(self.act, {"do": "col_del", "key": "c1"}).status_code, 200)


class CurrencyToggleTests(TalkBase):
    """💱 Ціни як пише постачальник (за замовчуванням) або ще й у валюті Minerva — перемикач, коли валюта інша."""

    def test_toggle(self):
        url = reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])
        self.assertNotIn('data-rw-cur="home"', self.client.get(url).content.decode())     # усі в EUR — без перемикача
        R.objects.filter(pk__in=[self.r.pk, self.r2.pk]).update(currency="USD")
        page = self.client.get(url).content.decode()
        self.assertIn('data-rw-cur="home"', page)
        self.assertIn('💱 USD</button>', page)
        self.assertIn('<span class="rw-cur-s">USD, </span>без разових', page)
        self.assertRegex(page, r'<span class="rw-cur-s">130,00 <span class="spf-muted">USD</span></span>')
