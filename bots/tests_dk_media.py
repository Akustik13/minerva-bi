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
        self.assertContains(settings, "Поля «Datasheet URL» DigiKey Minerva ще не знає")

    def test_view_only_user_cannot_apply(self):
        viewer = User.objects.create_user("vera", "v@x.y", "p", is_staff=True)
        self.client.force_login(viewer)
        r = self.client.post(self.url, {"action": "apply", "pick": [f"{self.l.pk}:image"]})
        self.assertIn(r.status_code, (302, 403))
        self.l.refresh_from_db()
        self.assertEqual(self.l.dk_image_url, "https://old.example.com/AN-1.jpg")


class DatasheetCodeTests(Base):
    """Поле «Datasheet URL» DigiKey: код — з товарів DigiKey, відмова DigiKey — повтор без нього."""

    def test_code_learned_from_digikey_product_and_pulled_attributes(self):
        self.assertIsNone(dk_media.datasheet_field())
        product = {"imageUrl": "", "additionalFields": [{"code": "datasheetUrl", "value": "https://a/x.pdf"}]}
        self.assertEqual(dk_media.on_digikey(product)["datasheet"], "https://a/x.pdf")
        self.assertEqual(DigiKeyConfig.get().media_datasheet_code, "datasheetUrl")
        self.assertEqual(dk_media.datasheet_field()["code"], "datasheetUrl")
        DigiKeyConfig.objects.filter(pk=1).update(media_datasheet_code="")
        dk_media.learn_from_attributes({"139": "433 MHz", "datasheetUrl": "https://a/x.pdf"})
        self.assertEqual(DigiKeyConfig.get().media_datasheet_code, "datasheetUrl")

    def test_new_value_replaces_old_pulled_one(self):
        DigiKeyConfig.objects.filter(pk=1).update(media_datasheet_code="datasheetUrl")
        self.l.dk_datasheet_url = "https://docs.example.com/new.pdf"
        fields = dk_media.add_datasheet([{"code": "datasheetUrl", "type": "String", "value": "https://old/x.pdf"}],
                                        self.l)
        self.assertEqual(fields, [{"code": "datasheetUrl", "type": "String", "value": "https://docs.example.com/new.pdf"}])

    def test_rejected_field_is_dropped_and_forgotten(self):
        from bots.services import dk_marketplace
        DigiKeyConfig.objects.filter(pk=1).update(media_datasheet_code="datasheetUrl")
        self.cfg = DigiKeyConfig.get()
        self.cfg.marketplace_supplier_id = "11111111-2222-3333-4444-555555555555"
        self.l.dk_datasheet_url = "https://docs.example.com/AN-1.pdf"
        self.l.dk_attributes = {"139": "433 MHz"}
        calls = []

        def post(url, json=None, **kw):
            calls.append([f["code"] for f in json["additionalFields"]])
            if len(calls) == 1:
                err = mock.Mock(status_code=400, json=lambda: {"detail": "Unknown field code: datasheetUrl"})
                import requests
                raise_ = requests.HTTPError(response=err)
                return mock.Mock(status_code=400, raise_for_status=mock.Mock(side_effect=raise_))
            return mock.Mock(status_code=200, json=lambda: {"_id": "staged-2"}, raise_for_status=lambda: None)
        with mock.patch.object(dk_marketplace, "get_marketplace_token", return_value="t"),                 mock.patch("requests.post", side_effect=post):
            self.assertEqual(dk_marketplace.upsert_product(self.cfg, self.l), "staged-2")
        self.assertEqual(len(calls), 2)
        self.assertIn("datasheetUrl", calls[0])
        self.assertEqual(calls[1], [c for c in calls[0] if c != "datasheetUrl"])  # решта параметрів лишились
        self.assertIn("139", calls[1])
        self.assertEqual(DigiKeyConfig.get().media_datasheet_code, "")


class DigiKeyPhotoCopyTests(Base):
    def test_digikey_hosted_photo_is_not_a_difference(self):
        dk = "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/8926/MFG_3228_AN-1.jpg"
        products = {self.l.pk: {"imageUrl": dk, "additionalFields": []}}
        self.l.dk_image_url = "https://img.example.com/AN-1.png"                # як за правилом
        image = dk_media.rows([self.l], products)[0]["items"][0]
        self.assertEqual((image["action"], image["pick"], image["dk_copy"]), ("dkcopy", False, True))
        self.l.dk_image_url = dk                                                # стягнуте з DigiKey
        image = dk_media.rows([self.l])[0]["items"][0]
        self.assertEqual((image["action"], image["pick"]), ("dkcopy", False))


