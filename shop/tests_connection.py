"""Перевірка зв'язку Minerva ↔ сайт магазину (shop/connection.py, кнопка в картці магазину).
python manage.py test shop --settings=tabele.settings_test — сайт імітується, мережі немає."""
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.utils import timezone

from api.models import APIKey, Webhook, WebhookDelivery
from inventory.models import Product

from . import connection
from .models import Shop, ShippingZone, ShopListing, ShopMediaSite

SCOPES = ["products:read", "orders:read", "orders:write"]


class FakeResponse:
    def __init__(self, status=200, data=None, text=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = text if text is not None else ""
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class ConnectionBase(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://shop.example.com")
        for sku in ("AN-1", "AN-2"):
            ShopListing.objects.create(shop=self.shop, product=Product.objects.create(sku=sku, name=sku))
        ShopListing.objects.create(shop=self.shop, is_visible=False,
                                   product=Product.objects.create(sku="HIDDEN", name="h"))
        ShippingZone.objects.create(shop=self.shop, name="DE", countries=["DE"], price=Decimal("6.90"))
        self.key = APIKey.objects.create(name="Сайт", scopes=SCOPES, shop=self.shop,
                                         last_used=timezone.now() - timezone.timedelta(minutes=3))
        self.calls = []

    def catalog(self, **over):
        data = {"ok": True, "enabled": True, "products": [{"sku": "AN-1"}, {"sku": "AN-2"}],
                "shipping_zones": [{"name": "DE"}], "accounts": False, "stale": False,
                "payment_methods": [{"id": "prepayment", "label": "Vorkasse"}],
                "online_payment": {"stripe": False}, "media": {},
                "updated_at": timezone.now().isoformat()}
        data.update(over)
        return data

    def site(self, response, callback=False):
        """Імітація сайту: GET catalog.php; callback — сайт під час відповіді звертається до Minerva ключем."""
        def get(url, **kw):
            self.calls.append(url)
            if callback:
                APIKey.objects.filter(pk=self.key.pk).update(last_used=timezone.now())
            if isinstance(response, Exception):
                raise response
            return response
        return mock.patch.object(connection.requests, "get", side_effect=get)

    def levels(self, res):
        return {c["title"]: c["level"] for c in res["checks"]}

    def detail(self, res, title):
        return next(c["detail"] for c in res["checks"] if c["title"] == title)


class ConnectionCheckTests(ConnectionBase):
    def test_all_good_both_directions(self):
        Webhook.objects.create(name="Сайт", url="https://shop.example.com/api/shop/webhook.php")
        with self.site(FakeResponse(data=self.catalog()), callback=True), \
                mock.patch("requests.post", return_value=FakeResponse(200, text='{"ok":true,"event":"ping"}')) as post:
            res = connection.check(self.shop)
        self.assertEqual(res["errors"], 0, res)
        self.assertEqual(res["warnings"], 0, res)
        self.assertTrue(res["summary"].startswith("✅"))
        self.assertEqual(self.calls, ["https://shop.example.com/api/shop/catalog.php"])
        lv = self.levels(res)
        for title in ("Ключ «Сайт»", "Minerva → сайт", "Сайт → Minerva", "Товари", "Доставка", "В обидва боки",
                      "Вебхук «Сайт»"):
            self.assertEqual(lv[title], "ok", title)
        self.assertIn("На сайті 2 — як у Minerva", self.detail(res, "Товари"))  # прихований товар не рахується
        # ping підписаний секретом вебхука
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Minerva-Event"], "ping")
        self.assertIn("v1=", headers["X-Minerva-Signature"])

    def test_no_key_and_no_address(self):
        self.key.delete()
        self.shop.url = ""
        self.shop.save()
        res = connection.check(self.shop)
        lv = self.levels(res)
        self.assertEqual((lv["Ключ API"], lv["Адреса сайту"]), ("error", "error"))
        self.assertIn("Ключ видає лише Minerva", self.detail(res, "Ключ API"))

    def test_missing_scopes_and_unused_key(self):
        self.shop.accounts_enabled = True
        self.shop.save()
        APIKey.objects.filter(pk=self.key.pk).update(scopes=["products:read"], last_used=None)
        with self.site(FakeResponse(data=self.catalog())):
            res = connection.check(self.shop)
        text = self.detail(res, "Ключ «Сайт»")
        self.assertEqual(self.levels(res)["Ключ «Сайт»"], "warn")
        for scope in ("orders:write", "accounts:read", "accounts:write"):
            self.assertIn(scope, text)
        self.assertIn("ще ніхто не звертався", text)
        self.assertEqual(self.levels(res)["Кабінет покупця"], "warn")

    def test_expired_key(self):
        APIKey.objects.filter(pk=self.key.pk).update(expires_at=timezone.localdate() - timezone.timedelta(days=1))
        with self.site(FakeResponse(data=self.catalog())):
            res = connection.check(self.shop)
        self.assertEqual(self.levels(res)["Ключ «Сайт»"], "error")

    def test_site_errors_explained(self):
        cases = [
            (FakeResponse(503), "private/shop-config.php"),
            (FakeResponse(404), "папку public"),
            (FakeResponse(301, headers={"Location": "https://www.shop.example.com/api/shop/catalog.php"}), "www.shop"),
            (FakeResponse(200, text="<html>"), "не як API магазину"),
            (requests.exceptions.ConnectionError("down"), "недоступний"),
            (requests.exceptions.SSLError("cert"), "сертифікат"),
        ]
        for response, hint in cases:
            with self.subTest(hint=hint), self.site(response):
                res = connection.check(self.shop)
                self.assertEqual(self.levels(res)["Minerva → сайт"], "error")
                self.assertIn(hint, self.detail(res, "Minerva → сайт"))
                self.assertTrue(res["summary"].startswith("❌"))

    def test_stale_catalog_means_site_cannot_reach_minerva(self):
        with self.site(FakeResponse(data=self.catalog(stale=True))):
            res = connection.check(self.shop)
        self.assertEqual(self.levels(res)["Сайт → Minerva"], "error")
        self.assertIn("minerva_token", self.detail(res, "Сайт → Minerva"))
        self.assertNotIn("В обидва боки", self.levels(res))

    def test_disabled_shop_and_differences(self):
        with self.site(FakeResponse(data=self.catalog(enabled=False))):
            res = connection.check(self.shop)
        self.assertEqual(self.levels(res)["Minerva → сайт"], "warn")
        with self.site(FakeResponse(data=self.catalog(products=[{"sku": "AN-1"}], shipping_zones=None))):
            res = connection.check(self.shop)
        lv = self.levels(res)
        self.assertEqual((lv["Товари"], lv["Доставка"]), ("warn", "warn"))

    def test_webhook_secret_mismatch_not_retried(self):
        hook = Webhook.objects.create(name="Сайт", url="https://shop.example.com/api/shop/webhook.php")
        Webhook.objects.create(name="Інший", url="https://other.example.org/hook")   # інший домен — не чіпаємо
        with self.site(FakeResponse(data=self.catalog())), \
                mock.patch("requests.post", return_value=FakeResponse(401, text='{"ok":false}')) as post:
            res = connection.check(self.shop)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.levels(res)["Вебхук «Сайт»"], "error")
        self.assertIn("webhook_secret", self.detail(res, "Вебхук «Сайт»"))
        d = WebhookDelivery.objects.get(webhook=hook)
        self.assertEqual((d.event, d.status), ("ping", WebhookDelivery.FAILED))

    def test_no_webhook_is_warning(self):
        with self.site(FakeResponse(data=self.catalog())):
            res = connection.check(self.shop)
        self.assertEqual(self.levels(res)["Вебхук"], "warn")
        self.assertIn("https://shop.example.com/api/shop/webhook.php", self.detail(res, "Вебхук"))

    def test_typed_address_and_reported_base_path(self):
        with self.site(FakeResponse(data=self.catalog())):
            res = connection.check(self.shop, "https://test.example.net/de/shop/")
        self.assertEqual(self.calls[-1], "https://test.example.net/api/shop/catalog.php")
        self.assertEqual(self.levels(res)["Перевіряється адреса з поля"], "info")
        # сайт у підпапці — спершу корінь, який сайт повідомив сам, потім корінь домену
        ShopMediaSite.objects.create(shop=self.shop, reported_at=timezone.now(), have=["a" * 64],
                                     sync_url="https://shop.example.com/neu/api/shop/media_sync.php")
        self.assertEqual(connection.site_bases(self.shop),
                         ["https://shop.example.com/neu/", "https://shop.example.com/"])
        self.assertEqual(connection.site_bases(self.shop, "https://test.example.net/x/"), ["https://test.example.net/"])
        for bad in ("ftp://shop.example.com", "https://user:pw@shop.example.com", "shop.example.com", ""):
            self.assertEqual(connection.site_bases(self.shop, bad), [], bad)
        with self.site(FakeResponse(data=self.catalog())):
            res = connection.check(self.shop)
        self.assertEqual((res["url"], self.calls[-1]), ("https://shop.example.com/neu/",
                                                        "https://shop.example.com/neu/api/shop/catalog.php"))
        self.assertIn("копій на сайті 1", self.detail(res, "Фото й даташити"))

    def test_moved_site_found_at_domain_root(self):
        """Сайт повідомив /neu/, а тепер домен вказує прямо на public — 404 там, каталог у корені."""
        ShopMediaSite.objects.create(shop=self.shop, sync_url="https://shop.example.com/neu/api/shop/media_sync.php")
        answers = {"https://shop.example.com/neu/api/shop/catalog.php": FakeResponse(404),
                   "https://shop.example.com/api/shop/catalog.php": FakeResponse(data=self.catalog())}
        with mock.patch.object(connection.requests, "get", side_effect=lambda url, **kw: answers[url]):
            res = connection.check(self.shop)
        self.assertEqual(res["url"], "https://shop.example.com/")
        self.assertEqual(self.levels(res)["Minerva → сайт"], "ok")
        self.assertNotIn("missing", res["checks"][1])


class ConnectionAdminTests(ConnectionBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.url = f"/admin/shop/shop/{self.shop.pk}/connection-check/"

    def test_button_and_json(self):
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, 'id="shop-conn-run"')
        self.assertContains(page, self.url)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        with self.site(FakeResponse(data=self.catalog())):
            data = self.client.post(self.url, {"url": "https://test.example.net"}).json()
        self.assertEqual(data["url"], "https://test.example.net/")
        self.assertIn("checks", data)

    def test_view_only_user_cannot_run(self):
        user = User.objects.create_user("viewer", "v@example.com", "p", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_shop"))
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, {"url": ""}).status_code, 403)
