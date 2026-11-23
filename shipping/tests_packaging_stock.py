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

    def test_archived_box_keeps_what_was_used_and_is_not_used_again(self):
        s = self.shipment()
        self.set_status(s, Shipment.Status.LABEL_READY)
        self.assertEqual(self.stock(), 4)
        PackagingMaterial.objects.filter(pk=self.box.pk).update(is_active=False)     # 🗄 в архів
        self.set_status(s, Shipment.Status.IN_TRANSIT)          # посилка поїхала далі — коробку не «повертаємо»
        self.assertEqual(self.stock(), 4)
        self.set_status(self.shipment(), Shipment.Status.LABEL_READY)                 # нова посилка — не списує
        self.assertEqual(self.stock(), 4)
        self.set_status(s, Shipment.Status.CANCELLED)           # скасували — повертається, хоч і в архіві
        self.assertEqual(self.stock(), 5)

    def test_archived_same_size_box_stays_with_its_shipment(self):
        s = self.shipment()
        self.set_status(s, Shipment.Status.LABEL_READY)
        PackagingMaterial.objects.filter(pk=self.box.pk).update(is_active=False)
        new = PackagingMaterial.objects.create(length_cm=20, width_cm=15, height_cm=5, track_stock=True)
        PackagingMaterial.objects.filter(pk=new.pk).update(stock_qty=10)
        self.set_status(s, Shipment.Status.DELIVERED)
        self.assertEqual((self.stock(), self.stock(new)), (4, 10))
        self.set_status(self.shipment(), Shipment.Status.LABEL_READY)                 # нова — уже нова коробка
        self.assertEqual((self.stock(), self.stock(new)), (4, 9))


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

    def test_archived_box_is_silent(self):
        PackagingMaterial.objects.filter(pk=self.box.pk).update(is_active=False)
        with mock.patch(NOTIFY) as notify, self.captureOnCommitCallbacks(execute=True):
            packaging_stock.count(self.box, 0)
        notify.assert_not_called()
        self.assertEqual(packaging_stock.low_materials(), [])

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


class ReportTests(Base):
    def setUp(self):
        super().setUp()
        self.none = PackagingMaterial.objects.create(name="Gone", length_cm=10, width_cm=10, height_cm=10,
                                                     track_stock=True, min_stock=1, reorder_qty=30)
        self.few = PackagingMaterial.objects.create(name="Few", length_cm=30, width_cm=20, height_cm=10,
                                                    track_stock=True, min_stock=3, buy_url="https://shop.example.com/b")
        PackagingMaterial.objects.filter(pk=self.few.pk).update(stock_qty=2)
        self.free = PackagingMaterial.objects.create(name="Loose", length_cm=40, width_cm=30, height_cm=20)
        self.old = PackagingMaterial.objects.create(name="Old", length_cm=50, width_cm=40, height_cm=30,
                                                    track_stock=True, is_active=False)

    def test_states_and_order(self):
        rep = packaging_stock.report()
        self.assertEqual([(str(r["material"]), r["state"]) for r in rep["rows"]],
                         [("Gone", "missing"), ("Few", "critical"), (str(self.box), "ok")])
        self.assertEqual(rep["counts"], {"missing": 1, "critical": 1, "ok": 1, "untracked": 0})
        rep = packaging_stock.report(untracked=True)
        self.assertEqual(rep["rows"][-1]["state"], "untracked")
        rep = packaging_stock.report(ids=[self.old.pk, self.few.pk])           # вибрані — і з архіву
        self.assertEqual({str(r["material"]) for r in rep["rows"]}, {"Old", "Few"})

    def test_mail_and_telegram_text(self):
        from config.models import NotificationSettings
        from dashboard import notifications
        ns = NotificationSettings.get()
        ns.email_host, ns.telegram_bot_token, ns.telegram_chat_id = "smtp.example.com", "1:x", "111"
        ns.save()
        seen = {}

        def mail(ns_, subject, html):
            seen["to"], seen["subject"], seen["html"] = notifications.recipients(ns_), subject, html

        def tg(ns_, text, chat_id_override=None):
            seen["text"] = text

        with mock.patch.object(notifications, "_send_event_email", side_effect=mail), \
                mock.patch.object(notifications, "_send_telegram", side_effect=tg):
            sent = packaging_stock.send_report(packaging_stock.report(), ["boss@example.com"], True)
        self.assertEqual(sent, ["лист", "Telegram"])
        self.assertEqual(seen["to"], ["boss@example.com"])
        self.assertIn("⛔ немає 1 · ⚠️ мало 1", seen["subject"])
        for part in ("Gone", "⛔ Немає", "#ffebee", "купити ≈ 30 шт", "⚠️ Мало", "https://shop.example.com/b",
                     "✓ Є: 1"):
            self.assertIn(part, seen["html"])
        self.assertNotIn("Old", seen["html"])
        self.assertIn("⛔", seen["text"])
        self.assertIn("купити ≈ 30 шт", seen["text"])
        with mock.patch.object(notifications, "_send_event_email") as m:      # без «лише цим» — нікому не шле сам
            packaging_stock.send_report(packaging_stock.report(), [], False)
        m.assert_not_called()


