"""FedEx і Jumingo: митна вартість і вага рядків, платник мита за Incoterm, оплата посилки «до нас», етикетки всіх
коробок FedEx; Jumingo — митниця для імпорту, дані відправника з форми, тарифи з усіма коробками.

    python manage.py test shipping.tests_carriers_audit --settings=tabele.settings_test
"""
import base64
import io
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from shipping.fedex_client import FedExClient, _merge_pdfs
from shipping.models import Carrier, Shipment
from shipping.services.jumingo import JumingoService


def fedex():
    return Carrier(name="FedEx", carrier_type="fedex", api_key="id", api_secret="s", connection_uuid="740561073",
                   sender_country="DE", sender_city="München", sender_zip="81829", sender_street="X 1")


def pdf_b64(pages=1):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=100, height=100)
    out = io.BytesIO()
    w.write(out)
    return base64.b64encode(out.getvalue()).decode()


class FedExCustomsTests(TestCase):
    def setUp(self):
        self.client_ = FedExClient(carrier=fedex())
        self.shipper = {"country": "DE", "city": "München", "postal": "81829", "address_line": "X 1"}
        self.to = {"country": "US", "city": "Plano", "postal": "75001", "address_line": "1 Main", "state": "TX"}
        self.customs = {"currency": "USD", "contents_type": "SALE", "terms_of_shipment": "DAP",
                        "items": [{"description": "Antenna", "quantity": 10, "value": 100.0, "weight_kg": 0.0125,
                                   "hs_code": "85299000", "country": "DE"}]}

    def payload(self, **kw):
        return self.client_.create_shipment(self.to, [{"weight_kg": 1}], from_address=kw.get("shipper", self.shipper),
                                            customs_info=kw.get("customs", self.customs), dry_run=True)

    def test_values_weights_purpose(self):
        cc = self.payload()["requestedShipment"]["customsClearanceDetail"]
        line = cc["commodities"][0]
        self.assertEqual(line["unitPrice"]["amount"], 10.0)          # 100 / 10 шт
        self.assertEqual(line["customsValue"]["amount"], 100.0)      # рядок, не ×10
        self.assertEqual(line["weight"]["value"], 0.125)             # 0,0125 × 10
        self.assertEqual(cc["customsValue"]["amount"], 100.0)
        self.assertEqual(cc["commercialInvoice"], {"shipmentPurpose": "SOLD", "termsOfSale": "DAP"})
        self.assertNotIn("exportDetail", cc)

    def test_duties_payer_by_incoterm(self):
        self.assertEqual(self.payload()["requestedShipment"]["customsClearanceDetail"]["dutiesPayment"],
                         {"paymentType": "RECIPIENT"})
        ddp = dict(self.customs, terms_of_shipment="DDP")
        duties = self.payload(customs=ddp)["requestedShipment"]["customsClearanceDetail"]["dutiesPayment"]
        self.assertEqual(duties["paymentType"], "SENDER")

    def test_inbound_paid_as_recipient(self):
        rs = self.payload()["requestedShipment"]
        self.assertEqual(rs["shippingChargesPayment"]["paymentType"], "SENDER")
        rs = self.client_.create_shipment({"country": "DE", "city": "München", "postal": "81829"}, [{"weight_kg": 1}],
                                          from_address={"country": "CN", "city": "Shenzhen", "postal": "518000"},
                                          dry_run=True)["requestedShipment"]
        self.assertEqual(rs["shippingChargesPayment"]["paymentType"], "RECIPIENT")

    def test_labels_of_every_box(self):
        data = {"output": {"transactionShipments": [{
            "masterTrackingNumber": "794600000001",
            "pieceResponses": [
                {"trackingNumber": "794600000001", "packageDocuments": [{"encodedLabel": pdf_b64()}]},
                {"trackingNumber": "794600000002", "packageDocuments": [{"encodedLabel": pdf_b64()}]}]}]}}
        r = self.client_._parse_shipment_response(data, "FEDEX_PRIORITY", "PDF")
        self.assertEqual(r["package_tracking"], ["794600000001", "794600000002"])
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(io.BytesIO(base64.b64decode(r["label_base64"]))).pages), 2)
        self.assertEqual(_merge_pdfs(["bad"]), "bad")


