"""🏭 Постачальники: категорії, контакти, з листа, з пошти, візитки, аналіз пошти, AI, запити (RFQ).

python manage.py test suppliers --settings=tabele.settings_test
"""
import datetime
import io
import json
import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import PurchaseOrder, PurchaseOrderLine, Supplier
from suppliers.models import (SupplierAnalysis, SupplierCategory, SupplierContact, SupplierEvent, SupplierMedia,
                              SupplierMailIgnore, SupplierRequest, SupplierRequestRecipient as R)
from suppliers.services import ai, discover, draft, jobs, mailscan, match, overview, rfq

ME = "slava@sevskiy.de"


def png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (40, 24), "white").save(out, "PNG")
    return out.getvalue()


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        jobs.BACKGROUND = False
        self.addCleanup(setattr, jobs, "BACKGROUND", True)
        self.user = User.objects.create_superuser("slava", ME, "p")
        self.client.force_login(self.user)
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address=ME, smtp_host="smtp.x", smtp_port=587,
                                                   smtp_username=ME, smtp_password="p", imap_username=ME,
                                                   imap_password="p")
        self.other = User.objects.create_user("olga", "olga@sevskiy.de", "p", is_staff=True)
        self.other_account = EmailAccount.objects.create(user=self.other, email_address="olga@sevskiy.de",
                                                         imap_username="o", imap_password="p")
        self.pcb, _ = SupplierCategory.objects.get_or_create(   # тести без міграцій — seed 0002 не діє
            name="Виробники плат", defaults={"icon": "🟩", "request_template": "pcb"})
        self.supplier = Supplier.objects.create(name="Shenzhen PCBWay Co., Ltd", email="sales@pcbway.com",
                                                website="https://www.pcbway.com")
        self.uid = 0

    def mail(self, frm="anna@pcbway.com", subject="Quotation for PCB", body="Dear Slava,\nprice 1.20 USD",
             to=(ME,), account=None, folder="inbox", when=None, thread=None, name="Anna Li", in_reply_to=""):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(
            account=account or self.account, folder=folder, imap_folder_name="INBOX", imap_uid=self.uid,
            subject=subject, from_email=frm, from_name=name, body_text=body, to_emails=list(to),
            sent_at=when or timezone.now(), thread=thread, message_id=f"<m{self.uid}@x>", in_reply_to=in_reply_to)


class MatchTests(Base):
    def test_find_by_email_domain_and_name(self):
        SupplierContact.objects.create(supplier=self.supplier, name="Anna", email="anna@pcbway.com")
        self.assertEqual(match.find(emails=["anna@pcbway.com"])[0][0], self.supplier)
        self.assertEqual(match.find(emails=["new.person@pcbway.com"])[0][0], self.supplier)     # домен
        self.assertEqual(match.find(emails=["x@mail.pcbway.com"])[0][0], self.supplier)         # піддомен
        self.assertEqual(match.find(name="PCBWay Limited")[0][0], self.supplier)                 # назва без форми
        self.assertEqual(match.find(emails=["someone@gmail.com"]), [])                           # поштовик — ні
        self.assertEqual(match.name_key("Shenzhen JLC Electronics Co., Ltd."), "shenzhenjlc")


class DraftTests(Base):
    def test_draft_from_signature_and_create(self):
        m = self.mail(frm="tom@cable-tech.de", name="Tom Becker", subject="Angebot Kabel",
                      body="Hallo,\nanbei unser Angebot.\n\nMit freundlichen Grüßen\nTom Becker - Vertrieb\n"
                           "Cable Tech GmbH\nHauptstr. 5\n70173 Stuttgart\nGermany\nTel. +49 711 123456\n"
                           "USt-IdNr. DE123456789")
        d = draft.from_message(m)
        self.assertEqual(d["email"], "tom@cable-tech.de")
        self.assertEqual(d["name"], "Cable Tech GmbH")
        self.assertEqual(d["website"], "https://www.cable-tech.de")
        self.assertEqual(d["contact_name"], "Tom Becker")
        self.assertEqual(d["addr_zip"], "70173")
        self.assertEqual(d["vat_id"], "DE123456789")
        self.assertEqual(draft.existing(d), [])
        s = draft.create(d, user=self.user, source="mail", message=m, categories=[self.pcb])
        self.assertEqual((s.source, s.status), ("mail", Supplier.STATUS_ACTIVE))
        self.assertEqual(list(s.categories.all()), [self.pcb])
        c = s.contacts.get()
        self.assertEqual((c.name, c.email, c.is_primary), ("Tom Becker", "tom@cable-tech.de", True))
        self.assertTrue(SupplierEvent.objects.filter(supplier=s, kind=SupplierEvent.CREATED, message=m).exists())
        with self.assertRaises(ValueError):
            draft.create(d)                                                     # та сама назва — ні
        self.assertEqual(draft.existing(d)[0][0], s)
        _c, created = draft.add_contact(s, {"contact_name": "Tom Becker", "contact_email": "TOM@cable-tech.de"})
        self.assertFalse(created)                                               # такий контакт уже є

    def test_fill_empty_keeps_existing(self):
        changed = draft.fill_empty(self.supplier, {"website": "https://other.com", "addr_city": "Shenzhen",
                                                   "vat_id": "CN1"})
        self.supplier.refresh_from_db()
        self.assertEqual(sorted(changed), ["addr_city", "vat_id"])
        self.assertEqual(self.supplier.website, "https://www.pcbway.com")


class MailScanTests(Base):
    def test_scan_groups_reply_times_problems_and_privacy(self):
        po = PurchaseOrder.objects.create(supplier=self.supplier, status="received", supplier_order_no="W123456",
                                          order_date=datetime.date(2026, 9, 1), expected_date=datetime.date(2026, 9, 10),
                                          received_date=datetime.date(2026, 9, 15))
        from email_assistant.models import EmailThread
        th = EmailThread.objects.create(account=self.account, subject="Order W123456")
        t0 = timezone.now() - datetime.timedelta(days=5)
        self.mail(frm=ME, to=["sales@pcbway.com"], folder="sent", subject="Order W123456", body="Please confirm",
                  when=t0, thread=th)
        self.mail(frm="sales@pcbway.com", subject="Re: Order W123456",
                  body="Sorry, production is delayed by one week.", when=t0 + datetime.timedelta(hours=6), thread=th)
        self.mail(frm="news@other.com", subject="Unrelated")
        self.mail(frm="sales@pcbway.com", account=self.other_account, subject="Olga only")   # чужа скринька
        r = mailscan.scan(self.supplier, self.user, timezone.localdate() - datetime.timedelta(days=30),
                          timezone.localdate())
        self.assertEqual((r["messages"], r["incoming"], r["outgoing"]), (2, 1, 1))
        self.assertEqual(r["their_reply_hours"], 6.0)
        self.assertEqual(r["groups"][0]["po"], po)
        self.assertEqual(len(r["problems"]), 1)
        self.assertEqual(r["problems"][0]["word"].lower(), "delayed")
        p = overview.purchases(self.supplier)
        self.assertEqual((p["count"], p["on_time"], p["late"], p["median_days"]), (1, 0, 1, 14))
        sc = overview.score(p, mailscan.summary(r), overview.requests_stats(self.supplier))
        self.assertIsNotNone(sc["value"])


class DiscoverTests(Base):
    def test_candidates_skip_known_customers_services_and_ignored(self):
        from crm.models import Customer
        Customer.objects.create(name="Kunde", email="buyer@client.de")
        for i in range(2):
            self.mail(frm="li@new-fab.cn", name="Li Wei", subject=f"Quotation for PCB {i}", body="MOQ 100, lead time 2 weeks")
        self.mail(frm=ME, to=["li@new-fab.cn"], folder="sent", subject="RFQ cable assembly", body="Please quote")
        self.mail(frm="buyer@client.de", subject="Angebot bitte", body="price list?")
        self.mail(frm="sales@pcbway.com", subject="Quotation", body="price")          # уже постачальник
        self.mail(frm="tracking@ups.com", subject="Invoice", body="invoice")           # сервіс
        rows = discover.candidates(self.user)
        self.assertEqual([g["key"] for g in rows], ["@new-fab.cn"])
        self.assertEqual(rows[0]["outgoing"], 1)
        discover.ignore("@new-fab.cn", self.user)
        self.assertEqual(discover.candidates(self.user), [])
        self.assertTrue(SupplierMailIgnore.objects.filter(key="@new-fab.cn").exists())


class AITests(Base):
    def test_parse_json_and_clean(self):
        data = ai.parse_json('Ось:\n```json\n{"company": "ACME GmbH", "country": "de", "contact": {"name": "Bob"},'
                             ' "products": ["PCB"], "categories": ["Виробники плат"]}\n```')
        out = ai.clean_supplier(data)
        self.assertEqual((out["name"], out["addr_country"], out["contact_name"]), ("ACME GmbH", "DE", "Bob"))
        self.assertIn("Продукція: PCB", out["description"])
        with self.assertRaises(ai.AIError):
            ai.parse_json("no json here")

    def test_card_reads_photo(self):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user)
        m.image.save("card.png", SimpleUploadedFile("card.png", png()), save=False)
        m.save()
        answer = json.dumps({"company": "Cable Tech GmbH", "email": "info@cable-tech.de",
                             "contact": {"name": "Tom", "email": "tom@cable-tech.de"}, "categories": ["Виробники плат"]})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.004"))) as call:
            data = ai.card(m, user=self.user)
        self.assertEqual(call.call_args.args[1][0]["type"], "image")
        m.refresh_from_db()
        self.assertEqual((m.state, data["name"], m.extracted["contact_email"]),
                         (SupplierMedia.READ, "Cable Tech GmbH", "tom@cable-tech.de"))

    def test_evaluate_keeps_only_real_quotes(self):
        from email_assistant.models import EmailThread
        th = EmailThread.objects.create(account=self.account, subject="x")
        self.mail(frm="sales@pcbway.com", body="Boards are delayed by two weeks.", thread=th)
        res = mailscan.scan(self.supplier, self.user)
        answer = json.dumps({"summary": "ok", "rating": 4, "problems": [
            {"text": "затримка", "mail": 1, "quote": "Boards are delayed by two weeks."},
            {"text": "вигадка", "mail": 1, "quote": "never written"}]})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.01"))):
            a = ai.evaluate(self.supplier, self.user, res, overview.purchases(self.supplier))
        self.assertEqual(a.kind, SupplierAnalysis.AI)
        self.assertEqual([p["text"] for p in a.result["problems"]], ["затримка"])
        self.assertEqual(a.result["rating"], 4)


