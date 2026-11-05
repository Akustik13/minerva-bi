"""MRN з Ausfuhrbegleitdokument → UPS Shipment.MovementReferenceNumber (shipping/services/mrn.py).

    python manage.py test shipping.tests_mrn --settings=tabele.settings_test
"""
from datetime import datetime, timezone as dt_tz
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from crm.models import Customer, CustomerTimeline
from email_assistant.models import EmailAccount, EmailMessage
from shipping.models import Shipment
from shipping.services import mrn
from shipping.tests_booking import UPS_RESULT, BookingBase
from shipping.ups_client import UPSClient

MRN = "26DE760298696748B4"
SUBJECT = f"ABD - Ausfuhrbegleitdokument (DE), 187472-1, {MRN}"


class MrnFormatTests(TestCase):
    def test_problem(self):
        self.assertEqual(mrn.problem("", "DE"), "")
        self.assertEqual(mrn.problem(MRN, "DE"), "")
        self.assertEqual(mrn.problem(MRN, ""), "")
        self.assertIn("18", mrn.problem(MRN[:-1], "DE"))
        self.assertIn("латинські", mrn.problem("26DE76029869674ЖB4", "DE"))
        self.assertIn("(AT)", mrn.problem(MRN, "AT"))

    def test_normalize_and_find(self):
        self.assertEqual(mrn.normalize(" 26de 7602-9869.6748b4 "), MRN)
        self.assertEqual(mrn.find_in_text(SUBJECT, "DE"), MRN)
        self.assertEqual(mrn.find_in_text("Order 102497269, LRN 187472-1", "DE"), "")
        self.assertEqual(mrn.find_in_text(f"26AT760298696748B4 {MRN}", "DE"), MRN)

    def test_ups_payload_has_mrn_only_for_international(self):
        from shipping.models import Carrier
        carrier = Carrier(name="UPS", carrier_type="ups", api_key="a", api_secret="b", connection_uuid="A1B2C3",
                          sender_country="DE", sender_city="München", sender_zip="81829", sender_street="X 1")
        client = UPSClient(carrier=carrier)
        pkg = [{"weight_kg": 0.4, "length_cm": 20, "width_cm": 10, "height_cm": 5}]
        customs = {"items": [{"description": "Antenna", "quantity": 1, "value": 10, "weight_kg": 0.1}],
                   "currency": "USD"}
        intl = client.create_shipment({"name": "Y", "country": "QA", "city": "Doha", "street": "Z"}, pkg,
                                      customs_info=customs, mrn=MRN, dry_run=True)
        self.assertEqual(intl["ShipmentRequest"]["Shipment"]["MovementReferenceNumber"], MRN)
        home = client.create_shipment({"name": "Y", "country": "DE", "city": "Berlin", "street": "Z",
                                       "zip": "10115"}, pkg, mrn=MRN, dry_run=True)
        self.assertNotIn("MovementReferenceNumber", home["ShipmentRequest"]["Shipment"])


class MrnBookingTests(BookingBase):
    carrier_type = "ups"

    def setUp(self):
        super().setUp()
        Shipment.objects.filter(pk=self.shipment.pk).update(recipient_country="QA", recipient_city="Doha",
                                                            recipient_zip="")

    def post_confirm(self, **extra):
        return self.client.post(self.urls["ups"][0], {"service_code": "65", "pickup_type": "dropoff",
                                                      "terms_of_shipment": "DAP", **extra})

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_valid_mrn_is_saved_and_sent_to_ups(self, create):
        self.post_confirm(export_mrn=" 26de760298696748b4 ")
        self.assertEqual(create.call_args.kwargs["mrn"], MRN)
        self.assertEqual(self.reload().export_mrn, MRN)

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_invalid_mrn_stops_before_ups(self, create):
        r = self.post_confirm(export_mrn="26AT760298696748B4")
        create.assert_not_called()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(any("MRN" in m for m in self.messages(r)), self.messages(r))
        shipment = self.reload()
        self.assertEqual((shipment.status, shipment.export_mrn), (Shipment.Status.DRAFT, ""))

    @mock.patch.object(UPSClient, "create_shipment", return_value=UPS_RESULT)
    def test_empty_mrn_is_fine(self, create):
        self.post_confirm(export_mrn="")
        self.assertEqual(create.call_args.kwargs["mrn"], "")

    def test_dry_run_uses_typed_mrn(self):
        with mock.patch.object(UPSClient, "get_token", return_value="tok"):
            r = self.client.get(self.urls["ups"][0].replace("ups-confirm", "ups-dry-run") +
                                f"?service_code=65&mrn={MRN.lower()}")
        self.assertEqual(r.json()["payload"]["ShipmentRequest"]["Shipment"].get("MovementReferenceNumber"), MRN)


class MrnFromMailTests(BookingBase):
    carrier_type = "ups"

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(name="Voltaat", email="y@voltaat.com")

    def _mail(self, user, address, subject=SUBJECT):
        account = EmailAccount.objects.create(
            user=user, email_address=address, imap_username=address, imap_password="x",
            smtp_username=address, smtp_password="x", imap_folder_inbox="INBOX", imap_folder_sent="Sent")
        msg = EmailMessage.objects.create(
            account=account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name="INBOX", imap_uid=7,
            subject=subject, from_email="noreply@dakosy.de", to_emails=[address], cc_emails=[],
            body_text="Ihr Ausfuhrbegleitdokument", sent_at=datetime(2026, 11, 2, 14, 1, tzinfo=dt_tz.utc))
        CustomerTimeline.objects.create(customer=self.customer, event_type="email_in", title=subject,
                                        related_order_id=self.order.pk, email_message=msg)
        return msg

    def test_found_in_own_linked_abd_mail(self):
        msg = self._mail(self.user, "me@sevskiy.de")
        found = mrn.find_in_mail(self.order, self.user, "DE")
        self.assertEqual((found["mrn"], found["message_pk"]), (MRN, msg.pk))

    def test_other_peoples_mailboxes_are_not_used(self):
        other = User.objects.create_user("colleague", password="x", is_staff=True)
        self._mail(other, "colleague@sevskiy.de")
        self.assertIsNone(mrn.find_in_mail(self.order, self.user, "DE"))

    def test_confirm_page_prefills_mrn_from_mail(self):
        self._mail(self.user, "me@sevskiy.de")
        with mock.patch("shipping.admin.ShipmentAdmin._ups_extract_customs",
                        return_value={"items": [{"description": "Antenna", "quantity": 1, "value": 10,
                                                 "weight_kg": 0.1}], "currency": "USD", "value_usd": 10}):
            Shipment.objects.filter(pk=self.shipment.pk).update(recipient_country="QA")
            r = self.client.get(self.urls["ups"][0] + "?service_code=65")
        self.assertContains(r, f'value="{MRN}"')
        self.assertContains(r, "Підставлено з листа ABD")
