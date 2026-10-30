"""🧭 Дорожня карта публікації на DigiKey і виклики Marketplace API за специфікацією (DigiKey імітується).

python manage.py test bots.tests_publish --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyConfig, DigiKeyListing
from bots.services import dk_marketplace as mp
from bots.services import dk_publish
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement

SUPPLIER = "11111111-2222-3333-4444-555555555555"


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class FakeMarketplace:
    """Products v1 / Offers v1: staged-запис ≠ затверджений товар; офер створюється лише на затверджений."""

    def __init__(self, approved=False, offer_reason="Active"):
        self.approved, self.offer_reason, self.calls = approved, offer_reason, []
        self.offer = None

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append(("POST", url, json))
        if url.endswith("/products/stage/upsert"):
            return _Resp(200, {"_id": "staged-1", "partNumber": json["partNumber"]})
        if url.endswith("/offers"):
            if json["productId"] != "prod-1" or not self.approved:
                return _Resp(400, {"detail": "Product not found"})
            self.offer = {"id": "offer-1", "productId": "prod-1", "supplierSku": json["supplierSku"],
                          "isActive": True, "offerStatusReason": self.offer_reason,
                          "createDateUtc": "2026-10-30T10:00:00Z", "maxOrderQuantity": 500,
                          "incrementQuantity": 5, "returnsAccepted": True}
            return _Resp(200, self.offer)
        raise AssertionError(url)

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append(("GET", url, params))
        if url.endswith("/products"):
            products = [{"_id": "prod-1", "partNumber": params.get("PartNumber"), "webDisplay": True}] \
                if self.approved else []
            return _Resp(200, {"products": products, "totalCount": len(products)})
        if url.endswith("/offers"):
            return _Resp(200, {"offers": [self.offer] if self.offer else []})
        raise AssertionError(url)

    def put(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append(("PUT", url, json))
        return _Resp(200, {**(self.offer or {}), **json, "id": "offer-1"})


class PublishBase(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id = SUPPLIER
        cfg.save()
        self.product = Product.objects.create(sku="FT220407", name="Filter")
        create_movement(product=self.product, tx_type="Incoming", qty=40)
        self.listing = DigiKeyListing.objects.create(
            product=self.product, card_source="stock", dk_category_id="Filters", dk_title="SMA Filter 2.4 GHz",
            dk_description="Band-pass filter", dk_manufacturer="Sevskiy", dk_min_order_qty=1,
            dk_prices=[{"qty": 1, "price": "19.50"}, {"qty": 10, "price": "17.00"}])
        token = mock.patch("bots.services.dk_marketplace.get_marketplace_token", return_value="tok")
        token.start()
        self.addCleanup(token.stop)

    def use(self, fake):
        for method in ("post", "get", "put"):
            p = mock.patch(f"requests.{method}", side_effect=getattr(fake, method))
            p.start()
            self.addCleanup(p.stop)
        return fake

    def reload(self):
        return DigiKeyListing.objects.get(pk=self.listing.pk)


class ProblemsTests(PublishBase):
    def test_limits_from_spec_are_checked_before_sending(self):
        self.assertEqual(dk_publish.problems(self.listing), [])
        self.listing.dk_title = "X" * 51
        self.listing.dk_category_id = ""
        self.listing.dk_prices = []
        todo = " | ".join(dk_publish.problems(self.listing))
        for part in ("Назва довша за 50", "Категорія DigiKey", "Немає цін"):
            self.assertIn(part, todo)
        fake = self.use(FakeMarketplace())
        with self.assertRaises(mp.DKMarketplaceError):
            mp.publish_listing(self.listing)
        self.assertEqual(fake.calls, [])                  # до DigiKey нічого не пішло


class FlowTests(PublishBase):
    def test_staged_then_approved_offer_on_real_product_id(self):
        fake = self.use(FakeMarketplace(approved=False))
        self.assertEqual(mp.publish_listing(self.listing), "staged")
        listing = self.reload()
        self.assertEqual(listing.pub_events["staged_id"], "staged-1")
        self.assertTrue({"checked", "sent"} <= set(listing.pub_events))
        rm = dk_publish.roadmap(listing)
        self.assertEqual(rm["current"], "approved")
        product_calls = [c for c in fake.calls if c[0] == "GET" and c[1].endswith("/products")]
        self.assertEqual(product_calls[0][2].get("SupplierIds"), SUPPLIER)     # лише свої товари

        fake.approved = True                                # DigiKey затвердив товар
        self.assertEqual(mp.check_staged_listing(listing), "published")
        listing = self.reload()
        self.assertEqual((listing.dk_product_id, listing.dk_offer_id), ("prod-1", "offer-1"))
        rm = dk_publish.roadmap(listing)
        self.assertEqual([s["state"] for s in rm["steps"]], ["done"] * 6)
        self.assertTrue(rm["total"])

    def test_real_offer_error_after_approval_is_shown_not_hidden(self):
        fake = FakeMarketplace(approved=True)
        fake.post = lambda url, json=None, **kw: (_Resp(400, {"detail": "title too long"})
                                                  if url.endswith("/offers") else
                                                  _Resp(200, {"_id": "staged-1"}))
        self.use(fake)
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(dk_product_id="prod-1")
        with self.assertRaises(mp.DKMarketplaceError):
            mp.check_staged_listing(self.reload())
        self.assertIn("title too long", self.reload().last_error)

    def test_offer_not_visible_reason_on_roadmap(self):
        self.use(FakeMarketplace(approved=True, offer_reason="QuantityAvailableLessThanMOQ"))
        mp.publish_listing(self.listing)
        rm = dk_publish.roadmap(self.reload())
        live = rm["steps"][-1]
        self.assertEqual((rm["current"], live["state"]), ("live", "warn"))
        self.assertIn("MOQ", live["hint"])

    def test_update_keeps_fields_minerva_does_not_manage(self):
        fake = self.use(FakeMarketplace(approved=True))
        mp.publish_listing(self.listing)
        listing = self.reload()
        mp.update_offer(DigiKeyConfig.get(), listing)
        body = [c for c in fake.calls if c[0] == "PUT"][-1][2]
        self.assertEqual((body["maxOrderQuantity"], body["incrementQuantity"], body["returnsAccepted"]),
                         (500, 5, True))


class AdminRoadmapTests(PublishBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))

    def test_card_list_and_filter(self):
        r = self.client.get(f"/admin/bots/digikeylisting/{self.listing.pk}/change/")
        self.assertContains(r, "🧭 Публікація на DigiKey")
        self.assertContains(r, "картка: зі складу Minerva")
        r = self.client.get("/admin/bots/digikeylisting/")
        self.assertContains(r, "🧭 Етап публікації")
        dk_publish.mark(self.listing, "sent")
        r = self.client.get("/admin/bots/digikeylisting/?pub_stage=sent")
        self.assertEqual(list(r.context["cl"].result_list), [self.listing])
        self.assertEqual(list(self.client.get("/admin/bots/digikeylisting/?pub_stage=live")
                              .context["cl"].result_list), [])


class PendingUpdateTests(PublishBase):
    """Повторне надсилання вже опублікованого товару — DigiKey тримає зміни в «Products → Pending»."""

    def test_republish_is_tracked_until_digikey_accepts(self):
        self.use(FakeMarketplace(approved=True))
        mp.publish_listing(self.listing)                       # перша публікація — не «зміни»
        listing = self.reload()
        self.assertIsNone(dk_publish.roadmap(listing)["update"])
        mp.publish_listing(listing)                            # «🚀 Опублікувати» ще раз
        listing = self.reload()
        rm = dk_publish.roadmap(listing, detail=True)
        self.assertTrue(rm["update"]["sent"])
        self.assertEqual(rm["update"]["done"], "")
        sent = dict(rm["steps"][2]["facts"])["Повторне надсилання (зміни товару)"]
        self.assertIn("Products → Pending", sent)
        old = {"lastUpdateDateUtc": "2025-03-06T14:26:00Z"}
        self.assertFalse(dk_publish.note_product(listing, old))          # затверджена версія — старіша
        self.assertTrue(dk_publish.note_product(listing, {"lastUpdateDateUtc": "2099-01-01T00:00:00Z"}))
        self.assertTrue(dk_publish.roadmap(self.reload())["update"]["done"])

    def test_stage_sync_checks_pending_product(self):
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(dk_offer_id="offer-1", dk_product_id="prod-1")
        dk_publish.note_update_sent(self.reload(), "staged-9")
        offers = [{"id": "offer-1", "supplierSku": "FT220407", "isActive": True, "offerStatusReason": "Active",
                   "createDateUtc": "2025-03-06T14:26:00Z"}]
        with mock.patch("bots.services.dk_marketplace.fetch_product_by_sku",
                        return_value={"_id": "prod-1", "lastUpdateDateUtc": "2099-01-01T00:00:00Z"}) as fp:
            dk_publish.sync_stages(offers=offers)
        self.assertEqual(fp.call_count, 1)
        self.assertTrue(dk_publish.pending_update(self.reload().pub_events)["done"])
        with mock.patch("bots.services.dk_marketplace.fetch_product_by_sku") as fp:
            dk_publish.sync_stages(offers=offers)             # прийнято — більше не питаємо
        fp.assert_not_called()


class ExistingOfferTests(PublishBase):
    """DigiKey: «An offer already exists for this product» — прив'язати наявний офер, а не падати."""

    def test_existing_offer_is_linked_instead_of_error(self):
        fake = FakeMarketplace(approved=True)
        existing = {"id": "offer-old", "productId": "prod-1", "supplierSku": "FT220407", "isActive": True,
                    "offerStatusReason": "Active", "createDateUtc": "2026-07-01T09:00:00Z", "quantityAvailable": 7,
                    "prices": [{"quantityBreak": 1, "price": 21.5}]}

        def post(url, json=None, **kw):
            fake.calls.append(("POST", url, json))
            if url.endswith("/offers"):
                return _Resp(400, {"title": "Bad Request", "detail": "An offer already exists for this product.",
                                   "errors": {"inner": ["Product 'prod-1' already has an offer."]}})
            return _Resp(200, {"_id": "staged-1"})

        def get(url, params=None, **kw):
            fake.calls.append(("GET", url, params))
            if url.endswith("/offers") and (params or {}).get("ProductId") == "prod-1":
                return _Resp(200, {"offers": [existing]})
            return FakeMarketplace.get(fake, url, params=params, **kw)

        fake.post, fake.get = post, get
        self.use(fake)
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(dk_product_id="prod-1", sync_status="staged",
                                                                 last_error="create_offer 400: already")
        mp.create_offer_for_listing(self.reload())
        listing = self.reload()
        self.assertEqual((listing.dk_offer_id, listing.sync_status, listing.last_error),
                         ("offer-old", "published", ""))
        self.assertEqual((listing.dk_prices, listing.dk_quantity_available), ([{"qty": 1, "price": 21.5}], 7))
        rm = dk_publish.roadmap(listing, detail=True)
        self.assertEqual(rm["steps"][4]["state"], "done")
        self.assertTrue(any("уже був на DigiKey" in v for _k, v in rm["steps"][4]["facts"]))
        self.assertFalse([c for c in fake.calls if c[0] == "PUT"])      # на DigiKey нічого не змінено

    def test_other_400_still_raises(self):
        fake = FakeMarketplace(approved=False)
        self.use(fake)
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(dk_product_id="staged-1")
        with self.assertRaises(mp.DKMarketplaceError):
            mp.create_offer_for_listing(self.reload())


