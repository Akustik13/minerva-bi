"""🔷 Картка DigiKey зі складу Minerva й «🔄 Етапи з DigiKey» (DigiKey імітується).

python manage.py test bots.tests_from_stock --settings=tabele.settings_test
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyConfig, DigiKeyListing
from bots.services import dk_from_stock as svc
from bots.services import dk_publish
from core.shared_cache import cache
from inventory.models import InventorySettings, Product

SUPPLIER = "11111111-2222-3333-4444-555555555555"
FIELDS = [
    {"code": "rohsStatus", "name": "RoHS Status", "required": True, "permissionLevel": "ReadWrite",
     "fieldValues": [{"value": "ROHS3 Compliant"}, {"value": "Non-RoHS"}]},
    {"code": "139", "name": "Frequency", "required": False, "permissionLevel": "ReadWrite"},
    {"code": "eccn", "name": "ECCN", "required": True, "permissionLevel": "ReadOnly"},
    {"code": "packaging", "name": "Packaging", "required": True, "permissionLevel": "ReadWrite",
     "fieldValues": [{"value": "Bulk"}, {"value": "Tape & Reel (TR)"}]},
]


class FromStockBase(TestCase):
    def setUp(self):
        cache.clear()
        InventorySettings.get()
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id, cfg.locale_currency = SUPPLIER, "EUR"
        cfg.save()
        other = Product.objects.create(sku="FT220401", name="Filter 5.8", category="filters")
        DigiKeyListing.objects.create(
            product=other, category_type="filter", dk_category_id="Filters", dk_category_name="RF Filters",
            dk_title="Filter 5.8 GHz", dk_description="x", dk_manufacturer="Sevskiy", dk_packaging="Bulk",
            fa_frequency="5.8 GHz", dk_attributes={"327": "1 dB", "999": "SMA", "Frequency": "5.8 GHz"},
            dk_prices=[{"qty": 1, "price": "10.00"}])
        self.product = Product.objects.create(
            sku="FT220407", name="Фільтр", name_export="Band-pass filter 2.4 GHz SMA female, 50 Ohm, aluminium",
            category="filters", sale_price="19.50",
            tech_attributes={"Frequency Range": "2.4 GHz", "Insertion Loss": "1.2 dB", "hts": "8529"})
        self.fetch = mock.patch("bots.services.dk_marketplace.fetch_custom_fields", return_value=FIELDS)
        self.fetch.start()
        self.addCleanup(self.fetch.stop)


class DraftTests(FromStockBase):
    def test_fields_from_digikey_and_similar_listings(self):
        defs = svc.field_defs()
        self.assertNotIn("eccn", defs)                                  # ReadOnly — не питаємо
        rows = {r["code"]: r for r in svc.template("Filters")}
        self.assertTrue(rows["rohsStatus"]["required"])
        self.assertEqual(rows["rohsStatus"]["values"], ["ROHS3 Compliant", "Non-RoHS"])
        self.assertEqual(rows["139"]["name"], "Frequency")              # параметр категорії з наших лістингів
        self.assertEqual(rows["999"]["examples"], ["SMA"])              # назви немає — підказка значеннями
        self.assertIn("packaging", rows)

    def test_draft_fills_from_stock_and_asks_the_rest(self):
        d = svc.build_draft(self.product)
        f = d["fields"]
        self.assertEqual((f["dk_category_id"], f["category_type"]), ("Filters", "filter"))
        self.assertLessEqual(len(f["dk_title"]), 50)
        self.assertIn("назву скорочено", " ".join(d["notes"]))
        self.assertEqual(f["dk_manufacturer"], "Sevskiy")               # як у наших лістингів
        self.assertEqual(f["dk_prices"], [{"qty": 1, "price": "19.5"}])
        self.assertEqual(f["dk_packaging"], "Bulk")
        self.assertEqual(d["attrs"]["139"], "2.4 GHz")                  # Frequency ← «Frequency Range» складу
        self.assertEqual(d["attrs"]["327"], "1.2 dB")
        self.assertEqual(f["fa_frequency"], "2.4 GHz")                  # фільтр — і в поля картки
        self.assertIn("склад: Frequency Range", d["sources"]["139"])
        missing = {m["code"]: m["required"] for m in d["missing"]}
        self.assertEqual(missing, {"rohsStatus": True, "999": False})

    def test_value_outside_digikey_list_is_asked_again(self):
        values, sources, missing = svc.autofill(self.product, svc.template("Filters"), {"rohsStatus": "maybe"})
        self.assertNotIn("rohsStatus", values)
        self.assertIn("немає серед значень DigiKey", missing[0]["hint"])

    def test_create_listing_learns_values_and_marks_roadmap(self):
        listing, draft = svc.create_listing(self.product, {"rohsStatus": "rohs3 compliant", "999": "SMA"})
        self.assertEqual(listing.card_source, "stock")
        self.assertEqual(listing.dk_attributes["rohsStatus"], "ROHS3 Compliant")
        self.assertTrue({"card", "checked"} <= set(listing.pub_events))
        sent = {a["code"]: a["value"] for a in DigiKeyListing.objects.get(pk=listing.pk).get_all_attributes_api()}
        self.assertEqual((sent["rohsStatus"], sent["999"], sent["139"]), ("ROHS3 Compliant", "SMA", "2.4 GHz"))
        self.assertNotIn("Frequency", sent)                             # людські назви DigiKey не шлемо
        self.product.refresh_from_db()
        self.assertEqual(self.product.tech_attributes["RoHS Status"], "ROHS3 Compliant")
        again = svc.build_draft(self.product)                         # наступного разу — зі складу, не питаємо
        self.assertEqual(again["sources"]["rohsStatus"], "склад: RoHS Status")
        self.assertNotIn("rohsStatus", [m["code"] for m in again["missing"]])
        with self.assertRaises(svc.AlreadyListed):
            svc.create_listing(self.product)

    def test_no_prices_or_category_blocks_checked(self):
        self.product.sale_price = None
        self.product.category = "other"
        self.product.save()
        DigiKeyListing.objects.update(category_type="antenna")
        listing, draft = svc.create_listing(self.product, {"rohsStatus": "Non-RoHS"})
        self.assertEqual(listing.dk_category_id, "")
        self.assertNotIn("checked", listing.pub_events)
        self.assertIn("Категорія DigiKey", " ".join(dk_publish.problems(listing)))


class FromStockPageTests(FromStockBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.url = "/admin/bots/digikeylisting/from-stock/"

    def test_picker_preview_and_create(self):
        r = self.client.get(self.url, {"q": "FT2204"})
        self.assertContains(r, "FT220407")
        self.assertNotContains(r, '<b>FT220401</b>')                    # уже має лістинг
        r = self.client.get(self.url, {"ids": self.product.pk})
        self.assertContains(r, "RoHS Status")
        self.assertContains(r, "склад: Frequency Range")
        self.assertContains(r, 'name="p%d-f-rohsStatus"' % self.product.pk)
        pk = self.product.pk
        r = self.client.post(self.url, {"ids": str(pk), "action": "preview", f"p{pk}-f-rohsStatus": "Non-RoHS",
                                        f"p{pk}-title": "Band-pass filter 2.4 GHz", f"p{pk}-cat": "Filters"})
        self.assertContains(r, "можна надсилати")
        self.assertContains(r, 'value="Non-RoHS" selected')
        r = self.client.post(self.url, {"ids": str(pk), "action": "create", f"p{pk}-on": "1",
                                        f"p{pk}-f-rohsStatus": "Non-RoHS", f"p{pk}-title": "Band-pass filter 2.4 GHz",
                                        f"p{pk}-cat": "Filters"})
        listing = DigiKeyListing.objects.get(product=self.product)
        self.assertRedirects(r, f"/admin/bots/digikeylisting/{listing.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(listing.dk_title, "Band-pass filter 2.4 GHz")
        r = self.client.get(self.url, {"ids": self.product.pk})
        self.assertContains(r, "уже є картка DigiKey")

    def test_product_card_and_action_link_here(self):
        r = self.client.get(f"/admin/inventory/product/{self.product.pk}/change/")
        self.assertContains(r, f"{self.url}?ids={self.product.pk}")
        r = self.client.post("/admin/inventory/product/", {"action": "action_dk_from_stock",
                                                           "_selected_action": [self.product.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"from-stock/?ids={self.product.pk}", r["Location"])

    def test_view_only_user_cannot_create(self):
        from core.models import UserProfile
        u = User.objects.create_user("ro", "ro@x.y", "p", is_staff=True)
        UserProfile.objects.update_or_create(user=u, defaults={"role": "readonly"})
        self.client.force_login(u)
        pk = self.product.pk
        r = self.client.post(self.url, {"ids": str(pk), "action": "create", f"p{pk}-on": "1"})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(DigiKeyListing.objects.filter(product=self.product).exists())


class StageSyncTests(FromStockBase):
    def test_imported_listing_gets_real_offer_dates(self):
        listing = DigiKeyListing.objects.get(product__sku="FT220401")
        DigiKeyListing.objects.filter(pk=listing.pk).update(
            card_source="digikey", dk_offer_id="o-1",
            pub_events={"card": "2026-10-20T10:00:00+00:00", "sent": "2026-10-20T10:00:00+00:00",
                        "approved": "2026-10-20T10:00:00+00:00", "offer": "2026-10-20T10:00:00+00:00"})
        gone = DigiKeyListing.objects.create(product=self.product, dk_offer_id="o-gone", dk_title="x")
        offers = [{"id": "o-1", "supplierSku": "FT220401", "createDateUtc": "2025-03-01T08:00:00Z",
                   "isActive": True, "offerStatusReason": "Active"}]
        res = dk_publish.sync_stages(offers=offers)
        self.assertEqual((res["updated"], res["not_found"]), (2, ["FT220407"]))
        ev = DigiKeyListing.objects.get(pk=listing.pk).pub_events
        self.assertTrue(ev["offer"].startswith("2025-03-01"))          # справжня дата офера, не міграції
        self.assertTrue(ev["card"].startswith("2025-03-01"))           # кроки до офера — не пізніше
        self.assertTrue(ev["live"].startswith("2025-03-01"))
        rm = dk_publish.roadmap(DigiKeyListing.objects.get(pk=listing.pk))
        self.assertTrue(rm["dk_dates"])
        self.assertEqual(rm["steps"][-1]["state"], "done")
        rm = dk_publish.roadmap(DigiKeyListing.objects.get(pk=gone.pk))
        self.assertIn("офера немає на DigiKey", rm["offer_status"])
        self.assertEqual(dk_publish.sync_stages(offers=offers)["updated"], 0)   # повтор нічого не змінює

    def test_button_on_list(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.assertContains(self.client.get("/admin/bots/digikeylisting/"), "🔄 Етапи з DigiKey")
        with mock.patch("bots.services.dk_publish.sync_stages",
                        return_value={"total": 1, "updated": 1, "not_found": [], "without_offer": 0}) as m:
            r = self.client.post("/admin/bots/digikeylisting/pub-sync/")
        self.assertEqual((r.status_code, m.call_count), (302, 1))
        self.assertEqual(self.client.get("/admin/bots/digikeylisting/pub-sync/").status_code, 403)
