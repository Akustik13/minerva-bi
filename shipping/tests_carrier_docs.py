"""Митні документи однаково для UPS, DHL і FedEx (shipping/services/carrier_docs.py): MRN і свій інвойс на
сторінках перевірки DHL / FedEx, FedEx ETD (з повтором без нього), мито при імпорті, документи до вже оформленої
посилки DHL / FedEx, «🖨️ Друкувати всі документи» — 3 примірники інвойсу. Перевізники імітуються.

    python manage.py test shipping.tests_carrier_docs --settings=tabele.settings_test
"""
import base64
import io
from decimal import Decimal
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from shipping.fedex_client import FedExClient, FedExError
from shipping.models import Carrier, Shipment
from shipping.services import carrier_docs
from shipping.tests_booking import DHL_RESULT, FEDEX_RESULT, BookingBase

MRN = "26DE760298696748B4"
ITEMS = {"incoterm": "DAP", "customs_line_items": [
    {"sku": "AN1", "description": "Antenna", "quantity": 4, "value": 100, "currency": "EUR",
     "origin_country": "DE", "customs_number": "85299000", "weight": 0.0125, "weight_auto": False}]}


def _pdf(pages=1):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


class FakeResp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data or {}
        self.ok = 200 <= status < 300
        self.text = str(data)

    def json(self):
        return self._data


class RulesTests(BookingBase):
    carrier_type = "dhl"

    def test_customs_and_copies(self):
        s = self.shipment
        self.assertFalse(carrier_docs.needs_customs(s))                    # DE → DE
        s.recipient_country = "FR"
        self.assertFalse(carrier_docs.needs_customs(s))                    # у межах ЄС
        s.recipient_country = "US"
        self.assertTrue(carrier_docs.needs_customs(s))
        s.sender_country, s.recipient_country = "CN", "DE"                 # імпорт до нас — теж
        self.assertTrue(carrier_docs.needs_customs(s))
        self.assertEqual(carrier_docs.invoice_copies(s), 3)                # на папері — 3
        s.notes = "DHL_ACCOUNT | 961444260 (імпорт) | Paperless Trade"
        self.assertEqual(carrier_docs.invoice_copies(s), 1)                # електронно — 1

    def test_doc_types_per_carrier(self):
        dhl = dict((c, de) for c, _n, de, _h in carrier_docs.doc_types("dhl"))
        self.assertEqual(dhl["002"], "DHL: CIN")
        self.assertEqual(dhl["004"], "DHL: DCL")
        fedex = dict((c, de) for c, _n, de, _h in carrier_docs.doc_types("fedex"))
        self.assertEqual(fedex["002"], "FedEx: COMMERCIAL_INVOICE")
        self.assertIn("ups.com", dict((c, de) for c, _n, de, _h in carrier_docs.doc_types("ups"))["004"])
        self.assertIn("приймає лише", carrier_docs.check("dhl", [("a.docx", b"x", "002")], 0))
        self.assertIn("до 5 документів", carrier_docs.check("fedex", [("a.pdf", b"x", "002")] * 6, 0))


class DHLConfirmTests(BookingBase):
    carrier_type = "dhl"

    def setUp(self):
        super().setUp()
        Carrier.objects.filter(pk=self.carrier.pk).update(connection_uuid="144648348", import_account="961444260",
                                                          api_url="")
        Shipment.objects.filter(pk=self.shipment.pk).update(recipient_country="US", recipient_city="Plano",
                                                            recipient_zip="75001", customs_articles=ITEMS)
        self.page = self.urls["dhl"][0] + "?product_code=P&product_name=X&price=1"

    def test_page_has_mrn_and_own_invoice(self):
        r = self.client.get(self.page)
        self.assertContains(r, 'name="export_mrn"')
        self.assertContains(r, 'name="custom_invoice_pdf"')
        self.assertContains(r, 'enctype="multipart/form-data"')
        self.assertContains(r, "Мито/податки оплачує")

    def test_bad_mrn_stops_before_booking(self):
        with mock.patch("shipping.services.dhl.create_shipment") as create:
            self.client.post(self.page, {"pickup_type": "dropoff", "export_mrn": "XX"})
        create.assert_not_called()
        self.assertEqual(self.reload().status, Shipment.Status.DRAFT)

    def test_mrn_and_own_invoice_saved_and_used(self):
        result = {**DHL_RESULT, "own_invoice": True, "mrn": MRN, "account": "144648348", "account_kind": "export",
                  "paperless": True, "notes": []}
        with mock.patch("shipping.services.dhl.create_shipment", return_value=result):
            self.client.post(self.page, {"pickup_type": "dropoff", "export_mrn": MRN, "use_custom_invoice": "1",
                                         "custom_invoice_pdf": SimpleUploadedFile("ci.pdf", b"%PDF-own")})
        s = self.reload()
        self.assertEqual(s.export_mrn, MRN)
        self.assertTrue(s.use_custom_invoice)
        self.assertEqual(s.customs_url, s.custom_invoice_pdf.url)
        self.assertIn(f"MRN {MRN}", s.notes)
        self.assertIn("Paperless Trade", s.notes)


