"""Оформлення у перевізника (UPS / DHL / FedEx): лише POST і лише один раз.

Кожне оформлення — платна етикетка. Друге натискання, «Назад» і ще раз, друга вкладка чи посилання (GET)
не мають звертатися до перевізника вдруге. Перевізники імітуються — мережі немає.

    python manage.py test shipping --settings=tabele.settings_test
"""
import base64
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings

from sales.models import SalesOrder
from shipping.admin import ShipmentAdmin
from shipping.fedex_client import FedExClient, FedExError
from shipping.models import Carrier, Shipment
from shipping.ups_client import UPSClient, UPSError

TEMPLATES = Path(__file__).resolve().parent / "templates" / "admin" / "shipping"

UPS_RESULT = {
    "tracking_number": "1ZA1B2C30499999999", "shipment_id": "1ZA1B2C30499999999",
    "service_name": "UPS Standard", "total_charge": Decimal("12.50"), "currency": "EUR",
    "label_base64": "", "label_format": "GIF",
}
DHL_RESULT = {
    "success": True, "tracking_number": "1234567890", "label_bytes": b"%PDF-1.4 dhl",
    "carrier_service": "P", "carrier_price": 30.0, "raw_request": {"plannedShippingDateAndTime": "x"},
    "raw_response": {"shipmentTrackingNumber": "1234567890"},
}
FEDEX_RESULT = {
    "tracking_number": "794600000000", "master_tracking": "794600000000",
    "label_base64": base64.b64encode(b"%PDF-1.4 fedex").decode(), "label_url": "", "label_format": "PDF",
    "total_charge": Decimal("40.00"), "currency": "EUR",
}


class BookingBase(TestCase):
    carrier_type = "ups"

    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        media = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.media = media
        self.carrier = Carrier.objects.create(
            name=self.carrier_type.upper(), carrier_type=self.carrier_type, api_key="id", api_secret="secret",
            connection_uuid="A1B2C3", api_url="sandbox", sender_country="DE", sender_city="München")
        self.order = SalesOrder.objects.create(source="webshop", order_number="300002")
        self.shipment = Shipment.objects.create(
            order=self.order, carrier=self.carrier, status=Shipment.Status.DRAFT, weight_kg=Decimal("1.2"),
            recipient_name="Muster GmbH", recipient_street="Hauptstr. 1", recipient_city="Berlin",
            recipient_zip="10115", recipient_country="DE", sender_country="DE")
        base = f"/admin/shipping/shipment/{self.shipment.pk}"
        self.change_url = f"{base}/change/"
        self.urls = {"ups": (f"{base}/ups-confirm/", f"{base}/ups-book/"),
                     "dhl": (f"{base}/dhl-confirm/", f"{base}/dhl-book/"),
                     "fedex": (f"{base}/fedex-confirm/", f"{base}/fedex-book/")}

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def reload(self):
        self.shipment.refresh_from_db()
        return self.shipment


class ClaimTests(BookingBase):
    def test_only_one_of_two_simultaneous_requests_claims_the_shipment(self):
        first, second = Shipment.objects.get(pk=self.shipment.pk), Shipment.objects.get(pk=self.shipment.pk)
        self.assertTrue(ShipmentAdmin._claim_booking(first))
        self.assertFalse(ShipmentAdmin._claim_booking(second))        # той самий стан у пам'яті — але не в БД
        self.assertEqual(self.reload().status, Shipment.Status.SUBMITTED)

    def test_error_state_can_be_booked_again_but_not_with_tracking_or_label(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.ERROR, error_message="old")
        self.assertTrue(ShipmentAdmin._claim_booking(self.reload()))
        self.assertEqual(self.reload().error_message, "")
        for status, tracking in ((Shipment.Status.ERROR, "1Z999"), (Shipment.Status.LABEL_READY, ""),
                                 (Shipment.Status.CANCELLED, "")):
            Shipment.objects.filter(pk=self.shipment.pk).update(status=status, tracking_number=tracking)
            self.assertFalse(ShipmentAdmin._claim_booking(self.reload()), (status, tracking))


