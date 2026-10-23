"""Пошук за технічними параметрами (inventory/param_filters.py): лічильники, «або» / «і», списки й огляд складу.
python manage.py test inventory --settings=tabele.settings_test"""
from django.contrib.auth.models import User
from django.test import TestCase

from inventory import param_filters as pf
from inventory.models import Product


def cable(sku, connector, cable_type, length, **extra):
    return Product.objects.create(sku=sku, name=sku, category="cable", tech_attributes={
        "Connector Type": connector, "Cable Type": cable_type, "Length": length,
        "hts": "8544.42", "Color": "Black", **extra})


class ParamFacetTests(TestCase):
    def test_parse_matches_and_facets(self):
        self.assertEqual(pf.parse(["Connector Type:SMA", "Connector Type:U.FL", "Length:1 m", "bad", ":x"]),
                         {"Connector Type": {"SMA", "U.FL"}, "Length": {"1 m"}})
        self.assertEqual(pf.params_of({"Gain": " 2 dBi ", "hts": "8529", "Bands": ["GPS", "GLONASS"], "Empty": ""}),
                         {"Gain": "2 dBi", "Bands": "GPS, GLONASS"})   # службові коди й порожні — ні
        rows = [{"Connector Type": "SMA", "Length": "1 m", "Color": "Black"},
                {"Connector Type": "SMA", "Length": "2 m", "Color": "Black"},
                {"Connector Type": "U.FL", "Length": "1 m", "Color": "Black"}]
        sel = {"Connector Type": {"SMA"}}
        out = {f["name"]: {v["value"]: v["count"] for v in f["values"]} for f in pf.facets(rows, sel)}
        self.assertEqual(out["Connector Type"], {"SMA": 2, "U.FL": 1})   # свій вибір не звужує власні значення («або»)
        self.assertEqual(out["Length"], {"1 m": 1, "2 m": 1})            # інші — лише серед SMA («і»)
        self.assertNotIn("Color", out)                                   # усі однакові — не фільтр


class ParamFilterAdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.a = cable("CA-SMA-1M", "SMA", "RG174", "1 m")
        self.b = cable("CA-SMA-2M", "SMA", "RG316", "2 m")
        self.c = cable("CA-UFL-1M", "U.FL", "1.13 mm", "1 m")
        Product.objects.create(sku="AN-1", name="Antenna", category="antenna", tech_attributes={"Gain": "2 dBi"})

    def shown(self, r):
        return sorted(p.sku if hasattr(p, "sku") else p.product.sku for p in r.context["cl"].result_list)

    def test_product_list(self):
        r = self.client.get("/admin/inventory/product/")
        self.assertContains(r, "оберіть категорію")                       # без категорії — підказка
        r = self.client.get("/admin/inventory/product/?category=cable")
        self.assertContains(r, "Connector Type")
        self.assertContains(r, "RG316")
        self.assertNotContains(r, "8544.42")                              # службовий код DigiKey — не параметр
        r = self.client.get("/admin/inventory/product/?category=cable&attr=Connector+Type:SMA")
        self.assertEqual(self.shown(r), ["CA-SMA-1M", "CA-SMA-2M"])
        r = self.client.get("/admin/inventory/product/?category=cable&attr=Connector+Type:SMA&attr=Connector+Type:U.FL&attr=Length:1+m")
        self.assertEqual(self.shown(r), ["CA-SMA-1M", "CA-UFL-1M"])        # SMA або U.FL, і 1 m
        self.assertContains(r, "✕ Connector Type: SMA")                   # у бічній панелі — зняти
        self.assertContains(r, "скинути всі")

    def test_listing_list_and_supply(self):
        from shop.models import Shop, ShopListing
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        for p in (self.a, self.b, self.c):
            ShopListing.objects.create(shop=shop, product=p)
        r = self.client.get("/admin/shop/shoplisting/?product__category=cable&attr=Cable+Type:RG316")
        self.assertEqual(self.shown(r), ["CA-SMA-2M"])
        self.assertContains(r, "🔎 Параметри")
        # посилання на значення зберігають категорію (product__… не губиться разом з номером сторінки «p»)
        links = [v["url"] for f in r.context["param_panel"]["facets"] for v in f["values"]]
        self.assertTrue(links and all("product__category=cable" in u for u in links))
        r = self.client.get("/admin/inventory/reorderproxy/supply/")
        self.assertContains(r, 'id="sv-params-data"')
        self.assertContains(r, "RG174")
        self.assertContains(r, "renderParams")
