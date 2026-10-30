"""📌 Панель праворуч у списках (core/side_panel.py, sales/side_panel.py): права, вибір людини, блоки замовлення.

    python manage.py test core.tests_side_panel --settings=tabele.settings_test
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from core import side_panel
from core.models import UserProfile


class SidePanelTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from inventory.models import Product
        from sales.models import SalesOrder, SalesOrderLine
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)
        self.product = Product.objects.create(sku="FT220407", name="Filter")
        self.order = SalesOrder.objects.create(source="manual", order_number="P-1", client="Kunde A GmbH",
                                               email="a@kunde.example", total_price=Decimal("486"), currency="EUR")
        SalesOrderLine.objects.create(order=self.order, product=self.product, sku_raw="FT220407", qty=Decimal("10"))
        self.panel = side_panel.get("sales.salesorder")

    def test_list_page_gets_config_and_scripts(self):
        html = self.client.get("/admin/sales/salesorder/").content.decode()
        self.assertIn('id="mv-panel-config"', html)
        self.assertIn("/static/admin/js/mv_panel.js", html)
        cfg = json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(cfg["url"], "/core/panel/sales.salesorder/")
        self.assertEqual(cfg["prefs"]["blocks"], ["summary", "lines", "shipping", "documents", "mail"])
        self.assertNotIn("mv-panel-config", self.client.get("/admin/inventory/product/").content.decode())

    def test_panel_html_with_stock_and_actions(self):
        r = self.client.get(f"/core/panel/sales.salesorder/{self.order.pk}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("№ P-1", html)
        self.assertIn('data-block="lines"', html)
        self.assertIn("FT220407", html)
        self.assertIn("бракує 10", html)                               # на складі нічого — бракує всієї кількості
        self.assertIn("📋 Відкрити картку", html)
        self.assertIn(f"/labels/order/{self.order.pk}/", html)
        self.assertNotIn('data-block="notes"', html)                    # не за замовчуванням

    def test_prefs_saved_cleaned_and_applied(self):
        r = self.client.post("/core/panel/sales.salesorder/prefs/", json.dumps(
            {"blocks": ["notes", "lines", "nope", "lines"], "actions": ["crm", "evil"], "width": 480, "on": True}),
            content_type="application/json")
        self.assertEqual(r.json()["prefs"], {"on": True, "blocks": ["notes", "lines"], "actions": ["crm"], "width": 480})
        p = UserProfile.objects.get(user=self.root)
        self.assertEqual(p.ui_prefs["panels"]["sales.salesorder"]["width"], 480)
        html = self.client.get(f"/core/panel/sales.salesorder/{self.order.pk}/").content.decode()
        self.assertLess(html.index('data-block="notes"'), html.index('data-block="lines"'))
        self.assertNotIn('data-block="summary"', html)
        r = self.client.post("/core/panel/sales.salesorder/prefs/", json.dumps({"blocks": None, "width": 999}),
                             content_type="application/json")
        self.assertEqual(r.json()["prefs"]["blocks"], side_panel.defaults(self.panel)["blocks"])   # «як було»
        self.assertEqual(r.json()["prefs"]["width"], side_panel.DEFAULT_WIDTH)

    def test_permissions(self):
        u = User.objects.create_user("clerk", "c@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role="readonly", allowed_modules=["dashboard"])
        self.client.force_login(u)
        self.assertIn(self.client.get(f"/core/panel/sales.salesorder/{self.order.pk}/").status_code, (403, 302))
        self.assertIn(self.client.post("/core/panel/sales.salesorder/prefs/", "{}",
                                       content_type="application/json").status_code, (403, 302))
        self.client.force_login(self.root)
        self.assertEqual(self.client.get("/core/panel/nope.model/1/").status_code, 404)
        self.assertEqual(self.client.get("/core/panel/sales.salesorder/999999/").status_code, 404)

    def test_mail_block_only_own_mailboxes(self):
        from crm.models import Customer, CustomerTimeline
        from email_assistant.models import EmailAccount, EmailMessage
        other = User.objects.create_user("olga", "o@x.y", "p", is_staff=True)
        customer = Customer.objects.create(name="Kunde A", email="a@kunde.example")
        for owner, subject in ((self.root, "Lieferung P-1"), (other, "Privat: P-1")):
            acc = EmailAccount.objects.create(user=owner, email_address=f"{owner.username}@firma.example")
            msg = EmailMessage.objects.create(account=acc, subject=subject, from_email="a@kunde.example")
            CustomerTimeline.objects.create(customer=customer, event_type="email", title=subject,
                                            related_order_id=self.order.pk, email_message=msg)
        side_panel.save_prefs(self.root, self.panel, {"blocks": ["mail"]})
        html = self.client.get(f"/core/panel/sales.salesorder/{self.order.pk}/").content.decode()
        self.assertIn("Lieferung P-1", html)
        self.assertNotIn("Privat: P-1", html)                          # чужа скринька — ні

    def test_broken_block_does_not_break_panel(self):
        from unittest import mock
        with mock.patch.object(self.panel.block("shipping"), "render", side_effect=RuntimeError("boom")):
            html = self.client.get(f"/core/panel/sales.salesorder/{self.order.pk}/").content.decode()
        self.assertIn("Не вдалося показати цей блок.", html)
        self.assertIn('data-block="lines"', html)