class UPSBookingTests(BookingBase):
    carrier_type = "ups"

    def post_confirm(self, client=None, **extra):
        data = {"service_code": "11", "pickup_type": "dropoff", "terms_of_shipment": "DAP", **extra}
        return (client or self.client).post(self.urls["ups"][0], data)

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_confirm_books_once_second_click_does_not_call_ups(self, create):
        r = self.post_confirm()
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        create.assert_called_once()
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.tracking_number),
                         (Shipment.Status.LABEL_READY, "1ZA1B2C30499999999"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_number, "1ZA1B2C30499999999")

        for url in self.urls["ups"]:                                    # «Назад» + ще раз, друга вкладка
            r = self.client.post(url, {"service_code": "11"})
            self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
            self.assertTrue(any("уже оформлене в UPS" in m for m in self.messages(r)), self.messages(r))
        create.assert_called_once()
        self.assertEqual(self.reload().tracking_number, "1ZA1B2C30499999999")

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_link_to_book_url_never_books(self, create):
        r = self.client.get(self.urls["ups"][1] + "?service_code=65&pickup_type=dropoff")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(self.urls["ups"][0] + "?"), r["Location"])
        self.assertIn("service_code=65", r["Location"])
        self.assertTrue(any("лише кнопкою «Підтвердити і відправити»" in m for m in self.messages(r)))
        create.assert_not_called()
        self.assertEqual(self.reload().status, Shipment.Status.DRAFT)

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_post_to_book_url_books_like_the_confirm_page(self, create):
        r = self.client.post(self.urls["ups"][1], {"service_code": "65", "package_weight_kg": "2.5"})
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["service_code"], "65")
        self.assertEqual(create.call_args.kwargs["packages"][0]["weight_kg"], 2.5)
        self.assertEqual(self.reload().status, Shipment.Status.LABEL_READY)

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_booking_in_progress_is_not_sent_again(self, create):
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.SUBMITTED)
        r = self.post_confirm()
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        self.assertTrue(any("саме оформлюється в UPS" in m for m in self.messages(r)))
        create.assert_not_called()
        self.assertEqual(self.reload().status, Shipment.Status.SUBMITTED)

    def test_ups_refusal_returns_shipment_to_error_and_it_can_be_retried(self):
        with mock.patch.object(UPSClient, "create_shipment", side_effect=UPSError("Invalid postal code")) as create:
            r = self.post_confirm()
        self.assertTrue(r["Location"].endswith("/detail/#debug"), r["Location"])
        create.assert_called_once()
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.error_message), (Shipment.Status.ERROR, "Invalid postal code"))
        with mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT) as create:
            self.post_confirm()
        create.assert_called_once()                                      # після помилки — можна знову
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.error_message), (Shipment.Status.LABEL_READY, ""))

    def test_unexpected_failure_does_not_leave_shipment_stuck(self):
        with mock.patch.object(UPSClient, "create_shipment", side_effect=ConnectionResetError("reset")):
            r = self.post_confirm()
        shipment = self.reload()
        self.assertEqual(shipment.status, Shipment.Status.ERROR)
        self.assertIn("ConnectionResetError", shipment.error_message)
        self.assertTrue(any("перевірте в UPS" in m for m in self.messages(r)))

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_booking_needs_csrf_token(self, create):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for url in self.urls["ups"]:
            self.assertEqual(client.post(url, {"service_code": "11"}).status_code, 403)
        create.assert_not_called()
        self.assertEqual(self.reload().status, Shipment.Status.DRAFT)

    @mock.patch.object(UPSClient, "upload_paperless_document", return_value="DOC-NEW")
    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_second_click_keeps_document_id_of_the_first_booking(self, create, upload):
        from django.core.files.uploadedfile import SimpleUploadedFile
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1Z1", ups_document_id="DOC-FIRST")
        pdf = SimpleUploadedFile("invoice.pdf", b"%PDF-1.4", content_type="application/pdf")
        self.post_confirm(use_custom_invoice="1", custom_invoice_pdf=pdf)
        upload.assert_not_called()
        create.assert_not_called()
        self.assertEqual(self.reload().ups_document_id, "DOC-FIRST")

    def test_confirm_page_of_a_booked_shipment_sends_to_its_card(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1ZA1B2C30499999999")
        r = self.client.get(self.urls["ups"][0] + "?service_code=11")
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        self.assertTrue(any("1ZA1B2C30499999999" in m for m in self.messages(r)))

    def test_confirm_page_still_renders_for_a_draft(self):
        r = self.client.get(self.urls["ups"][0] + "?service_code=11")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'method="post"')
        self.assertContains(r, 'id="ups-confirm-form"')


