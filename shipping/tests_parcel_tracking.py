"""Посилки одного замовлення: «📦 Відстежувати» не переписує номер іншої посилки, лагодження, рядки в картці.

    python manage.py test shipping.tests_parcel_tracking --settings=tabele.settings_test
"""
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from sales.models import SalesOrder
from shipping.models import Carrier, Shipment
from shipping.services import parcel_repair
from shipping.services.import_tracking import ensure_shipment_for_order, tracking_numbers_in

TN1 = "1Z0R6B300439360978"
TN2 = "1Z0R6B300439360987"


class ParcelTrackingBase(TestCase):
    def setUp(self):
        self.carrier = Carrier.objects.create(name="UPS API", carrier_type="ups")
        self.order = SalesOrder.objects.create(source="digikey", order_number="102519268",
                                               tracking_number=TN1, affects_stock=False)

    def ship(self, tn="", status=Shipment.Status.IN_TRANSIT, **kw):
        return Shipment.objects.create(order=self.order, carrier=self.carrier, tracking_number=tn,
                                       status=status, **kw)


class EnsureShipmentTests(ParcelTrackingBase):
    def test_second_parcel_keeps_its_own_number(self):
        first = self.ship(TN1)
        second = self.ship(TN2, status=Shipment.Status.LABEL_READY)
        shipment, created = ensure_shipment_for_order(self.order, TN1, "UPS")
        self.assertEqual((shipment.pk, created), (first.pk, False))
        second.refresh_from_db()
        self.assertEqual((second.tracking_number, second.status), (TN2, Shipment.Status.LABEL_READY))

    def test_draft_without_number_gets_it(self):
        draft = self.ship("", status=Shipment.Status.DRAFT)
        shipment, created = ensure_shipment_for_order(self.order, TN1, "UPS")
        self.assertEqual((shipment.pk, created, shipment.tracking_number), (draft.pk, False, TN1))

    def test_unknown_number_makes_new_shipment(self):
        self.ship(TN2)
        shipment, created = ensure_shipment_for_order(self.order, TN1, "UPS")
        self.assertTrue(created)
        self.assertEqual(self.order.shipments.count(), 2)

    def test_several_numbers_in_field(self):
        self.assertEqual(tracking_numbers_in(f"Part1: {TN1} Part2: {TN2}"), [TN1, TN2])
        second = self.ship(TN2)
        shipment, created = ensure_shipment_for_order(self.order, f"{TN1}, {TN2}", "UPS")
        self.assertEqual((shipment.pk, created), (second.pk, False))


class RepairTests(ParcelTrackingBase):
    def test_restores_number_from_label_file(self):
        first = self.ship(TN1, carrier_shipment_id=TN1)
        second = self.ship(TN1)
        Shipment.objects.filter(pk=second.pk).update(
            label_url=f"/media/shipping/labels/ups_{second.pk}_{TN2}.pdf", carrier_shipment_id=TN2)
        rows = parcel_repair.plan()
        self.assertEqual([(r["pk"], r["new"]) for r in rows], [(second.pk, TN2)])
        out = StringIO()
        call_command("repair_parcel_tracking", stdout=out)
        second.refresh_from_db()
        self.assertEqual(second.tracking_number, TN1)          # без --apply нічого не змінюється
        call_command("repair_parcel_tracking", "--apply", stdout=out)
        second.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual((first.tracking_number, second.tracking_number), (TN1, TN2))
        self.assertEqual(parcel_repair.plan(), [])

    def test_unknown_real_number_is_reported(self):
        self.ship(TN1)
        second = self.ship(TN1)
        rows = parcel_repair.plan()
        self.assertEqual([(r["pk"], r["new"]) for r in rows], [(second.pk, "")])
        self.assertEqual(parcel_repair.apply(rows), 0)