class RFQTests(Base):
    def setUp(self):
        super().setUp()
        self.s2 = Supplier.objects.create(name="JLC", email="")
        SupplierContact.objects.create(supplier=self.s2, name="Mia", email="mia@jlcpcb.com", is_primary=True)
        self.req = SupplierRequest.objects.create(title="PCB 4 layers", template="pcb", quantities="10; 100",
                                                  specs={"layers": "4", "finish": "ENIG"}, owner=self.user,
                                                  reply_by=timezone.localdate() + datetime.timedelta(days=5))

    def test_body_recipients_and_send_once(self):
        self.assertTrue(self.req.code.startswith(f"RFQ-{timezone.localdate().year}-"))
        body = rfq.build_body(self.req)
        self.assertIn("Layers: 4", body)
        self.assertIn("Quantities: 10 / 100", body)
        self.assertIn(self.req.code, body)
        self.req.body, self.req.subject = body, rfq.subject_for(self.req)
        self.req.save()
        out = rfq.add_recipients(self.req, [self.supplier, self.s2, Supplier.objects.create(name="NoMail")])
        self.assertEqual((out["added"], out["skipped"]), (2, ["NoMail"]))
        r2 = self.req.recipients.get(supplier=self.s2)
        self.assertEqual((r2.email, r2.contact.name), ("mia@jlcpcb.com", "Mia"))
        sent = []

        def fake_send(client, to_emails, subject, body_text, **kw):
            sent.append((to_emails, subject, body_text))
            return {"ok": True, "message_id": f"<rfq{len(sent)}@sevskiy.de>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            out = rfq.send(self.req, self.user)
            again = rfq.send(self.req, self.user)
        self.assertEqual((out["sent"], again["sent"]), (2, 0))                    # удруге не шле
        self.assertIn("Dear Mia,", next(b for t, s, b in sent if t == ["mia@jlcpcb.com"]))
        self.assertTrue(all(s.startswith(f"[{self.req.code}]") for _t, s, _b in sent))
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, SupplierRequest.SENT)
        r2.refresh_from_db()
        self.assertEqual(r2.state, R.SENT)
        self.assertIsNotNone(r2.thread_id)
        # відповідь у тій самій гілці → «Відповів», запит → «Є відповіді»
        reply = self.mail(frm="mia@jlcpcb.com", subject=f"Re: [{self.req.code}] quote", thread=r2.thread,
                          body="10 pcs 5.00 USD, 100 pcs 1.20 USD, 7 days")
        self.assertEqual(rfq.on_incoming(reply), 1)
        r2.refresh_from_db()
        self.assertEqual((r2.state, r2.reply_message_id), (R.REPLIED, reply.pk))
        self.assertEqual(SupplierRequest.objects.get(pk=self.req.pk).status, SupplierRequest.ANSWERED)
        # номер запиту в темі, але від чужого — не відповідь
        stranger = self.mail(frm="spam@else.com", subject=f"Re: [{self.req.code}]")
        self.assertEqual(rfq.on_incoming(stranger), 0)
        # AI-розбір → ціни, строк, «Є пропозиція»
        answer = json.dumps({"currency": "USD", "tiers": [{"qty": 10, "unit_price": 5}, {"qty": 100, "unit_price": 1.2}],
                             "lead_days": 7, "summary": "ok"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.002"))):
            data = ai.quote(r2, user=self.user)
        rfq.apply_ai(r2, data)
        r2.refresh_from_db()
        self.assertEqual((r2.state, r2.price, r2.lead_days, r2.currency), (R.QUOTED, Decimal("5.0000"), 7, "USD"))
        rows = rfq.comparison(self.req)
        self.assertTrue(any(x.get("fastest") for x in rows))
        rfq.choose(r2, self.user)
        po = rfq.make_purchase(r2, self.user)
        self.assertEqual((po.supplier, po.status), (self.s2, PurchaseOrder.Status.DRAFT))
        self.assertEqual(PurchaseOrderLine.objects.get(purchase_order=po).qty_ordered, Decimal("10"))
        rfq.close(self.req, "беремо JLC", self.user)
        self.assertEqual(self.req.recipients.get(supplier=self.supplier).state, R.SILENT)

    def test_reply_signal_runs_after_commit(self):
        self.req.body = "x"
        self.req.save()
        rfq.add_recipients(self.req, [self.supplier])
        r = self.req.recipients.get()
        R.objects.filter(pk=r.pk).update(state=R.SENT, message_id="<orig@x>")
        with self.captureOnCommitCallbacks(execute=True):
            self.mail(frm="sales@pcbway.com", subject="Re: offer", in_reply_to="<orig@x>")
        r.refresh_from_db()
        self.assertEqual(r.state, R.REPLIED)

    def test_failed_smtp_keeps_recipient_for_retry(self):
        self.req.body = "x"
        self.req.save()
        rfq.add_recipients(self.req, [self.supplier])
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", return_value={"ok": False, "error": "535 auth"}):
            out = rfq.send(self.req, self.user)
        self.assertEqual(out["failed"][0][1], "535 auth")
        self.assertEqual(self.req.recipients.get().state, R.FAILED)
        self.assertEqual(SupplierRequest.objects.get(pk=self.req.pk).status, SupplierRequest.DRAFT)


class AdminPagesTests(Base):
    def test_supplier_card_list_and_pages(self):
        self.supplier.categories.add(self.pcb)
        PurchaseOrder.objects.create(supplier=self.supplier, status="ordered")
        url = reverse("admin:inventory_supplier_change", args=[self.supplier.pk])
        page = self.client.get(url)
        self.assertContains(page, "Аналіз пошти з цим постачальником")
        self.assertContains(page, "Контактні особи")
        self.assertContains(page, "Знайти про них в інтернеті")
        lst = self.client.get(reverse("admin:inventory_supplier_changelist") + f"?cat={self.pcb.pk}")
        self.assertContains(lst, "PCBWay")
        self.assertContains(lst, "Знайти постачальників у пошті")
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_add")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_analysis", args=[self.supplier.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_find_in_mail")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_card")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_cards")).status_code, 200)

    def test_from_mail_only_own_message_and_create(self):
        m = self.mail(frm="tom@cable-tech.de", name="Tom Becker", subject="Angebot",
                      body="Hallo\n\nBest regards,\nTom Becker\nCable Tech GmbH\n70173 Stuttgart")
        url = reverse("admin:inventory_supplier_from_mail", args=[m.pk])
        self.assertContains(self.client.get(url), "Cable Tech GmbH")
        foreign = self.mail(frm="x@y.de", account=self.other_account)
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_from_mail", args=[foreign.pk])).status_code, 404)
        resp = self.client.post(url, {"action": "create", "name": "Cable Tech GmbH", "email": "tom@cable-tech.de",
                                      "contact_name": "Tom Becker", "contact_email": "tom@cable-tech.de",
                                      "categories": [self.pcb.pk]})
        s = Supplier.objects.get(name="Cable Tech GmbH")
        self.assertRedirects(resp, reverse("admin:inventory_supplier_change", args=[s.pk]), fetch_redirect_response=False)
        self.assertEqual(s.contacts.count(), 1)
        # той самий лист ще раз — «уже є в базі»
        self.assertContains(self.client.get(url), "уже є в базі")

    def test_card_upload_ai_and_create(self):
        answer = json.dumps({"company": "Antenna Fab Ltd", "email": "info@antfab.com",
                             "contact": {"name": "Ken", "email": "ken@antfab.com"}})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.003"))):
            resp = self.client.post(reverse("admin:inventory_supplier_card"), {
                "image": SimpleUploadedFile("card.png", png(), content_type="image/png"), "kind": "card",
                "event": "electronica 2026", "note": "Говорили про зразки", "marks": ["fair", "sample"], "read_ai": "1"})
        m = SupplierMedia.objects.get()
        self.assertEqual((m.state, m.marks, m.event), (SupplierMedia.READ, ["fair", "sample"], "electronica 2026"))
        self.assertTrue(resp["Location"].startswith(reverse("admin:inventory_supplier_card_review", args=[m.pk])))
        review = self.client.get(reverse("admin:inventory_supplier_card_review", args=[m.pk]))
        self.assertContains(review, "Antenna Fab Ltd")
        resp = self.client.post(reverse("admin:inventory_supplier_card_review", args=[m.pk]), {
            "action": "create", "name": "Antenna Fab Ltd", "email": "info@antfab.com", "contact_name": "Ken",
            "contact_email": "ken@antfab.com"})
        s = Supplier.objects.get(name="Antenna Fab Ltd")
        m.refresh_from_db()
        self.assertEqual((m.supplier, m.state, s.source, s.status),
                         (s, SupplierMedia.APPLIED, "card", Supplier.STATUS_POTENTIAL))
        self.assertContains(self.client.get(reverse("admin:inventory_supplier_change", args=[s.pk])), "Говорили про зразки")

    def test_research_job(self):
        answer = json.dumps({"summary": "PCB maker", "sources": [{"title": "site", "url": "https://pcbway.com"},
                                                                 {"title": "bad", "url": "javascript:x"}]})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.05"))):
            job = self.client.post(reverse("admin:inventory_supplier_research", args=[self.supplier.pk]),
                                   {"model": "claude-haiku-5-5"}).json()["job"]
        state = self.client.get(reverse("admin:inventory_supplier_job", args=[job])).json()
        self.assertEqual(state["status"], "done")
        a = SupplierAnalysis.objects.get(kind=SupplierAnalysis.WEB)
        self.assertEqual([s["url"] for s in a.result["sources"]], ["https://pcbway.com"])
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.get(reverse("admin:inventory_supplier_job", args=[job])).status_code, 200)

    def test_rfq_pages(self):
        new = reverse("admin:suppliers_supplierrequest_new")
        self.assertContains(self.client.get(new + f"?category={self.pcb.pk}"), "Стекап")
        resp = self.client.post(new, {"title": "PCB 4L", "template": "pcb", "category": self.pcb.pk,
                                      "quantities": "10; 100", "language": "de", "spec_layers": "4",
                                      "rcp": [self.supplier.pk], "go": "work",
                                      "files": SimpleUploadedFile("gerber.zip", b"PK\x03\x04zip")})
        req = SupplierRequest.objects.get()
        self.assertRedirects(resp, reverse("admin:suppliers_supplierrequest_work", args=[req.pk]),
                             fetch_redirect_response=False)
        self.assertIn("Lagen: 4", req.body)
        self.assertEqual(req.files.get().name, "gerber.zip")
        self.assertEqual(req.recipients.get().email, "sales@pcbway.com")
        work = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[req.pk]))
        self.assertContains(work, "Надіслати (1)")
        with mock.patch("email_assistant.smtp_client.SMTPClient.send",
                        return_value={"ok": True, "message_id": "<a@b>"}) as send:
            self.client.post(reverse("admin:suppliers_supplierrequest_action", args=[req.pk]), {"do": "send"})
        self.assertEqual(send.call_args.kwargs["attachments"][0]["name"], "gerber.zip")
        self.assertEqual(req.recipients.get().state, R.SENT)
        self.assertEqual(self.client.get(reverse("admin:suppliers_supplierrequest_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:suppliers_suppliercategory_changelist")).status_code, 200)

    def test_staff_without_rights_gets_nothing(self):
        self.other.profile.role = "readonly"
        self.other.profile.save()
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.get(reverse("admin:inventory_supplier_find_in_mail")).status_code, 200)
        self.assertNotEqual(self.client.get(reverse("admin:suppliers_supplierrequest_new")).status_code, 200)


VCARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nN:Becker;Tom;;;\r\nFN:Tom Becker\r\nORG:Cable Tech GmbH;Vertrieb\r\n"
         "TITLE:Sales Manager\r\nTEL;TYPE=WORK,VOICE:+49 711 123456\r\nTEL;TYPE=CELL:+49 170 1234567\r\n"
         "EMAIL;TYPE=INTERNET:tom@cable-tech.de\r\nURL:www.cable-tech.de\r\n"
         "ADR;TYPE=WORK:;;Hauptstr. 5;Stuttgart;;70173;Germany\r\nNOTE:Messe electronica\\, Halle B3\r\n"
         " Stand 101\r\nEND:VCARD")


class QRTests(Base):
    def test_vcard_mecard_links(self):
        from suppliers.services import qr
        d = qr.parse(VCARD)
        self.assertEqual((d["kind"], d["name"], d["contact_name"], d["contact_position"]),
                         ("vcard", "Cable Tech GmbH", "Tom Becker", "Sales Manager"))
        self.assertEqual((d["contact_phone"], d["contact_mobile"], d["phone"]),
                         ("+49 711 123456", "+49 170 1234567", "+49 711 123456"))
        self.assertEqual((d["email"], d["contact_email"], d["website"]),
                         ("tom@cable-tech.de", "tom@cable-tech.de", "https://www.cable-tech.de"))
        self.assertEqual((d["addr_street"], d["addr_city"], d["addr_zip"], d["addr_country"]),
                         ("Hauptstr. 5", "Stuttgart", "70173", "DE"))
        self.assertEqual(d["notes"], "Messe electronica, Halle B3Stand 101")       # згорнутий рядок склеєно
        old = ("BEGIN:VCARD\nVERSION:2.1\nN:Petrenko;Olena\n"
               "ORG;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=A2=D0=9E=D0=92 =D0=90=D0=BD=D1=82=D0=B5=D0=BD=D0=B0\n"
               "TEL;CELL:+380501234567\nEND:VCARD")
        d = qr.parse(old)
        self.assertEqual((d["name"], d["contact_name"], d["contact_mobile"]),
                         ("ТОВ Антена", "Olena Petrenko", "+380501234567"))
        d = qr.parse("MECARD:N:Li,Anna;ORG:PCBWay;TEL:+86 755 1;EMAIL:anna@pcbway.com;URL:www.pcbway.com;;")
        self.assertEqual((d["kind"], d["contact_name"], d["name"], d["website"]),
                         ("mecard", "Anna Li", "PCBWay", "https://www.pcbway.com"))
        self.assertEqual(qr.parse("https://www.example.com/de/kontakt")["website"], "https://www.example.com/de/kontakt")
        self.assertEqual(qr.parse("https://wa.me/491701234567")["contact_mobile"], "+491701234567")
        self.assertEqual(qr.parse("just some text"), {})
        both = qr.parse_all(qr.join("https://www.other.com", VCARD))            # vCard головніший за посилання
        self.assertEqual((both["website"], both["name"]), ("https://www.cable-tech.de", "Cable Tech GmbH"))
        merged = qr.merge(qr.parse(VCARD), {"name": "Cable Tec", "email": "wrong@x.de", "description": "Kabel"})
        self.assertEqual((merged["name"], merged["email"], merged["description"]),
                         ("Cable Tech GmbH", "tom@cable-tech.de", "Kabel"))
        self.assertIn("contact_mobile", merged["from_qr"])

    def test_upload_with_qr_fills_without_ai_and_ai_keeps_qr(self):
        with mock.patch.object(ai, "call") as call:
            self.client.post(reverse("admin:inventory_supplier_card"), {
                "image": SimpleUploadedFile("card.png", png(), content_type="image/png"), "kind": "card",
                "qr_front": VCARD})
        call.assert_not_called()
        m = SupplierMedia.objects.get()
        self.assertEqual((m.state, m.extracted["name"], m.extracted["contact_mobile"]),
                         (SupplierMedia.READ, "Cable Tech GmbH", "+49 170 1234567"))
        page = self.client.get(reverse("admin:inventory_supplier_card_review", args=[m.pk]))
        self.assertContains(page, "vCard")
        self.assertContains(page, 'value="Cable Tech GmbH"')
        answer = json.dumps({"company": "Cable Tec", "email": "info@cable-tech.de", "products": ["SMA"]})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.003"))) as call:
            data = ai.card(m, user=self.user)
        self.assertIn("QR-код з цієї візитки", call.call_args.args[1][-1]["text"])   # AI бачить QR
        self.assertEqual((data["name"], data["email"]), ("Cable Tech GmbH", "tom@cable-tech.de"))
        self.assertIn("SMA", data["description"])

    def test_review_reads_qr_later(self):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user, extracted={"name": "Guess GmbH"},
                          state=SupplierMedia.READ)
        m.image.save("card.png", SimpleUploadedFile("card.png", png()), save=False)
        m.save()
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        self.client.post(url, {"action": "qr", "qr_front": "MECARD:N:Li,Anna;ORG:PCBWay;;"})
        m.refresh_from_db()
        self.assertEqual((m.extracted["name"], m.extracted["contact_name"]), ("PCBWay", "Anna Li"))
        self.assertTrue(m.qr_text.startswith("MECARD:"))
        self.client.post(url, {"action": "qr", "qr_front": "", "qr_back": ""})       # нічого — нічого не змінює
        m.refresh_from_db()
        self.assertEqual(m.extracted["name"], "PCBWay")


class PresetTests(Base):
    def test_presets_save_default_shared_and_delete(self):
        url = reverse("admin:inventory_supplier_card_presets")
        j = self.client.post(url, {"action": "save", "name": "electronica 2026, хол B3", "event": "electronica 2026, хол B3",
                                   "kind": "card", "marks": ["fair", "sample", "bogus"], "categories": [self.pcb.pk],
                                   "read_ai": "1", "model": "claude-haiku-5-5", "shared": "1",
                                   "make_default": "1"}).json()
        from suppliers.models import SupplierCardPreset
        p = SupplierCardPreset.objects.get()
        self.assertEqual((j["id"], j["default"], p.marks, p.model, p.shared), (p.pk, p.pk, ["fair", "sample"],
                                                                             "claude-haiku-5-5", True))
        self.assertEqual(list(p.categories.all()), [self.pcb])
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.ui_prefs.get("supplier_card_preset"), p.pk)
        page = self.client.get(reverse("admin:inventory_supplier_card"))
        self.assertContains(page, "electronica 2026, хол B3")
        # колега бачить спільний пресет, але не може його видалити
        self.other.profile.role = "warehouse"
        self.other.profile.save()
        self.client.force_login(self.other)
        j = self.client.post(url, {"action": "delete", "id": p.pk}).json()
        self.assertTrue(SupplierCardPreset.objects.filter(pk=p.pk).exists())
        self.assertEqual([x["name"] for x in j["presets"]], ["electronica 2026, хол B3"])
        self.client.force_login(self.user)
        j = self.client.post(url, {"action": "delete", "id": p.pk}).json()
        self.assertEqual((j["presets"], j["default"]), ([], None))

    def test_save_and_next_keeps_preset_and_categories(self):
        from suppliers.models import SupplierCardPreset
        p = SupplierCardPreset.objects.create(owner=self.user, name="Fair", event="Fair 2026")
        resp = self.client.post(reverse("admin:inventory_supplier_card"), {
            "image": SimpleUploadedFile("card.png", png(), content_type="image/png"), "kind": "card",
            "event": "Fair 2026", "preset": p.pk, "categories": [self.pcb.pk], "go": "next"})
        self.assertEqual(resp["Location"], reverse("admin:inventory_supplier_card") + f"?preset={p.pk}")
        m = SupplierMedia.objects.get()
        self.assertEqual((m.preset, list(m.categories.all())), (p, [self.pcb]))
        review = self.client.get(reverse("admin:inventory_supplier_card_review", args=[m.pk]))
        self.assertContains(review, f'name="categories" value="{self.pcb.pk}" checked')
        foreign = SupplierCardPreset.objects.create(owner=self.other, name="Olga private")
        self.client.post(reverse("admin:inventory_supplier_card"), {
            "image": SimpleUploadedFile("card.png", png(), content_type="image/png"), "preset": foreign.pk})
        self.assertIsNone(SupplierMedia.objects.order_by("-pk").first().preset)     # чужий особистий — ні


