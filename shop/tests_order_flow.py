"""Блок «🚦 Шлях замовлення» картки магазину (shop/order_flow.py):
python manage.py test shop.tests_order_flow --settings=tabele.settings_test"""
from django.contrib.auth.models import User
from django.test import TestCase

from config.models import NotificationSettings
from inventory.models import InventorySettings

from . import order_flow
from .models import Shop


def by_status(shop):
    return {r["status"]: r for r in order_flow.steps(shop)}


def text(row):
    return " | ".join([t for _, t in row["who"]] + row["what"])


class OrderFlowTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Sevskiy", slug="webshop", is_default=True, url="https://web.example.com",
                                        payment_methods=["prepayment", "invoice", "stripe"])

    def test_all_statuses_in_order(self):
        self.assertEqual([r["status"] for r in order_flow.steps(self.shop)],
                         ["🆕 Отримано", "💳 Оплачено", "⚙️ В обробці", "🚚 Відправлено", "✅ Доставлено", "✖ Скасовано"])

    def test_paid_row_follows_settings(self):
        paid = text(by_status(self.shop)["💳 Оплачено"])
        self.assertIn("Stripe — сам", paid)
        self.assertIn("Vorkasse, рахунок", paid)
        self.assertIn("«В обробці»", paid)
        self.shop.paid_order_status = ""
        self.shop.payment_methods = ["prepayment"]
        paid = text(by_status(self.shop)["💳 Оплачено"])
        self.assertNotIn("Stripe", paid)
        self.assertIn("лишається «Отримано»", paid)
        self.assertNotIn("Minerva — після оплати", text(by_status(self.shop)["⚙️ В обробці"]))

    def test_stock_mode(self):
        s = InventorySettings.get()
        s.deduct_on, s.use_reservation = "shipped", False
        s.save()
        rows = by_status(self.shop)
        self.assertIn("списання — при «Відправлено»", text(rows["🆕 Отримано"]))
        self.assertIn("списується", text(rows["🚚 Відправлено"]))
        self.assertIn("повертається", text(rows["✖ Скасовано"]))
        self.shop.is_test, self.shop.test_affects_stock = True, False
        self.assertIn("не рухається", text(by_status(self.shop)["🆕 Отримано"]))

    def test_ship_mail(self):
        ns = NotificationSettings.get()
        ns.email_enabled, ns.customer_notify_enabled, ns.customer_notify_auto = True, True, True
        ns.save()
        self.assertIn("сам після оформлення UPS", text(by_status(self.shop)["🚚 Відправлено"]))
        ns.customer_notify_enabled = False
        ns.save()
        self.assertIn("вимкнено", text(by_status(self.shop)["🚚 Відправлено"]))

    def test_shop_card_shows_block(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        user =User.objects.create_superuser("admin", "admin@example.com", "x")
        self.client.force_login(user)
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("🚦 Шлях замовлення", html)
        self.assertIn('data-nt-go="paid_order_status"', html)
        self.assertIn("Bestellbestätigung", html)
        self.assertNotIn("&gt;label", html)   # стилі не екрановані
