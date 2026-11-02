"""Захист і приватність (пакет H): ключі «…secret_key» у перегляді картки, документи клієнтів у /media/,
«Set stock…» товару й ⭐ клієнта — лише з правом змінювати.

python manage.py test core.tests_hardening --settings=tabele.settings_test
"""
import os
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from core.secrets import mask
from tabele.admin import SECRET_NAME_RE

# фальшиві ключі складаються під час тесту — у файлі немає рядка, схожого на справжній ключ (GitHub push protection)
STRIPE_KEY = "sk_" + "test_51" + "SECRET" * 3 + "9876"
WEBHOOK_SECRET = "wh" + "sec_" + "ABCDEFGHIJKLMNOP1234"
JLC_KEY = "JLC-SECRET-KEY-0123456789ABCDEF"
MANAGER_MODULES = ["crm", "strategy", "sales", "shipping", "dashboard", "tasks", "email_assistant", "projects",
                   "offers", "calendar_app", "worktime", "faq", "labels_app", "documents"]


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})


class SecretKeyMaskTests(_Base):
    """Назви, що закінчуються на secret_key (Shop.stripe_secret_key, JLCConfig.secret_key), — замасковано."""

    def test_names(self):
        for name in ("stripe_secret_key", "secret_key", "SECRET_KEY", "stripe_webhook_secret", "assistant_key",
                     "api_key", "smtp_password"):
            self.assertTrue(SECRET_NAME_RE.search(name), name)
        for name in ("name", "secret_key_hint", "key_mode", "public_name"):
            self.assertFalse(SECRET_NAME_RE.search(name), name)

    def test_shop_viewer_sees_stripe_key_masked(self):
        from shop.models import Shop
        shop = Shop.objects.create(name="Muster Shop", slug="mustershop", stripe_secret_key=STRIPE_KEY,
                                   stripe_webhook_secret=WEBHOOK_SECRET)
        viewer = person("vera", "readonly", allowed_modules=["shop"])   # модуль є, операцій немає — лише перегляд
        self.client.force_login(viewer)
        r = self.client.get(f"/admin/shop/shop/{shop.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, STRIPE_KEY)
        self.assertNotContains(r, WEBHOOK_SECRET)
        self.assertContains(r, mask(STRIPE_KEY))                       # sk_test_51••••••••9876 — видно, що ключ є
        self.assertNotContains(r, 'name="stripe_secret_key"')          # поля для введення немає — лише перегляд

    def test_jlcpcb_viewer_sees_secret_key_masked(self):
        from jlcpcb.models import JLCConfig
        JLCConfig.objects.update_or_create(pk=1, defaults={"secret_key": JLC_KEY})
        viewer = person("vera", "readonly", allowed_modules=["jlcpcb"])
        self.client.force_login(viewer)
        r = self.client.get("/admin/jlcpcb/jlcconfig/1/change/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, JLC_KEY)
        self.assertContains(r, mask(JLC_KEY))


class CustomerMediaTests(_Base):
    """/media/customers/<pk>/ — документи клієнта (картка CRM «📎 Документи») лише з доступом до розділу CRM."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        over = override_settings(MEDIA_ROOT=self.root)
        over.enable()
        self.addCleanup(over.disable)
        customer = Customer.objects.create(name="Muster GmbH", email="info@muster.example")
        folder = os.path.join(self.root, "customers", str(customer.pk))
        os.makedirs(folder)
        with open(os.path.join(folder, "a.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4")
        self.url = f"/media/customers/{customer.pk}/a.pdf"

    def test_only_with_crm_module(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)                           # без входу — на сторінку входу
        self.assertIn("/login", r["Location"])
        self.client.force_login(person("kim", "warehouse"))           # складник: розділу CRM немає
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(person("olga"))                       # менеджер: CRM є
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")


class SetStockTests(_Base):
    """«Set stock…» (/admin/inventory/product/<pk>/set-stock/) пише коригування — лише з правом змінювати товари."""

    def setUp(self):
        super().setUp()
        from inventory.models import Location, Product
        Location.objects.create(code="MAIN")
        self.product = Product.objects.create(sku="FT220407", name="Filter FT220407")
        self.url = f"/admin/inventory/product/{self.product.pk}/set-stock/"

    def _moves(self):
        from inventory.models import InventoryTransaction
        return list(InventoryTransaction.objects.filter(product=self.product).order_by("pk")
                    .values_list("tx_type", "qty"))

    def test_view_only_roles_refused(self):
        for viewer in (person("anna", "accountant"),                   # склад — перегляд і експорт
                       person("vera", "readonly", allowed_modules=["inventory"])):
            self.client.force_login(viewer)
            self.assertEqual(self.client.get(self.url).status_code, 403, viewer.username)
            r = self.client.post(self.url, {"target_stock": "25"})
            self.assertEqual(r.status_code, 403, viewer.username)
        self.assertEqual(self._moves(), [])

    def test_editors_still_adjust(self):
        from inventory.models import InventoryTransaction
        kim = person("kim", "warehouse")                              # склад — view / add / change
        self.client.force_login(kim)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        r = self.client.post(self.url, {"target_stock": "5"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], f"/admin/inventory/product/{self.product.pk}/change/")
        olga = person("olga", "manager", allowed_modules=MANAGER_MODULES + ["inventory"],
                      module_operations={"inventory": ["view", "add", "change"]})
        self.client.force_login(olga)                                  # менеджер, якому дали зміни на складі
        r = self.client.post(self.url, {"target_stock": "3"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._moves(), [(InventoryTransaction.TxType.ADJUSTMENT, Decimal("5")),
                                         (InventoryTransaction.TxType.ADJUSTMENT, Decimal("-2"))])


class FavoriteTests(_Base):
    """⭐ клієнта (/admin/crm/customer/<pk>/favorite/) — лише POST і лише з правом змінювати клієнтів."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        self.customer = Customer.objects.create(name="Muster GmbH", email="info@muster.example")
        self.url = f"/admin/crm/customer/{self.customer.pk}/favorite/"

    def _favorite(self):
        self.customer.refresh_from_db()
        return self.customer.is_favorite

    def test_get_not_allowed(self):
        self.client.force_login(person("olga"))
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 405)
        self.assertEqual(r["Allow"], "POST")
        self.assertIn("error", r.json())
        self.assertFalse(self._favorite())

    def test_view_only_refused(self):
        for viewer in (person("ai", "ai"),                             # роль AI: CRM лише перегляд
                       person("vera", "readonly", allowed_modules=["crm"])):
            self.client.force_login(viewer)
            r = self.client.post(self.url)
            self.assertEqual(r.status_code, 403, viewer.username)
            self.assertIn("error", r.json())
        self.assertFalse(self._favorite())

    def test_manager_toggles(self):
        self.client.force_login(person("olga"))
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"is_favorite": True, "pk": self.customer.pk})
        self.assertTrue(self._favorite())
        self.assertEqual(self.client.post(self.url).json()["is_favorite"], False)
        self.assertFalse(self._favorite())
        self.assertEqual(self.client.post(f"/admin/crm/customer/{self.customer.pk + 999}/favorite/").status_code,
                         404)
