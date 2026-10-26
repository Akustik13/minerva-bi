"""Фото й даташити з Minerva для сайту: перевірка вмісту, порядок джерел, копії, API, сторінка адмінки.
python manage.py test shop --settings=tabele.settings_test"""
import shutil
import tempfile
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import Product
from inventory.services import media_files
from inventory.services.media_files import DATASHEET, IMAGE, MediaError

from . import media
from .models import ProductMedia, Shop, ShopListing, ShopMediaSettings, ShopMediaSite

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
JPG2 = b"\xff\xd8\xff\xe1" + b"\x01" * 300
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
PDF = b"%PDF-1.7\n" + b"x" * 500
HTML = b"<!DOCTYPE html><html><body>Not found</body></html>"


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self.status_code, self.body = status, body

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def web(pages):
    """requests.get-двійник: {url: bytes | (status, bytes) | Exception}."""
    def get(url, **kwargs):
        value = pages.get(url, (404, b""))
        if isinstance(value, Exception):
            raise value
        status, body = value if isinstance(value, tuple) else (200, value)
        return FakeResponse(body, status)
    return mock.patch.object(media_files.requests, "get", side_effect=get)


class MediaTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.tmp)
        override.enable()
        self.addCleanup(override.disable)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://web.example.com")
        self.p = Product.objects.create(sku="AN-1", name="Antenna", image_url="https://img.example.com/download.php?file=AN-1.jpg",
                                        datasheet_url="https://docs.example.com/download.php?file=AN-1.pdf")
        ShopListing.objects.create(shop=self.shop, product=self.p)
        # ручний запуск у тестах — без потоку
        patcher = mock.patch.object(media, "_start_background", side_effect=lambda fn: fn())
        patcher.start()
        self.addCleanup(patcher.stop)
        mock.patch.object(media, "ping_sites").start()
        self.addCleanup(mock.patch.stopall)

    def enable(self):
        s = media.settings_obj()
        s.enabled = True
        s.save()


class MediaFilesTests(TestCase):
    def test_sniff_and_check(self):
        self.assertEqual(media_files.check(JPG, IMAGE), ("jpg", "image/jpeg"))
        self.assertEqual(media_files.check(PNG, IMAGE), ("png", "image/png"))
        self.assertEqual(media_files.check(PDF, DATASHEET), ("pdf", "application/pdf"))
        for data, kind, text in [(HTML, DATASHEET, "HTML-сторінку"), (b"<svg onload=1>", IMAGE, "SVG"),
                                 (PDF, IMAGE, "це PDF"), (b"", IMAGE, "порожній"), (b"hello", DATASHEET, "не схожий")]:
            with self.assertRaisesRegex(MediaError, text):
                media_files.check(data, kind)

    def test_fetch(self):
        with web({"https://a/x.pdf": PDF, "https://a/html": HTML, "https://a/down": requests.exceptions.ConnectionError()}):
            self.assertEqual(media_files.fetch("https://a/x.pdf", DATASHEET), PDF)
            with self.assertRaisesRegex(MediaError, "HTTP 404"):
                media_files.fetch("https://a/missing", DATASHEET)
            with self.assertRaisesRegex(MediaError, "HTML"):
                media_files.fetch("https://a/html", DATASHEET)
            with self.assertRaisesRegex(MediaError, "з'єднання"):
                media_files.fetch("https://a/down", DATASHEET)
        with self.assertRaisesRegex(MediaError, "http"):
            media_files.fetch("ftp://a/x.pdf", DATASHEET)