class DHLUploadTests(BookingBase):
    carrier_type = "dhl"

    def test_upload_image_body(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1234567890",
            raw_request={"productCode": "P", "plannedShippingDateAndTime": "2026-11-17T09:00:00 GMT+01:00",
                         "accounts": [{"typeCode": "shipper", "number": "961444260"}]})
        s = self.reload()
        self.assertTrue(carrier_docs.booked_ok(s))
        with mock.patch("tabele.api_logger.logged_request", return_value=FakeResp(200)) as call:
            done = carrier_docs.upload(s, [("ABD.pdf", b"%PDF", "004"), ("Rechnung.pdf", b"%PDF", "002")])
        self.assertEqual([c for _n, c, _d in done], ["004", "002"])
        method, url = call.call_args.args[2], call.call_args.args[3]
        body = call.call_args.kwargs["json"]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/shipments/1234567890/upload-image"))
        self.assertEqual(body["productCode"], "P")
        self.assertEqual(body["originalPlannedShippingDate"], "2026-11-17")
        self.assertEqual(body["accounts"], [{"typeCode": "shipper", "number": "961444260"}])
        self.assertEqual([i["typeCode"] for i in body["documentImages"]], ["DCL", "CIN"])

    def test_view_sends_and_notes(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1234567890",
            raw_request={"productCode": "P", "plannedShippingDateAndTime": "2026-11-17T09:00:00 GMT+01:00"})
        page = self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/detail/")
        self.assertContains(page, 'Документи для митниці в <span translate="no">DHL</span>')
        with mock.patch("shipping.services.dhl.upload_images", return_value={"success": True}) as up:
            self.client.post(f"/admin/shipping/shipment/{self.shipment.pk}/ups-abd/",
                             {"pc_file": SimpleUploadedFile("ABD_123.pdf", b"%PDF-1.4"), "pc_type": "004"})
        self.assertEqual(up.call_args.args[2][0][2], "DCL")
        self.assertIn("DHL_DOC |", self.reload().notes)

    def test_refusal_keeps_notes(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1234567890", raw_request={"productCode": "P"})
        with mock.patch("shipping.services.dhl.upload_images", return_value={"success": False, "error": "HTTP 400"}):
            r = self.client.post(f"/admin/shipping/shipment/{self.shipment.pk}/ups-abd/",
                                 {"pc_file": SimpleUploadedFile("ABD.pdf", b"%PDF"), "pc_type": "004"})
        self.assertNotIn("DHL_DOC", self.reload().notes or "")
        self.assertTrue(any("mydhl" in m for m in self.messages(r)))


class FedExPayloadTests(BookingBase):
    carrier_type = "fedex"

    def client_(self):
        return FedExClient(carrier=Carrier(name="F", carrier_type="fedex", api_key="a", api_secret="b",
                                           connection_uuid="820353684", sender_country="DE"))

    def customs(self, terms="DAP"):
        return {"currency": "EUR", "contents_type": "SALE", "terms_of_shipment": terms,
                "items": [{"description": "Antenna", "quantity": 4, "value": 100, "weight_kg": 0.0125,
                           "country": "DE", "hs_code": "85299000"}]}

    def test_export_mrn_etd_and_invoice_document(self):
        p = self.client_().create_shipment({"country": "US", "city": "Plano"}, [{"weight_kg": 1}],
                                           from_address={"country": "DE"}, customs_info=self.customs(),
                                           mrn=MRN, dry_run=True)
        rs = p["requestedShipment"]
        self.assertEqual(rs["customsClearanceDetail"]["exportDetail"], {"exportComplianceStatement": MRN})
        self.assertEqual(rs["shipmentSpecialServices"]["specialServiceTypes"], ["ELECTRONIC_TRADE_DOCUMENTS"])
        self.assertEqual(rs["shipmentSpecialServices"]["etdDetail"]["requestedDocumentTypes"], ["COMMERCIAL_INVOICE"])
        self.assertEqual(rs["shippingDocumentSpecification"]["shippingDocumentTypes"], ["COMMERCIAL_INVOICE"])
        self.assertEqual(rs["customsClearanceDetail"]["dutiesPayment"], {"paymentType": "RECIPIENT"})

    def test_import_duties_on_our_account(self):
        p = self.client_().create_shipment({"country": "DE", "city": "München"}, [{"weight_kg": 1}],
                                           from_address={"country": "CN"}, customs_info=self.customs(), dry_run=True)
        rs = p["requestedShipment"]
        self.assertEqual(rs["shippingChargesPayment"]["paymentType"], "RECIPIENT")
        duties = rs["customsClearanceDetail"]["dutiesPayment"]
        self.assertEqual(duties["paymentType"], "RECIPIENT")
        self.assertEqual(duties["payor"]["responsibleParty"]["accountNumber"]["value"], "820353684")

    def test_own_invoice_attached_from_etd(self):
        c = self.client_()
        with mock.patch.object(FedExClient, "upload_etd", return_value="DOC-1") as up, \
                mock.patch.object(FedExClient, "_post", return_value={"output": {}}) as post:
            r = c.create_shipment({"country": "US"}, [{"weight_kg": 1}], from_address={"country": "DE"},
                                  customs_info=self.customs(), own_invoice=(b"%PDF", "ci.pdf"))
        self.assertEqual(up.call_args.args[2], "COMMERCIAL_INVOICE")
        rs = post.call_args.args[1]["requestedShipment"]
        self.assertEqual(rs["shipmentSpecialServices"]["etdDetail"]["attachedDocuments"][0]["documentId"], "DOC-1")
        self.assertNotIn("shippingDocumentSpecification", rs)
        self.assertTrue(r["paperless"] and r["own_invoice"])

    def test_etd_refused_retry_without_it(self):
        c = self.client_()
        refusal = FedExError("FedEx API [400]: ELECTRONIC_TRADE_DOCUMENTS not allowed", code=400)
        with mock.patch.object(FedExClient, "_post", side_effect=[refusal, {"output": {}}]) as post:
            r = c.create_shipment({"country": "US"}, [{"weight_kg": 1}], from_address={"country": "DE"},
                                  customs_info=self.customs())
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("shipmentSpecialServices", post.call_args.args[1]["requestedShipment"])
        self.assertFalse(r["paperless"])
        self.assertTrue(r["notes"])

    def test_invoice_from_response(self):
        data = {"output": {"transactionShipments": [{"shipmentDocuments": [
            {"contentType": "COMMERCIAL_INVOICE", "encodedLabel": base64.b64encode(b"%PDF inv").decode()}],
            "pieceResponses": [{"trackingNumber": "794600000000"}]}]}}
        r = self.client_()._parse_shipment_response(data, "FEDEX_PRIORITY", "PDF")
        self.assertEqual(base64.b64decode(r["invoice_base64"]), b"%PDF inv")

    def test_etd_upload_post_shipment(self):
        c = self.client_()
        with mock.patch.object(FedExClient, "get_token", return_value="t"), \
                mock.patch("shipping.fedex_client.requests.post",
                           return_value=FakeResp(201, {"output": {"meta": {"docId": "D9"}}})) as post:
            doc = c.upload_etd(b"%PDF", "ABD.pdf", "OTHER", "DE", "US", tracking="794600000000")
        self.assertEqual(doc, "D9")
        import json
        meta = json.loads(post.call_args.kwargs["files"]["document"][1])
        self.assertEqual(meta["workflowName"], "ETDPostshipment")
        self.assertEqual(meta["meta"]["trackingNumber"], "794600000000")
        self.assertIn("documentapi", post.call_args.args[0])


class FedExBookingTests(BookingBase):
    carrier_type = "fedex"

    def test_mrn_passed_and_invoice_saved(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(recipient_country="US", recipient_city="Plano",
                                                            recipient_zip="75001", customs_articles=ITEMS)
        page = self.client.get(self.urls["fedex"][0] + "?service_code=FEDEX_PRIORITY")
        self.assertContains(page, 'name="export_mrn"')
        result = {**FEDEX_RESULT, "invoice_base64": base64.b64encode(b"%PDF inv").decode(), "paperless": True,
                  "own_invoice": False, "notes": []}
        with mock.patch.object(FedExClient, "create_shipment", return_value=result) as create:
            self.client.post(self.urls["fedex"][0], {"service_code": "FEDEX_PRIORITY", "export_mrn": MRN})
        self.assertEqual(create.call_args.kwargs["mrn"], MRN)
        s = self.reload()
        self.assertTrue(s.customs_url.endswith("fedex_invoice_%s_794600000000.pdf" % s.pk))
        self.assertIn("FEDEX_ETD |", s.notes)
        self.assertTrue(carrier_docs.paperless(s))


class PrintAllTests(BookingBase):
    carrier_type = "dhl"

    def test_three_invoice_copies_for_import(self):
        label = self.media / "labels" / "dhl" / "1234567890.pdf"
        label.parent.mkdir(parents=True)
        label.write_bytes(_pdf(1))
        inv = self.media / "shipping" / "customs" / "dhl_invoice.pdf"
        inv.parent.mkdir(parents=True)
        inv.write_bytes(_pdf(2))
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1234567890", sender_country="CN",
            recipient_country="DE", label_url="/media/labels/dhl/1234567890.pdf",
            customs_url="/media/shipping/customs/dhl_invoice.pdf")
        r = self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/print-all/")
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")     # інакше фрейм сторінки ?print=1 порожній
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(io.BytesIO(r.content)).pages), 1 + 3 * 2)     # етикетка + 3 × інвойс
        r = self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/print-all/?copies=1")
        self.assertEqual(len(PdfReader(io.BytesIO(r.content)).pages), 3)
        page = self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/print-all/?print=1")
        self.assertContains(page, "contentWindow.print()")
