"""Оплата й рахунок замовлення (sales/payment_info.py): список, картка, лист «Нове замовлення», фільтри.

python manage.py test sales.tests_payment_info --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from dashboard import notifications
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement
from sales import payment_info
from sales.models import SalesOrder
from shop.models import Shop, ShopListing, ShopOrder

BILLING = {"company": "Fogra e.V.", "name": "Anna Muster", "street": "Streitfeldstr. 19", "zip": "81673",
           "city": "München", "country": "DE", "vat_id": "DE129514004", "email": "rechnung@fogra.example",
           "phone": "+49 89 1"}
NOTE = "Webshop | Zahlung: Vorkasse (Überweisung) | USt 19% | Brutto: 177.31 EUR"


class _Base(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="sevskiy.de", slug="sevskiy_de", is_default=True)
        self.order = SalesOrder.objects.create(
            source="sevskiy_de", order_number="PO-31730-001", client="Fogra e.V.", contact_name="Anna Muster",
            email="einkauf@fogra.example", addr_street="Lager 1", addr_zip="63741", addr_city="Aschaffenburg",
            addr_country="DE", payment_method="prepayment", payment_status="unpaid", internal_note=NOTE,
            buyer_vat_id="DE129514004")
        ShopOrder.objects.create(order=self.order, shop=self.shop, billing=BILLING, vat_rate=Decimal("19.00"))
        self.order.refresh_from_db()


class PaymentInfoTests(_Base):
    def test_payment_and_invoice(self):
        pay = payment_info.payment(self.order)
        self.assertEqual((pay["text"], pay["tone"]), ("Передоплата (Vorkasse) · Не оплачено", "warn"))
        inv = payment_info.invoice(self.order)
        self.assertEqual(inv["lines"][:3], ["Fogra e.V.", "Anna Muster", "Streitfeldstr. 19"])
        self.assertTrue(inv["separate"])                        # доставка — на іншу адресу
        self.assertEqual((inv["vat_id"], inv["email"]), ("DE129514004", "rechnung@fogra.example"))
        self.assertEqual(payment_info.site_note(self.order), NOTE)

    def test_without_site_data(self):
        o = SalesOrder.objects.create(source="digikey", order_number="DK-1", client="X")
        self.assertIsNone(payment_info.payment(o))
        self.assertFalse(payment_info.invoice(o)["from_site"])
        self.assertEqual(payment_info.site_note(o), "")


class NotificationTests(_Base):
    def test_new_order_mail_and_telegram(self):
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_enabled, ns.new_order_email = True, True
        ns.telegram_enabled, ns.new_order_telegram = True, True
        ns.save()
        with mock.patch.object(notifications, "_send_event_email") as mail, \
                mock.patch.object(notifications, "_send_telegram") as tg:
            notifications.notify_new_order(self.order)
        html = mail.call_args.args[2]
        for text in ("💳 Оплата:", "Передоплата (Vorkasse) · Не оплачено", "🧾 Рахунок на:", "Streitfeldstr. 19",
                     "USt-IdNr: DE129514004", "ПДВ 19 %", "Доставка на іншу адресу", "📝 Від сайту:"):
            self.assertIn(text, html)
        self.assertIn("💳 <b>Передоплата (Vorkasse) · Не оплачено</b>", tg.call_args.args[1])


class ApiOrderTests(TestCase):
    """Замовлення з сайту: лист «Нове замовлення» вже бачить платіжну адресу (ShopOrder у тій самій транзакції)."""

    def test_shop_order_has_billing_when_mail_goes(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        shop = Shop.objects.create(name="sevskiy.de", slug="sevskiy_de", is_default=True)
        p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("71.05"))
        create_movement(product=p, tx_type="Incoming", qty=10)
        ShopListing.objects.create(shop=shop, product=p)
        key = APIKey.objects.create(name="site", shop=shop, scopes=["orders:write", "orders:read"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        seen = []
        with mock.patch("dashboard.notifications.notify_new_order",
                        side_effect=lambda o: seen.append(ShopOrder.objects.filter(order=o).values_list(
                            "billing", flat=True).first())), self.captureOnCommitCallbacks(execute=True):
            r = c.post("/api/v1/orders/", {"order_number": "WS-1", "client": "Fogra e.V.", "email": "a@b.de",
                                           "shop": True, "payment_method": "prepayment", "billing": BILLING,
                                           "lines": [{"sku": "AN-1", "qty": 2}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(seen and seen[0].get("street"), "Streitfeldstr. 19")


class AdminTests(_Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        SalesOrder.objects.create(source="digikey", order_number="DK-1", client="Other", payment_status="paid")

    def test_list_shows_payment_and_filters(self):
        page = self.client.get("/admin/sales/salesorder/").content.decode()
        self.assertIn("Передоплата (Vorkasse) · Не оплачено", page)
        shop_only = self.client.get("/admin/sales/salesorder/?from_shop=1").content.decode()
        self.assertIn("PO-31730-001", shop_only)
        self.assertNotIn("DK-1", shop_only)
        unpaid = self.client.get("/admin/sales/salesorder/?pay=open").content.decode()
        self.assertIn("PO-31730-001", unpaid)
        self.assertNotIn("DK-1", unpaid)

    def test_card_shows_payment_and_invoice(self):
        page = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/").content.decode()
        self.assertIn("💳 Оплата й рахунок", page)
        self.assertIn("Streitfeldstr. 19", page)
        self.assertIn("rechnung@fogra.example", page)
