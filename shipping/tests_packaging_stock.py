"""Запас коробок — `shipping/services/packaging_stock.py`: оформлене відправлення списує коробку, скасоване
повертає, прихід / інвентаризація, повідомлення «📦 Мало коробок».

    python manage.py test shipping.tests_packaging_stock --settings=tabele.settings_test
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from sales.models import SalesOrder
from shipping.models import (Carrier, PackagingMaterial, PackagingMovement, Shipment, ShipmentPackage,
                             ShippingSettings)
from shipping.services import packaging_stock

NOTIFY = "dashboard.notifications.notify_packaging_low"


class Base(TestCase):
    def setUp(self):
        self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups", sender_country="DE")
        self.box = PackagingMaterial.objects.create(length_cm=20, width_cm=15, height_cm=5, track_stock=True,
                                                    min_stock=2)
        PackagingMaterial.objects.filter(pk=self.box.pk).update(stock_qty=5)
        self.n = 0

    def stock(self, m=None):
        return PackagingMaterial.objects.get(pk=(m or self.box).pk).stock_qty

    def shipment(self, rows=None, dims=(20, 15, 5), status=Shipment.Status.DRAFT, **kw):
        self.n += 1
        order = SalesOrder.objects.create(source="webshop", order_number=f"7000{self.n}")
        s = Shipment.objects.create(order=order, carrier=self.carrier, status=status, weight_kg=Decimal("1"),
                                    length_cm=dims[0], width_cm=dims[1], height_cm=dims[2], **kw)
        for r in rows or []:
            ShipmentPackage.objects.create(shipment=s, **r)
        return s

    def set_status(self, s, status):
        with self.captureOnCommitCallbacks(execute=True):
            s.status = status
            s.save()


class ShipmentStockTests(Base):
    def test_booked_shipment_uses_box_and_cancel_returns_it(self):
        s = self.shipment()
        self.set_status(s, Shipment.Status.LABEL_READY)
        self.assertEqual(self.stock(), 4)
        mv = PackagingMovement.objects.get(shipment=s)
        self.assertEqual((mv.kind, mv.qty, mv.balance), ("shipment", -1, 4))
        self.set_status(s, Shipment.Status.IN_TRANSIT)          # повтор — нічого нового
        self.assertEqual(self.stock(), 4)
        self.set_status(s, Shipment.Status.CANCELLED)
        self.assertEqual(self.stock(), 5)
        self.assertEqual(list(PackagingMovement.objects.filter(shipment=s).values_list("kind", flat=True)),
                         ["return", "shipment"])

    def test_package_rows_count_identical_boxes_and_rotated_dims(self):
        s = self.shipment(rows=[{"weight_kg": 1, "length_cm": 5, "width_cm": 20, "height_cm": 15, "quantity": 3}])
        self.set_status(s, Shipment.Status.SUBMITTED)
        self.assertEqual(self.stock(), 2)
        with self.captureOnCommitCallbacks(execute=True):        # коробку додали до вже оформленої посилки
            ShipmentPackage.objects.create(shipment=s, weight_kg=1, length_cm=20, width_cm=15, height_cm=5)
        self.assertEqual(self.stock(), 1)

    def test_untracked_other_size_incoming_and_old_shipments_not_counted(self):
        other = PackagingMaterial.objects.create(length_cm=40, width_cm=30, height_cm=20)   # без обліку
        self.set_status(self.shipment(dims=(40, 30, 20)), Shipment.Status.LABEL_READY)
        self.set_status(self.shipment(dims=(33, 22, 11)), Shipment.Status.LABEL_READY)      # такої коробки немає
        self.set_status(self.shipment(direction="in"), Shipment.Status.IN_TRANSIT)          # посилка до нас
        old = self.shipment(submitted_at=timezone.now() - timedelta(days=3))                # до початку обліку
        self.set_status(old, Shipment.Status.DELIVERED)
        self.assertEqual((self.stock(), self.stock(other)), (5, 0))
        self.assertFalse(PackagingMovement.objects.exists())

    def test_draft_and_error_do_not_use_boxes(self):
        s = self.shipment()
        self.set_status(s, Shipment.Status.ERROR)
        self.assertEqual(self.stock(), 5)


class ManualMovesTests(Base):
    def test_receive_and_count(self):
        user = User.objects.create_user("w")
        packaging_stock.receive(self.box, 10, user=user, note="Rechnung 42")
        self.assertEqual(self.stock(), 15)
        self.assertIsNone(packaging_stock.count(self.box, 15))                  # уже стільки
        mv = packaging_stock.count(self.box, 12)
        self.assertEqual((mv.kind, mv.qty, mv.balance), ("adjust", -3, 12))
        with self.assertRaises(ValueError):
            packaging_stock.receive(self.box, 0)
        with self.assertRaises(ValueError):
            packaging_stock.count(self.box, -1)

    def test_tracking_start_sets_since(self):
        m = PackagingMaterial.objects.create(length_cm=30, width_cm=20, height_cm=10)
        self.assertIsNone(m.stock_since)
        m.track_stock = True
        m.save()
        self.assertIsNotNone(m.stock_since)
        m.track_stock = False
        m.save()
        self.assertIsNone(m.stock_since)


class LowStockTests(Base):
    def test_alert_once_when_low_then_again_after_restock(self):
        with mock.patch(NOTIFY, return_value=["лист"]) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                packaging_stock.count(self.box, 3)
            notify.assert_not_called()                                   # 3 > мінімуму 2
            with self.captureOnCommitCallbacks(execute=True):
                packaging_stock.count(self.box, 2)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[0][0].pk, self.box.pk)
            with self.captureOnCommitCallbacks(execute=True):
                packaging_stock.count(self.box, 1)
            self.assertEqual(notify.call_count, 1)                       # уже попереджали
            with self.captureOnCommitCallbacks(execute=True):
                packaging_stock.receive(self.box, 10)
            self.assertIsNone(PackagingMaterial.objects.get(pk=self.box.pk).low_notified_at)
            with self.captureOnCommitCallbacks(execute=True):
                packaging_stock.count(self.box, 0)
            self.assertEqual(notify.call_count, 2)

    def test_disabled_or_no_minimum_is_silent(self):
        s = ShippingSettings.get()
        s.packaging_alert_enabled = False
        s.save()
        with mock.patch(NOTIFY) as notify, self.captureOnCommitCallbacks(execute=True):
            packaging_stock.count(self.box, 0)
        notify.assert_not_called()

    def test_remind_after_days(self):
        s = ShippingSettings.get()
        s.packaging_alert_repeat_days = 2
        s.save()
        now = timezone.now()
        PackagingMaterial.objects.filter(pk=self.box.pk).update(stock_qty=1, low_notified_at=now - timedelta(days=1))
        with mock.patch(NOTIFY) as notify:
            self.assertEqual(packaging_stock.remind_due(now), [])
            PackagingMaterial.objects.filter(pk=self.box.pk).update(low_notified_at=now - timedelta(days=3))
            self.assertEqual([m.pk for m in packaging_stock.remind_due(now)], [self.box.pk])
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(packaging_stock.remind_due(now), [])              # щойно нагадали

    def test_message_goes_to_packaging_route(self):
        from config.models import NotificationSettings
        from dashboard import notifications
        ns = NotificationSettings.get()
        ns.email_enabled = ns.telegram_enabled = True
        ns.email_host, ns.email_to = "smtp.example.com", "all@example.com"
        ns.telegram_bot_token, ns.telegram_chat_id = "1:x", "111"
        ns.routes = {"packaging": {"to": ["boxes@example.com"], "tg": ["222"]}}
        ns.save()
        self.box.reorder_qty, self.box.buy_url, self.box.stock_qty = 50, "https://shop.example.com/box", 1
        seen = {}

        def mail(ns_, subject, html):
            seen["to"], seen["subject"], seen["html"] = notifications.recipients(ns_), subject, html

        def tg(ns_, text, chat_id_override=None):
            seen["chats"], seen["text"] = notifications.tg_chats(ns_), text

        with mock.patch.object(notifications, "_send_event_email", side_effect=mail), \
                mock.patch.object(notifications, "_send_telegram", side_effect=tg):
            sent = notifications.notify_packaging_low([self.box])
        self.assertEqual(sent, ["лист", "Telegram"])
        self.assertEqual((seen["to"], seen["chats"]), (["boxes@example.com"], ["222"]))
        self.assertIn("Мало коробок", seen["subject"])
        for part in ("на складі 1 шт", "мінімум 2 шт", "купити ≈ 50 шт", "https://shop.example.com/box"):
            self.assertIn(part, seen["html"])
            self.assertIn(part, seen["text"])


class AdminTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True))

    def post_box(self, **extra):
        data = {"box_type": "box", "name": self.box.name, "is_active": "on", "notes": "", "length_cm": "20",
                "width_cm": "15", "height_cm": "5", "tare_weight_kg": "0", "track_stock": "on", "min_stock": "2",
                "buy_url": "", **extra}
        return self.client.post(f"/admin/shipping/packagingmaterial/{self.box.pk}/change/", data)

    def test_card_receive_and_count(self):
        with mock.patch(NOTIFY):
            r = self.post_box(receive_qty="7", move_note="Rechnung 42")
            self.assertEqual(r.status_code, 302)
            self.assertEqual(self.stock(), 12)
            self.post_box(count_qty="9")
            self.assertEqual(self.stock(), 9)
        page = self.client.get(f"/admin/shipping/packagingmaterial/{self.box.pk}/change/")
        self.assertContains(page, "Rechnung 42")
        self.assertContains(page, "9 шт")

    def test_card_needs_tracking_for_moves(self):
        r = self.post_box(track_stock="", receive_qty="3")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Вести облік на складі")
        self.assertEqual(self.stock(), 5)

    def test_list_low_filter_and_column(self):
        PackagingMaterial.objects.filter(pk=self.box.pk).update(stock_qty=1)
        page = self.client.get("/admin/shipping/packagingmaterial/?stock=low")
        self.assertContains(page, "⚠️ 1 шт")
        PackagingMaterial.objects.filter(pk=self.box.pk).update(stock_qty=8)
        page = self.client.get("/admin/shipping/packagingmaterial/?stock=low")
        self.assertNotContains(page, "8 шт")

    def test_settings_panel_and_send_now(self):
        ShippingSettings.get()
        page = self.client.get("/admin/shipping/shippingsettings/1/change/")
        self.assertContains(page, "📦 Запас коробок")
        self.assertContains(page, "pkg-alert-form")
        url = "/admin/shipping/shippingsettings/1/packaging-alert/"
        with mock.patch(NOTIFY, return_value=["лист"]) as notify:
            self.assertEqual(self.client.get(url).status_code, 302)      # GET нічого не надсилає
            notify.assert_not_called()
            self.client.post(url)                                        # запасу 5 > 2 — нема про що
            notify.assert_not_called()
            PackagingMaterial.objects.filter(pk=self.box.pk).update(stock_qty=1)
            self.client.post(url)
            self.assertEqual(notify.call_count, 1)
        self.assertIsNotNone(PackagingMaterial.objects.get(pk=self.box.pk).low_notified_at)

    def test_route_row_in_notification_map(self):
        from config.models import NotificationSettings
        from dashboard import notify_routes
        self.assertIn("packaging", notify_routes.BY_KEY)
        row = next(r for r in notify_routes.rows(NotificationSettings.get()) if r.event.key == "packaging")
        self.assertTrue(row.email_on and row.tg_on)
        self.assertTrue(row.ext_url.endswith("/shippingsettings/"))