class QRManyCodesTests(Base):
    def test_vcard_wins_and_messenger_links_go_to_notes(self):
        from suppliers.services import qr
        text = qr.join("https://u.wechat.com/example-taylor" + qr.SEPARATOR + "https://www.linkedin.com/in/taylor",
                       "BEGIN:VCARD\nFN:Taylor Young\nORG:PSElectronics\nURL:www.pspcb.example\nEND:VCARD")
        d = qr.parse_all(text)
        self.assertEqual((d["kind"], d["name"], d["website"]), ("vcard", "PSElectronics", "https://www.pspcb.example"))
        self.assertEqual(d["notes"], "WeChat: https://u.wechat.com/example-taylor; LinkedIn: https://www.linkedin.com/in/taylor")
        self.assertEqual(qr.parse_all("https://t.me/fabsales")["notes"], "Telegram: https://t.me/fabsales")
        self.assertNotIn("website", qr.parse_all("https://u.wechat.com/x"))


class EditSavedPhotoTests(Base):
    def test_edit_keeps_original_and_restore(self):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user, extracted={"name": "PSElectronics",
                                                                                   "notes": "booth B3"})
        m.image.save("card.png", SimpleUploadedFile("card.png", png()), save=False)
        m.save()
        first = m.image.name
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        self.assertContains(self.client.get(url), "Повернути / обрізати")
        j = self.client.post(url, {"action": "edit", "side": "front",
                                   "image": SimpleUploadedFile("edited.jpg", png(), content_type="image/jpeg"),
                                   "qr_front": "https://wa.me/message/5P4DBBAOJ7MEF1?src=qr"}).json()
        self.assertEqual(j["url"], url)
        m.refresh_from_db()
        self.assertEqual(m.image_original.name, first)                   # оригінал — той самий файл
        self.assertNotEqual(m.image.name, first)
        self.assertEqual(m.extracted["notes"], "booth B3; WhatsApp: https://wa.me/message/5P4DBBAOJ7MEF1?src=qr")
        second = m.image.name
        self.client.post(url, {"action": "edit", "side": "front",
                               "image": SimpleUploadedFile("edited.jpg", png(), content_type="image/jpeg")})
        m.refresh_from_db()
        self.assertEqual(m.image_original.name, first)                   # друга правка — оригінал той самий
        self.assertFalse(m.image.storage.exists(second))                 # проміжна версія прибрана
        self.assertContains(self.client.get(url), "↺ Оригінал")
        self.client.post(url, {"action": "restore", "side": "front"})
        m.refresh_from_db()
        self.assertEqual((m.image.name, m.image_original.name), (first, ""))
        bad = self.client.post(url, {"action": "edit", "side": "front",
                                     "image": SimpleUploadedFile("x.jpg", b"not an image", content_type="image/jpeg")})
        self.assertEqual(bad.status_code, 400)

    def test_drop_one_side(self):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user)
        m.image.save("front.png", SimpleUploadedFile("front.png", png()), save=False)
        m.image_back.save("back.png", SimpleUploadedFile("back.png", png()), save=False)
        m.save()
        front, back = m.image.name, m.image_back.name
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        self.assertContains(self.client.get(url), "🗑 Прибрати цю сторону</button>", count=2)
        self.client.post(url, {"action": "drop_side", "side": "front"})         # лицьову — зворотна стає першою
        m.refresh_from_db()
        self.assertEqual((m.image.name, m.image_back.name), (back, ""))
        self.assertFalse(m.image.storage.exists(front))
        self.assertNotContains(self.client.get(url), "Прибрати цю сторону")      # одна сторона — лише «повністю»
        self.client.post(url, {"action": "drop_side", "side": "front"})
        m.refresh_from_db()
        self.assertEqual(m.image.name, back)
        self.client.force_login(self.other)                                     # чужому — ні
        self.assertIn(self.client.post(url, {"action": "drop_side", "side": "back"}).status_code, (403, 404))

    def test_edit_can_reread_with_ai(self):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user)
        m.image.save("card.png", SimpleUploadedFile("card.png", png()), save=False)
        m.save()
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        with mock.patch.object(ai, "call", return_value=('{"company": "Cropped GmbH"}', Decimal("0.002"))):
            j = self.client.post(url, {"action": "edit", "side": "front", "read_ai": "1",
                                       "image": SimpleUploadedFile("e.jpg", png(), content_type="image/jpeg")}).json()
        self.assertIn("?job=", j["url"])
        m.refresh_from_db()
        self.assertEqual(m.extracted["name"], "Cropped GmbH")


class HistoryTests(Base):
    def _att(self, m, name, data, size=None):
        import os
        rel = f"email_attachments/{m.account_id}/{m.pk}_{name}"
        path = os.path.join(self.media, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        m.attachments = list(m.attachments or []) + [{"name": name, "file_path": rel, "size": size or len(data),
                                                      "content_type": "application/pdf"}]
        m.save(update_fields=["attachments"])

    def _thread(self):
        from email_assistant.models import EmailThread
        th = EmailThread.objects.create(account=self.account, subject="RFQ for 20 pcs Flexible PCB")
        t0 = timezone.now() - datetime.timedelta(days=40)
        self.mail(frm=ME, to=["sales@pcbway.com"], folder="sent", subject="RFQ for 20 pcs Flexible PCB",
                  body="Please quote", when=t0, thread=th)
        q = self.mail(frm="sales@pcbway.com", subject="Re: RFQ for 20 pcs Flexible PCB", thread=th,
                      when=t0 + datetime.timedelta(hours=3), body="Quotation attached")
        self._att(q, "Quotation 20260121MC02.pdf", b"%PDF-1.4 quote")
        self._att(q, "image001.png", b"\x89PNG small logo")
        pi = self.mail(frm="sales@pcbway.com", subject="Re[4]: RFQ for 20 pcs Flexible PCB", thread=th,
                       when=t0 + datetime.timedelta(days=2), body="PI attached")
        self._att(pi, "PI#20260402MC02.pdf", b"%PDF-1.4 proforma")
        self.mail(frm="sales@pcbway.com", subject="Golden October Peak Season - Any PCBA Needs Coming Up?",
                  when=t0 + datetime.timedelta(days=30), body="Promotion")
        return th

    def test_helpers(self):
        from suppliers.services import history
        self.assertEqual(history.topic_of("Re: Re[2]:  Re: Fw: Re: Re[4]: RFQ for 20 pcs Flexible PCB"),
                         "RFQ for 20 pcs Flexible PCB")
        self.assertEqual(history.topic_of("AW: Re: Aw: TM270402-03H"), "TM270402-03H")
        self.assertFalse(history.is_document({"name": "image001.png", "size": 400_000}))
        self.assertFalse(history.is_document({"name": "photo.jpg", "size": 20_000}))
        self.assertTrue(history.is_document({"name": "Stand photo.jpg", "size": 900_000}))
        self.assertTrue(history.is_document({"name": "BOM_TR140401.xlsx", "size": 9_000}))
        self.assertFalse(history.is_document({"name": "x.pdf", "is_inline": True}))
        self.assertEqual([history.doc_kind(n) for n in ("PI#20260402MC02.pdf", "Quotation#20260804MC03.pdf",
                                                         "Gerber.zip", "BOM_TR140401.xlsx", "TR140401_PandP.txt",
                                                         "4-Layer Impedance Control Stackup.docx", "notes.txt")],
                         ["pi", "quotation", "gerber", "bom", "pnp", "stackup", "other"])

    def test_rebuild_topics_events_and_purchase(self):
        from suppliers.services import history
        self._thread()
        out = history.rebuild(self.supplier, self.user)
        self.assertEqual((out["messages"], out["topics"]), (4, 2))
        data = history.topics_for(self.supplier, self.user)
        rfq = next(t for t in data["topics"] if t["title"] == "RFQ for 20 pcs Flexible PCB")
        self.assertEqual((rfq["stage"], rfq["incoming"], rfq["outgoing"]), ("pi", 2, 1))
        self.assertEqual(sorted(d["name"] for d in rfq["docs"]), ["PI#20260402MC02.pdf", "Quotation 20260121MC02.pdf"])
        ads = next(t for t in data["topics"] if t["title"].startswith("Golden October"))
        self.assertEqual(ads["stage"], "marketing")
        texts = list(SupplierEvent.objects.filter(supplier=self.supplier).values_list("text", flat=True))
        self.assertTrue(any(t.startswith("📨 Наш запит: «RFQ for 20 pcs Flexible PCB»") for t in texts))
        self.assertTrue(any("PI#20260402MC02.pdf" in t for t in texts))
        self.assertFalse(any("Golden October" in t for t in texts if not t.startswith("✉️ Перше")))
        self.assertEqual(history.rebuild(self.supplier, self.user)["events"], 0)          # без повторів
        po, docs = history.make_purchase(self.supplier, self.user, rfq["key"])
        self.assertEqual((po.supplier_order_no, po.status, docs["added"]), ("20260402MC02", "ordered", 2))
        cats = dict(po.documents.values_list("title", "category"))
        self.assertEqual(cats, {"PI#20260402MC02.pdf": "invoice", "Quotation 20260121MC02.pdf": "quote"})
        history.rebuild(self.supplier, self.user)
        rfq = next(t for t in history.topics_for(self.supplier, self.user)["topics"] if t.get("po"))
        self.assertEqual(rfq["po"], po.pk)                                          # тема тепер під закупівлею
        with self.assertRaises(ValueError):
            history.make_purchase(self.supplier, self.user, rfq["key"])

    def test_card_block_buttons_and_own_attachments_only(self):
        self._thread()
        card = reverse("admin:inventory_supplier_change", args=[self.supplier.pk])
        self.assertContains(self.client.get(card), "Зібрати історію з пошти")
        resp = self.client.post(reverse("admin:inventory_supplier_history", args=[self.supplier.pk]),
                                {"sup_action": "rebuild"})
        self.assertRedirects(resp, card, fetch_redirect_response=False)
        page = self.client.get(card)
        self.assertContains(page, "RFQ for 20 pcs Flexible PCB")
        self.assertContains(page, "Завести закупівлю")
        self.assertNotContains(page, "image001.png")
        msg = next(m for m in self.supplier_messages() if m.attachments)
        url = reverse("admin:inventory_supplier_attachment", args=[msg.pk, 0])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.get(url).status_code, 200)

    def supplier_messages(self):
        from email_assistant.models import EmailMessage
        return EmailMessage.objects.filter(account=self.account).order_by("pk")

    def test_create_from_mail_collects_history(self):
        m = self.mail(frm="mia@newfab.example", name="Mia", subject="Request for Quotation PCBA",
                      body="Hello\n\nBest regards,\nMia\nNew Fab Ltd\n518000 Shenzhen\nChina")
        self.mail(frm="mia@newfab.example", subject="Re: Request for Quotation PCBA", body="see PI")
        self.client.post(reverse("admin:inventory_supplier_from_mail", args=[m.pk]),
                         {"action": "create", "name": "New Fab Ltd", "email": "mia@newfab.example"})
        s = Supplier.objects.get(name="New Fab Ltd")
        from suppliers.services import history
        data = history.topics_for(s, self.user)
        self.assertEqual([t["title"] for t in data["topics"]], ["Request for Quotation PCBA"])
        self.assertTrue(SupplierEvent.objects.filter(supplier=s, text__startswith="✉️ Перше листування").exists())

    def test_purchase_as_history_keeps_stock_and_incoming(self):
        from inventory.models import InventoryTransaction
        from suppliers.services import history
        self._thread()
        history.rebuild(self.supplier, self.user)
        rfq = next(t for t in history.topics_for(self.supplier, self.user)["topics"] if t["stage"] == "pi")
        self.assertTrue(history.history_default(rfq))                         # останній лист — 38 днів тому
        self.assertFalse(history.history_default({"last": timezone.now().isoformat()}))
        po, out = history.make_purchase(self.supplier, self.user, rfq["key"], as_history=True)
        self.assertEqual((po.status, po.received_date, po.lines.count(), out["added"]), ("received", None, 0, 2))
        self.assertTrue(history.is_history(po))
        self.assertFalse(InventoryTransaction.objects.exists())
        self.assertEqual(overview.purchases(self.supplier)["open"], 0)       # не «відкрита» й не «Очікується»
        with self.assertRaises(ValueError):                                     # тема вже під закупівлею — без оновлення
            history.make_purchase(self.supplier, self.user, rfq["key"])
        history.rebuild(self.supplier, self.user)
        t = next(t for t in history.topics_for(self.supplier, self.user)["topics"] if t.get("po"))
        self.assertEqual((t["po"], t["po_history"]), (po.pk, True))
        # тема стала закупівлею («— «PO-…»» замість назви теми) — віхи не дублюються, старі повтори прибираються
        pi_events = SupplierEvent.objects.filter(supplier=self.supplier, text__contains="PI#20260402MC02.pdf")
        self.assertEqual(pi_events.count(), 1)
        e = pi_events.get()
        SupplierEvent.objects.create(supplier=self.supplier, kind=e.kind, message=e.message, user=self.user,
                                     text=e.text.split(" — «")[0] + " — «PO-999»")
        history.rebuild(self.supplier, self.user)
        self.assertEqual(list(pi_events.values_list("pk", flat=True)), [e.pk])
        self.assertTrue(SupplierEvent.objects.filter(supplier=self.supplier,
                                                     text__startswith=f"📜 Закупівля {po.code}").exists())

    def test_card_checkbox_picks_history_or_open(self):
        from suppliers.services import history
        self._thread()
        url = reverse("admin:inventory_supplier_history", args=[self.supplier.pk])
        self.client.post(url, {"sup_action": "rebuild"})
        page = self.client.get(reverse("admin:inventory_supplier_change", args=[self.supplier.pk]))
        self.assertContains(page, 'name="hist"')
        self.assertContains(page, 'name="make_po"')
        key = next(t for t in history.topics_for(self.supplier, self.user)["topics"] if t["stage"] == "pi")["key"]
        self.client.post(url + "?action=po", {"topic": key, "hist": ["other", key]})
        self.assertEqual(PurchaseOrder.objects.get(supplier=self.supplier).status, "received")
        PurchaseOrder.objects.all().delete()
        history.rebuild(self.supplier, self.user)
        self.client.post(url + "?action=po", {"topic": key})                  # без галочки — відкрита закупівля
        self.assertEqual(PurchaseOrder.objects.get(supplier=self.supplier).status, "ordered")
        real = User.has_perm                                                    # без права на закупівлі — без галочок
        with mock.patch.object(User, "has_perm", lambda u, perm, obj=None: perm != "inventory.add_purchaseorder"
                               and real(u, perm, obj)):
            page = self.client.get(reverse("admin:inventory_supplier_change", args=[self.supplier.pk]))
        self.assertContains(page, "RFQ for 20 pcs Flexible PCB")
        self.assertNotContains(page, 'name="make_po"')
        self.assertNotContains(page, 'name="hist"')

    def test_rebuild_makes_history_purchases_once(self):
        self._thread()
        url = reverse("admin:inventory_supplier_history", args=[self.supplier.pk])
        self.client.post(url, {"sup_action": "rebuild", "make_po": "1", "po_history": "1"})
        pos = PurchaseOrder.objects.filter(supplier=self.supplier)
        self.assertEqual([(p.status, p.supplier_order_no, p.documents.count()) for p in pos],
                         [("received", "20260402MC02", 2)])                    # лише тема з PI, розсилка — ні
        self.client.post(url, {"sup_action": "rebuild", "make_po": "1", "po_history": "1"})
        self.assertEqual(pos.count(), 1)
        self.client.post(url, {"sup_action": "rebuild"})                        # без галочки — нічого не заводить
        self.assertEqual(pos.count(), 1)

    def test_create_from_mail_with_history_purchases(self):
        m = self.mail(frm="mia@newfab.example", name="Mia", subject="PCBA order TR140401", body="Please quote")
        pi = self.mail(frm="mia@newfab.example", subject="Re: PCBA order TR140401", body="PI attached")
        self._att(pi, "PI 20261103NF01.pdf", b"%PDF-1.4 pi")
        page = self.client.get(reverse("admin:inventory_supplier_from_mail", args=[m.pk]))
        self.assertContains(page, 'name="po_history"')
        self.client.post(reverse("admin:inventory_supplier_from_mail", args=[m.pk]),
                         {"action": "create", "name": "New Fab Ltd", "email": "mia@newfab.example", "make_po": "1",
                          "po_history": "1"})
        po = PurchaseOrder.objects.get(supplier__name="New Fab Ltd")
        self.assertEqual((po.status, po.supplier_order_no, po.documents.count()), ("received", "20261103NF01", 1))