class DHLBookingTests(BookingBase):
    carrier_type = "dhl"

    def confirm_url(self):
        return self.urls["dhl"][0] + "?product_code=P&product_name=EXPRESS+WORLDWIDE&price=30.00"

    @mock.patch("shipping.services.dhl.create_shipment", return_value=DHL_RESULT)
    def test_confirm_books_once_second_click_does_not_call_dhl(self, create):
        r = self.client.post(self.confirm_url(), {"pickup_type": "dropoff"})
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[2:5], ("P", "EXPRESS WORLDWIDE", 30.0))
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.tracking_number), (Shipment.Status.LABEL_READY, "1234567890"))
        self.assertTrue((self.media / "labels" / "dhl" / "1234567890.pdf").exists())

        r = self.client.post(self.confirm_url(), {"pickup_type": "dropoff"})
        self.assertTrue(any("уже оформлене в DHL" in m for m in self.messages(r)))
        r = self.client.post(self.urls["dhl"][1], {"product_code": "P", "price": "30"})
        create.assert_called_once()

    @mock.patch("shipping.services.dhl.create_shipment", return_value=DHL_RESULT)
    def test_link_to_book_url_never_books(self, create):
        """Колишнє «🚀 Оформити» на сторінці тарифів було посиланням (GET) — тепер воно лише веде на перевірку."""
        r = self.client.get(self.urls["dhl"][1] + "?product_code=P&product_name=X&price=30&pickup=1&customs=1")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(self.urls["dhl"][0] + "?product_code=P"), r["Location"])
        create.assert_not_called()
        self.assertEqual(self.reload().status, Shipment.Status.DRAFT)

    @mock.patch("shipping.services.dhl.create_shipment", return_value=DHL_RESULT)
    def test_rates_page_form_posts_pickup_and_customs(self, create):
        r = self.client.post(self.urls["dhl"][1], {
            "product_code": "N", "product_name": "DOMESTIC", "price": "9.90", "pickup": "1",
            "pickup_close": "17:00", "pickup_location": "warehouse", "customs": "0"})
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        kwargs = create.call_args.kwargs
        self.assertEqual(create.call_args.args[2:5], ("N", "DOMESTIC", 9.9))
        self.assertEqual((kwargs["request_pickup"], kwargs["pickup_close_time"], kwargs["pickup_location"],
                          kwargs["include_customs"]), (True, "17:00", "warehouse", False))

    def test_confirm_page_pickup_and_auto_customs(self):
        with mock.patch("shipping.services.dhl.create_shipment", return_value=DHL_RESULT) as create:
            self.client.post(self.confirm_url(), {"pickup_type": "pickup", "pickup_date": "2026-11-02",
                                                  "pickup_ready": "10:00", "pickup_close": "15:00"})
        kwargs = create.call_args.kwargs
        self.assertEqual((kwargs["request_pickup"], kwargs["pickup_date"], kwargs["pickup_ready_time"],
                          kwargs["include_customs"]), (True, "2026-11-02", "10:00", None))

    def test_dhl_refusal_and_crash_return_shipment_to_error(self):
        refused = {"success": False, "error": "HTTP 400: postalCode\nsecond line", "raw_request": {}, "raw_response": {}}
        with mock.patch("shipping.services.dhl.create_shipment", return_value=refused):
            r = self.client.post(self.confirm_url(), {"pickup_type": "dropoff"})
        self.assertEqual(self.reload().status, Shipment.Status.ERROR)
        self.assertIn("second line", self.messages(r))
        with mock.patch("shipping.services.dhl.create_shipment", side_effect=RuntimeError("boom")):
            r = self.client.post(self.confirm_url(), {"pickup_type": "dropoff"})
        shipment = self.reload()
        self.assertEqual(shipment.status, Shipment.Status.ERROR)
        self.assertIn("RuntimeError: boom", shipment.error_message)
        with mock.patch("shipping.services.dhl.create_shipment", return_value=DHL_RESULT) as create:
            self.client.post(self.confirm_url(), {"pickup_type": "dropoff"})
        create.assert_called_once()
        self.assertEqual(self.reload().status, Shipment.Status.LABEL_READY)

    def test_rates_page_books_with_a_csrf_post_form(self):
        rates = {"products": [{"name": "EXPRESS WORLDWIDE", "code": "P", "price": 30.5, "currency": "EUR",
                               "transit_days": 2, "delivery_date": "2026-11-03"}]}
        with mock.patch("shipping.services.dhl.get_rates", return_value=rates):
            r = self.client.get(f"/admin/shipping/shipment/{self.shipment.pk}/dhl-rates/")
        html = r.content.decode()
        self.assertNotIn("location.href=u", html)
        form = html[html.index('id="dhl-book-form"') - 120:][:900]
        self.assertIn(f'method="post" action="{self.urls["dhl"][1]}"', form)
        self.assertIn('name="csrfmiddlewaretoken"', form)
        self.assertIn('data-code="P" data-name="EXPRESS WORLDWIDE" data-price="30.5"', html)   # без «30,5»
        self.assertIn('onclick="return _dhlBook(this)"', html)
        self.assertIn("Кур\\u0027єр буде замовлений.", html)              # апостроф не ламає рядок JS


