"""Публікація товарів у магазині (shop/publish.py), групи меню «Дія» (core/admin_actions.py).

python manage.py test shop.tests_publish --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from core import side_panel as sp
from core import panel_edit
from core.admin_actions import flat_choices
from inventory.models import Product
from shop import lots, publish
from shop.admin_lots import _examples
from shop.models import Shop, ShopListing, ShopMediaSettings


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.root = User.objects.create_superuser("a", "a@x.y", "p")
        self.client.force_login(self.root)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://shop.example.com")
        self.based = Product.objects.create(sku="RES-1", name="Widerstand", sale_price=Decimal("1.00"),
                                            base_prices=[{"min_qty": 1, "unit_price": "1.00"},
                                                         {"min_qty": 1000, "unit_price": "0.24"}])
        self.plain = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))


class PublishTests(_Base):
    def test_publish_adds_shows_hidden_and_explains(self):
        hidden = ShopListing.objects.create(shop=self.shop, product=self.plain, is_visible=False)
        res = publish.publish(self.shop, Product.objects.filter(pk__in=[self.based.pk, self.plain.pk]))
        self.assertEqual((res["added"], res["existed"], res["shown"]), (1, 1, 1))
        listing = ShopListing.objects.get(shop=self.shop, product=self.based)
        self.assertEqual((listing.price_source, listing.is_visible), (ShopListing.PRICE_BASE, True))
        hidden.refresh_from_db()
        self.assertTrue(hidden.is_visible)
        self.assertEqual(res["base_missing"], ["AN-1"])              # ціна — «Ціна продажу», не базова
        self.assertIn("вимкнено", res["media"])                     # «🖼 Фото й даташити» за замовчуванням вимкнено
        self.assertIn("додано 1", publish.message(self.shop, res))

    def test_push_starts_media_for_these_products(self):
        ShopMediaSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
        with mock.patch("shop.media.start", return_value=True) as start:
            note = publish.push(self.shop, [self.based.pk], by="a")
        start.assert_called_once_with(mode="changed", product_ids=[self.based.pk], by="a", shop_id=self.shop.pk)
        self.assertIn("сайт забере", note)
        with mock.patch("shop.media.start", return_value=False), mock.patch("shop.media.ping_sites") as ping:
            publish.push(self.shop, [self.based.pk])
        ping.assert_called_once_with(only=self.shop)
        Shop.objects.filter(pk=self.shop.pk).update(url="")
        self.shop.refresh_from_db()
        self.assertIn("немає адреси сайту", publish.push(self.shop, [self.based.pk]))


class ProductActionTests(_Base):
    def test_actions_menu_is_grouped(self):
        page = self.client.get("/admin/inventory/product/").content.decode()
        for title in ("📦 Товар", "🏪 Інтернет-магазин", "💰 Ціни", "🖼 Фото й даташити", "🛒 DigiKey", "🤖 Агент"):
            self.assertIn(f'<optgroup label="{title}">', page)
        self.assertIn("Опублікувати в інтернет-магазині", page)

    def test_panel_gets_flat_actions(self):
        ma = admin.site._registry[Product]
        request = RequestFactory().get("/admin/inventory/product/")
        request.user = self.root
        names = [a["name"] for a in panel_edit.admin_actions(sp.get(Product), ma, request)]
        self.assertIn("action_publish_shop", names)
        self.assertNotIn("🛒 DigiKey", names)
        self.assertEqual(flat_choices([("", "---"), ("G", [("a", "A"), ("b", "B")])]), [("", "---"), ("a", "A"), ("b", "B")])

    def test_publish_action(self):
        url = "/admin/inventory/product/"
        r = self.client.post(url, {"action": "action_publish_shop", "_selected_action": [self.based.pk]})
        self.assertContains(r, "Опублікувати в інтернет-магазині")
        r = self.client.post(url, {"action": "action_publish_shop", "_selected_action": [self.based.pk], "apply": "1",
                                   "shop": self.shop.pk, "visible": "on", "price_source": "base"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ShopListing.objects.get(shop=self.shop, product=self.based).price_source, ShopListing.PRICE_BASE)

    def test_publish_needs_shop_rights(self):
        viewer = User.objects.create_user("v", "v@x.y", "p", is_staff=True)
        viewer.profile.role = "warehouse"
        viewer.profile.save()
        ma = admin.site._registry[Product]
        request = RequestFactory().get("/admin/inventory/product/")
        request.user = User.objects.get(pk=viewer.pk)
        self.assertNotIn("action_publish_shop", [n for n, _ in flat_choices(ma.get_action_choices(request))])

    def test_card_add_pushes_photos(self):
        from inventory.admin_prices import apply_shop_choices
        request = RequestFactory().post("/x/", {"shop_apply": [str(self.shop.pk)], f"shop_visible_{self.shop.pk}": "on",
                                                f"shop_factor_{self.shop.pk}": "100"})
        request.user = self.root
        with mock.patch("shop.publish.push", return_value="перевіряються") as push, \
                mock.patch("inventory.admin_prices.messages") as msgs:
            apply_shop_choices(request, self.based)
        push.assert_called_once_with(self.shop, [self.based.pk], by="a")
        self.assertIn("Фото: перевіряються", msgs.add_message.call_args[0][2])


class LotTextTests(TestCase):
    def test_min_without_step(self):
        self.assertEqual(_examples(lots.make(1000, 1, [], lots.SOURCE_PRODUCT)), "1 000 шт. або більше")
        self.assertTrue(_examples(lots.make(1000, 1000, [], lots.SOURCE_PRODUCT)).startswith("1 000, 2 000"))
