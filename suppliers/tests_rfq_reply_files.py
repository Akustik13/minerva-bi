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
        self.assertIn("MVAttNames.all(this)", html)
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


class ProjectFilesTests(Base):
    """📁 Проєкт гілки: документи проєкту, запити RFQ проєкту (файли запиту й від постачальників), інші гілки — по
    листах; лише свої листи; що вже показано для гілки — не вдруге."""

    def setUp(self):
        super().setUp()
        from django.core.files.base import ContentFile
        from projects.services import create_project
        from rag_assistant import knowledge_items as ki
        from suppliers.models import SupplierReplyFile
        self.th = EmailThread.objects.create(account=self.account, subject="Neue Testboards")
        self.cur = self.mail(subject="Re: Neue Testboards", thread=self.th, frm="martin@iot.example")
        self.project = create_project(name="Transponder 2", user=self.user, threads=[self.th])
        self.other_th = EmailThread.objects.create(account=self.account, subject="Gehäuse Zeichnung")
        from projects.services import link_thread
        link_thread(self.project, self.other_th, user=self.user)
        self.old = self.mail(subject="Gehäuse Zeichnung", thread=self.other_th, frm="cad@werk.example",
                             name="CAD Werk")
        self.old.attachments = [{"name": "housing_v3.step", "size": 2048},
                                {"name": "logo.png", "size": 10, "is_inline": True}]
        self.old.save(update_fields=["attachments"])
        # чужа скринька в тій самій гілці проєкту — не показується
        self.foreign = self.mail(subject="Gehäuse", thread=self.other_th, account=self.other_account)
        self.foreign.attachments = [{"name": "secret.pdf", "size": 1}]
        self.foreign.save(update_fields=["attachments"])
        self.doc = ki.add_file(self.user, SimpleUploadedFile("Pflichtenheft.pdf", b"%PDF spec"),
                               links={"project": self.project})
        self.req = SupplierRequest.objects.create(title="Gehäuse", owner=self.user, account=self.account,
                                                  project=self.project, status=SupplierRequest.ANSWERED)
        self.req_file = rfq.add_file(self.req, SimpleUploadedFile("housing_drawing.pdf", b"%PDF draw"), self.user)
        rcp = R.objects.create(request=self.req, supplier=self.supplier, email="anna@pcbway.com", state=R.QUOTED)
        self.reply_file = SupplierReplyFile(recipient=rcp, name="Quotation_PCBWay.pdf", size=9, sha256="x")
        self.reply_file.file.save("Quotation_PCBWay.pdf", ContentFile(b"%PDF quote"), save=False)
        self.reply_file.save()

    def options(self):
        return attachments.reply_options([self.cur], [], self.th, self.user, self.account)

    def test_project_sections_by_source(self):
        opts = self.options()
        self.assertEqual(len(opts["projects"]), 1)
        p = opts["projects"][0]
        self.assertEqual((p["code"], p["name"]), (self.project.code, "Transponder 2"))
        kinds = [g["kind"] for g in p["groups"]]
        self.assertEqual(kinds, ["docs", "rfq", "thread"])
        docs, req, thread = p["groups"]
        self.assertEqual(docs["parts"][0]["files"][0]["ref"], f"kb:{self.doc.pk}")
        self.assertEqual([(x["kind"], [f["ref"] for f in x["files"]]) for x in req["parts"]],
                         [("request", [f"rfq:{self.req_file.pk}"]), ("supplier", [f"rfqreply:{self.reply_file.pk}"])])
        self.assertEqual(req["parts"][1]["who"], self.supplier.name)
        self.assertIn(self.req.code, req["name"])
        self.assertEqual(thread["name"], "Gehäuse Zeichnung")
        letter = thread["parts"][0]
        self.assertEqual((letter["kind"], letter["who"], [f["name"] for f in letter["files"]]),
                         ("letter", "CAD Werk", ["housing_v3.step"]))        # без вбудованої картинки
        self.assertNotIn("secret.pdf", str(opts))                            # чужа скринька
        self.assertEqual(p["count"], 4)
        self.assertEqual(opts["count"], 4)

    def test_current_thread_and_its_request_not_repeated(self):
        self.req.recipients.update(thread=self.th)                           # запит — цієї гілки
        opts = self.options()
        self.assertEqual([f["ref"] for f in opts["rfq_files"]], [f"rfq:{self.req_file.pk}"])
        self.assertEqual([g["kind"] for g in opts["projects"][0]["groups"]], ["docs", "thread"])

    def test_no_project_permission(self):
        with mock.patch.object(get_user_model(), "has_perm",
                               side_effect=lambda perm, obj=None: perm != "projects.view_project"):
            self.assertEqual(self.options()["projects"], [])

    def test_refs_load_for_sending(self):
        kb = attachments.load_ref(self.account, f"kb:{self.doc.pk}")
        self.assertEqual((kb["name"], kb["content"]), ("Pflichtenheft.pdf", b"%PDF spec"))
        rep = attachments.load_ref(self.account, f"rfqreply:{self.reply_file.pk}")
        self.assertEqual((rep["name"], rep["content"]), ("Quotation_PCBWay.pdf", b"%PDF quote"))
        # 🔒 приватний документ іншої людини — не вкладається
        from rag_assistant.models import KnowledgeItem
        KnowledgeItem.objects.filter(pk=self.doc.pk).update(visibility=KnowledgeItem.PRIVATE, owner=self.other)
        self.assertIsNone(attachments.load_ref(self.account, f"kb:{self.doc.pk}"))

    def test_pages_show_project_sections(self):
        html = self.client.get("/email/compose/", {"reply_to": self.cur.pk}).content.decode()
        self.assertIn('class="em-att-proj"', html)
        self.assertIn(f'data-ref="kb:{self.doc.pk}"', html)
        self.assertIn(f'data-ref="rfqreply:{self.reply_file.pk}"', html)
        self.assertIn('onclick="attAdd(this)"', html)
        pv = self.client.get(f"/email/message/{self.cur.pk}/preview/").content.decode()
        self.assertIn(f'data-ref="msg:{self.old.pk}:0"', pv)
        self.assertIn('onclick="pvAttRef(this)"', pv)
        self.assertIn('id="pv-att-orderdocs"', pv)
