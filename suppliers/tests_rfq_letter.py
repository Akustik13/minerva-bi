"""✉️ Лист запиту постачальнику: привітання без посади в імені («Anduin Chen Sales Representative» → «Dear Anduin
Chen,»), підпис скриньки — у листі, у копії в «Надіслані» й у перегляді, 👁 перегляд листа перед «📤 Надіслати».

python manage.py test suppliers.tests_rfq_letter --settings=tabele.settings_test
"""
import datetime
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from inventory.models import Supplier
from suppliers.models import SupplierContact, SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import draft, rfq
from suppliers.tests import Base


class LetterTests(Base):
    def setUp(self):
        super().setUp()
        self.req = SupplierRequest.objects.create(
            title="Samples TM270402", template="pcba", category=self.pcb, quantities="5; 2500", language="en",
            owner=self.user, account=self.account, reply_by=timezone.localdate() + datetime.timedelta(days=5),
            status=SupplierRequest.SENT, body="{greeting}\n\nPlease quote.\n\nBest regards,", subject="RFQ {code}")
        self.hong = Supplier.objects.create(name="HongRong Electronics", addr_country="CN")
        self.anduin = SupplierContact.objects.create(supplier=self.hong, name="Anduin Chen Sales Representative",
                                                     email="anduin@hongrong.cn", is_primary=True)
        self.r = R.objects.create(request=self.req, supplier=self.hong, contact=self.anduin, email="anduin@hongrong.cn")
        self.act = reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk])

    def test_person_name_and_greeting(self):
        cases = {("Anduin Chen Sales Representative", ""): "Anduin Chen", ("Wendy Wu - Sales", ""): "Wendy Wu",
                 ("Tom Becker Sales Manager", "Sales Manager"): "Tom Becker", ("Sales Manager", ""): "",
                 ("Dr. Hans Müller, Geschäftsführer", ""): "Dr. Hans Müller", ("Li Na (Purchasing)", ""): "Li Na",
                 ("Mia", ""): "Mia"}
        for (name, pos), want in cases.items():
            self.assertEqual(rfq.person_name(name, pos), want, name)
        self.assertEqual(rfq.greeting(self.req, self.r), "Dear Anduin Chen,")
        self.assertTrue(rfq.render(self.req, self.r)[1].startswith("Dear Anduin Chen,\n"))
        only_title = SupplierContact.objects.create(supplier=self.hong, name="Sales Manager", email="s@hongrong.cn")
        self.r.contact = only_title
        self.assertEqual(rfq.greeting(self.req, self.r), "Dear Sir or Madam,")

    def test_new_contact_name_and_position_split(self):
        c, created = draft.add_contact(self.hong, {"contact_name": "Kevin Zhou Overseas Sales Manager",
                                                  "contact_email": "kevin@hongrong.cn"})
        self.assertTrue(created)
        self.assertEqual((c.name, c.position), ("Kevin Zhou", "Overseas Sales Manager"))

    def _send(self, sig):
        self.account.signature = sig
        self.account.save(update_fields=["signature"])
        calls = []

        def fake_send(client, to_emails, subject, body_text, body_html="", **kw):
            calls.append({"to": to_emails, "text": body_text, "html": body_html, "sig": kw.get("signature")})
            return {"ok": True, "message_id": f"<l{len(calls)}@sevskiy.de>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            rfq.send(self.req, self.user)
        self.r.refresh_from_db()
        return calls, self.r.sent_message

    def test_plain_signature_in_letter_and_sent_copy(self):
        calls, sent = self._send("Viacheslav Pryimak\nSEVSKIY GmbH")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["sig"])                          # підпис додаємо самі — SMTP не дублює
        self.assertIn("Best regards,\n\n-- \nViacheslav Pryimak\nSEVSKIY GmbH", calls[0]["text"])
        self.assertEqual(calls[0]["html"], "")                     # простий підпис — лист простим текстом
        self.assertIn("SEVSKIY GmbH", sent.body_text)              # копія в «Надіслані» — така сама

    def test_html_signature_goes_as_html(self):
        calls, sent = self._send('<p><b>Viacheslav</b><br><img src="https://sevskiy.de/logo.png" alt=""></p>'
                                 '<script>x()</script>')
        self.assertIn("<b>Viacheslav</b>", calls[0]["html"])
        self.assertIn("https://sevskiy.de/logo.png", calls[0]["html"])
        self.assertNotIn("<script", calls[0]["html"])
        self.assertIn("Dear Anduin Chen,", calls[0]["html"])
        self.assertIn("-- \nViacheslav", calls[0]["text"])
        self.assertIn("<b>Viacheslav</b>", sent.body_html)

    def test_preview_before_send(self):
        self.account.signature = "Viacheslav Pryimak"
        self.account.save(update_fields=["signature"])
        url = reverse("admin:suppliers_supplierrequest_letters", args=[self.req.pk])
        j = self.client.get(url).json()
        self.assertEqual(len(j["letters"]), 1)
        L = j["letters"][0]
        self.assertEqual((L["to"], L["subject"]), ("anduin@hongrong.cn", f"RFQ {self.req.code}"))
        self.assertTrue(L["body"].startswith("Dear Anduin Chen,"))
        self.assertEqual(j["signature"]["text"], "Viacheslav Pryimak")
        self.assertEqual(j["from"], self.account.email_address)
        other = R.objects.create(request=self.req, supplier=self.supplier, email="sales@pcbway.com")
        self.assertEqual([x["rid"] for x in self.client.get(url, {"rid": other.pk}).json()["letters"]], [other.pk])
        self.r.state = R.SENT
        self.r.save(update_fields=["state"])
        self.assertEqual([x["rid"] for x in self.client.get(url).json()["letters"]], [other.pk])   # лише ненадіслані
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn('id="rw-pv"', page)
        self.assertIn('class="rw-send"', page)
        # без підпису — попередження з посиланням на скриньку
        self.account.signature = ""
        self.account.save(update_fields=["signature"])
        self.user.profile.smtp_signature = ""
        self.user.profile.save()
        j = self.client.get(url).json()
        self.assertEqual(j["signature"], {"text": "", "html": ""})
        self.assertTrue(j["signature_url"])