class StepDetailsTests(AdminRoadmapTests):
    """Клік по кроку карти: звідки дата, ID, помилка DigiKey, кнопки дій."""

    def test_approved_but_offer_refused_shows_error_and_create_offer(self):
        # як AN120202-01A: авто-перевірка знайшла товар затвердженим, POST /offers відмовив, стан — «очікує»
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(
            sync_status="staged", dk_product_id="prod-1", last_error="create_offer 400: {'detail': 'MOQ too high'}")
        dk_publish.mark(self.reload(), "sent")
        dk_publish.mark(self.reload(), "approved")
        rm = dk_publish.roadmap(self.reload(), detail=True)
        offer = rm["steps"][4]
        self.assertEqual((rm["current"], offer["state"], offer["open"]), ("offer", "error", True))
        self.assertIn("MOQ too high", offer["error"])
        self.assertIn(("DK Product ID", "prod-1"), rm["steps"][3]["facts"])
        self.assertEqual(rm["steps"][3]["how"], dk_publish.SRC_TEXT["minerva"])
        urls = [a.get("url", "") for a in offer["actions"]]
        self.assertIn(f"/admin/bots/digikeylisting/{self.listing.pk}/create-offer/", urls)
        r = self.client.get(f"/admin/bots/digikeylisting/{self.listing.pk}/change/")
        self.assertContains(r, "DigiKey відповів")
        self.assertContains(r, f'data-pr-post="/admin/bots/digikeylisting/{self.listing.pk}/create-offer/"')
        self.assertContains(r, 'data-pr-step="offer"')

    def test_missing_fields_link_to_form_fields(self):
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(dk_category_id="", dk_title="")
        rm = dk_publish.roadmap(self.reload(), detail=True)
        anchors = {a.get("anchor") for a in rm["steps"][1]["actions"]}
        self.assertTrue({"id_dk_category_id", "id_dk_title"} <= anchors)

    def test_dates_from_digikey_and_offer_not_linked(self):
        offer = {"id": "o-9", "supplierSku": "FT220407", "createDateUtc": "2025-05-01T08:00:00Z",
                 "isActive": True, "offerStatusReason": "QuantityAvailableLessThanMOQ"}
        dk_publish.sync_offer(self.reload(), offer)
        rm = dk_publish.roadmap(self.reload(), detail=True)
        self.assertEqual(rm["steps"][4]["how"], dk_publish.SRC_TEXT["digikey"])
        self.assertEqual(rm["steps"][0]["how"], dk_publish.SRC_TEXT["digikey_clamp"])
        self.assertIn(("На DigiKey є офер з цим артикулом", "o-9"), rm["steps"][4]["facts"])
        live = rm["steps"][5]
        self.assertEqual(live["state"], "warn")
        self.assertIn("оновіть залишок", live["text"])
        self.assertTrue(any(a.get("url", "").endswith("/sync-qty/") for a in live["actions"]))

    def test_view_only_user_sees_no_action_buttons(self):
        from core.models import UserProfile
        u = User.objects.create_user("ro", "ro@x.y", "p", is_staff=True)
        UserProfile.objects.update_or_create(user=u, defaults={"role": "readonly"})
        self.client.force_login(u)
        r = self.client.get(f"/admin/bots/digikeylisting/{self.listing.pk}/change/")
        if r.status_code == 200:
            self.assertNotContains(r, "data-pr-post=")

    def test_stage_sync_from_card_returns_to_card(self):
        with mock.patch("bots.services.dk_publish.sync_stages",
                        return_value={"total": 1, "updated": 0, "not_found": [], "without_offer": 0}) as m:
            r = self.client.post(f"/admin/bots/digikeylisting/pub-sync/?pk={self.listing.pk}")
        self.assertRedirects(r, f"/admin/bots/digikeylisting/{self.listing.pk}/change/", fetch_redirect_response=False)
        self.assertEqual([l.pk for l in m.call_args[0][0]], [self.listing.pk])
