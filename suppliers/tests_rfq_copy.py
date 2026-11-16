"""📋 Копія запиту постачальникам: форма нового запиту на основі старого, файли, кому, підсвічені поля.

python manage.py test suppliers.tests_rfq_copy --settings=tabele.settings_test
"""
import datetime
import json
import re
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from inventory.models import Supplier
from suppliers.admin import SupplierRequestAdmin
from suppliers.models import SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import rfq, rfq_copy
from suppliers.tests import Base


class CopyTests(Base):
    def setUp(self):
        super().setUp()
        self.src = SupplierRequest.objects.create(
            title="PCB 4 layers 100x80", template="pcb", category=self.pcb, group="Плати для X", quantities="10; 100",
            specs={"layers": "4", "finish": "ENIG", "_notes": "RoHS"}, language="en", owner=self.user,
            account=self.account, reply_by=timezone.localdate() + datetime.timedelta(days=10),
            status=SupplierRequest.ANSWERED, cc="boss@sevskiy.de")
        self.src.created_at = timezone.now()
        self.src.save()
        d = self.src.reply_by.strftime("%d.%m.%Y")
        self.src.body = f"{{greeting}}\n\nPlease quote PCB 4 layers 100x80.\nReply by {d}.\nRef: {self.src.code}"
        self.src.subject = rfq.subject_for(self.src)          # тему склала Minerva — у копії складеться заново
        self.src.save()
        self.f1 = rfq.add_file(self.src, SimpleUploadedFile("gerber.zip", b"PK zip"), self.user)
        self.f2 = rfq.add_file(self.src, SimpleUploadedFile("old drawing.pdf", b"%PDF old"), self.user)
        self.quoted = Supplier.objects.create(name="JLC Ltd", email="sales@jlc.com")
        self.blocked = Supplier.objects.create(name="Bad Corp", email="x@bad.com", status=Supplier.STATUS_BLOCKED)
        R.objects.create(request=self.src, supplier=self.supplier, email="sales@pcbway.com", state=R.SILENT)
        R.objects.create(request=self.src, supplier=self.quoted, email="sales@jlc.com", state=R.QUOTED, price=1)
        R.objects.create(request=self.src, supplier=self.blocked, email="x@bad.com", state=R.SENT)
        self.new = reverse("admin:suppliers_supplierrequest_new")

    def form_post(self, **extra):
        data = {"copy_from": self.src.pk, "title": self.src.title, "template": "pcb", "category": self.pcb.pk,
                "group": self.src.group, "quantities": self.src.quantities, "language": "en",
                "reply_by": rfq_copy.reply_by_for(self.src).isoformat(), "spec_layers": "4", "spec_finish": "ENIG",
                "notes": "RoHS", "subject": "", "body": rfq_copy.templatize(self.src.body, self.src)[0],
                "cc": self.src.cc, "rcp": [self.supplier.pk, self.quoted.pk], "account": self.account.pk, "go": "stay"}
        data.update(extra)
        return self.client.post(self.new, data)

    def test_form_prefilled_highlighted_nothing_created(self):
        page = self.client.get(self.new, {"copy": self.src.pk}).content.decode()
        self.assertEqual(SupplierRequest.objects.count(), 1)                         # до «Зберегти» — нічого
        self.assertIn("📋 Новий запит на основі", page)
        self.assertIn(f'name="copy_from" value="{self.src.pk}"', page)
        self.assertIn('value="PCB 4 layers 100x80"', page)
        self.assertIn("не брати", page)
        review = json.loads(re.search(r'<script id="rq-review" type="application/json">(.*?)</script>', page, re.S)[1])
        self.assertEqual(review["title"]["level"], "must")
        self.assertEqual(review["reply_by"]["level"], "maybe")
        self.assertIn("spec:layers", review)
        self.assertIn("files", review)
        self.assertIn("{code}", review["body"]["value"])                              # старий номер → змінна
        self.assertIn("{reply_by}", review["body"]["value"])
        self.assertNotIn(self.src.code, review["body"]["value"])
        self.assertNotIn("subject", review)                                           # тему складе Minerva
        groups = json.loads(re.search(r'<script id="rq-copy-data" type="application/json">(.*?)</script>', page, re.S)[1])
        self.assertEqual(groups["all"], [self.supplier.pk, self.quoted.pk])          # ⛔ — ні
        self.assertEqual(groups["quoted"], [self.quoted.pk])
        self.assertIn("Bad Corp", page)                                               # «не потрапили»

    def test_save_copies_kept_files_and_keeps_unchanged_fields_highlighted(self):
        res = self.form_post(quantities="500", del_file=[self.f2.pk])
        new = SupplierRequest.objects.exclude(pk=self.src.pk).get()
        self.assertRedirects(res, reverse("admin:suppliers_supplierrequest_edit", args=[new.pk]),
                             fetch_redirect_response=False)
        self.assertEqual((new.copied_from, new.status, new.owner), (self.src, SupplierRequest.DRAFT, self.user))
        self.assertEqual([f.name for f in new.files.all()], ["gerber.zip"])           # «не брати» — не скопійовано
        f = new.files.get()
        self.assertNotEqual(f.file.name, self.f1.file.name)                           # своя копія на диску
        with f.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"PK zip")
        self.assertEqual(new.subject, rfq.subject_for(new))                           # новий номер у темі
        self.assertIn(new.code, new.subject)
        self.assertEqual(set(new.recipients.values_list("supplier_id", flat=True)), {self.supplier.pk, self.quoted.pk})
        self.assertTrue(all(r.state == R.PLANNED for r in new.recipients.all()))
        self.assertIn("title", new.review)                                            # не змінили — підсвічено
        self.assertNotIn("quantities", new.review)                                    # змінили — ні
        self.assertNotIn("files", new.review)                                         # «не брати» — файли переглянуто
        self.assertEqual(self.src.files.count(), 2)                                    # старий запит не зачеплено
        self.assertEqual(self.src.recipients.count(), 3)

    def test_reviewed_field_and_all_done(self):
        self.form_post(reviewed=["title", "spec:layers"])
        new = SupplierRequest.objects.exclude(pk=self.src.pk).get()
        self.assertNotIn("title", new.review)
        self.assertNotIn("spec:layers", new.review)
        self.assertIn("spec:finish", new.review)
        self.assertIn("files", new.review)                                            # узяли всі — ще перевірити
        edit = reverse("admin:suppliers_supplierrequest_edit", args=[new.pk])
        page = self.client.get(edit).content.decode()
        self.assertIn("rq-review", page)
        self.assertIn(f">{self.src.code}<", page)                                     # «Копія запиту RFQ-…»
        work = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[new.pk])).content.decode()
        self.assertIn("Після копіювання ще не перевірено", work)
        self.client.post(reverse("admin:suppliers_supplierrequest_action", args=[new.pk]), {"do": "review_done"})
        new.refresh_from_db()
        self.assertEqual(new.review, {})
        self.assertNotIn("Після копіювання ще не перевірено",
                         self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[new.pk])).content.decode())

    def test_preview_and_test_mail_use_kept_files_of_source(self):
        data = {"copy_from": self.src.pk, "title": "X", "language": "en", "subject": "", "body": "Hello",
                "supplier": self.supplier.pk, "del_file": [self.f2.pk]}
        out = self.client.post(reverse("admin:suppliers_supplierrequest_preview"), data).json()
        self.assertEqual(out["files"], ["gerber.zip"])

    def test_list_link_action_and_work_button(self):
        changelist = reverse("admin:suppliers_supplierrequest_changelist")
        page = self.client.get(changelist).content.decode()
        self.assertIn(f"{self.new}?copy={self.src.pk}", page)
        res = self.client.post(changelist, {"action": "copy_selected", "_selected_action": [self.src.pk]})
        self.assertRedirects(res, f"{self.new}?copy={self.src.pk}", fetch_redirect_response=False)
        other = SupplierRequest.objects.create(title="Cables", owner=self.user)
        res = self.client.post(changelist, {"action": "copy_selected", "_selected_action": [self.src.pk, other.pk]},
                               follow=True)
        self.assertContains(res, "Виберіть один запит")
        work = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.src.pk])).content.decode()
        self.assertIn("📋 Копіювати як новий", work)
        self.form_post()
        page = self.client.get(changelist).content.decode()
        self.assertIn("⚠️ перевірити", page)
        self.assertIn(f"📋 з <span translate=\"no\">{self.src.code}", page)

    def test_without_add_permission_no_copy(self):
        with mock.patch.object(SupplierRequestAdmin, "has_add_permission", return_value=False):
            self.assertEqual(self.client.get(self.new, {"copy": self.src.pk}).status_code, 404)
            self.assertNotIn("?copy=", self.client.get(
                reverse("admin:suppliers_supplierrequest_changelist")).content.decode())
            self.assertNotIn("📋 Копіювати як новий", self.client.get(
                reverse("admin:suppliers_supplierrequest_work", args=[self.src.pk])).content.decode())


