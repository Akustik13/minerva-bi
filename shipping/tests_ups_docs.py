"""📑 Документи для митниці в UPS: тип, до якої посилки, папка замовлення, кілька файлів; 🔧 автоперевірка номерів.

    python manage.py test shipping.tests_ups_docs --settings=tabele.settings_test
"""
import os
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from shipping.models import Shipment
from shipping.services import ups_docs
from shipping.tests_booking import BookingBase
from shipping.ups_client import UPSClient

TN1 = "1Z0R6B300439360978"
TN2 = "1Z0R6B300419812857"


class DetectTests(BookingBase):
    def test_types_by_name_and_text(self):
        cases = {
            "ABD_187472.pdf": "004", "Ausfuhrbegleitdokument.pdf": "004", "Invoice_17701.pdf": "002",
            "Rechnung 17701.docx": "002", "Packing List_17701.pdf": "010", "Lieferschein.pdf": "010",
            "Ursprungszeugnis.pdf": "003", "Vollmacht UPS.pdf": "009", "DigiKey_PackList_102519268.pdf": "010",
        }
        for name, code in cases.items():
            self.assertEqual(ups_docs.detect(name)[0], code, name)
        self.assertEqual(ups_docs.detect("scan001.pdf", "Ausfuhrbegleitdokument MRN 26DE…")[0], "004")
        self.assertEqual(ups_docs.detect("scan001.pdf", "nothing here"), (None, ""))

    def test_match_by_tracking_mrn_and_date(self):
        now = timezone.now()
        Shipment.objects.filter(pk=self.shipment.pk).update(tracking_number=TN1, submitted_at=now - timedelta(days=2))
        self.reload()
        second = Shipment.objects.create(order=self.order, carrier=self.carrier, tracking_number=TN2,
                                         status=Shipment.Status.LABEL_READY, submitted_at=now,
                                         export_mrn="26DE760298696748B4")
        parcels = ups_docs.live_parcels(self.order)
        self.assertTrue(ups_docs.match(second, parcels, f"Sendung {TN2}", now)["here"])
        m = ups_docs.match(second, parcels, f"Sendung {TN1}", now)
        self.assertEqual((m["here"], m["other"]), (False, 1))
        self.assertTrue(ups_docs.match(second, parcels, "", now, "26DE760298696748B4")["here"])
        m = ups_docs.match(second, parcels, "", now - timedelta(days=2))
        self.assertEqual((m["here"], m["other"]), (None, 1))


class UpsDocsViewTests(BookingBase):
    def setUp(self):
        super().setUp()
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number=TN1, carrier_shipment_id=TN1,
            package_tracking=[TN1, "1Z0R6B300439360987"], recipient_country="QA")
        self.reload()
        folder = ups_docs.order_folder(self.order)
        os.makedirs(folder, exist_ok=True)
        for name, body in (("Invoice_300002.pdf", b"%PDF inv"), ("Packing List_300002.pdf", b"%PDF pl"),
                           (f"ups_{self.shipment.pk}_{TN1}.pdf", b"%PDF label"), ("notes.dymo", b"x"),
                           (f"ups_customs_{self.shipment.pk}_{TN1}.pdf", b"%PDF ups inv"),
                           (f"ups_doc_{self.shipment.pk}_ABD.pdf", b"%PDF copy")):
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(body)
        self.url = f"/admin/shipping/shipment/{self.shipment.pk}/ups-abd/"
        self.detail = f"/admin/shipping/shipment/{self.shipment.pk}/detail/"

    def test_order_folder_candidates_without_labels(self):
        rows = {r["name"]: r for r in ups_docs.candidates(self.shipment, self.user)}
        self.assertEqual(set(rows), {"Invoice_300002.pdf", "Packing List_300002.pdf"})
        self.assertEqual((rows["Invoice_300002.pdf"]["type"], rows["Packing List_300002.pdf"]["type"]),
                         ("002", "010"))
        page = self.client.get(self.detail)
        self.assertContains(page, "Документи для митниці в UPS")
        self.assertContains(page, "Invoice_300002.pdf")
        self.assertContains(page, "Які документи потрібні")
        r = self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/ups-abd/preview/",
                            {"ref": "order:Invoice_300002.pdf"})
        self.assertEqual(b"".join(r), b"%PDF inv")
        self.assertEqual(self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/ups-abd/preview/",
                                         {"ref": "order:../../secret.pdf"}).status_code, 404)

    @mock.patch.object(UPSClient, "push_paperless_documents", return_value="G1")
    @mock.patch.object(UPSClient, "upload_paperless_document", side_effect=["A" * 26, "B" * 26])
    def test_several_documents_one_push(self, upload, push):
        r = self.client.post(self.url, {"doc": ["order:Invoice_300002.pdf", "order:Packing List_300002.pdf"],
                                        "type_order:Invoice_300002.pdf": "002",
                                        "type_order:Packing List_300002.pdf": "010"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([c.kwargs["document_type"] for c in upload.call_args_list], ["002", "010"])
        ids, ups_id, tracking, _when = push.call_args.args
        self.assertEqual((ids, ups_id), (["A" * 26, "B" * 26], TN1))
        self.assertEqual(tracking, [TN1, "1Z0R6B300439360987"])     # і номери коробок
        sent = ups_docs.sent(self.reload())
        self.assertEqual([(s["type"], s["name"]) for s in sent],
                         [("002", "Invoice_300002.pdf"), ("010", "Packing List_300002.pdf")])
        rows = {r["name"]: r for r in ups_docs.candidates(self.shipment, self.user)}
        self.assertTrue(rows["Invoice_300002.pdf"]["sent_here"])
        page = self.client.get(self.detail)
        self.assertContains(page, '<details id="abd-box" >')      # надіслано — блок згорнутий

    @mock.patch.object(UPSClient, "upload_paperless_document")
    def test_bad_type_refused(self, upload):
        r = self.client.post(self.url, {"doc": ["order:Invoice_300002.pdf"], "type_order:Invoice_300002.pdf": "999"})
        self.assertTrue(any("Невідомий тип" in m for m in self.messages(r)))
        upload.assert_not_called()


class RepairPageTests(BookingBase):
    def test_check_then_confirm(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(tracking_number=TN1, carrier_shipment_id=TN1,
                                                            status=Shipment.Status.IN_TRANSIT)
        second = Shipment.objects.create(order=self.order, carrier=self.carrier, tracking_number=TN1,
                                         carrier_shipment_id=TN2, status=Shipment.Status.IN_TRANSIT)
        url = f"/admin/shipping/shipment/repair-tracking/?order={self.order.pk}"
        page = self.client.get(url)
        self.assertContains(page, TN2)
        self.assertContains(page, "Виправити вибрані")
        second.refresh_from_db()
        self.assertEqual(second.tracking_number, TN1)              # перегляд нічого не змінює
        self.client.post("/admin/shipping/shipment/repair-tracking/", {"order": self.order.pk, "pk": [second.pk]})
        second.refresh_from_db()
        self.assertEqual(second.tracking_number, TN2)
        self.assertContains(self.client.get(url), "не знайдено")
