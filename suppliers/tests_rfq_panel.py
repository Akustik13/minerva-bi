"""📨 Запит постачальникам: вкладення з відповідей у запиті, панель рядка, підказка при наведенні, /email/?msg=.

python manage.py test suppliers.tests_rfq_panel --settings=tabele.settings_test
"""
import datetime
import os
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from suppliers.models import SupplierReplyFile, SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import rfq, rfq_replies
from suppliers.tests import ME, Base


class ReplyPanelTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailThread
        self.req = SupplierRequest.objects.create(title="PCB 4 layers", quantities="5", owner=self.user,
                                                  account=self.account, body="Please quote", subject="RFQ",
                                                  status=SupplierRequest.SENT)
        self.thread = EmailThread.objects.create(account=self.account, subject=f"[{self.req.code}] PCB")
        t0 = timezone.now() - datetime.timedelta(days=2)
        self.sent = self.mail(frm=ME, to=["sales@pcbway.com"], folder="sent", subject=f"[{self.req.code}] PCB",
                              thread=self.thread, body="Please quote", when=t0)
        self.r = R.objects.create(request=self.req, supplier=self.supplier, email="sales@pcbway.com", state=R.SENT,
                                  sent_message=self.sent, thread=self.thread, message_id=self.sent.message_id,
                                  sent_at=t0)

    def att(self, m, name, data, ctype="application/pdf", stored=True, inline=False):
        item = {"name": name, "size": len(data), "content_type": ctype}
        if inline:
            item["is_inline"] = True
        if stored:
            rel = f"email_attachments/{m.account_id}/{m.pk}_{len(m.attachments or [])}_{name}"
            path = os.path.join(self.media, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
            item["file_path"] = rel
        m.attachments = list(m.attachments or []) + [item]
        m.save(update_fields=["attachments"])

    def reply(self, body="Dear Slava,\nOur quote: 1.20 USD per pcs, 7 days.\n\n> Please quote", files=()):
        m = self.mail(frm="sales@pcbway.com", subject=f"Re: [{self.req.code}] PCB", thread=self.thread, body=body)
        for f in files:
            self.att(m, *f)
        return m

    def test_reply_attachments_saved_once_without_signature_logos(self):
        m = self.reply(files=[("Quote 2026.pdf", b"%PDF-1.4 quote"), ("image001.png", b"\x89PNG logo", "image/png"),
                              ("Drawing.pdf", b"%PDF-1.4 drawing", "application/pdf", True, True)])
        self.assertEqual(rfq.on_incoming(m), 1)
        self.assertEqual([f.name for f in self.r.reply_files.all()], ["Quote 2026.pdf"])   # без логотипа й вбудованого
        f = self.r.reply_files.get()
        with f.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 quote")
        self.assertTrue(f.file.name.startswith(f"supplier_requests/{self.req.pk}/replies/{self.r.pk}/"))
        # наступний лист у гілці: той самий файл ще раз і новий — у запиті лише новий
        m2 = self.reply(body="PI attached", files=[("Quote 2026 (1).pdf", b"%PDF-1.4 quote"),
                                                   ("PI-77.pdf", b"%PDF-1.4 proforma")])
        rfq.on_incoming(m2)
        self.assertEqual(sorted(self.r.reply_files.values_list("name", flat=True)), ["PI-77.pdf", "Quote 2026.pdf"])
        self.assertEqual(rfq_replies.collect(self.r)["saved"], 0)                              # повтор нічого не додає
        # наш лист у гілці — не вкладення постачальника
        ours = self.mail(frm=ME, folder="sent", thread=self.thread, subject="Re: PCB", body="ok")
        self.att(ours, "our-po.pdf", b"%PDF-1.4 ours")
        rfq_replies.collect(self.r)
        self.assertFalse(SupplierReplyFile.objects.filter(name="our-po.pdf").exists())

    def test_files_not_on_disk_come_from_mail_only_when_asked(self):
        m = self.reply(files=[("Offer.xlsx", b"PK offer", "application/vnd.ms-excel", False)])
        rfq.on_incoming(m)
        self.assertEqual(self.r.reply_files.count(), 0)
        url = reverse("admin:suppliers_supplierrequest_panel", args=[self.req.pk, self.r.pk])
        page = self.client.get(url).content.decode()
        self.assertIn('data-waiting="1"', page)
        self.assertIn("забираю", page)

        def fake_fetch(msg, indexes):
            rel = f"email_attachments/{msg.account_id}/fetched_{msg.pk}.xlsx"
            path = os.path.join(self.media, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"PK offer")
            return {indexes[0]: rel}
        with mock.patch("email_assistant.attachments.fetch_documents", side_effect=fake_fetch) as fetch:
            page = self.client.get(url + "?fetch=1").content.decode()
        fetch.assert_called_once()
        self.assertIn("Offer.xlsx", page)
        self.assertIn('data-waiting="0"', page)
        m.refresh_from_db()
        self.assertTrue(m.attachments[0]["file_path"])                                         # наступного разу — з диска
        with mock.patch("email_assistant.attachments.fetch_documents", side_effect=OSError("imap down")):
            self.assertEqual(rfq_replies.collect(self.r, fetch=True)["saved"], 0)               # нічого вже не бракує

    def test_mail_error_shown_and_rest_works(self):
        m = self.reply(files=[("Offer.xlsx", b"PK", "application/vnd.ms-excel", False)])
        rfq.on_incoming(m)
        url = reverse("admin:suppliers_supplierrequest_panel", args=[self.req.pk, self.r.pk])
        with mock.patch("email_assistant.attachments.fetch_documents", side_effect=OSError("imap down")):
            page = self.client.get(url + "?fetch=1").content.decode()
        self.assertIn("imap down", page)
        self.assertIn("Our quote: 1.20 USD", page)

    def test_panel_letter_text_only_for_mailbox_owner(self):
        m = self.reply(files=[("Quote.pdf", b"%PDF-1.4 q")])
        rfq.on_incoming(m)
        url = reverse("admin:suppliers_supplierrequest_panel", args=[self.req.pk, self.r.pk])
        page = self.client.get(url).content.decode()
        for text in ("Our quote: 1.20 USD", "Quote.pdf", f"/email/?msg={m.pk}", "↗ Відкрити в пошті", "відповідь"):
            self.assertIn(text, page)
        self.assertNotIn("&gt; Please quote", page)                                            # цитата прибрана
        # колега бачить запит і файли, але не листи чужої скриньки
        from django.contrib.auth.models import User
        User.objects.filter(pk=self.other.pk).update(is_superuser=True)
        self.client.force_login(self.other)
        page = self.client.get(url).content.decode()
        self.assertIn("Quote.pdf", page)
        self.assertIn("🔒", page)
        self.assertNotIn("Our quote", page)
        self.assertNotIn(f"/email/?msg={m.pk}", page)
        self.assertNotIn('class="rwp-subj"', page)                                              # тема листа — теж ні

    def test_work_page_reply_peek_files_count_and_panel(self):
        m = self.reply(files=[("Quote.pdf", b"%PDF-1.4 q")])
        rfq.on_incoming(m)
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn(f'data-rw-reply="{self.r.pk}"', page)
        self.assertIn("Our quote: 1.20 USD", page)                                              # підказка при наведенні
        self.assertIn("📎 1", page)
        self.assertIn('id="rw-panel"', page)
        self.assertIn(f'data-rw-row="{self.r.pk}"', page)
        from django.contrib.auth.models import User
        User.objects.filter(pk=self.other.pk).update(is_superuser=True)
        self.client.force_login(self.other)
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertNotIn("Our quote", page)
        self.assertNotIn(f'data-rw-reply="{self.r.pk}"', page)
        self.assertIn("📎 1", page)

    def test_panel_unknown_recipient_404(self):
        url = reverse("admin:suppliers_supplierrequest_panel", args=[self.req.pk, 999999])
        self.assertEqual(self.client.get(url).status_code, 404)


class InboxOpenMessageTests(Base):
    def test_msg_link_opens_that_letter(self):
        m = self.mail(subject="Quote")
        page = self.client.get("/email/", {"msg": m.pk})
        self.assertEqual(page.context["open_msg"], m.pk)
        self.assertIn(f"var pk = {m.pk};", page.content.decode())
        foreign = self.mail(account=self.other_account, subject="Olga's")
        self.assertEqual(self.client.get("/email/", {"msg": foreign.pk}).context["open_msg"], -1)
        self.assertEqual(self.client.get("/email/").context["open_msg"], 0)