class IndexTests(MediaTestBase):
    def test_url_then_file_then_none(self):
        self.p.datasheet_file.save("AN-1_datasheet.php", ContentFile(PDF), save=True)   # .php — як після старої кнопки
        with web({self.p.image_url: JPG, self.p.datasheet_url: (404, b"")}):
            img, ds = media.index_product(self.p)
        self.assertEqual((img.status, img.source, img.content_type), (ProductMedia.OK, "url", "image/jpeg"))
        self.assertTrue(default_storage.exists(img.stored) and img.stored.endswith(".jpg"))
        self.assertEqual((ds.status, ds.source), (ProductMedia.FALLBACK, "file"))   # посилання 404 → файл Minerva
        self.assertIn("HTTP 404", ds.url_error)
        self.assertTrue(ds.stored.endswith(".pdf"))
        Product.objects.filter(pk=self.p.pk).update(image_url="")
        self.p.refresh_from_db()
        img = media.index_one(self.p, IMAGE)
        self.assertEqual((img.status, img.sha256, img.stored), (ProductMedia.NONE, "", ""))   # сайт показує своє

    def test_stale_copy_kept_only_while_sources_unchanged(self):
        with web({self.p.image_url: JPG}):
            first = media.index_one(self.p, IMAGE)
        with web({self.p.image_url: requests.exceptions.Timeout()}):
            row = media.index_one(self.p, IMAGE)
        self.assertEqual((row.status, row.stale, row.sha256), (ProductMedia.ERROR, True, first.sha256))
        self.assertIn("вчасно", row.url_error)
        self.p.image_url = "https://img.example.com/new.jpg"
        self.p.save()
        with web({}):
            row = media.index_one(self.p, IMAGE)
        self.assertEqual((row.status, row.stale, row.sha256), (ProductMedia.ERROR, False, ""))

    def test_due_run_and_cleanup(self):
        with web({self.p.image_url: JPG, self.p.datasheet_url: PDF}):
            self.assertEqual(len(media.due()), 2)       # ще не перевірено
            state = media.run()
        self.assertEqual((state["state"], state["total"], state["counts"]["ok"], state["counts"]["changed"]), ("done", 2, 2, 2))
        self.assertEqual(media.due(), [])
        self.p.image_url = "https://img.example.com/v2.jpg"   # змінили в картці → перевірити знову
        self.p.save()
        self.assertEqual([(p.sku, kind) for p, kind, _ in media.due()], [("AN-1", IMAGE)])
        old = ProductMedia.objects.get(kind=IMAGE).stored
        with web({"https://img.example.com/v2.jpg": JPG2}):
            media.run()
        self.assertFalse(default_storage.exists(old))     # стара копія більше не потрібна
        self.assertEqual(len(media.due("all")), 2)

    def test_start_lock_and_progress(self):
        with web({self.p.image_url: JPG, self.p.datasheet_url: PDF}):
            self.assertTrue(media.start("changed", by="adm"))
        p = media.progress()
        self.assertEqual((p["run"]["state"], p["run"]["done"], p["run"]["by"]), ("done", 2, "adm"))
        self.assertEqual((p["summary"]["products"], p["summary"]["image"]["ok"], p["summary"]["datasheet"]["url"]), (1, 1, 1))
        ShopMediaSettings.objects.filter(pk=1).update(run={"state": "running", "heartbeat": "2000-01-01T00:00:00+00:00"})
        self.assertEqual(media.progress()["run"]["state"], "interrupted")   # «пульс» давно — можна знову
        with web({}):
            self.assertTrue(media.start("changed"))

    def test_auto_only_when_enabled(self):
        self.assertIsNone(media.auto())
        self.enable()
        with web({self.p.image_url: JPG, self.p.datasheet_url: PDF}):
            self.assertEqual(media.auto()["total"], 2)
            self.assertIsNone(media.auto())   # не частіше ніж раз на 5 хв