class CardsTests(Base):
    """🪪 Візитки: плитка, категорії й нова категорія від AI, поділитись поштою, vCard, клієнт CRM, AI для кількох."""

    def card(self, **extracted):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user, event="electronica 2026", note="стенд B3",
                          extracted=extracted)
        m.image.save("front.png", SimpleUploadedFile("front.png", png()), save=False)
        m.image_back.save("back.png", SimpleUploadedFile("back.png", png()), save=False)
        m.save()
        return m

    def test_ai_card_picks_categories_and_proposes_new(self):
        from suppliers.services import cards
        self.assertEqual(cards.match_categories(["🟩 «Виробники плат»", "виробники ПЛАТ (PCB)", "Щось інше"]),
                         [self.pcb])
        m = self.card()
        answer = json.dumps({"company": "Shenzhen Huaqiu", "description": "Схоже, друковані плати (NextPCB).",
                             "categories": ["«Виробники плат»"]})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.004"))) as call:
            ai.card(m, user=self.user)
        self.assertIn("NextPCB", call.call_args.args[0])                       # підказка: висновок з назви / логотипа
        self.assertEqual(list(m.categories.all()), [self.pcb])                 # категорія — одразу візитці
        quartz = self.card()
        answer = json.dumps({"company": "Donghai Kaikai Quartz", "categories": [],
                             "new_category": {"name": "«Виробники кварцових резонаторів»", "icon": "💎"}})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.004"))):
            ai.card(quartz, user=self.user)
        quartz.refresh_from_db()
        self.assertEqual(quartz.extracted["new_category"]["name"], "Виробники кварцових резонаторів")
        page = self.client.get(reverse("admin:inventory_supplier_cards"))
        self.assertContains(page, "AI пропонує нову категорію")
        self.assertContains(page, "🧠 AI ✓")
        url = reverse("admin:inventory_supplier_card_review", args=[quartz.pk])
        j = self.client.post(url, {"action": "add_category"}).json()
        self.assertEqual((j["name"], j["icon"], j["created"]), ("Виробники кварцових резонаторів", "💎", True))
        self.assertEqual(list(quartz.categories.values_list("name", flat=True)), ["Виробники кварцових резонаторів"])
        self.assertFalse(self.client.post(url, {"action": "add_category"}).json()["created"])   # без дублів

    def test_tile_info_country_site_and_filters(self):
        from suppliers.services import cards
        m = self.card(name="Coax Connectors Ltd", contact_name="Lolly Zheng", addr_country="CN",
                      website="www.coax-connectors.com", description="Роз'єми й кабелі\nПродукція: SMA")
        m.categories.add(self.pcb)
        i = cards.info(m)
        self.assertEqual((i["company"], i["flag"], i["site"]), ("Coax Connectors Ltd", "🇨🇳",
                                                                 "https://www.coax-connectors.com"))
        self.assertEqual((i["desc"], i["ai"]["key"], [c.pk for c in i["cats"]]), ("Роз'єми й кабелі", "none",
                                                                                  [self.pcb.pk]))
        self.assertEqual(cards.site_url("javascript:alert(1)"), "")
        page = self.client.get(reverse("admin:inventory_supplier_cards") + "?q=Lolly")
        self.assertContains(page, "Coax Connectors Ltd")
        self.assertContains(page, "https://www.coax-connectors.com")
        self.assertNotContains(self.client.get(reverse("admin:inventory_supplier_cards") + "?ai=done"),
                               "Coax Connectors Ltd")

    def test_qr_links_clickable(self):
        m = self.card(name="Coax")
        SupplierMedia.objects.filter(pk=m.pk).update(qr_text="http://www.coax-connectors.com/")
        page = self.client.get(reverse("admin:inventory_supplier_card_review", args=[m.pk]))
        self.assertContains(page, '<a href="http://www.coax-connectors.com/"')

    def test_share_makes_draft_with_photos_and_vcard(self):
        from email_assistant.models import EmailDraft
        m = self.card(name="Cable Tech GmbH", contact_name="Tom Becker", contact_position="Sales",
                      contact_email="tom@cable-tech.de", contact_mobile="+49 170 1234567", website="www.cable-tech.de")
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        resp = self.client.post(url, {"action": "share"})
        d = EmailDraft.objects.get()
        self.assertRedirects(resp, f"/email/compose/?draft={d.pk}", fetch_redirect_response=False)
        self.assertEqual((d.account, d.to_emails), (self.account, []))
        self.assertIn("Cable Tech GmbH", d.subject)
        self.assertIn("Tom Becker — Sales", d.body)
        self.assertEqual([a["name"] for a in d.attachments], ["Tom_Becker.png", "Tom_Becker-back.png", "Tom_Becker.vcf"])
        vcf = self.client.get(url + "?vcf=1")
        self.assertEqual(vcf["Content-Type"], "text/vcard; charset=utf-8")
        text = vcf.content.decode()
        for line in ("FN:Tom Becker", "N:Becker;Tom;;;", "ORG:Cable Tech GmbH", "TEL;TYPE=CELL:+49 170 1234567",
                     "EMAIL;TYPE=INTERNET:tom@cable-tech.de", "URL:www.cable-tech.de"):
            self.assertIn(line, text)
        self.client.force_login(self.other)                                     # без права — нічого
        self.assertNotEqual(self.client.post(url, {"action": "share"}).status_code, 302)

    def test_card_to_crm_customer_new_and_existing(self):
        import os
        from crm.models import Customer, CustomerTimeline
        m = self.card(name="Leeno Industrial", contact_name="Castor Park", contact_email="park@leeno.co.kr",
                      addr_country="KR")
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        form = {"name": "Leeno Industrial", "contact_name": "Castor Park", "contact_email": "park@leeno.co.kr",
                "contact_mobile": "+82 10 5005 7087", "addr_city": "Busan", "addr_country": "KR"}
        self.client.post(url, {**form, "action": "crm_create"})
        c = Customer.objects.get()
        self.assertEqual((c.name, c.company, c.email, c.phone, c.country, c.source),
                         ("Castor Park", "Leeno Industrial", "park@leeno.co.kr", "+82 10 5005 7087", "KR", "card"))
        m.refresh_from_db()
        self.assertEqual(m.customer, c)
        self.assertTrue(os.path.exists(os.path.join(self.media, "customers", str(c.pk), f"Візитка-{m.pk}.png")))
        self.assertTrue(CustomerTimeline.objects.filter(customer=c, title__startswith="🪪").exists())
        self.client.post(url, {**form, "action": "crm_create"})                 # той самий email — без дубля
        self.assertEqual(Customer.objects.count(), 1)
        other = self.card(name="Leeno Industrial", contact_email="sales@leeno.co.kr")
        page = self.client.get(reverse("admin:inventory_supplier_card_review", args=[other.pk]))
        self.assertContains(page, "той самий домен leeno.co.kr")
        old = Customer.objects.create(name="Kim", company="")
        self.client.post(reverse("admin:inventory_supplier_card_review", args=[other.pk]),
                         {"name": "Leeno Industrial", "contact_email": "sales@leeno.co.kr", "action": "crm_attach",
                          "customer": old.pk})
        old.refresh_from_db()
        self.assertEqual((old.company, old.email), ("Leeno Industrial", "sales@leeno.co.kr"))   # лише порожнє
        self.assertEqual(self.client.get(reverse("admin:inventory_supplier_crm_search") + "?q=leeno").json()
                         ["results"][0]["id"], c.pk)

    def test_read_many_unread_only_and_stops_on_errors(self):
        from suppliers.services import cards
        a, b = self.card(), self.card()
        done = self.card()
        SupplierMedia.objects.filter(pk=done.pk).update(cost_usd=Decimal("0.01"))
        self.assertEqual(set(cards.to_read(self.user, "unread").values_list("pk", flat=True)), {a.pk, b.pk})
        answer = json.dumps({"company": "X", "categories": ["Виробники плат"]})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.002"))):
            j = self.client.post(reverse("admin:inventory_supplier_cards"),
                                 {"action": "read_all", "scope": "unread"}).json()
        self.assertIn("Прочитано 2", jobs.state(j["job"], self.user)["result"]["message"])
        self.assertFalse(cards.to_read(self.user, "nocat").filter(pk__in=[a.pk, b.pk]).exists())
        with mock.patch.object(ai, "call", side_effect=ai.AIError("Немає ключа AI")) as call:
            out = cards.read_many([self.card().pk for _ in range(5)], self.user, "")
        self.assertEqual((out["done"], out["failed"], call.call_count), (0, 3, 3))

    def test_tile_prefs_per_person(self):
        from suppliers.services import cards
        self.card(name="Leeno", description="Pogo pins")
        url = reverse("admin:inventory_supplier_cards")
        page = self.client.get(url)
        self.assertContains(page, 'data-desc="collapsed"')
        self.assertContains(page, "<summary>📝 Що роблять</summary>")
        self.assertNotContains(page, "<details class=\"spc-desc spc-f-desc\" open>")
        self.user.profile.ui_prefs = {"theme": "dark"}
        self.user.profile.save()
        j = self.client.post(url, {"action": "tile_prefs", "hide": ["crm", "note", "bogus"], "desc": "full"}).json()
        self.assertEqual((j["hide"], j["desc"]), (["crm", "note"], "full"))
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.ui_prefs["theme"], "dark")            # решта вибору людини — як була
        page = self.client.get(url)
        self.assertContains(page, '<details class="spc-desc spc-f-desc" open>')
        self.assertContains(page, 'class="spc-cf" value="crm" >')                 # галочка знята
        self.assertEqual(cards.tile_prefs(self.other), {"hide": [], "desc": "collapsed"})   # інших не зачіпає
        self.client.post(url, {"action": "tile_prefs", "reset": "1"})
        self.user.profile.refresh_from_db()
        self.assertNotIn(cards.PREFS_KEY, self.user.profile.ui_prefs)

    def test_share_many_and_vcard_many(self):
        from email_assistant.models import EmailDraft
        a = self.card(name="Leeno Industrial", contact_name="Castor Park")
        b = self.card(name="Bornico", contact_name="Maciej Bornikowski")
        url = reverse("admin:inventory_supplier_cards")
        resp = self.client.post(url, {"action": "share_many", "ids": [b.pk, a.pk, 99999]})
        d = EmailDraft.objects.get()
        self.assertRedirects(resp, f"/email/compose/?draft={d.pk}", fetch_redirect_response=False)
        self.assertEqual(d.subject, "Візитки (2): electronica 2026")
        self.assertTrue(d.body.startswith("1. ") and "2. " in d.body)
        self.assertLess(d.body.index("Bornico"), d.body.index("Leeno"))            # у порядку вибору
        self.assertIn(f"/admin/inventory/supplier/cards/{a.pk}/", d.body)
        self.assertEqual(len(d.attachments), 5)                                      # 2 × 2 фото + один .vcf
        self.assertEqual(d.attachments[-1]["name"], "contacts-2.vcf")
        vcf = self.client.get(url + f"?vcf={a.pk},{b.pk}")
        self.assertEqual(vcf.content.decode().count("BEGIN:VCARD"), 2)
        self.assertIn("contacts-2.vcf", vcf["Content-Disposition"])
        with mock.patch("suppliers.services.cards.PHOTOS_MAX_BYTES", 1):          # фото не влазять — лише текст і .vcf
            from suppliers.services import cards
            d2 = cards.share_many([a, b], self.user)
        self.assertEqual([x["name"] for x in d2.attachments], ["contacts-2.vcf"])
        self.assertIn("Фото не всіх візиток вклалися", d2.body)
        self.client.post(url, {"action": "share_many"})                              # нічого не вибрано
        self.assertEqual(EmailDraft.objects.count(), 2)

    def test_stats_by_event_and_filter(self):
        from crm.models import Customer
        from suppliers.services import cards
        a = self.card(name="Leeno Industrial Co., Ltd", contact_email="park@leeno.co.kr", addr_country="KR")
        self.card(name="Leeno Industrial", contact_email="kim@leeno.co.kr", addr_country="KR")
        c = self.card(name="Bornico Sp. z o.o.", contact_name="Maciej", addr_country="PL")
        SupplierMedia.objects.filter(pk=c.pk).update(event="", customer=Customer.objects.create(name="M"))
        SupplierMedia.objects.filter(pk=a.pk).update(supplier=self.supplier, cost_usd=Decimal("0.01"), marks=["star"])
        s = cards.stats()
        fair = next(g for g in s["events"] if g["event"] == "electronica 2026")
        self.assertEqual((fair["cards"], fair["people"], fair["companies"], fair["suppliers"], fair["new"], fair["ai"]),
                         (2, 2, 1, 1, 1, 1))
        self.assertEqual(fair["countries"][0][1], 2)
        self.assertEqual(fair["marks"], [("⭐ Важливий", 1)])
        none = next(g for g in s["events"] if g["key"] == "-")
        self.assertEqual((none["event"], none["customers"]), (cards.NO_EVENT, 1))
        self.assertEqual(s["total"]["cards"], 3)
        url = reverse("admin:inventory_supplier_cards")
        page = self.client.get(url)
        self.assertContains(page, "📊 Статистика візиток")
        self.assertEqual(len(self.client.get(url + "?event=-").context["items"]), 1)
        self.assertEqual(len(self.client.get(url + "?event=ELECTRONICA 2026").context["items"]), 2)

    def test_delete_many_only_allowed(self):
        import os
        mine, theirs = self.card(), self.card()
        SupplierMedia.objects.filter(pk=theirs.pk).update(created_by=self.other)
        path = os.path.join(self.media, *mine.image.name.split("/"))
        self.assertTrue(os.path.exists(path))
        url = reverse("admin:inventory_supplier_cards")
        self.client.post(url, {"action": "delete_many", "ids": [mine.pk, theirs.pk]})
        self.assertFalse(SupplierMedia.objects.exists())                         # суперкористувачу — усі
        self.assertFalse(os.path.exists(path))                                   # і файли
        mine, theirs = self.card(), self.card()
        SupplierMedia.objects.filter(pk=theirs.pk).update(created_by=self.other)
        real = User.has_perm                                                    # лише «додавати»: чуже — ні
        with mock.patch.object(User, "has_perm", lambda u, perm, obj=None: perm != "inventory.change_supplier"
                               and real(u, perm, obj)):
            self.client.post(url, {"action": "delete_many", "ids": [mine.pk, theirs.pk]})
        self.assertEqual(list(SupplierMedia.objects.values_list("pk", flat=True)), [theirs.pk])

    def test_filter_by_category(self):
        cable, _ = SupplierCategory.objects.get_or_create(name="Виробники кабелів", defaults={"icon": "🔌"})
        a = self.card(name="Victory PCB")
        a.categories.add(self.pcb)
        guessed = self.card(name="Hitech Circuits", categories=["Виробники плат"])     # лише запропонував AI
        b = self.card(name="Mingxin Sleeving")
        b.categories.add(cable)
        sup = self.card(name="PCBWay")
        SupplierMedia.objects.filter(pk=sup.pk).update(supplier=self.supplier)
        self.supplier.categories.add(self.pcb)                                       # категорія постачальника
        url = reverse("admin:inventory_supplier_cards")
        names = lambda resp: sorted(m.info["company"] for m in resp.context["items"])   # noqa: E731
        self.assertEqual(names(self.client.get(url + f"?cat={self.pcb.pk}")), ["Hitech Circuits", "PCBWay", "Victory PCB"])
        self.assertEqual(names(self.client.get(url + f"?cat={cable.pk}")), ["Mingxin Sleeving"])
        page = self.client.get(url)
        self.assertContains(page, f'<option value="{self.pcb.pk}" >🟩 Виробники плат (3)</option>')
        self.assertContains(page, f'href="?cat={cable.pk}"')                         # категорія на плитці — фільтр
        self.assertEqual(guessed.categories.count(), 0)