class CopyHelperTests(Base):
    def test_helpers(self):
        src = SupplierRequest.objects.create(title="Cable SMA 500mm", owner=self.user,
                                             reply_by=timezone.localdate() + datetime.timedelta(days=3))
        text, done = rfq_copy.templatize(f"{src.code}: Cable SMA 500mm by {src.reply_by:%d.%m.%Y}", src)
        self.assertEqual(text, "{code}: {title} by {reply_by}")
        self.assertEqual(done, ["title", "code", "reply_by"])
        short = SupplierRequest(title="Plate", code="RFQ-1")
        self.assertEqual(rfq_copy.templatize("Plate RFQ-1", short)[0], "Plate {code}")   # коротку назву — ні
        today = datetime.date(2026, 11, 16)
        self.assertEqual(rfq_copy.reply_by_for(src, today), today + datetime.timedelta(days=3))
        self.assertEqual(rfq_copy.reply_by_for(SupplierRequest(), today), today + datetime.timedelta(days=7))
        self.assertEqual(rfq_copy.norm("a  \r\nb \n\n"), "a\nb")
        src.review = {"title": "Cable SMA 500mm", "group": "old"}
        self.assertEqual(rfq_copy.settle(src.review, src), {"title": "Cable SMA 500mm"})
        self.assertEqual(rfq_copy.settle(src.review, src, ["title"]), {})
        self.assertEqual(rfq_copy.pending(src)[0]["label"], "Що запитуємо")
        src.status = SupplierRequest.SENT
        self.assertEqual(rfq_copy.pending(src), [])                                   # надісланий — не підсвічуємо
