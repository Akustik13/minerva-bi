"""🏷️ Бейдж категорії товару — значок і колір (inventory/category_badges.py, admin_category.py).
python manage.py test inventory --settings=tabele.settings_test"""
from django.contrib.auth.models import User
from django.test import TestCase

from inventory import category_badges as cb
from inventory.admin_category import ProductCategoryAdminForm
from inventory.models import Product, ProductCategory


class CleanTests(TestCase):
    def test_color(self):
        self.assertEqual(cb.clean_color("#E91E63"), "#e91e63")
        self.assertEqual(cb.clean_color("e91e63"), "#e91e63")
        self.assertEqual(cb.clean_color(" #e1e "), "#ee11ee")
        for bad in ("red", "#12345", "#gg0000", "#e91e63;background:url(x)", "", None, 5):
            self.assertIsNone(cb.clean_color(bad), bad)
        self.assertEqual(cb.safe_color("red;x"), cb.DEFAULT_COLOR)      # у style — лише HEX

    def test_icon(self):
        self.assertEqual(cb.clean_icon(" 📡 "), "📡")
        self.assertEqual(cb.clean_icon("🛰️"), "🛰️")
        self.assertEqual(cb.clean_icon(""), "")                          # без значка — можна
        for bad in ("A", "📡x", "<b>", "📡 📶", "1️⃣", "&", "📡📡📡📡📡📡📡📡📡", None):
            self.assertIsNone(cb.clean_icon(bad), bad)

    def test_text_color_contrast(self):
        self.assertEqual(cb.text_color("#212121"), "#ffffff")
        self.assertEqual(cb.text_color("#fdd835"), "#000000")
        for c in cb.COLORS:                                               # кожен готовий колір читабельний
            fg = cb.text_color(c)
            hi, lo = sorted((cb._luminance(c), cb._luminance(fg)), reverse=True)
            self.assertGreaterEqual((hi + 0.05) / (lo + 0.05), 4.5, c)

    def test_palettes_valid(self):
        icons = [e for _t, group in cb.ICON_GROUPS for e, _h in group]
        self.assertEqual(len(icons), len(set(icons)))                    # без повторів
        for e in icons:
            self.assertEqual(cb.clean_icon(e), e)
        self.assertEqual(len(cb.COLORS), len(set(cb.COLORS)))
        for c in cb.COLORS:
            self.assertEqual(cb.clean_color(c), c)

    def test_badge_escapes(self):
        html = str(cb.badge('<script>alert(1)</script>', "#fdd835", "📡"))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("📡", html)
        self.assertIn("background:#fdd835;color:#000000", html)
        self.assertIn('translate="no"', html)                            # назва — дані, не переклад
        self.assertIn("background:#607d8b", str(cb.badge("X", 'red" onclick="x', "")))
        self.assertIn("dashed", str(cb.unknown_badge("old-code")))
        self.assertEqual(cb.unknown_badge(""), "—")


class FormTests(TestCase):
    def data(self, **kw):
        return {"slug": "quarze", "name": "Quarze", "color": "#607d8b", "icon": "", "order": 0,
                "customs_hs_code": "", "customs_description_de": "", "customs_country_of_origin": "DE", **kw}

    def test_normalizes_and_rejects(self):
        f = ProductCategoryAdminForm(self.data(color="FDD835", icon=" 💎 "))
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["color"], "#fdd835")
        self.assertEqual(f.cleaned_data["icon"], "💎")
        f = ProductCategoryAdminForm(self.data(color="yellow", icon="Q"))
        self.assertFalse(f.is_valid())
        self.assertIn("color", f.errors)
        self.assertIn("icon", f.errors)


class AdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.cat = ProductCategory.objects.create(slug="antenna", name="Антени", color="#1e88e5", icon="📡", order=1)
        ProductCategory.objects.create(slug="quarze", name="Quarze", color="#fdd835", icon="", order=2)
        Product.objects.create(sku="AN-1", name="Antenna", category="antenna")
        Product.objects.create(sku="Q-1", name="Quartz", category="quarze")
        Product.objects.create(sku="OLD-1", name="Old", category="legacy")

    def test_card_has_palettes_and_preview(self):
        r = self.client.get(f"/admin/inventory/productcategory/{self.cat.pk}/change/")
        self.assertContains(r, 'data-pcb="color"')
        self.assertContains(r, 'data-pcb="icon"')
        self.assertContains(r, 'class="pcb-sw" data-v="#fdd835"')
        self.assertContains(r, 'data-v="💎"')                             # кварци
        self.assertContains(r, 'id="pcb-preview"')
        self.assertContains(r, "AN220213-01C")                           # рядок списку в «Як виглядатиме»
        self.assertContains(r, 'value="📡"')
        r = self.client.get("/admin/inventory/productcategory/add/")
        self.assertContains(r, 'id="pcb-preview"')

    def test_card_saves_icon_and_color(self):
        url = f"/admin/inventory/productcategory/{self.cat.pk}/change/"
        r = self.client.post(url, {"slug": "antenna", "name": "Антени", "color": "43A047", "icon": "📶", "order": 1,
                                   "customs_hs_code": "", "customs_description_de": "",
                                   "customs_country_of_origin": "DE"})
        self.assertEqual(r.status_code, 302)
        self.cat.refresh_from_db()
        self.assertEqual((self.cat.color, self.cat.icon), ("#43a047", "📶"))
        r = self.client.post(url, {"slug": "antenna", "name": "Антени", "color": "#43a047", "icon": "<i>", "order": 1,
                                   "customs_country_of_origin": "DE"})
        self.assertEqual(r.status_code, 200)                              # помилка у формі, не збережено
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.icon, "📶")

    def test_category_list(self):
        r = self.client.get("/admin/inventory/productcategory/")
        self.assertContains(r, "📡")
        self.assertContains(r, "background:#1e88e5;color:#000000")

    def test_product_list_badges_and_filter(self):
        r = self.client.get("/admin/inventory/product/")
        self.assertContains(r, "background:#1e88e5")
        self.assertContains(r, "<span aria-hidden=\"true\">📡</span>Антени", html=False)
        self.assertContains(r, "mv-cat-unknown")                          # код поза довідником — пунктиром
        filt = r.context["cl"].filter_specs[0]
        labels = [c["display"] for c in filt.choices(r.context["cl"])]
        self.assertEqual(labels[1:], ["📡 Антени", "Quarze", "legacy"])   # порядок довідника, потім невідомі
        r = self.client.get("/admin/inventory/product/?category=antenna")
        self.assertEqual([p.sku for p in r.context["cl"].result_list], ["AN-1"])

    def test_product_form_select_labels(self):
        p = Product.objects.get(sku="AN-1")
        r = self.client.get(f"/admin/inventory/product/{p.pk}/change/")
        self.assertContains(r, '<option value="antenna" selected>📡 Антени</option>', html=True)

    def test_other_lists(self):
        r = self.client.get("/admin/inventory/reorderproxy/")
        self.assertContains(r, "📡")
        r = self.client.get("/admin/shop/shopproduct/")
        self.assertContains(r, "background:#1e88e5")
        r = self.client.get("/admin/inventory/reorderproxy/supply/")
        self.assertContains(r, 'id="sv-cats-data"')
        self.assertEqual(r.context["cats"]["antenna"]["label"], "📡 Антени")

    def test_side_panel_and_api(self):
        from api.serializers import CategorySerializer
        from inventory.side_panel import _category_name
        self.assertEqual(_category_name(Product.objects.get(sku="AN-1")), "📡 Антени")
        self.assertEqual(_category_name(Product.objects.get(sku="OLD-1")), "legacy")
        self.assertEqual(CategorySerializer(self.cat).data["icon"], "📡")
        self.assertEqual(self.cat.label, "📡 Антени")