class MediaApiTests(MediaTestBase):
    def setUp(self):
        super().setUp()
        self.key = APIKey.objects.create(name="site", shop=self.shop, scopes=["products:read", "orders:write"])
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {self.key.key}")
        with web({self.p.image_url: JPG, self.p.datasheet_url: PDF}):
            media.index_product(self.p)
        self.img = ProductMedia.objects.get(kind=IMAGE)

    def item(self):
        return self.api.get("/api/v1/shop/products/").json()["results"][0]

    def test_disabled_then_enabled(self):
        self.assertIsNone(self.item()["media"])
        self.assertEqual(self.api.get(f"/api/v1/shop/media/{self.img.sha256}/").status_code, 404)
        self.enable()
        m = self.item()["media"]
        self.assertEqual((m["image"]["sha256"], m["image"]["ext"], m["image"]["source"]), (self.img.sha256, "jpg", "url"))
        self.assertTrue(m["image"]["url"].endswith(f"/api/v1/shop/media/{self.img.sha256}/"))
        self.assertEqual(m["datasheet"]["content_type"], "application/pdf")
        r = self.api.get(f"/api/v1/shop/media/{self.img.sha256}/")
        self.assertEqual((r.status_code, r["Content-Type"], b"".join(r.streaming_content)), (200, "image/jpeg", JPG))

    def test_other_shop_and_hidden_product(self):
        self.enable()
        other = Shop.objects.create(name="Other", slug="other")
        key = APIKey.objects.create(name="other", shop=other, scopes=["products:read"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        self.assertEqual(c.get(f"/api/v1/shop/media/{self.img.sha256}/").status_code, 404)
        ShopListing.objects.filter(product=self.p).update(is_visible=False)
        self.assertEqual(self.api.get(f"/api/v1/shop/media/{self.img.sha256}/").status_code, 404)
        self.assertEqual(self.api.get("/api/v1/shop/media/not-a-hash/").status_code, 404)

    def test_status_report(self):
        self.enable()
        r = self.api.post("/api/v1/shop/media/status/", {
            "have": [self.img.sha256, "zz"], "failed": {"a" * 64: "HTTP 500"},
            "sync_url": "https://web.example.com/dist/api/shop/media_sync.php"}, format="json")
        self.assertEqual(r.status_code, 200)   # products:read достатньо
        self.assertEqual((r.json()["wanted"], r.json()["have"], r.json()["sync_url_accepted"]), (2, 1, True))
        site = ShopMediaSite.objects.get(shop=self.shop)
        self.assertEqual(site.have, [self.img.sha256])
        self.assertEqual(media.sites_status()[0]["have"], 1)
        self.assertEqual(media.on_site(self.img)[0][1], "✅ на сайті")
        r = self.api.post("/api/v1/shop/media/status/", {"have": [], "sync_url": "https://evil.example.org/x.php"}, format="json")
        self.assertFalse(r.json()["sync_url_accepted"])   # лише домен «Адреса сайту» магазину
        site.refresh_from_db()
        self.assertEqual(site.sync_url, "https://web.example.com/dist/api/shop/media_sync.php")


class MediaAdminTests(MediaTestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def test_page_buttons_and_status(self):
        r = self.client.get("/admin/shop/productmedia/")
        self.assertContains(r, "Фото й даташити для сайту")
        self.assertContains(r, "Індексувати нові й змінені")
        with web({self.p.image_url: JPG, self.p.datasheet_url: (500, b"")}):
            r = self.client.post("/admin/shop/productmedia/run/", {"mode": "changed"})
        self.assertEqual(r.status_code, 302)
        data = self.client.get("/admin/shop/productmedia/status/").json()
        self.assertEqual((data["run"]["state"], data["run"]["counts"]["ok"], data["run"]["counts"]["error"]), ("done", 1, 1))
        r = self.client.get("/admin/shop/productmedia/")
        self.assertContains(r, "AN-1")
        self.assertContains(r, "HTTP 500")
        self.client.post("/admin/shop/productmedia/settings/", {"enabled": "1"})
        self.client.post("/admin/shop/productmedia/settings/", {"recheck_days": "14"})
        s = ShopMediaSettings.objects.get(pk=1)
        self.assertEqual((s.enabled, s.recheck_days), (True, 14))

    def test_listing_column_marker_and_filter(self):
        with web({self.p.image_url: JPG, self.p.datasheet_url: PDF}):
            media.index_product(self.p)
        self.enable()
        r = self.client.get("/admin/shop/shoplisting/")
        self.assertContains(r, 'class="inv-thumb"')
        self.assertContains(r, "🌐⏳")                                   # сайт ще не забрав
        sha = [m.sha256 for m in ProductMedia.objects.filter(product=self.p)]
        ShopMediaSite.objects.create(shop=self.shop, have=sha)
        r = self.client.get("/admin/shop/shoplisting/")
        self.assertContains(r, "🌐✓")
        self.assertContains(r, "фото: з Minerva — ✅ на сайті")
        other = Product.objects.create(sku="NO-MEDIA", name="Без нічого")
        ShopListing.objects.create(shop=self.shop, product=other)
        r = self.client.get("/admin/shop/shoplisting/?media=no_both")
        self.assertEqual([l.product.sku for l in r.context["cl"].result_list], ["NO-MEDIA"])

    def test_product_card_check_now(self):
        r = self.client.get(f"/admin/inventory/product/{self.p.pk}/change/")
        self.assertContains(r, "Для сайту")
        self.assertContains(r, "Перевірити зараз")
        with web({self.p.image_url: JPG, self.p.datasheet_url: PDF}):
            r = self.client.post(f"/admin/shop/productmedia/check-product/{self.p.pk}/")
        self.assertRedirects(r, f"/admin/inventory/product/{self.p.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(ProductMedia.objects.filter(product=self.p, status=ProductMedia.OK).count(), 2)

    def test_old_download_button_fixes_php_extension(self):
        from inventory.models import InventorySettings
        InventorySettings.objects.get_or_create(pk=1)
        self.p.image.save("AN-1.php", ContentFile(JPG), save=True)
        with web({self.p.datasheet_url: HTML}):
            d = self.client.post("/admin/inventory/inventorysettings/media-cache/").json()
        self.p.refresh_from_db()
        self.assertTrue(self.p.image.name.endswith(".jpg"))
        self.assertEqual((d["stats"]["images"]["fixed"], d["stats"]["datasheets"]["failed"]), (1, 1))
        self.assertIn("HTML", d["errors"][0])
        self.assertFalse(self.p.datasheet_file)   # сторінку помилки не зберегли як «даташит»


class PerShopMediaTests(MediaTestBase):
    """Фото для кожного магазину окремо: перемикач сайту й індексація лише товарів вибраного магазину."""
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def test_toggle_and_index_one_shop(self):
        self.enable()
        test = Shop.objects.create(name="Test", slug="test", is_test=True, url="https://test.example.com")
        other = Product.objects.create(sku="AN-2", name="Other", image_url="https://img.example.com/AN-2.jpg")
        ShopListing.objects.create(shop=test, product=other)
        with web({other.image_url: JPG}):
            self.client.post("/admin/shop/productmedia/run/", {"mode": "changed", "shop": str(test.pk)})
        run = self.client.get("/admin/shop/productmedia/status/").json()["run"]
        self.assertEqual((run["state"], run["shop"], run["total"]), ("done", "Test", 2))   # лише AN-2: фото + даташит
        self.assertFalse(ProductMedia.objects.filter(product=self.p).exists())
        self.assertTrue(media.wanted(test))
        self.client.post(f"/admin/shop/productmedia/site-toggle/{test.pk}/", {"enabled": "0"})
        test.refresh_from_db()
        self.assertFalse(test.media_enabled)
        self.assertEqual(media.wanted(test), {})
        sites = {s["name"]: s for s in self.client.get("/admin/shop/productmedia/status/").json()["sites"]}
        self.assertEqual((sites["Test (test)"]["media_enabled"], sites["Test (test)"]["is_test"], sites["Web (webshop)"]["media_enabled"]),
                         (False, True, True))
        self.assertContains(self.client.get("/admin/shop/productmedia/"), "усіх магазинів")
        # фільтри по магазину: лише його товари, панель — його підсумок і сайт
        page = self.client.get(f"/admin/shop/productmedia/?shop={test.pk}")
        self.assertContains(page, "Показано для магазину")
        self.assertContains(page, "AN-2")
        self.assertNotContains(page, ">AN-1<")
        data = self.client.get(f"/admin/shop/productmedia/status/?shop={test.pk}").json()
        self.assertEqual(([s["name"] for s in data["sites"]], data["summary"]["products"]), (["Test (test)"], 1))
        from .models import ShopMediaSite
        img = ProductMedia.objects.get(product=other, kind=IMAGE)
        ShopMediaSite.objects.create(shop=test, have=[img.sha256])
        have = self.client.get(f"/admin/shop/productmedia/?shop={test.pk}&onsite=have")
        missing = self.client.get(f"/admin/shop/productmedia/?shop={test.pk}&onsite=missing")
        self.assertEqual((have.context["cl"].result_count, missing.context["cl"].result_count), (1, 0))
