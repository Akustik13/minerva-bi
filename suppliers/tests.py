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