class ImageOnceTests(Base):
    """Фото йде на DigiKey на першій публікації або з «🖼 Оновити фото на DigiKey»; інакше — копія DigiKey."""

    COPY = "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/8926/MFG_3228_AN-1.jpg"

    def upsert(self):
        from bots.services import dk_marketplace
        self.cfg.marketplace_supplier_id = "11111111-2222-3333-4444-555555555555"
        sent = {}

        def post(url, json=None, **kw):
            sent.update(json)
            return mock.Mock(status_code=200, json=lambda: {"_id": "staged-1"}, raise_for_status=lambda: None)
        with mock.patch.object(dk_marketplace, "get_marketplace_token", return_value="t"), \
                mock.patch("requests.post", side_effect=post):
            dk_marketplace.upsert_product(self.cfg, self.l)
        self.l.refresh_from_db()
        return sent["imageUrl"]

    def published(self, **kw):
        DigiKeyListing.objects.filter(pk=self.l.pk).update(dk_offer_id="offer-1", **kw)
        self.l.refresh_from_db()

    def test_first_publication_sends_our_link(self):
        self.assertEqual(self.upsert(), "https://old.example.com/AN-1.jpg")

    def test_published_keeps_digikey_copy(self):
        self.published(dk_image_on_dk=self.COPY)
        self.assertEqual(self.upsert(), self.COPY)                       # опис / даташит — фото не чіпаємо
        self.assertEqual(self.l.dk_image_on_dk, self.COPY)

    def test_published_reads_current_photo_from_digikey_once(self):
        self.published()
        with mock.patch("bots.services.dk_marketplace.fetch_product_by_sku",
                        return_value={"imageUrl": self.COPY}) as fetch:
            self.assertEqual(self.upsert(), self.COPY)
            self.assertEqual(self.upsert(), self.COPY)
        self.assertEqual(fetch.call_count, 1)                             # далі — запам'ятована адреса

    def test_update_flag_sends_our_link_once(self):
        self.published(dk_image_on_dk=self.COPY, dk_image_update=True)
        self.assertEqual(self.upsert(), "https://old.example.com/AN-1.jpg")
        self.assertEqual((self.l.dk_image_update, self.l.dk_image_on_dk), (False, ""))
        with mock.patch("bots.services.dk_marketplace.fetch_product_by_sku", return_value={"imageUrl": self.COPY}):
            self.assertEqual(self.upsert(), self.COPY)                    # наступного разу — знову копія DigiKey

    def test_compare_page_picks(self):
        self.published(dk_image_on_dk=self.COPY)
        dk_media.apply({self.l.pk: {"datasheet"}})
        self.l.refresh_from_db()
        self.assertFalse(self.l.dk_image_update)                          # лише даташит — фото не піде
        dk_media.apply({self.l.pk: {"image"}})
        self.l.refresh_from_db()
        self.assertTrue(self.l.dk_image_update)

    def test_changing_photo_link_in_card_ticks_update(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        admin = site._registry[DigiKeyListing]
        request = RequestFactory().post("/")
        request.user = User.objects.create_superuser("root3", "r3@x.y", "p")
        with mock.patch.object(admin, "message_user"), mock.patch("bots.admin.messages"):
            self.l.dk_image_url = "https://new.example.com/AN-1.jpg"
            form = mock.Mock(changed_data=["dk_image_url"])
            admin.save_model(request, self.l, form, True)
        self.l.refresh_from_db()
        self.assertTrue(self.l.dk_image_update)

    def test_what_digikey_sees_treats_copy_as_same(self):
        from bots.services import dk_view
        rows = {r["key"]: r for r in dk_view.product_rows(self.l, {"imageUrl": self.COPY, "additionalFields": []})}
        self.assertEqual(rows["imageUrl"]["state"], "same")
        self.assertIn("копія DigiKey", rows["imageUrl"]["dk"])
