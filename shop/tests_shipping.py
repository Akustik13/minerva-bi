"""Тести зон доставки: довідник країн, винятки країн, термін доставки, редактор у картці магазину.
python manage.py test shop --settings=tabele.settings_test"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.forms import inlineformset_factory
from django.test import TestCase

from . import countries
from .admin import ShippingZoneForm, ShippingZoneFormSet
from .models import EU_COUNTRIES, ShippingZone, Shop
from .shipping import clean_overrides, delivery_label, public_config, rule_for, shipping_cost


class CountryCatalogTests(TestCase):
    def test_catalog(self):
        self.assertEqual(set(countries.REGION_CODES["eu"]), set(EU_COUNTRIES))
        codes = [c for r in countries.REGIONS for c in (x[0] for x in r[4])]
        self.assertEqual(len(codes), len(set(codes)))  # країна лише в одному регіоні
        self.assertGreater(len(codes), 180)
        self.assertEqual(countries.name("DE"), "Німеччина (DE)")
        self.assertEqual(countries.name("CH", "de"), "Schweiz (CH)")
        self.assertEqual(countries.name("XK"), "XK")


class ZoneRuleTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True,
                                        free_shipping_enabled=True, free_shipping_threshold=Decimal("250"))
        self.eu = ShippingZone.objects.create(
            shop=self.shop, name="Europa", countries=["AT", "FR", "CH"], price=Decimal("17.00"),
            delivery_days_min=3, delivery_days_max=6,
            overrides={"CH": {"price": "25.00", "days_min": 5, "days_max": 8}, "FR": {"days_max": 4}})
        ShippingZone.objects.create(shop=self.shop, name="Welt", countries=["*"], price=Decimal("42"),
                                    free_shipping=False, delivery_days_min=5, delivery_days_max=10)

    def test_override_price_and_days(self):
        r = rule_for(self.shop, "CH")
        self.assertEqual((r["price"], r["days_min"], r["days_max"], r["override"]), (Decimal("25.00"), 5, 8, True))
        r = rule_for(self.shop, "AT")
        self.assertEqual((r["price"], r["days_min"], r["days_max"], r["override"]), (Decimal("17.00"), 3, 6, False))
        r = rule_for(self.shop, "FR")  # лише свій термін — ціна зони
        self.assertEqual((r["price"], r["days_min"], r["days_max"]), (Decimal("17.00"), None, 4))
        self.assertEqual(rule_for(self.shop, "JP")["zone"].name, "Welt")

    def test_shipping_cost(self):
        self.assertEqual(shipping_cost(self.shop, "CH", Decimal("100")), Decimal("25.00"))
        self.assertEqual(shipping_cost(self.shop, "CH", Decimal("300")), Decimal("0.00"))  # безкоштовно від порогу
        self.assertEqual(shipping_cost(self.shop, "JP", Decimal("300")), Decimal("42"))   # «Welt» — без безкоштовної

    def test_delivery_label(self):
        self.assertEqual(delivery_label(1, 2), "1–2 Werktage")
        self.assertEqual(delivery_label(3, 3), "3 Werktage")
        self.assertEqual(delivery_label(1, None), "1 Werktag")
        self.assertEqual(delivery_label(None, None), "")
        self.assertEqual(delivery_label(2, 4, "en"), "2–4 business days")

    def test_public_config(self):
        z = public_config(self.shop)["zones"][0]
        self.assertEqual(z["delivery_days"], {"min": 3, "max": 6})
        self.assertEqual(z["overrides"]["CH"], {"price": Decimal("25.00"), "delivery_days": {"min": 5, "max": 8}})
        self.assertEqual(z["overrides"]["FR"], {"price": None, "delivery_days": {"min": None, "max": 4}})

    def test_clean_overrides(self):
        self.assertEqual(clean_overrides({"ch": {"price": "25,5", "days_min": "4", "days_max": ""}, "US": {"price": 1},
                                          "AT": {"price": ""}}, ["CH", "AT"]),
                         {"CH": {"price": "25.50", "days_min": 4}})  # US не в зоні, AT порожній
        for bad in ({"CH": {"price": "-1"}}, {"CH": {"days_min": 9, "days_max": 2}}, {"CH": {"price": "abc"}},
                    {"CH": {"days_max": 400}}):
            with self.assertRaises(ValueError):
                clean_overrides(bad, ["CH"])


class ZoneEditorTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.de = ShippingZone.objects.create(shop=self.shop, name="DE", countries=["DE"], price=Decimal("6.90"))
        self.FormSet = inlineformset_factory(Shop, ShippingZone, form=ShippingZoneForm, formset=ShippingZoneFormSet,
                                             extra=0, can_delete=True)

    def data(self, *zones):
        d = {"shipping_zones-TOTAL_FORMS": str(len(zones)), "shipping_zones-INITIAL_FORMS": "1",
             "shipping_zones-MIN_NUM_FORMS": "0", "shipping_zones-MAX_NUM_FORMS": "1000"}
        for i, z in enumerate(zones):
            for k, v in z.items():
                d[f"shipping_zones-{i}-{k}"] = v
        return d

    def zone(self, **kw):
        z = {"id": "", "shop": self.shop.pk, "name": "Zone", "price": "10", "free_shipping": "on", "is_active": "on",
             "sort_order": "0", "delivery_days_min": "", "delivery_days_max": "", "countries": "[]", "overrides": "{}"}
        z.update(kw)
        return z

    def test_save_countries_and_overrides(self):
        fs = self.FormSet(self.data(
            self.zone(id=self.de.pk, name="DE", countries='["DE"]', delivery_days_min="1", delivery_days_max="2"),
            self.zone(name="Europa", countries='["AT","CH","FR"]', delivery_days_min="3", delivery_days_max="6",
                      overrides=json.dumps({"CH": {"price": "25", "days_min": 5, "days_max": 8}, "US": {"price": 9}}))),
            instance=self.shop)
        self.assertTrue(fs.is_valid(), fs.errors)
        fs.save()
        eu = ShippingZone.objects.get(name="Europa")
        self.assertEqual(eu.countries, ["AT", "CH", "FR"])
        self.assertEqual(eu.overrides, {"CH": {"price": "25.00", "days_min": 5, "days_max": 8}})  # US — не в зоні
        self.assertEqual((ShippingZone.objects.get(name="DE").delivery_days_min, eu.delivery_days_max), (1, 6))

    def test_text_without_javascript_and_errors(self):
        fs = self.FormSet(self.data(self.zone(id=self.de.pk, name="DE", countries="DE, at")), instance=self.shop)
        self.assertTrue(fs.is_valid(), fs.errors)
        # країна в двох зонах
        fs = self.FormSet(self.data(self.zone(id=self.de.pk, name="DE", countries='["DE"]'),
                                    self.zone(name="EU", countries='["DE","AT"]')), instance=self.shop)
        self.assertFalse(fs.is_valid())
        self.assertIn("Німеччина (DE)", str(fs.non_form_errors()))
        # «до» менше за «від», від'ємна ціна винятку, порожні країни
        fs = self.FormSet(self.data(self.zone(id=self.de.pk, name="DE", countries='["DE"]', delivery_days_min="5",
                                              delivery_days_max="2", overrides='{"DE": {"price": "-3"}}')),
                          instance=self.shop)
        self.assertFalse(fs.is_valid())
        errors = str(fs.errors)
        self.assertIn("менше за", errors)
        self.assertIn("від'ємною", errors)
        fs = self.FormSet(self.data(self.zone(id=self.de.pk, name="DE", countries="[]")), instance=self.shop)
        self.assertFalse(fs.is_valid())

    def test_change_page_has_editor(self):
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(r, 'id="ship-catalog"')
        self.assertContains(r, 'class="ship-picker"')
        self.assertContains(r, "South Korea")  # json_script екранує кирилицю (\u…)
        self.assertContains(r, "Зони доставки")

    def test_payment_methods_prechecked_when_not_configured(self):
        # картка магазину без збережених способів оплати: галочки як зараз на сайті, а не порожні
        from .admin_payments import ShopForm
        form = ShopForm(instance=self.shop)
        self.assertEqual(form["payment_methods"].value(), ["prepayment", "invoice"])
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertRegex(r.content.decode(), r'<input[^>]*value="prepayment"[^>]*checked|<input[^>]*checked[^>]*value="prepayment"')
