"""«🔍 Що бачить DigiKey» і «⏱ Строки відвантаження» (DigiKey імітується).

python manage.py test bots.tests_dk_tools --settings=tabele.settings_test
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyConfig, DigiKeyListing
from bots.services import dk_lead, dk_view
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement

SUPPLIER = "11111111-2222-3333-4444-555555555555"


def offer(**kw):
    base = {"id": "o-1", "supplierSku": "FT220407", "title": "SMA Filter 2.4 GHz", "isActive": True,
            "quantityAvailable": 40, "minOrderQuantity": 1, "leadTimeToShip": 11, "offerStatusReason": "Active",
            "prices": [{"quantityBreak": 1, "price": 19.5}, {"quantityBreak": 10, "price": 17.0}],
            "lastUpdateDateUtc": "2026-10-29T08:00:00Z", "lastUpdatedSource": "Manual"}
    return {**base, **kw}


class ToolsBase(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id = SUPPLIER
        cfg.save()
        self.product = Product.objects.create(sku="FT220407", name="Filter")
        create_movement(product=self.product, tx_type="Incoming", qty=40)
        self.listing = DigiKeyListing.objects.create(
            product=self.product, dk_category_id="855", dk_title="SMA Filter 2.4 GHz", dk_description="Band-pass",
            dk_manufacturer="Sevskiy", dk_offer_id="o-1", dk_product_id="p-1", dk_lead_time_days=11,
            dk_packaging="Bulk", dk_attributes={"139": "2.4 GHz"},
            dk_prices=[{"qty": 1, "price": "19.50"}, {"qty": 10, "price": "17.00"}])
        dk_lead.BACKGROUND = False
        self.addCleanup(setattr, dk_lead, "BACKGROUND", True)
        from core.shared_cache import cache
        cache.clear()

    def admin(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))


class DkViewTests(ToolsBase):
    def test_offer_fields_side_by_side(self):
        rows = {r["key"]: r for r in dk_view.offer_rows(self.listing, offer())}
        self.assertEqual({k for k, r in rows.items() if r["state"] != "same"}, set())
        rows = {r["key"]: r for r in dk_view.offer_rows(self.listing, offer(
            leadTimeToShip=21, prices=[{"quantityBreak": 1, "price": 22.0}], quantityAvailable=0))}
        self.assertEqual(rows["leadTimeToShip"]["state"], "diff")
        self.assertEqual((rows["leadTimeToShip"]["minerva"], rows["leadTimeToShip"]["dk"]), (11, 21))
        self.assertEqual(rows["prices"]["state"], "diff")
        self.assertEqual(rows["quantityAvailable"]["state"], "diff")             # «думав, що передав»: на DigiKey 0
        missing = dk_view.offer_rows(self.listing, None)
        self.assertTrue(all(r["state"] == "missing" for r in missing))

    def test_live_product_and_attributes(self):
        product = {"_id": "p-1", "categoryId": "855", "description": "Band-pass", "manufacturer": "Other",
                   "imageUrl": "", "webDisplay": False,
                   "additionalFields": [{"code": "139", "value": "2.4 GHz"}, {"code": "packaging", "value": "Bulk"}]}
        with mock.patch("bots.services.dk_prices._fetch_one", return_value=offer()), \
                mock.patch("bots.services.dk_marketplace.fetch_product_by_sku", return_value=product):
            v = dk_view.live(self.listing)
        p = {r["key"]: r for r in v["product_rows"]}
        self.assertEqual((p["categoryId"]["state"], p["manufacturer"]["state"]), ("same", "diff"))
        attrs = {r["key"]: r["state"] for r in v["attr_rows"]}
        self.assertEqual(attrs["attr:139"], "same")
        self.assertFalse(v["web"])
        self.assertEqual(v["source"], "Manual")

    def test_live_survives_network_errors(self):
        with mock.patch("bots.services.dk_prices._fetch_one", side_effect=RuntimeError("timeout")), \
                mock.patch("bots.services.dk_marketplace.fetch_product_by_sku", return_value=None):
            v = dk_view.live(self.listing)
        self.assertIn("офер: timeout", v["errors"])
        self.assertTrue(all(r["state"] == "missing" for r in v["offer_rows"]))

    def test_pages_and_push(self):
        self.admin()
        url = "/admin/bots/digikeylisting/dk-diff/"
        with mock.patch("bots.services.dk_prices._fetch_one", return_value=offer(leadTimeToShip=30)), \
                mock.patch("bots.services.dk_marketplace.fetch_product_by_sku", return_value=None):
            r = self.client.get(url, {"pk": self.listing.pk})
        self.assertContains(r, "Строк відвантаження, дн")
        self.assertContains(r, "📤 Оновити вибране на DigiKey")
        self.assertContains(r, "📤 Надіслати весь офер з Minerva")
        self.assertContains(r, "вручну на supplier.digikey.com")
        with mock.patch("bots.services.dk_prices._offers_for", return_value=[offer(title="Old title")]):
            r = self.client.get(url, {"all": "1"})
        self.assertContains(r, "інакше: Назва")
        with mock.patch("bots.services.dk_marketplace.update_offer") as upd:
            r = self.client.post(f"{url}push/{self.listing.pk}/")
        self.assertEqual((r.status_code, upd.call_count), (302, 1))
        self.assertEqual(self.client.get(f"{url}push/{self.listing.pk}/").status_code, 403)
        r = self.client.get(f"/admin/bots/digikeylisting/{self.listing.pk}/change/")
        self.assertContains(r, f"dk-diff/?pk={self.listing.pk}")


class LeadTimeTests(ToolsBase):
    def setUp(self):
        super().setUp()
        p2 = Product.objects.create(sku="AN220213", name="Antenna")
        self.other = DigiKeyListing.objects.create(product=p2, dk_title="Antenna", dk_offer_id="o-2",
                                                   dk_lead_time_days=5, dk_prices=[{"qty": 1, "price": "9"}])
        self.none = DigiKeyListing.objects.create(product=Product.objects.create(sku="X-1"), dk_title="No offer")
        self.puts = []
        p = mock.patch("bots.services.dk_prices._put_offer", side_effect=lambda oid, body: self.puts.append((oid, body)))
        p.start()
        self.addCleanup(p.stop)

    def offers(self):
        return [offer(leadTimeToShip=11, quantityAvailable=33),
                offer(id="o-2", supplierSku="AN220213", title="Antenna", leadTimeToShip=5,
                      prices=[{"quantityBreak": 1, "price": 8.5}])]

    def test_only_lead_time_changes_on_digikey(self):
        self.assertEqual(dk_lead.set_days([self.listing.pk, self.other.pk], 21), 2)
        with mock.patch("bots.services.dk_prices._offers_for", return_value=self.offers()):
            st = dk_lead.push([self.listing.pk, self.other.pk])
        self.assertEqual((st["ok"], st["errors"]), (2, []))
        body = dict(self.puts)["o-1"]
        self.assertEqual(body["leadTimeToShip"], 21)
        self.assertEqual(body["quantityAvailable"], 33)                        # як на DigiKey, не склад Minerva
        self.assertEqual(dict(self.puts)["o-2"]["prices"], [{"quantityBreak": 1, "price": 8.5}])   # ціни DigiKey
        self.puts.clear()
        with mock.patch("bots.services.dk_prices._offers_for",
                        return_value=[offer(leadTimeToShip=21), offer(id="o-2", supplierSku="AN220213", leadTimeToShip=21)]):
            st = dk_lead.push([self.listing.pk, self.other.pk])
        self.assertEqual((st["same"], self.puts), (2, []))                    # уже такий — без PUT

    def test_missing_offer_reported_and_lock_released(self):
        DigiKeyListing.objects.filter(pk=self.listing.pk).update(dk_lead_time_days=30)
        with mock.patch("bots.services.dk_prices._offers_for", return_value=[]):
            self.assertTrue(dk_lead.start_push([self.listing.pk, self.none.pk]))
        st = dk_lead.state()
        self.assertEqual(st["total"], 1)                                       # без офера в Minerva — не надсилаємо
        self.assertIn("офера немає", st["errors"][0][1])
        with mock.patch("bots.services.dk_prices._offers_for", return_value=[]):
            self.assertTrue(dk_lead.start_push([self.listing.pk]))             # блокування знято

    def test_days_validation(self):
        self.assertEqual(dk_lead.parse_days(" 14 "), 14)
        for bad in ("", "x", "-1", "400"):
            with self.assertRaises(ValueError):
                dk_lead.parse_days(bad)

    def test_page_live_and_apply(self):
        self.admin()
        url = "/admin/bots/digikeylisting/lead-times/"
        with mock.patch("bots.services.dk_prices._offers_for", return_value=self.offers()[:1] + [
                offer(id="o-2", supplierSku="AN220213", leadTimeToShip=9)]):
            r = self.client.get(url, {"live": "1"})
        self.assertContains(r, "Вибрати всі з різницею (1)")
        with mock.patch("bots.services.dk_prices._offers_for", return_value=self.offers()):
            r = self.client.post(url, {"pick": [self.listing.pk, self.other.pk], "days": "14", "push": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(set(DigiKeyListing.objects.filter(pk__in=[self.listing.pk, self.other.pk])
                             .values_list("dk_lead_time_days", flat=True)), {14})
        self.assertEqual(sorted(oid for oid, _b in self.puts), ["o-1", "o-2"])
        self.assertEqual(self.client.get(url + "status/").json()["ok"], 2)
        r = self.client.post(url, {"pick": [self.listing.pk], "days": "999"})
        self.assertEqual(DigiKeyListing.objects.get(pk=self.listing.pk).dk_lead_time_days, 14)


class SelectivePushTests(ToolsBase):
    def test_numeric_parameters_the_api_does_not_return_are_not_differences(self):
        product = {"_id": "p-1", "categoryId": "855", "description": "Band-pass", "manufacturer": "Sevskiy",
                   "imageUrl": "", "additionalFields": [{"code": "packaging", "value": "Bulk"},
                                                        {"code": "productLifecycleStatus", "value": "Active"}]}
        rows = {r["key"]: r for r in dk_view.product_rows(self.listing, product)}
        self.assertEqual(rows["attr:139"]["state"], "unknown")
        self.assertEqual(dk_view.summary(list(rows.values()))["diff"], 0)

    def test_only_selected_offer_fields_change(self):
        puts = []
        live = offer(quantityAvailable=38, leadTimeToShip=25, title="Old title",
                     prices=[{"quantityBreak": 1, "price": 21.0}])
        with mock.patch("bots.services.dk_prices._fetch_one", return_value=live), \
                mock.patch("bots.services.dk_prices._put_offer", side_effect=lambda oid, body: puts.append(body)):
            done = dk_view.push_offer_fields(self.listing, ["quantityAvailable", "supplierSku", "bogus"])
        self.assertEqual(done, ["quantityAvailable"])
        body = puts[0]
        self.assertEqual(body["quantityAvailable"], 40)                       # із Minerva
        self.assertEqual((body["title"], body["leadTimeToShip"]), ("Old title", 25))   # як на DigiKey
        self.assertEqual(body["prices"], [{"quantityBreak": 1, "price": 21.0}])

    def test_product_push_is_tracked_as_pending(self):
        with mock.patch("bots.services.dk_marketplace.upsert_product", return_value="staged-7"):
            dk_view.push_product(self.listing)
        events = DigiKeyListing.objects.get(pk=self.listing.pk).pub_events
        self.assertEqual(events["update_staged_id"], "staged-7")             # офер є — це зміни (Pending)

    def test_page_checkboxes_and_post(self):
        self.admin()
        url = "/admin/bots/digikeylisting/dk-diff/"
        live = offer(quantityAvailable=38)
        with mock.patch("bots.services.dk_prices._fetch_one", return_value=live), \
                mock.patch("bots.services.dk_marketplace.fetch_product_by_sku", return_value=None):
            r = self.client.get(url, {"pk": self.listing.pk})
        self.assertContains(r, 'name="fields" value="quantityAvailable" checked')
        self.assertNotContains(r, 'name="fields" value="title"')              # однакове — без галочки
        with mock.patch("bots.services.dk_prices._fetch_one", return_value=live), \
                mock.patch("bots.services.dk_prices._put_offer") as put:
            r = self.client.post(f"{url}push/{self.listing.pk}/", {"what": "fields", "fields": ["quantityAvailable"]})
        self.assertEqual((r.status_code, put.call_count), (302, 1))
