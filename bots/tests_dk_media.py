"""🖼 Фото й даташити на DigiKey (bots/services/dk_media.py, bots/admin_dk_media.py). DigiKey і перевірка посилань
імітуються — без мережі.

    python manage.py test bots.tests_dk_media --settings=tabele.settings_test
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyConfig, DigiKeyListing
from bots.services import dk_media
from inventory.models import InventorySettings, Product

DS_FIELD = {"datasheetUrl": {"code": "datasheetUrl", "name": "Datasheet", "type": "Link", "required": False,
                             "values": []}}


def _fields(defs):
    from bots.services.dk_from_stock import FIELDS_CACHE_KEY
    from core.shared_cache import cache
    if defs is None:
        cache.delete(FIELDS_CACHE_KEY)
    else:
        cache.set(FIELDS_CACHE_KEY, defs, 3600)


class Base(TestCase):
    def setUp(self):
        dk_media.BACKGROUND = False
        self.addCleanup(setattr, dk_media, "BACKGROUND", True)
        _fields(None)
        self.addCleanup(_fields, None)
        from core.shared_cache import cache
        cache.delete(dk_media.STATE_KEY)
        cache.delete(dk_media.LOCK_KEY)
        s = InventorySettings.get()
        s.image_url_template = "https://docs.example.com/pictures/download.php?file={key}.jpg"
        s.datasheet_url_template = "https://docs.example.com/datasheets/download.php?file={key}.pdf"
        s.save()
        self.cfg = DigiKeyConfig.get()
        self.p = Product.objects.create(sku="AN-1", name="Antenna", image_url="https://img.example.com/AN-1.png")
        self.l = DigiKeyListing.objects.create(product=self.p, dk_title="Antenna", dk_description="d",
                                               dk_product_id="prod-1", sync_status="published",
                                               dk_image_url="https://old.example.com/AN-1.jpg")


class TargetTests(Base):
    def test_stock_mode_uses_product_link_then_stock_rule(self):
        self.assertEqual(dk_media.candidates(self.p, "image"), (["https://img.example.com/AN-1.png"], "товар (склад)"))
        found, how = dk_media.candidates(self.p, "datasheet")
        self.assertEqual((found[0], how), ("https://docs.example.com/datasheets/download.php?file=AN-1.pdf",
                                           "правило складу"))

    def test_own_rule_for_digikey(self):
        self.cfg.media_source = DigiKeyConfig.MEDIA_RULE
        self.cfg.media_image_template = "https://dk.example.com/img?file="     # лише початок адреси
        self.cfg.save()
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.media_image_template, "https://dk.example.com/img?file={key}.jpg")
        self.assertEqual(dk_media.target(self.p, "image"), "https://dk.example.com/img?file=AN-1.jpg")
        self.assertEqual(dk_media.target(self.p, "datasheet"), "")              # правила даташита немає

    def test_too_long_url_is_not_used(self):
        self.p.image_url = "https://img.example.com/" + "x" * 300
        self.assertEqual(dk_media.target(self.p, "image"), "")


class DatasheetFieldTests(Base):
    def test_sent_only_when_digikey_has_the_field(self):
        self.l.dk_datasheet_url = "https://docs.example.com/AN-1.pdf"
        self.assertEqual(dk_media.add_datasheet([], self.l), [])               # поля не знаємо — не надсилаємо
        _fields(DS_FIELD)
        self.assertEqual(dk_media.add_datasheet([], self.l),
                         [{"code": "datasheetUrl", "type": "String", "value": "https://docs.example.com/AN-1.pdf"}])
        product = {"imageUrl": "https://a/x.jpg",
                   "additionalFields": [{"code": "datasheetUrl", "value": "https://a/x.pdf"}]}
        self.assertEqual(dk_media.on_digikey(product), {"image": "https://a/x.jpg", "datasheet": "https://a/x.pdf"})

    def test_upsert_payload_carries_image_and_datasheet(self):
        from bots.services import dk_marketplace
        _fields(DS_FIELD)
        self.cfg.marketplace_supplier_id = "11111111-2222-3333-4444-555555555555"
        self.l.dk_datasheet_url = "https://docs.example.com/AN-1.pdf"
        sent = {}

        def post(url, json=None, **kw):
            sent.update(json)
            return mock.Mock(status_code=200, json=lambda: {"_id": "staged-1"}, raise_for_status=lambda: None)
        with mock.patch.object(dk_marketplace, "get_marketplace_token", return_value="t"), \
                mock.patch("requests.post", side_effect=post):
            dk_marketplace.upsert_product(self.cfg, self.l)
        self.assertEqual(sent["imageUrl"], "https://old.example.com/AN-1.jpg")
        self.assertIn({"code": "datasheetUrl", "type": "String", "value": "https://docs.example.com/AN-1.pdf"},
                      sent["additionalFields"])


class RowTests(Base):
    def test_actions(self):
        image, sheet = dk_media.rows([self.l])[0]["items"]
        self.assertEqual((image["action"], image["pick"]), ("replace", True))   # у картці інше посилання
        self.assertEqual(sheet["action"], "fill")                               # у картці порожньо
        self.l.dk_image_url = "https://img.example.com/AN-1.png"
        products = {self.l.pk: {"imageUrl": "https://other.example.com/x.jpg", "additionalFields": []}}
        image = dk_media.rows([self.l], products)[0]["items"][0]
        self.assertEqual((image["action"], image["dk"]), ("push", "https://other.example.com/x.jpg"))
        products[self.l.pk]["imageUrl"] = "https://img.example.com/AN-1.png"
        image = dk_media.rows([self.l], products)[0]["items"][0]
        self.assertEqual((image["action"], image["dk_same"]), ("same", True))

    def test_broken_new_link_keeps_working_current(self):
        def probe(url, kind):
            return "" if "old.example.com" in url else "HTTP 404"
        with mock.patch.object(dk_media, "probe", probe):
            image = dk_media.rows([self.l], check=True)[0]["items"][0]
        self.assertEqual((image["action"], image["pick"]), ("keep", False))


class PageTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.url = "/admin/bots/digikeylisting/media/"

    def test_page_check_apply_and_push(self):
        page = self.client.get(self.url)
        self.assertContains(page, "https://img.example.com/AN-1.png")
        self.assertContains(page, "не читали")
        live = {self.l.pk: {"imageUrl": "https://old.example.com/AN-1.jpg", "additionalFields": []}}
        with mock.patch.object(dk_media, "live_products", return_value=live), \
                mock.patch.object(dk_media, "probe", return_value=""):
            r = self.client.post(self.url, {"action": "check"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(dk_media.state()["mode"], "check")
        page = self.client.get(self.url)
        self.assertContains(page, "замінити")                                  # звіт перевірки на сторінці
        with mock.patch("bots.services.dk_view.push_product") as push:
            self.client.post(self.url, {"action": "apply", "pick": [f"{self.l.pk}:image"], "push": "1"})
        self.l.refresh_from_db()
        self.assertEqual(self.l.dk_image_url, "https://img.example.com/AN-1.png")
        self.assertEqual(push.call_count, 1)
        self.assertEqual(dk_media.state()["ok"], 1)

    def test_listing_card_and_settings_panels(self):
        card = self.client.get(f"/admin/bots/digikeylisting/{self.l.pk}/change/")
        self.assertContains(card, "⇢ підставити")
        self.assertContains(card, 'data-url="https://img.example.com/AN-1.png"')
        settings = self.client.get(f"/admin/bots/digikeyconfig/{self.cfg.pk}/change/")
        self.assertContains(settings, "Звірити фото й даташити на DigiKey")
        self.assertContains(settings, "Поля даташита DigiKey Minerva не знає")

    def test_view_only_user_cannot_apply(self):
        viewer = User.objects.create_user("vera", "v@x.y", "p", is_staff=True)
        self.client.force_login(viewer)
        r = self.client.post(self.url, {"action": "apply", "pick": [f"{self.l.pk}:image"]})
        self.assertIn(r.status_code, (302, 403))
        self.l.refresh_from_db()
        self.assertEqual(self.l.dk_image_url, "https://old.example.com/AN-1.jpg")