class FedExBookingTests(BookingBase):
    carrier_type = "fedex"

    @mock.patch.object(FedExClient, "create_shipment", return_value=FEDEX_RESULT)
    def test_confirm_books_once_and_saves_tracking_and_label(self, create):
        """Раніше після успішного бронювання падало (статус LABEL_CREATED, поле label_pdf) — трекінг губився,
        і повторне натискання створювало другу етикетку."""
        r = self.client.post(self.urls["fedex"][0], {"service_code": "FEDEX_PRIORITY"})
        self.assertRedirects(r, self.change_url, fetch_redirect_response=False)
        create.assert_called_once()
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.tracking_number), (Shipment.Status.LABEL_READY, "794600000000"))
        label = self.media / "shipping" / "labels" / f"fedex_{shipment.pk}_794600000000.pdf"
        self.assertEqual(label.read_bytes(), b"%PDF-1.4 fedex")
        self.assertEqual(shipment.label_url, f"/media/shipping/labels/{label.name}")

        r = self.client.post(self.urls["fedex"][1], {"service_code": "FEDEX_PRIORITY"})
        self.assertTrue(any("уже оформлене в FedEx" in m for m in self.messages(r)))
        create.assert_called_once()

    @mock.patch.object(FedExClient, "create_shipment", return_value=FEDEX_RESULT)
    def test_link_to_book_url_never_books(self, create):
        r = self.client.get(self.urls["fedex"][1] + "?service_code=FEDEX_PRIORITY")
        self.assertTrue(r["Location"].startswith(self.urls["fedex"][0]), r["Location"])
        create.assert_not_called()

    def test_fedex_refusal_returns_shipment_to_error(self):
        with mock.patch.object(FedExClient, "create_shipment", side_effect=FedExError("Account invalid")):
            r = self.client.post(self.urls["fedex"][0], {"service_code": "FEDEX_PRIORITY"})
        self.assertTrue(r["Location"].endswith("/edit-draft/"), r["Location"])
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.error_message), (Shipment.Status.ERROR, "Account invalid"))