class HistoryPeriodTests(Base):
    """«🔄 Зібрати / Оновити з пошти» за вибраний період; понад SCAN_MAX — найновіші листи."""
    _att = HistoryTests._att
    _thread = HistoryTests._thread

    def test_period_helpers(self):
        from suppliers.services import history
        today = timezone.localdate()
        self.assertEqual(history.period_of("1"), (today - datetime.timedelta(days=365), today))
        self.assertEqual(history.period_of("all"), (history.ALL_FROM, today))
        self.assertEqual(history.period_of("custom", "2026-01-01", "2026-03-31"),
                         (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31)))
        self.assertEqual(history.period_of("bogus"), (today - datetime.timedelta(days=365 * history.YEARS), today))
        for value in ("1", "2", "5", "all"):
            self.assertEqual(history.period_choice(*history.period_of(value)), value)
        self.assertEqual(history.period_choice(datetime.date(2026, 1, 1), datetime.date(2026, 3, 31)), "custom")
        yesterday = today - datetime.timedelta(days=1)                           # зібрано вчора «за рік»
        self.assertEqual(history.period_choice(yesterday - datetime.timedelta(days=365), yesterday, yesterday), "1")

    def test_rebuild_for_chosen_period(self):
        from suppliers.services import history
        self._thread()                                         # листи 40 днів тому
        url = reverse("admin:inventory_supplier_history", args=[self.supplier.pk])
        card = reverse("admin:inventory_supplier_change", args=[self.supplier.pk])
        self.client.post(url, {"sup_action": "rebuild", "hist_period": "1"})
        data = history.topics_for(self.supplier, self.user)
        self.assertEqual(len(data["topics"]), 2)
        self.assertContains(self.client.get(card), '<option value="1" selected>')
        old = (timezone.localdate() - datetime.timedelta(days=400)).isoformat()
        self.client.post(url, {"sup_action": "rebuild", "hist_period": "custom", "hist_from": old,
                               "hist_to": (timezone.localdate() - datetime.timedelta(days=300)).isoformat()})
        data = history.topics_for(self.supplier, self.user)
        self.assertEqual((data["topics"], data["date_from"].isoformat()), ([], old))
        self.assertContains(self.client.get(card), '<option value="custom" selected>')

    def test_too_many_mails_keep_newest(self):
        from suppliers.services import history
        self._thread()
        with mock.patch.object(mailscan, "SCAN_MAX", 2):
            out = history.rebuild(self.supplier, self.user)
        self.assertTrue(out["truncated"])
        data = history.topics_for(self.supplier, self.user)
        self.assertTrue(data["truncated"])
        self.assertTrue(any(t["title"].startswith("Golden October") for t in data["topics"]))   # найновіший лист є
        with mock.patch.object(mailscan, "SCAN_MAX", 2):
            page = self.client.get(reverse("admin:inventory_supplier_change", args=[self.supplier.pk]))
        self.assertContains(page, "показано найновіші 2")


