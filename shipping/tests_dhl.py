"""DHL Express (shipping/services/dhl.py): два акаунти (експорт / імпорт і третя країна), тарифи для кількох коробок,
митниця за обома країнами, вага рядків, Incoterm, інвойс DHL і Paperless Trade.

    python manage.py test shipping.tests_dhl --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from shipping.models import Carrier, Shipment, ShipmentPackage
from shipping.services import dhl
from shipping.tests_booking import DHL_RESULT, BookingBase


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.ok = 200 <= status < 300
        self.text = str(data)

    def json(self):
        return self._data


def carrier(**kw):
    return Carrier(name="DHL", carrier_type="dhl", api_key="u", api_secret="p", connection_uuid="144648348",
                   import_account="961444260", sender_country="DE", sender_city="München", sender_zip="81829",
                   sender_street="X 1", **kw)


class AccountTests(BookingBase):
    carrier_type = "dhl"

    def test_account_by_sender_country(self):
        c = carrier()
        self.assertEqual(dhl.account_for(c, "DE"), ("144648348", "export"))   # експорт і Innerdeutsch
        self.assertEqual(dhl.account_for(c, "CN"), ("961444260", "import"))   # до нас
        self.assertEqual(dhl.account_for(c, "FR"), ("961444260", "import"))   # FR → IT: третя країна
        c.import_account = ""
        self.assertEqual(dhl.account_for(c, "CN")[0], "144648348")

    def test_account_choice(self):
        c = carrier()
        self.assertEqual(dhl.account_choice(c, "CN", "auto"), ("961444260", "import"))
        self.assertEqual(dhl.account_choice(c, "CN", "export"), ("144648348", "export"))
        self.assertEqual(dhl.account_choice(c, "DE", "import"), ("961444260", "import"))
        c.import_account = ""
        self.assertEqual(dhl.account_choice(c, "DE", "import"), ("144648348", "import"))

    def test_compare_switch_and_confirm_shows_account(self):
        import json
        Carrier.objects.filter(pk=self.carrier.pk).update(connection_uuid="144648348", import_account="961444260",
                                                          api_url="")
        with mock.patch("shipping.services.dhl.get_rates", return_value={"products": []}) as rates:
            r = self.client.post("/admin/shipping/shipment/compare-rates/", json.dumps({
                "recipient_country": "DE", "recipient_zip": "81829", "recipient_city": "München",
                "sender_country": "CN", "sender_zip": "523000", "sender_city": "Dongguan",
                "packages": [{"weight_kg": 13, "length_cm": 50, "width_cm": 38, "height_cm": 39, "quantity": 2}],
                "dhl_account": "export"}), content_type="application/json")
        entry = next(c for c in r.json()["results"] if c["carrier_type"] == "dhl")
        self.assertEqual((entry["dhl_account"]["number"], entry["dhl_account"]["choice"]), ("144648348", "export"))
        self.assertEqual(rates.call_args.kwargs["account_number"], "144648348")
        page = self.client.get(self.urls["dhl"][0] + "?product_code=P&product_name=X&price=1&account=import")
        self.assertContains(page, "961444260")
        with mock.patch("shipping.services.dhl.create_shipment", return_value=DHL_RESULT) as create:
            self.client.post(self.urls["dhl"][0] + "?product_code=P&product_name=X&price=1&account=import",
                             {"pickup_type": "dropoff"})
        self.assertEqual(create.call_args.kwargs["account"], "import")

    def test_customs_needed(self):
        self.assertFalse(dhl.customs_needed("DE", "DE"))
        self.assertFalse(dhl.customs_needed("DE", "FR"))
        self.assertTrue(dhl.customs_needed("DE", "US"))
        self.assertTrue(dhl.customs_needed("CN", "DE"))      # імпорт до нас — теж митниця

    def test_zero_price_products_are_not_rates(self):
        resp = FakeResp(200, {"products": [
            {"productName": "EXPRESS BREAKBULK", "productCode": "B",
             "totalPrice": [{"priceType": "BILLC", "price": 0, "priceCurrency": "EUR"}]},
            {"productName": "PARCEL PRODUCT", "productCode": "9", "totalPrice": []},
            {"productName": "EXPRESS WORLDWIDE", "productCode": "U",
             "totalPrice": [{"priceType": "BASEC", "price": 150, "priceCurrency": "USD"},
                            {"priceType": "BILLC", "price": 143.12, "priceCurrency": "EUR"}]}]})
        with mock.patch("tabele.api_logger.logged_request", return_value=resp):
            r = dhl.get_rates(carrier(), "US", "75001", "Plano", 5, 30, 20, 10)
        self.assertEqual([(p["code"], p["price"], p["currency"]) for p in r["products"]], [("U", 143.12, "EUR")])
        self.assertEqual(r["unpriced"], ["EXPRESS BREAKBULK", "PARCEL PRODUCT"])

    def test_rates_post_each_box_and_real_sender(self):
        resp = FakeResp(200, {"products": [{"productName": "EXPRESS WORLDWIDE", "productCode": "P",
                                            "totalPrice": [{"priceType": "BILLC", "price": 80, "priceCurrency": "EUR"}]}]})
        with mock.patch("tabele.api_logger.logged_request", return_value=resp) as call:
            r = dhl.get_rates(carrier(), "DE", "81829", "München", 5, 30, 20, 10,
                              packages=[{"weight_kg": 2, "length_cm": 30, "width_cm": 20, "height_cm": 10, "quantity": 2},
                                        {"weight_kg": 1, "length_cm": 20, "width_cm": 15, "height_cm": 10, "quantity": 1}],
                              origin={"country": "CN", "postal": "518000", "city": "Shenzhen"})
        self.assertEqual(r["products"][0]["price"], 80.0)
        method, body = call.call_args.args[2], call.call_args.kwargs["json"]
        self.assertEqual(method, "POST")
        self.assertEqual(len(body["packages"]), 3)
        self.assertEqual(body["accounts"], [{"typeCode": "shipper", "number": "961444260"}])
        self.assertEqual(body["customerDetails"]["shipperDetails"]["countryCode"], "CN")
        self.assertTrue(body["isCustomsDeclarable"])


class ShipmentPayloadTests(BookingBase):
    carrier_type = "dhl"

    def setUp(self):
        super().setUp()
        Carrier.objects.filter(pk=self.carrier.pk).update(connection_uuid="144648348", import_account="961444260")
        self.carrier.refresh_from_db()
        Shipment.objects.filter(pk=self.shipment.pk).update(
            recipient_country="US", recipient_city="Plano", recipient_zip="75001", declared_value=Decimal("100"),
            customs_articles={"incoterm": "DDP", "customs_line_items": [
                {"sku": "AN1", "description": "Antenna", "quantity": 4, "value": 100, "currency": "USD",
                 "origin_country": "DE", "customs_number": "85299000", "weight": 0.0125, "weight_auto": False}]})
        self.reload()

    def test_export_payload(self):
        p = dhl.create_shipment(self.carrier, self.shipment, "P", dry_run=True)
        # DDP від нас — ще й платник мита (duties-taxes) і послуга DD
        self.assertEqual(p["accounts"], [{"typeCode": "shipper", "number": "144648348"},
                                         {"typeCode": "duties-taxes", "number": "144648348"}])
        self.assertEqual(p["content"]["incoterm"], "DDP")
        line = p["content"]["exportDeclaration"]["lineItems"][0]
        self.assertEqual(line["weight"]["netValue"], 0.05)              # 0,0125 кг × 4
        self.assertEqual(line["commodityCodes"], [{"typeCode": "outbound", "value": "85299000"}])
        self.assertIn({"typeCode": "invoice", "templateName": "COMMERCIAL_INVOICE_P_10", "isRequested": True,
                       "invoiceType": "commercial"}, p["outputImageProperties"]["imageOptions"])
        self.assertEqual(p["valueAddedServices"], [{"serviceCode": "WY"}, {"serviceCode": "DD"}])
        self.assertNotIn("customerReferences", p["content"]["exportDeclaration"]["invoice"])

    def test_mrn_and_own_invoice(self):
        from django.core.files.base import ContentFile
        s = self.shipment
        s.export_mrn = "26DE760298696748B4"
        s.use_custom_invoice = True
        s.custom_invoice_pdf.save("ci.pdf", ContentFile(b"%PDF-own"), save=True)
        p = dhl.create_shipment(self.carrier, self.reload(), "P", dry_run=True)
        self.assertEqual(p["content"]["exportDeclaration"]["invoice"]["customerReferences"],
                         [{"typeCode": "MRN", "value": "26DE760298696748B4"}])
        self.assertEqual(p["documentImages"][0]["typeCode"], "INV")
        self.assertNotIn("invoice", [o["typeCode"] for o in p["outputImageProperties"]["imageOptions"]])

    def test_import_uses_import_account(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(sender_country="CN", sender_city="Shenzhen",
                                                            recipient_country="DE")
        p = dhl.create_shipment(self.carrier, self.reload(), "P", dry_run=True)
        self.assertEqual(p["accounts"], [{"typeCode": "shipper", "number": "961444260"}])
        self.assertTrue(p["content"]["isCustomsDeclarable"])

    def test_wy_refused_retry_without_it(self):
        refusal = FakeResp(400, {"title": "Bad request", "detail": "Service WY not allowed for account"})
        ok = FakeResp(201, {"shipmentTrackingNumber": "1234567890", "packages": [], "documents": [
            {"typeCode": "label", "content": "JVBERi0="}, {"typeCode": "invoice", "content": "JVBERi0="}]})
        with mock.patch("tabele.api_logger.logged_request", side_effect=[refusal, ok]) as call:
            r = dhl.create_shipment(self.carrier, self.shipment, "P")
        self.assertTrue(r["success"])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.kwargs["json"]["valueAddedServices"], [{"serviceCode": "DD"}])  # WY — ні
        self.assertFalse(r["paperless"])
        self.assertTrue(r["notes"])
        self.assertEqual((r["label_bytes"], r["invoice_bytes"]), (b"%PDF-", b"%PDF-"))


class BookingSavesInvoiceTests(BookingBase):
    carrier_type = "dhl"

    def test_invoice_saved_as_customs_doc(self):
        result = {**DHL_RESULT, "invoice_bytes": b"%PDF inv", "account": "144648348", "account_kind": "export",
                  "paperless": True, "notes": [], "package_tracking": ["JD1", "JD2"]}
        with mock.patch("shipping.services.dhl.create_shipment", return_value=result):
            self.client.post(self.urls["dhl"][0] + "?product_code=P&product_name=X&price=30",
                             {"pickup_type": "dropoff"})
        s = self.reload()
        self.assertTrue(s.customs_url.endswith(f"dhl_invoice_{s.pk}_1234567890.pdf"))
        self.assertEqual(s.package_tracking, ["JD1", "JD2"])
        self.assertIn("DHL_ACCOUNT | 144648348", s.notes)

    def test_rates_page_sends_boxes(self):
        ShipmentPackage.objects.create(shipment=self.shipment, weight_kg=2, length_cm=30, width_cm=20,
                                       height_cm=10, quantity=2)
        with mock.patch("shipping.services.dhl.get_rates", return_value={"products": []}) as rates:
            self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/dhl-rates/")
        self.assertEqual(rates.call_args.kwargs["packages"][0]["quantity"], 2)
        self.assertEqual(rates.call_args.kwargs["origin"]["country"], "DE")
