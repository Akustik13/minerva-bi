"""Доставка: скасування відправлення UPS («🗑️ Void — скасувати в UPS») і підказки форми відправлення.

UPS імітується — мережі немає.  python manage.py test shipping --settings=tabele.settings_test
"""
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase

from sales.models import SalesOrder
from shipping.models import Carrier, Shipment
from shipping.ups_client import UPSClient, UPSError, void_result

VOIDED = {"VoidShipmentResponse": {
    "Response": {"ResponseStatus": {"Code": "1", "Description": "Success"}},
    "SummaryResult": {"Status": {"Code": "1", "Description": "Voided"}}}}
TEMPLATES = Path(__file__).resolve().parent / "templates" / "admin" / "shipping"


class VoidResultTests(TestCase):
    def test_parse(self):
        self.assertEqual(void_result(VOIDED), {"ok": True, "status": "Voided"})
        partial = {"VoidShipmentResponse": {"SummaryResult": {"Status": {"Code": "0", "Description": "Partially Voided"}}}}
        self.assertEqual(void_result(partial), {"ok": False, "status": "Partially Voided"})
        short = {"VoidShipmentResponse": {"Response": {"ResponseStatus": {"Code": "1", "Description": "Success"}}}}
        self.assertTrue(void_result(short)["ok"])
        self.assertFalse(void_result({})["ok"])
        self.assertFalse(void_result(None)["ok"])


class UPSVoidTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.ups = Carrier.objects.create(name="UPS", carrier_type="ups", api_key="id", api_secret="secret",
                                          connection_uuid="A1B2C3", api_url="sandbox")
        self.order = SalesOrder.objects.create(source="webshop", order_number="300001")
        self.shipment = Shipment.objects.create(
            order=self.order, carrier=self.ups, status=Shipment.Status.LABEL_READY,
            tracking_number="1ZA1B2C30412345678", carrier_shipment_id="1ZA1B2C30412345678",
            recipient_name="Muster GmbH", recipient_country="CH")
        self.void_url = f"/admin/shipping/shipment/{self.shipment.pk}/ups-void/"
        self.detail_url = f"/admin/shipping/shipment/{self.shipment.pk}/detail/"

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_detail_page_has_void_button_with_confirmation(self):
        r = self.client.get(self.detail_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'action="{self.void_url}"')
        self.assertContains(r, "Void — скасувати в UPS")
        self.assertContains(r, "return confirm(")

    def test_no_void_button_for_other_carriers_or_finished_shipments(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.DELIVERED)
        self.assertNotContains(self.client.get(self.detail_url), self.void_url)
        dhl = Carrier.objects.create(name="DHL", carrier_type="dhl")
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.LABEL_READY, carrier=dhl)
        self.assertNotContains(self.client.get(self.detail_url), self.void_url)

    @mock.patch.object(UPSClient, "void_shipment", return_value=VOIDED)
    def test_void_success_cancels_and_logs(self, void):
        r = self.client.post(self.void_url)
        self.assertRedirects(r, self.detail_url, fetch_redirect_response=False)
        void.assert_called_once_with("1ZA1B2C30412345678")
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.CANCELLED)
        self.assertRegex(self.shipment.notes, r"UPS_VOID \| \d\d\.\d\d\.\d{4} \d\d:\d\d \| ID:1ZA1B2C30412345678")
        self.assertTrue(any("скасовано в UPS (Void)" in m for m in self.messages(r)))
        page = self.client.get(self.detail_url)
        self.assertContains(page, "Відправлення скасовано в UPS (Void)")
        self.assertNotContains(page, self.void_url)                 # скасоване вдруге не скасовується

    @mock.patch.object(UPSClient, "void_shipment", return_value=VOIDED)
    def test_void_warns_about_scheduled_pickup(self, void):
        Shipment.objects.filter(pk=self.shipment.pk).update(
            notes="UPS_PICKUP | PRN:2929602E9CP | DATE:20261031 | TIME:1100-1600 | POINT:SUITE")
        r = self.client.post(self.void_url)
        self.assertTrue(any("PRN 2929602E9CP" in m and "Скасувати забір UPS" in m for m in self.messages(r)))
        self.shipment.refresh_from_db()
        self.assertIn("UPS_PICKUP | PRN:2929602E9CP", self.shipment.notes)    # рядок забору лишився
        self.assertIn("UPS_VOID |", self.shipment.notes)

    @mock.patch.object(UPSClient, "void_shipment",
                       side_effect=UPSError("UPS API [400]: [190117] Shipment has been picked up", code=400))
    def test_void_refused_by_ups_keeps_status(self, void):
        r = self.client.post(self.void_url)
        self.assertRedirects(r, self.detail_url, fetch_redirect_response=False)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_READY)
        self.assertNotIn("UPS_VOID", self.shipment.notes)
        msgs = self.messages(r)
        self.assertTrue(any("190117" in m and "Статус вручну" in m for m in msgs), msgs)

    @mock.patch.object(UPSClient, "void_shipment", return_value={
        "VoidShipmentResponse": {"SummaryResult": {"Status": {"Code": "0", "Description": "Not Voided"}}}})
    def test_void_not_confirmed_keeps_status(self, void):
        r = self.client.post(self.void_url)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_READY)
        self.assertTrue(any("Not Voided" in m for m in self.messages(r)))

    def test_missing_credentials_is_a_clear_error(self):
        Carrier.objects.filter(pk=self.ups.pk).update(api_secret="")
        with mock.patch.object(UPSClient, "void_shipment") as void:
            r = self.client.post(self.void_url)
        void.assert_not_called()
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_READY)
        self.assertTrue(any("API Secret" in m for m in self.messages(r)))

    def test_nothing_to_void(self):
        with mock.patch.object(UPSClient, "void_shipment") as void:
            self.assertEqual(self.client.get(self.void_url).status_code, 302)        # лише POST
            Shipment.objects.filter(pk=self.shipment.pk).update(tracking_number="", carrier_shipment_id="")
            r = self.client.post(self.void_url)
            self.assertTrue(any("1Z" in m for m in self.messages(r)))
            Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.DELIVERED,
                                                                tracking_number="1ZX")
            r = self.client.post(self.void_url)
            self.assertTrue(any("Доставлено" in m for m in self.messages(r)))
            dhl = Carrier.objects.create(name="DHL", carrier_type="dhl")
            Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.LABEL_READY, carrier=dhl)
            r = self.client.post(self.void_url)
            self.assertTrue(any("лише для відправлень UPS" in m for m in self.messages(r)))
        void.assert_not_called()

    def test_void_needs_change_permission(self):
        from shipping.admin import ShipmentAdmin
        with mock.patch.object(UPSClient, "void_shipment") as void, \
                mock.patch.object(ShipmentAdmin, "has_change_permission", return_value=False):
            r = self.client.post(self.void_url)
        self.assertEqual(r.status_code, 403)
        void.assert_not_called()
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_READY)

    def test_confirm_handlers_are_valid_js(self):
        """Апостроф («кур'єр») або справжній перевід рядка в confirm('…') ламали обробник — і форма
        скасування йшла БЕЗ підтвердження. Перевіряємо кожен confirm у картці: UPS, DHL, «⚡ Дії» скасованого."""
        import html
        import re
        dhl = Carrier.objects.create(name="DHL", carrier_type="dhl")
        cases = [(self.ups, Shipment.Status.LABEL_READY), (dhl, Shipment.Status.LABEL_READY),
                 (self.ups, Shipment.Status.CANCELLED)]
        for carrier, status in cases:
            Shipment.objects.filter(pk=self.shipment.pk).update(carrier=carrier, status=status)
            page = self.client.get(self.detail_url).content.decode()
            handlers = re.findall(r'on(?:click|submit)="return confirm\(\'(.*?)\'\)"', page, re.S)
            self.assertTrue(handlers, (carrier.carrier_type, status))
            for text in handlers:
                text = html.unescape(text)
                self.assertNotIn("\n", text, (carrier.carrier_type, status))
                self.assertNotRegex(text, r"(?<!\\)'", (carrier.carrier_type, status))

    def test_client_calls_ups_void_endpoint_and_logs(self):
        """UPSClient.void_shipment: DELETE …/void/cancel/<1Z> з токеном; виклик іде в «📋 UPS Лог»."""
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = VOIDED
        with mock.patch.object(UPSClient, "get_token", return_value="tok"), \
                mock.patch("shipping.ups_client.requests.delete", return_value=response) as delete, \
                mock.patch("shipping.ups_logger.log_call") as log:
            body = UPSClient(carrier=self.ups).void_shipment("1ZA1B2C30412345678")
        self.assertEqual(body, VOIDED)
        url = delete.call_args[0][0]
        self.assertEqual(url, "https://wwwcie.ups.com/api/shipments/v2409/void/cancel/1ZA1B2C30412345678")
        self.assertEqual(delete.call_args[1]["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(log.call_args[1]["action"], "void_shipment")

    def test_client_raises_ups_error_on_refusal(self):
        response = mock.Mock(ok=False, status_code=400, text="{}")
        response.json.return_value = {"response": {"errors": [{"code": "190117", "message": "Picked up"}]}}
        with mock.patch.object(UPSClient, "get_token", return_value="tok"), \
                mock.patch("shipping.ups_client.requests.delete", return_value=response), \
                mock.patch("shipping.ups_logger.log_call"):
            with self.assertRaisesRegex(UPSError, "190117"):
                UPSClient(carrier=self.ups).void_shipment("1ZA1B2C30412345678")


class ShipmentTextsTests(TestCase):
    """Тексти, що посилаються на кнопки: лише ті, що справді є на сторінках."""

    def test_error_banner_points_to_current_flow(self):
        html = (TEMPLATES / "create_shipment.html").read_text(encoding="utf-8")
        self.assertNotIn("Надіслати до перевізника", html)
        banner = html[html.index("Помилка при попередньому оформленні"):][:800]
        self.assertIn("📊 Порівняти тарифи", banner)
        self.assertIn("📊 Порівняти тарифи", html[html.index('id="btn-compare"'):][:200])   # така кнопка є

    def test_ups_confirm_names_the_real_void_button(self):
        confirm = (TEMPLATES / "ups_confirm.html").read_text(encoding="utf-8")
        detail = (TEMPLATES / "shipment_detail.html").read_text(encoding="utf-8")
        self.assertIn("«🗑️ Void — скасувати в UPS»", confirm)
        self.assertIn("🗑️ {% trans \"Void — скасувати в UPS\" %}", detail)