class PeopleAndBulkTests(Base):
    """👥 Усі люди з листування → контактні особи; дії для вибраних постачальників у списку."""
    _att = HistoryTests._att
    _thread = HistoryTests._thread

    def test_people_from_mail_become_contacts(self):
        from suppliers.services import history, people
        self.mail(frm="anna@pcbway.com", name="Anna Li", subject="Quotation", body="Price\n\n--\nAnna Li\n+86 755 1234 5678")
        self.mail(frm=ME, to=["anna@pcbway.com"], folder="sent", subject="Re: Quotation", body="Thanks")
        m = self.mail(frm=ME, to=["anna@pcbway.com"], folder="sent", subject="Order", body="Order")
        m.cc_emails = ["Bob Wu <bob.wu@pcbway.com>", "colleague@sevskiy.de", "x@other-firm.com"]
        m.save(update_fields=["cc_emails"])
        self.mail(frm="newsletter@pcbway.com", name="PCBWay News", subject="Peak season", body="Promo")
        msgs = [x for g in mailscan.scan(self.supplier, self.user)["groups"] for x in g["messages"]]
        found = {p["email"]: p for p in people.collect(self.supplier, msgs)}
        self.assertEqual(set(found), {"anna@pcbway.com", "bob.wu@pcbway.com"})
        self.assertEqual((found["anna@pcbway.com"]["name"], found["anna@pcbway.com"]["phone"]),
                         ("Anna Li", "+86 755 1234 5678"))
        out = history.rebuild(self.supplier, self.user)
        self.assertEqual(out["contacts"], 2)
        self.assertEqual(sorted(self.supplier.contacts.values_list("name", "email")),
                         [("Anna Li", "anna@pcbway.com"), ("Bob Wu", "bob.wu@pcbway.com")])
        self.assertEqual(history.rebuild(self.supplier, self.user)["contacts"], 0)          # без дублів
        self.assertEqual(people.pretty("cindy.pan@ringpcb.com"), "Cindy Pan")
        self.assertEqual(people.pretty("sales@ringpcb.com"), "Sales")

    def test_actions_redirect_to_bulk_and_rfq(self):
        other = Supplier.objects.create(name="Ring PCB", email="cindy.pan@ringpcb.com")
        url = reverse("admin:inventory_supplier_changelist")
        resp = self.client.post(url, {"action": "action_history", "_selected_action": [self.supplier.pk, other.pk]})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("admin:inventory_supplier_bulk") + "?do=history&ids=", resp["Location"])
        page = self.client.get(resp["Location"])
        self.assertContains(page, "Ring PCB")
        self.assertContains(page, 'name="hist_period"')
        self.assertContains(page, 'name="make_po"')
        resp = self.client.post(url, {"action": "action_rfq", "_selected_action": [self.supplier.pk, other.pk]})
        self.assertIn("?suppliers=", resp["Location"])
        form = self.client.get(resp["Location"])
        self.assertEqual(form.status_code, 200)
        self.assertEqual(set(form.context["picked"]), {self.supplier.pk, other.pk})

    def test_bulk_history_collects_contacts_and_purchases(self):
        self._thread()
        bulk = reverse("admin:inventory_supplier_bulk")
        j = self.client.post(bulk + f"?do=history&ids={self.supplier.pk}",
                             {"do": "history", "ids": str(self.supplier.pk), "hist_period": "1", "make_po": "1",
                              "po_history": "1"}).json()
        s = jobs.state(j["job"], self.user)
        self.assertEqual(s["status"], "done")
        self.assertIn("постачальників 1", s["result"]["message"])
        self.assertEqual(PurchaseOrder.objects.get(supplier=self.supplier).status, "received")
        self.assertIn("🛒 PO-", s["result"]["rows"][0]["text"])
        self.assertTrue(self.supplier.contacts.filter(email="sales@pcbway.com").exists())

    def test_bulk_evaluate_with_ai_and_rights(self):
        self.mail(frm="anna@pcbway.com", body="Boards ready")
        bulk = reverse("admin:inventory_supplier_bulk")
        answer = json.dumps({"summary": "ok", "rating": 4, "problems": []})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.01"))):
            j = self.client.post(bulk, {"do": "evaluate", "ids": str(self.supplier.pk)}).json()
        s = jobs.state(j["job"], self.user)
        self.assertEqual(s["result"]["rows"][0]["text"], "оцінка 4/5")
        self.assertTrue(SupplierAnalysis.objects.filter(supplier=self.supplier, kind=SupplierAnalysis.AI).exists())
        self.client.force_login(self.other)                                     # без права — нічого
        self.assertNotEqual(self.client.post(bulk, {"do": "evaluate", "ids": str(self.supplier.pk)}).status_code, 200)


class SourceEventListTests(Base):
    """Список постачальників: «Звідки» (🎪 з виставки / з пошти / з візитки…) і «Де взяли» — колонки й фільтри."""

    def card_for(self, supplier, event="", marks=()):
        m = SupplierMedia(supplier=supplier, kind=SupplierMedia.CARD, event=event, marks=list(marks))
        m.image.save("c.png", SimpleUploadedFile("c.png", png()), save=False)
        m.save()
        return m

    def test_columns_filters_and_sort(self):
        fair = Supplier.objects.create(name="Victory PCB", source="card")
        self.card_for(fair, "Electronica 2026", ["fair", "star"])
        mail = Supplier.objects.create(name="Ring PCB", source="mail")
        old = Supplier.objects.create(name="Kontron", source="")
        self.card_for(old, "ELECTRONICA 2026")
        url = reverse("admin:inventory_supplier_changelist")
        names = lambda resp: sorted(o.name for o in resp.context["cl"].result_list)   # noqa: E731
        page = self.client.get(url)
        self.assertContains(page, "🎪 З виставки")
        self.assertContains(page, "✉️ З пошти")
        self.assertContains(page, '?where=Electronica+2026')
        self.assertEqual(names(self.client.get(url + "?src=fair")), ["Victory PCB"])
        self.assertEqual(names(self.client.get(url + "?src=mail")), ["Ring PCB"])
        self.assertEqual(names(self.client.get(url + "?src=manual")), ["Kontron", "Shenzhen PCBWay Co., Ltd"])
        self.assertEqual(names(self.client.get(url + "?where=electronica 2026")), ["Kontron", "Victory PCB"])
        self.assertEqual(names(self.client.get(url + "?where=-")), ["Ring PCB", "Shenzhen PCBWay Co., Ltd"])
        lookups = dict(self.client.get(url).context["cl"].filter_specs[3].lookup_choices)
        self.assertEqual(list(lookups), ["-", "Electronica 2026"])                 # регістр — одна подія
        cl = self.client.get(url + "?o=-5").context["cl"]                          # «Де взяли» за спаданням
        self.assertEqual([o.name for o in cl.result_list][:2], ["Victory PCB", "Kontron"])
        self.assertEqual(mail.pk, Supplier.objects.get(name="Ring PCB").pk)