class ConfirmTemplatesTests(TestCase):
    def test_confirm_pages_ignore_a_second_submit(self):
        for name in ("ups", "dhl", "fedex"):
            html = (TEMPLATES / f"{name}_confirm.html").read_text(encoding="utf-8")
            self.assertIn(f"getElementById('{name}-confirm-form').addEventListener('submit', function(e){{", html)
            self.assertIn("if (this.dataset.sent) { e.preventDefault(); return; }", html, name)


class FedExOrderTrackingTests(BookingBase):
    carrier_type = "fedex"

    @mock.patch.object(FedExClient, "create_shipment", return_value=FEDEX_RESULT)
    def test_tracking_and_courier_copied_to_order(self, create):
        self.client.post(self.urls["fedex"][0], {"service_code": "FEDEX_PRIORITY"})
        self.order.refresh_from_db()
        self.assertEqual((self.order.tracking_number, self.order.shipping_courier), ("794600000000", "FedEx"))


class FakeJumingo:
    def __init__(self, methods=("invoice",), order=None):
        self.methods, self.order, self.booked = list(methods), order, 0

    def cart_total(self, shipment_id):
        return {"paymentMethods": self.methods}

    def book_order(self, shipment_id, method):
        self.booked += 1
        return self.order if self.order is not None else {"orderNumber": f"JO-{self.booked}"}

    def get_order_documents(self, number):
        return {"labels": {}}


class JumingoBookingTests(BookingBase):
    carrier_type = "jumingo"

    def setUp(self):
        super().setUp()
        Shipment.objects.filter(pk=self.shipment.pk).update(selected_tariff_id="T1", carrier_shipment_id="J1")
        self.book_url = f"/admin/shipping/shipment/{self.shipment.pk}/book/"

    def test_link_shows_confirmation_and_books_nothing(self):
        fake = FakeJumingo()
        with mock.patch("shipping.admin.get_service", return_value=fake):
            r = self.client.get(self.book_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Підтвердити й замовити")
        self.assertContains(r, "csrfmiddlewaretoken")
        self.assertEqual((fake.booked, self.reload().status), (0, Shipment.Status.DRAFT))

    def test_post_books_once_second_post_refused(self):
        fake = FakeJumingo()
        with mock.patch("shipping.admin.get_service", return_value=fake):
            self.client.post(self.book_url)
            r = self.client.post(self.book_url)
        self.assertEqual(fake.booked, 1)
        shipment = self.reload()
        self.assertEqual((shipment.jumingo_order_number, shipment.status), ("JO-1", Shipment.Status.LABEL_READY))
        self.assertTrue(any("уже замовлене" in m for m in self.messages(r)))

    def test_refusal_and_crash_return_shipment_to_error(self):
        with mock.patch("shipping.admin.get_service", return_value=FakeJumingo(order={"error": "kein Guthaben"})):
            self.client.post(self.book_url)
        self.assertEqual(self.reload().status, Shipment.Status.ERROR)
        broken = FakeJumingo()
        broken.book_order = mock.Mock(side_effect=ConnectionError("timeout"))
        with mock.patch("shipping.admin.get_service", return_value=broken):
            self.client.post(self.book_url)
        shipment = self.reload()
        self.assertEqual(shipment.status, Shipment.Status.ERROR)
        self.assertIn("timeout", shipment.error_message)