class DefaultMinTests(Base):
    """«Мінімальний запас за замовчуванням» (Налаштування доставки) — для коробок без свого мінімуму."""

    def setUp(self):
        super().setUp()
        self.bare = PackagingMaterial.objects.create(name="Mietel", length_cm=30, width_cm=20, height_cm=10,
                                                     track_stock=True)
        PackagingMaterial.objects.filter(pk=self.bare.pk).update(stock_qty=1)

    def test_default_minimum_marks_low(self):
        rep = packaging_stock.report()
        self.assertEqual({str(r["material"]): r["state"] for r in rep["rows"]}["Mietel"], "critical")
        self.assertIn(self.bare.pk, [m.pk for m in packaging_stock.low_materials()])
        s = ShippingSettings.get()
        s.packaging_default_min = None
        s.save()
        rep = packaging_stock.report()
        self.assertEqual({str(r["material"]): r["state"] for r in rep["rows"]}["Mietel"], "ok")

    def test_own_minimum_wins(self):
        PackagingMaterial.objects.filter(pk=self.bare.pk).update(min_stock=0)
        self.assertNotIn(self.bare.pk, [m.pk for m in packaging_stock.low_materials()])

    def test_changing_default_alerts_once(self):
        with mock.patch(NOTIFY, return_value=["лист"]) as notify:
            self.assertEqual([m.pk for m in packaging_stock.check_all_low()], [self.bare.pk])
            self.assertEqual(packaging_stock.check_all_low(), [])         # уже попереджали
        self.assertEqual(notify.call_count, 1)

    def test_report_mail_shows_general_minimum(self):
        from dashboard.notifications import packaging_report_message
        items = {i["name"]: i for i in packaging_report_message(packaging_stock.report())["items"]}
        self.assertEqual(items["Mietel"]["min"], "3 шт (загальний)")
        self.assertEqual(items["Mietel"]["state"], "critical")


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

    def test_archive_and_restore_actions(self):
        unused = PackagingMaterial.objects.create(name="castom", length_cm=20, width_cm=11, height_cm=7)
        url = "/admin/shipping/packagingmaterial/"
        self.assertContains(self.client.get(url), "castom")
        r = self.client.post(url, {"action": "archive_selected", "_selected_action": [unused.pk]}, follow=True)
        self.assertContains(r, "🗄 В архіві: castom")
        self.assertFalse(PackagingMaterial.objects.get(pk=unused.pk).is_active)
        page = self.client.get(url)                                       # за замовчуванням — лише ті, що в роботі
        self.assertNotContains(page, ">castom<")
        self.assertContains(page, "🗄 В архіві · 1")
        self.assertContains(self.client.get(url + "?archive=yes"), "castom")
        self.assertContains(self.client.get(url + "?archive=all"), "castom")
        with mock.patch(NOTIFY):
            self.client.post(url + "?archive=yes", {"action": "restore_selected", "_selected_action": [unused.pk]})
        self.assertTrue(PackagingMaterial.objects.get(pk=unused.pk).is_active)

    def test_stock_report_page_and_send(self):
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_host, ns.email_to = "smtp.example.com", "team@example.com"
        ns.routes = {"packaging": {"to": ["boxes@example.com"]}}
        ns.save()
        PackagingMaterial.objects.filter(pk=self.box.pk).update(stock_qty=0)
        url = "/admin/shipping/packagingmaterial/stock-report/"
        self.assertContains(self.client.get("/admin/shipping/packagingmaterial/"), url)
        page = self.client.get(url)
        self.assertContains(page, "⛔ Немає")
        self.assertContains(page, "boxes@example.com")                         # адреси виду «Мало коробок»
        with mock.patch("dashboard.notifications._send_event_email") as mail:
            r = self.client.post(url, {"to": "bad-address"})
            self.assertContains(r, "Неправильні адреси")
            mail.assert_not_called()
            r = self.client.post(url, {"to": "boss@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(mail.call_count, 1)
        self.assertIn("⛔ немає 1", mail.call_args.args[1])

    def test_bulk_settings_action(self):
        other = PackagingMaterial.objects.create(name="Other", length_cm=40, width_cm=30, height_cm=20)
        url = "/admin/shipping/packagingmaterial/"
        ids = [self.box.pk, other.pk]
        page = self.client.post(url, {"action": "bulk_settings_selected", "index": 0, "_selected_action": ids})
        self.assertContains(page, "Застосувати до 2 коробок")
        self.assertEqual(PackagingMaterial.objects.get(pk=other.pk).min_stock, None)          # ще нічого
        with mock.patch(NOTIFY):
            r = self.client.post(url, {"action": "bulk_settings_selected", "_selected_action": ids, "apply": "1",
                                       "min_stock": "4", "reorder_qty": "25", "track": "on", "buy_url": ""})
        self.assertEqual(r.status_code, 302)
        for m in PackagingMaterial.objects.filter(pk__in=ids):
            self.assertEqual((m.min_stock, m.reorder_qty, m.track_stock), (4, 25, True))
        self.assertIsNotNone(PackagingMaterial.objects.get(pk=other.pk).stock_since)
        with mock.patch(NOTIFY):
            self.client.post(url, {"action": "bulk_settings_selected", "_selected_action": [other.pk], "apply": "1",
                                   "min_clear": "on"})
        self.assertIsNone(PackagingMaterial.objects.get(pk=other.pk).min_stock)
        self.assertEqual(PackagingMaterial.objects.get(pk=self.box.pk).min_stock, 4)          # не вибрана
        r = self.client.post(url, {"action": "bulk_settings_selected", "_selected_action": [other.pk], "apply": "1",
                                   "min_stock": "2", "min_clear": "on"})
        self.assertContains(r, "Або вписати значення, або прибрати")

    def test_min_stock_in_action_menu_not_in_list(self):
        page = self.client.get("/admin/shipping/packagingmaterial/")
        self.assertNotContains(page, 'name="form-0-min_stock"')
        self.assertContains(page, "bulk_settings_selected")
        card = self.client.get(f"/admin/shipping/packagingmaterial/{self.box.pk}/change/")
        self.assertContains(card, 'name="min_stock"')
        self.assertContains(card, "3 (загальний)")

    def test_card_saves_several_fold_heights(self):
        r = self.post_box(length_cm="30", width_cm="30", height_cm="30", fold_heights="30, 16, 22, 27")
        self.assertEqual(r.status_code, 302)
        m = PackagingMaterial.objects.get(pk=self.box.pk)
        self.assertEqual(m.fold_heights, "27, 22, 16")                  # повна висота відкинута, від вищої
        card = self.client.get(f"/admin/shipping/packagingmaterial/{self.box.pk}/change/")
        self.assertContains(card, "Ще висота")
        r = self.post_box(length_cm="30", width_cm="30", height_cm="30", fold_heights="35")
        self.assertContains(r, "має бути меншою")

    def test_stock_report_action_keeps_selection(self):
        r = self.client.post("/admin/shipping/packagingmaterial/",
                             {"action": "stock_report_selected", "_selected_action": [self.box.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"stock-report/?ids={self.box.pk}", r["Location"])

    def test_viewer_has_no_archive_action(self):
        viewer = User.objects.create_user("viewer", password="x", is_staff=True)
        viewer.profile.role = "readonly"                                 # склад / доставка — лише перегляд
        viewer.profile.allowed_modules = ["shipping"]
        viewer.profile.save()
        self.client.force_login(User.objects.get(pk=viewer.pk))
        self.assertNotContains(self.client.get("/admin/shipping/packagingmaterial/"), "archive_selected")
        r = self.client.post("/admin/shipping/packagingmaterial/",
                             {"action": "archive_selected", "_selected_action": [self.box.pk]})
        self.assertTrue(PackagingMaterial.objects.get(pk=self.box.pk).is_active)
        self.assertIn(r.status_code, (200, 302, 403))
        url = "/admin/shipping/packagingmaterial/stock-report/"
        self.assertNotContains(self.client.get(url), "Надіслати звіт</button>")   # дивитись — так, надсилати — ні
        with mock.patch("dashboard.notifications._send_event_email") as mail:
            self.assertEqual(self.client.post(url, {"to": "x@example.com"}).status_code, 403)
        mail.assert_not_called()

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