class CompareRatesBoxesTests(TestCase):
    """«📊 Порівняти тарифи»: UPS і FedEx отримують кожну коробку (раніше — одну з усією вагою), DHL без дублів."""

    def test_each_box(self):
        from shipping.admin import _each_box
        boxes = _each_box([{"weight_kg": 13.2, "length_cm": 50, "width_cm": 38, "height_cm": 39, "quantity": 25},
                           {"weight_kg": 11.1, "length_cm": 41, "width_cm": 41, "height_cm": 20}], 300, 50, 38, 39)
        self.assertEqual(len(boxes), 26)
        self.assertEqual(boxes[-1]["weight_kg"], 11.1)
        self.assertEqual(_each_box([], 5, 20, 15, 10), [{"weight_kg": 5, "length_cm": 20, "width_cm": 15,
                                                          "height_cm": 10}])

    def test_fedex_rate_has_packaging(self):
        c = FedExClient(carrier=fedex())
        with mock.patch.object(FedExClient, "_post", return_value={"output": {}}) as post:
            c.get_rates({"country": "DE", "postal": "81829", "city": "München"}, [{"weight_kg": 13}, {"weight_kg": 21}],
                        from_address={"country": "CN", "postal": "523000", "city": "Dongguan"})
        rs = post.call_args.args[1]["requestedShipment"]
        self.assertEqual((rs["packagingType"], len(rs["requestedPackageLineItems"])), ("YOUR_PACKAGING", 2))

    def test_dhl_same_product_once(self):
        from shipping.services import dhl

        class R:
            ok, status_code = True, 200
            def json(self):
                p = {"productName": "EXPRESS WORLDWIDE", "productCode": "P",
                     "totalPrice": [{"price": 34789.07, "priceCurrency": "EUR"}]}
                return {"products": [dict(p, deliveryCapabilities={"totalTransitDays": 3}),
                                     dict(p, deliveryCapabilities={"totalTransitDays": 2})]}
        c = Carrier(name="DHL", carrier_type="dhl", api_key="u", api_secret="p", connection_uuid="1", sender_country="DE")
        with mock.patch("tabele.api_logger.logged_request", return_value=R()):
            out = dhl.get_rates(c, "DE", "81829", "München", 1, 20, 15, 10)
        self.assertEqual([(p["code"], p["transit_days"]) for p in out["products"]], [("P", 2)])


class JumingoTests(TestCase):
    def setUp(self):
        from sales.models import SalesOrder
        self.carrier = Carrier.objects.create(name="Jumingo", carrier_type="jumingo", api_key="t",
                                              sender_country="DE", sender_city="München", sender_email="a@x.de")
        self.order = SalesOrder.objects.create(source="manual", order_number="J1", affects_stock=False)
        self.service = JumingoService(self.carrier)

    def shipment(self, **kw):
        return Shipment.objects.create(order=self.order, carrier=self.carrier, weight_kg=Decimal("1"),
                                       declared_value=Decimal("100"), customs_articles={"customs_line_items": [
                                           {"description": "Antenna", "quantity": 2, "value": 100}]}, **kw)

    def test_customs_for_import_and_not_inside_eu(self):
        self.assertIsNotNone(self.service._build_customs_invoice(
            self.shipment(sender_country="CN", recipient_country="DE")))
        self.assertIsNone(self.service._build_customs_invoice(
            self.shipment(sender_country="DE", recipient_country="FR")))
        self.assertIsNotNone(self.service._build_customs_invoice(
            self.shipment(sender_country="DE", recipient_country="US")))

    def test_sender_email_and_state_from_form(self):
        s = self.shipment(sender_country="US", sender_state="TX", sender_email="sup@x.com", recipient_country="DE")
        fa = self.service._build_payload(s)["from_address"]
        self.assertEqual((fa["settings"]["email"], fa["state"]), ("sup@x.com", "TX"))

    def test_preview_sends_every_box_and_real_origin(self):
        class R:
            def __init__(self, data):
                self._d = data
            def raise_for_status(self):
                pass
            def json(self):
                return self._d
        with mock.patch("tabele.api_logger.logged_request",
                        side_effect=[R({"shipment_id": "p1"}), R([{"id": "t1"}])]) as call:
            out = self.service.get_rates_preview(
                "DE", "81829", "München", 5, 30, 20, 10,
                packages=[{"weight_kg": 2, "length_cm": 30, "width_cm": 20, "height_cm": 10, "quantity": 2}],
                origin={"country": "CN", "postal": "518000", "city": "Shenzhen"})
        body = call.call_args_list[0].kwargs["json"]
        self.assertEqual(len(body["packages"]), 2)
        self.assertEqual(body["from_address"]["country"], "CN")
        self.assertEqual(out["tariffs"], [{"id": "t1"}])