class CardsToSuppliersTests(Base):
    """🏭 Вибрані візитки → постачальники: створити нових, додати до наявних (дописати порожнє), позначка «✅ Уже в базі»."""

    def card(self, **extracted):
        m = SupplierMedia(kind=SupplierMedia.CARD, created_by=self.user, event="electronica 2026", extracted=extracted)
        m.image.save("c.png", SimpleUploadedFile("c.png", png()), save=False)
        m.save()
        return m

    def test_create_attach_skip_and_fill_empty(self):
        from suppliers.models import SupplierContact
        SupplierContact.objects.create(supplier=self.supplier, name="Anna Li", email="anna@pcbway.com")
        known = self.card(name="Shenzhen PCBWay Co., Ltd", contact_name="Anna Li", contact_email="anna@pcbway.com",
                          contact_position="Sales Manager", contact_mobile="+86 138 0000 0000", phone="+86 755 1",
                          addr_city="Shenzhen", addr_country="CN")
        new1 = self.card(name="Hitech Circuits Co., Ltd", email="sales@hitechcircuits.com", contact_name="Susie Zhao")
        new2 = self.card(name="Hitech Circuits", contact_email="susie@hitechcircuits.com")   # та сама фірма
        empty = self.card()
        maybe = self.card(name="PCBWay")                                                     # лише схожа назва
        url = reverse("admin:inventory_supplier_cards")
        page = self.client.get(url)
        self.assertContains(page, "✅ Уже в базі:")
        self.assertContains(page, "❔ Схоже на:")
        self.assertEqual(len(self.client.get(url + "?show=inbase").context["items"]), 2)
        self.client.post(url, {"action": "to_suppliers", "ids": [known.pk, new1.pk, new2.pk, empty.pk, maybe.pk]})
        for m in (known, new1, new2, empty, maybe):
            m.refresh_from_db()
        self.assertEqual(known.supplier, self.supplier)                             # додано до наявного
        self.supplier.refresh_from_db()
        self.assertEqual((self.supplier.phone, self.supplier.addr_city, self.supplier.contact_person),
                         ("+86 755 1", "Shenzhen", "Anna Li"))                     # дописано порожнє
        self.assertEqual(self.supplier.website, "https://www.pcbway.com")           # заповнене не чіпаємо
        anna = self.supplier.contacts.get(email="anna@pcbway.com")
        self.assertEqual((anna.position, anna.mobile), ("Sales Manager", "+86 138 0000 0000"))
        self.assertEqual(self.supplier.contacts.filter(email="anna@pcbway.com").count(), 1)
        hitech = Supplier.objects.get(name="Hitech Circuits Co., Ltd")
        self.assertEqual((new1.supplier, new2.supplier), (hitech, hitech))          # друга візитка — без дубля
        self.assertEqual((hitech.source, hitech.status), ("card", Supplier.STATUS_POTENTIAL))
        self.assertIsNone(empty.supplier)                                           # без даних — пропуск
        self.assertIsNone(maybe.supplier)                                           # «схожа назва» — вирішує людина
        self.assertEqual(Supplier.objects.count(), 2)

    def test_only_editable_cards(self):
        theirs = self.card(name="Bornico Sp. z o.o.", email="m@bornico.com.pl")
        SupplierMedia.objects.filter(pk=theirs.pk).update(created_by=self.other)
        real = User.has_perm
        with mock.patch.object(User, "has_perm", lambda u, perm, obj=None: perm != "inventory.change_supplier"
                               and real(u, perm, obj)):
            self.client.post(reverse("admin:inventory_supplier_cards"), {"action": "to_suppliers", "ids": [theirs.pk]})
        theirs.refresh_from_db()
        self.assertIsNone(theirs.supplier)
        self.assertFalse(Supplier.objects.filter(name="Bornico Sp. z o.o.").exists())


class RequestRecipientFiltersTests(Base):
    """📨 Запит → «🏭 Кому»: фільтри стан / категорія / звідки / де взяли / країна, як у списку постачальників."""

    def test_filter_data_and_options(self):
        fair = Supplier.objects.create(name="Victory PCB", source="card", status=Supplier.STATUS_POTENTIAL,
                                       addr_country="CN")
        m = SupplierMedia(supplier=fair, kind=SupplierMedia.CARD, event="Electronica 2026", marks=["fair"])
        m.image.save("c.png", SimpleUploadedFile("c.png", png()), save=False)
        m.save()
        Supplier.objects.create(name="Never Again Ltd", status=Supplier.STATUS_BLOCKED)
        page = self.client.get(reverse("admin:suppliers_supplierrequest_new"))
        self.assertContains(page, 'data-status="potential" data-src="fair" data-where="electronica 2026" '
                                  'data-country="CN"')
        self.assertContains(page, '<option value="fair">🎪 З виставки</option>')
        self.assertContains(page, '<option value="electronica 2026">Electronica 2026</option>')
        self.assertContains(page, '<option value="CN">')
        self.assertNotContains(page, "Never Again Ltd")                            # ⛔ не працювати — не пропонуємо
        self.assertNotContains(page, '<option value="blocked">')
        self.assertContains(page, "Позначити всіх показаних")


class RequestLetterTests(Base):
    """📨 Лист запиту: свій текст зі змінними, копія (CC), 👁 попередній перегляд, надсилання."""

    def setUp(self):
        super().setUp()
        self.s2 = Supplier.objects.create(name="JLC", email="")
        self.mia = SupplierContact.objects.create(supplier=self.s2, name="Mia Chen", email="mia@jlcpcb.com",
                                                  is_primary=True)
        SupplierContact.objects.create(supplier=self.s2, name="Bob", email="bob@jlcpcb.com")
        self.req = SupplierRequest.objects.create(title="PCB 4 layers", owner=self.user, language="en",
                                                  subject="[{code}] Quote for {company}",
                                                  body="Hi {first_name},\nplease quote {title} by {reply_by}.",
                                                  cc="boss@sevskiy.de, not-an-address",
                                                  reply_by=datetime.date(2026, 12, 1))
        rfq.add_recipients(self.req, [self.s2])

    def test_render_cc_and_send(self):
        r = self.req.recipients.get()
        subject, body = rfq.render(self.req, r)
        self.assertEqual(subject, f"[{self.req.code}] Quote for JLC")
        self.assertEqual(body, "Hi Mia,\nplease quote PCB 4 layers by 01.12.2026.")          # без авто-привітання
        self.assertEqual(rfq.cc_for(self.req, r), ["boss@sevskiy.de"])
        nobody = R(request=self.req, supplier=Supplier(name="Cable Tech GmbH"), email="info@cable.de")
        self.assertTrue(rfq.render(self.req, nobody)[1].startswith("Hi team,"))           # без людини — не «Hi Cable»
        self.req.cc_contacts = True
        self.assertEqual(rfq.cc_for(self.req, r), ["boss@sevskiy.de", "bob@jlcpcb.com"])   # себе — ні
        self.req.body = "Please quote {title}."                                             # без змінних імені
        self.assertTrue(rfq.render(self.req, r)[1].startswith("Dear Mia Chen,\n\nPlease quote PCB 4 layers."))
        self.req.save()
        sent = []

        def fake_send(client, to_emails, subject, body_text, **kw):
            sent.append((to_emails, subject, body_text, kw.get("cc_emails")))
            return {"ok": True, "message_id": "<x@y>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            rfq.send(self.req, self.user)
        self.assertEqual(sent[0][:2], (["mia@jlcpcb.com"], f"[{self.req.code}] Quote for JLC"))
        self.assertEqual(sent[0][3], ["boss@sevskiy.de", "bob@jlcpcb.com"])
        self.req.refresh_from_db()
        self.assertEqual(self.req.subject, "[{code}] Quote for {company}")                  # шаблон теми лишився

    def test_preview_and_form_save(self):
        url = reverse("admin:suppliers_supplierrequest_preview")
        j = self.client.post(url, {"req": self.req.pk, "title": "Cable 1 m", "language": "en",
                                   "subject": "RFQ {code}: {title}", "body": "Hello {name} ({company})",
                                   "cc": "a@b.de", "cc_contacts": "1", "supplier": self.s2.pk,
                                   "contact": self.mia.pk}).json()
        self.assertEqual((j["to"], j["subject"], j["body"]), ("mia@jlcpcb.com", f"RFQ {self.req.code}: Cable 1 m",
                                                              "Hello Mia Chen (JLC)"))
        self.assertEqual(j["cc"], ["a@b.de", "bob@jlcpcb.com"])
        self.req.refresh_from_db()
        self.assertEqual(self.req.title, "PCB 4 layers")                                    # перегляд не зберігає
        self.assertEqual(self.client.post(url, {"body": "x"}).status_code, 400)               # без постачальника
        edit = reverse("admin:suppliers_supplierrequest_edit", args=[self.req.pk])
        page = self.client.get(edit)
        self.assertContains(page, 'data-ph="first_name"')
        self.assertContains(page, "одразу</b> надсилає")
        self.client.post(edit, {"title": "PCB 4 layers", "template": "generic", "language": "en", "rcp": [self.s2.pk],
                                "subject": "S", "body": "B {name}", "cc": "x@y.de; nope, z@w.com",
                                "cc_contacts": "1", "go": "stay"})
        self.req.refresh_from_db()
        self.assertEqual((self.req.cc, self.req.cc_contacts), ("x@y.de, z@w.com", True))
