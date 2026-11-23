"""📨 Файли запиту постачальникам (RFQ) — ще раз у відповіді в гілці запиту: «📂 З Minerva» на сторінці «Написати»,
«📎» швидкої відповіді в прев'ю, вкладення «rfq:<pk>» (файл додає сервер), файли, згадані в тексті, — самі.

python manage.py test suppliers.tests_rfq_reply_files --settings=tabele.settings_test
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from email_assistant import attachments
from email_assistant.models import EmailThread
from suppliers.models import SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import rfq
from suppliers.tests import Base

ZIP = "TM270402-04H _4L_7628_1x34x85.zip"


class RfqReplyFilesTests(Base):
    def setUp(self):
        super().setUp()
        self.req = SupplierRequest.objects.create(title="PCBA TM270402-04H", owner=self.user, account=self.account,
                                                  status=SupplierRequest.ANSWERED)
        self.f_zip = rfq.add_file(self.req, SimpleUploadedFile(ZIP, b"PK gerber"), self.user)
        self.f_bom = rfq.add_file(self.req, SimpleUploadedFile("BOM_TM270402-04H.xlsx", b"PK bom"), self.user)
        self.th = EmailThread.objects.create(account=self.account, subject="RFQ")
        R.objects.create(request=self.req, supplier=self.supplier, email="anna@pcbway.com", state=R.REPLIED,
                         thread=self.th)
        self.reply = self.mail(subject=f"RE: RFQ {self.req.code}: PCBA", thread=self.th,
                               body="Please send the files again.")

    def test_thread_offers_request_files(self):
        files = attachments.rfq_files([self.reply], self.th, self.user)
        self.assertEqual([f["ref"] for f in files], [f"rfq:{self.f_zip.pk}", f"rfq:{self.f_bom.pk}"])
        self.assertEqual({f["group"] for f in files}, {self.req.code})
        self.assertEqual(attachments.reply_options([self.reply], [], self.th, self.user)["rfq_files"], files)
        # інша гілка — лише за номером запиту в темі
        other = self.mail(subject=f"Fwd: {self.req.code}", thread=EmailThread.objects.create(account=self.account))
        self.assertEqual(len(attachments.rfq_files([other], other.thread, self.user)), 2)
        plain = self.mail(subject="Hello", thread=EmailThread.objects.create(account=self.account))
        self.assertEqual(attachments.rfq_files([plain], plain.thread, self.user), [])

    def test_no_permission_no_files(self):
        with mock.patch.object(get_user_model(), "has_perm", return_value=False):
            self.assertEqual(attachments.rfq_files([self.reply], self.th, self.user), [])
            self.assertIsNone(attachments.load_ref(self.account, f"rfq:{self.f_zip.pk}"))

    def test_ref_loads_file_for_sending(self):
        att = attachments.load_ref(self.account, f"rfq:{self.f_zip.pk}")
        self.assertEqual((att["name"], att["content"]), (ZIP, b"PK gerber"))
        self.assertIn("zip", att["content_type"])
        self.assertIsNone(attachments.load_ref(self.account, "rfq:999999"))
        items, err = attachments.from_form(self.account, [f"rfq:{self.f_bom.pk}"], ["BOM_TM270402-04H.xlsx"], [],
                                           check=True)
        self.assertEqual(err, "")
        loaded, missing = attachments.load_items(self.account, items)
        self.assertEqual((missing, loaded[0]["content"]), ("", b"PK bom"))

    def test_compose_reply_has_minerva_picker(self):
        html = self.client.get("/email/compose/", {"reply_to": self.reply.pk}).content.decode()
        self.assertIn('id="att-pick-btn"', html)
        self.assertIn(f'data-ref="rfq:{self.f_zip.pk}"', html)
        self.assertIn(self.req.code, html)
        self.assertIn("attAddAll(this)", html)
        self.assertIn("_attAuto", html)                    # згадані в тексті — самі
        new = self.client.get("/email/compose/").content.decode()
        self.assertNotIn('id="att-pick-btn"', new)          # новий лист без гілки — без «З Minerva»

    def test_preview_reply_lists_request_files(self):
        html = self.client.get(f"/email/message/{self.reply.pk}/preview/").content.decode()
        self.assertIn(f'data-ref="rfq:{self.f_bom.pk}"', html)
        self.assertIn("_pvAttMentioned", html)

    def test_send_reply_with_request_file(self):
        with mock.patch("email_assistant.smtp_client.SMTPClient.send",
                        return_value={"ok": True, "message_id": "<out1@x>"}) as send:
            d = self.client.post(f"/email/compose/?reply_to={self.reply.pk}", {
                "to": "anna@pcbway.com", "subject": "Re: files", "body": f"im Anhang: {ZIP}",
                "att_ref": [f"rfq:{self.f_zip.pk}"], "att_ref_name": [ZIP]}).json()
        self.assertTrue(d["ok"], d)
        sent = send.call_args.kwargs["attachments"]
        self.assertEqual([(a["name"], a["content"]) for a in sent], [(ZIP, b"PK gerber")])
        self.assertEqual(send.call_args.kwargs["reply_to_message"], self.reply)
