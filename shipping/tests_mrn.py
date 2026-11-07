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


class AbdToUpsTests(MrnFromMailTests):
    """📑 ABD в UPS до вже оформленої посилки: Paperless upload (004) + PushToImageRepository."""
    test_confirm_page_prefills_mrn_from_mail = None      # сторінка оформлення — лише для чернетки

    def setUp(self):
        super().setUp()
        Shipment.objects.filter(pk=self.shipment.pk).update(
            status=Shipment.Status.LABEL_READY, tracking_number="1Z0R6B300439360978",
            carrier_shipment_id="1Z0R6B300439360978", recipient_country="QA")
        self.url = f"/admin/shipping/shipment/{self.shipment.pk}/ups-abd/"
        self.detail = f"/admin/shipping/shipment/{self.shipment.pk}/detail/"

    def _abd_mail(self, user=None, address="me@sevskiy.de"):
        msg = self._mail(user or self.user, address)
        (self.media / "mail").mkdir(exist_ok=True)
        (self.media / "mail" / "abd.pdf").write_bytes(b"%PDF-1.4 ABD")
        msg.attachments = [{"name": "ABD_187472.pdf", "file_path": "mail/abd.pdf", "content_type": "application/pdf"}]
        msg.save(update_fields=["attachments"])
        return msg

    @mock.patch.object(UPSClient, "push_paperless_documents", return_value="G1")
    @mock.patch.object(UPSClient, "upload_paperless_document", return_value="D" * 26)
    def test_from_mail(self, upload, push):
        msg = self._abd_mail()
        page = self.client.get(self.detail)
        self.assertContains(page, "ABD_187472.pdf")
        r = self.client.post(self.url, {"abd_ref": f"msg:{msg.pk}:0"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(upload.call_args.args[:2], (b"%PDF-1.4 ABD", "ABD_187472.pdf"))
        self.assertEqual(upload.call_args.kwargs, {"document_type": "004", "file_format": "pdf"})
        ids, ups_id, tracking, _when = push.call_args.args
        self.assertEqual((ids, ups_id, tracking), (["D" * 26], "1Z0R6B300439360978", ["1Z0R6B300439360978"]))
        self.assertIn("UPS_ABD |", self.reload().notes)
        self.assertContains(self.client.get(self.detail), "Надіслано")

    @mock.patch.object(UPSClient, "push_paperless_documents", return_value="")
    @mock.patch.object(UPSClient, "upload_paperless_document", return_value="D" * 26)
    def test_from_pc(self, upload, push):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.post(self.url, {"abd_file": SimpleUploadedFile("ABD.png", b"\x89PNG..", "image/png")})
        self.assertEqual(upload.call_args.kwargs["file_format"], "png")
        push.assert_called_once()

    @mock.patch.object(UPSClient, "upload_paperless_document")
    def test_other_mailbox_and_bad_format_refused(self, upload):
        from django.core.files.uploadedfile import SimpleUploadedFile
        other = User.objects.create_user("colleague", password="x", is_staff=True)
        msg = self._abd_mail(other, "colleague@sevskiy.de")
        r = self.client.post(self.url, {"abd_ref": f"msg:{msg.pk}:0"})
        self.assertTrue(any("не знайдено" in m for m in self.messages(r)))
        r = self.client.post(self.url, {"abd_file": SimpleUploadedFile("x.exe", b"MZ", "application/octet-stream")})
        self.assertTrue(any("UPS приймає лише" in m for m in self.messages(r)))
        upload.assert_not_called()

    @mock.patch.object(UPSClient, "upload_paperless_document")
    def test_ups_error_keeps_notes(self, upload):
        from shipping.ups_client import UPSError
        upload.side_effect = UPSError("Upload Forms Created Offline not enabled")
        msg = self._abd_mail()
        r = self.client.post(self.url, {"abd_ref": f"msg:{msg.pk}:0"})
        self.assertTrue(any("UPS не прийняв ABD" in m for m in self.messages(r)))
        self.assertNotIn("UPS_ABD", self.reload().notes or "")

    def test_draft_and_get_do_nothing(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.DRAFT)
        self.assertNotContains(self.client.get(self.detail), "Надіслати ABD в UPS")

    def test_push_payload(self):
        from datetime import datetime as _dt
        from shipping.models import Carrier
        client = UPSClient(carrier=Carrier(name="UPS", carrier_type="ups", api_key="a", api_secret="b",
                                           connection_uuid="A1B2C3"))
        with mock.patch.object(UPSClient, "_post", return_value={"PushToImageRepositoryResponse": {
                "Response": {"ResponseStatus": {"Code": "1"}}, "FormsGroupID": "G1"}}) as post:
            self.assertEqual(client.push_paperless_documents(["D" * 26], "1ZX", ["1ZX"], _dt(2026, 11, 5, 11, 27, 15)),
                             "G1")
        endpoint, body = post.call_args.args[:2]
        req = body["PushToImageRepositoryRequest"]
        self.assertEqual(endpoint, "/api/paperlessdocuments/v2/image")
        self.assertEqual((req["ShipmentDateAndTime"], req["ShipmentType"], req["ShipperNumber"]),
                         ("2026-11-05-11.27.15", "1", "A1B2C3"))
