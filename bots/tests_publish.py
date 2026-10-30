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