class OrderCardTests(ParcelTrackingBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(self.user)
        self.url = reverse("admin:sales_salesorder_change", args=[self.order.pk])

    def test_one_shipment_no_parcel_rows(self):
        self.ship(TN1)
        self.assertNotContains(self.client.get(self.url), "📦 Посилки")

    def test_two_parcels_listed_with_duplicate_warning(self):
        self.ship(TN1)
        self.ship(TN1)
        r = self.client.get(self.url)
        self.assertContains(r, "📦 Посилки")
        self.assertContains(r, "<b>2/2</b>", html=False)
        self.assertContains(r, "той самий номер, що посилка 1")

    def test_parcel_table_has_boxes_cost_and_total(self):
        from decimal import Decimal
        from shipping.models import ShipmentPackage
        a = self.ship(TN1, carrier_price=Decimal("28.68"), carrier_currency="EUR")
        b = self.ship(TN2, carrier_price=Decimal("30.00"), carrier_currency="EUR")
        ShipmentPackage.objects.create(shipment=a, length_cm=30, width_cm=20, height_cm=15, weight_kg=Decimal("1.2"))
        ShipmentPackage.objects.create(shipment=b, length_cm=40, width_cm=30, height_cm=20, weight_kg=2, quantity=2)
        r = self.client.get(self.url)
        for text in (TN1, TN2, "30×20×15 см, 1.2 кг", "40×30×20 см, 2 кг ×2", "28,68 EUR",
                     "Разом за посилки", "58,68 EUR", "Вписати у «Вартість доставки»"):
            self.assertContains(r, text)
        self.assertNotContains(r, "той самий номер")

    def test_one_shipment_with_two_boxes_shows_box_numbers(self):
        self.ship(TN1, package_tracking=[TN1, TN2])
        r = self.client.get(self.url)
        self.assertContains(r, "📦 Посилки")
        self.assertContains(r, TN2)

    def test_fix_tracking_on_shipment_page(self):
        first = self.ship(TN1)
        second = self.ship(TN1)
        fix = reverse("admin:shipping_shipment_set_tracking", args=[second.pk])
        detail = reverse("admin:shipping_shipment_detail", args=[second.pk])
        self.assertContains(self.client.get(detail + "?fix=1"), "Виправити трек-номер")
        self.client.post(fix, {"tracking_number": TN2, "package_tracking": ""})
        second.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((second.tracking_number, second.package_tracking), (TN2, []))
        self.assertEqual(self.order.tracking_number, TN1)        # номер першої посилки лишається в замовленні
        self.client.post(reverse("admin:shipping_shipment_set_tracking", args=[first.pk]),
                         {"tracking_number": TN1, "package_tracking": f"{TN1}\n{TN2}\n{TN2}"})
        first.refresh_from_db()
        self.assertEqual(first.package_tracking, [TN1, TN2])

    def test_fix_own_number_updates_order(self):
        only = self.ship(TN1)
        self.client.post(reverse("admin:shipping_shipment_set_tracking", args=[only.pk]), {"tracking_number": TN2})
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_number, TN2)


class UpsPackageNumbersTests(TestCase):
    def test_package_numbers_from_response(self):
        from unittest import mock
        from shipping.ups_client import UPSClient
        carrier = Carrier(name="UPS", carrier_type="ups", api_key="a", api_secret="b", connection_uuid="A1B2C3",
                          sender_country="DE", sender_city="München", sender_zip="81829", sender_street="X 1")
        resp = {"ShipmentResponse": {"ShipmentResults": {
            "ShipmentIdentificationNumber": TN1,
            "PackageResults": [{"TrackingNumber": TN1, "ShippingLabel": {}},
                               {"TrackingNumber": TN2, "ShippingLabel": {}}]}}}
        pkgs = [{"weight_kg": 1, "length_cm": 20, "width_cm": 10, "height_cm": 5}] * 2
        with mock.patch.object(UPSClient, "_post", return_value=resp):
            result = UPSClient(carrier=carrier).create_shipment(
                {"name": "Y", "country": "DE", "city": "Berlin", "street": "Z", "zip": "10115"}, pkgs)
        self.assertEqual((result["tracking_number"], result["package_tracking"]), (TN1, [TN1, TN2]))
