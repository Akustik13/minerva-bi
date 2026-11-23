"""Класифікація коробок (клас, назва, код) і наклейка DYMO коробки.

    python manage.py test shipping.tests_box_names --settings=tabele.settings_test
"""
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import Product
from shipping.models import PackagingMaterial, ProductPackaging
from shipping.services import box_names


def box(name, l, w, h, **kw):
    return PackagingMaterial.objects.create(name=name, length_cm=l, width_cm=w, height_cm=h, **kw)


class ClassTests(TestCase):
    def test_auto_class_by_volume(self):
        for dims, cls in (((23, 16, 2), "XS"), ((24, 16, 5), "S"), ((30, 20, 10), "M"), ((31, 22, 15), "M"),
                          ((30, 30, 30), "L"), ((45, 45, 22), "XL")):
            self.assertEqual(box_names.auto_class(PackagingMaterial(length_cm=dims[0], width_cm=dims[1],
                                                                    height_cm=dims[2])), cls, dims)

    def test_plan_names_and_free_codes(self):
        a = box("Smoll", 23, 16, 2)
        b = box("Medium", 31, 22, 15)
        c = box("Mietel", 30, 20, 10)
        cube = box("Cube", 30, 30, 30, fold_heights="22, 16", code="BX-L-07")
        box("Other", 33, 22, 14, code="BX-M-01")                     # номер уже зайнятий
        rows = {r["material"].pk: r for r in box_names.plan([a, b, c, cube])}
        self.assertEqual((rows[a.pk]["name"], rows[a.pk]["code"]), ("XS 23×16×2", "BX-XS-01"))
        self.assertEqual(rows[c.pk]["code"], "BX-M-02")               # менша коробка класу — менший номер
        self.assertEqual(rows[b.pk]["code"], "BX-M-03")
        self.assertEqual((rows[cube.pk]["name"], rows[cube.pk]["code"], rows[cube.pk]["new_code"]),
                         ("L 30×30×30 ↧22/16", "BX-L-07", False))   # наявний код не змінюється

    def test_apply_validates_and_renumbers(self):
        a = box("A", 30, 20, 10)
        b = box("B", 31, 22, 15, code="BX-L-01")
        done = box_names.apply([{"material": a, "cls": "L", "name": "L 30×20×10", "code": "bx-l-01",
                                 "new_code": True}])                  # клас змінили на сторінці — зайнятий номер
        self.assertEqual(PackagingMaterial.objects.get(pk=a.pk).code, "BX-L-02")
        self.assertEqual(PackagingMaterial.objects.get(pk=a.pk).size_class, "L")
        self.assertEqual(len(done), 1)
        with self.assertRaises(box_names.NamingError):
            box_names.apply([{"material": a, "code": "BX-L-01"}])        # людина вписала чужий код
        with self.assertRaises(box_names.NamingError):
            box_names.apply([{"material": a, "code": "Коробка 1"}])
        self.assertEqual(PackagingMaterial.objects.get(pk=b.pk).code, "BX-L-01")


class AdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True))
        self.a = box("Smoll 23x16x2cm", 23, 16, 2)
        self.b = box("Medium v2", 41, 31, 15)

    def test_classify_action(self):
        url = "/admin/shipping/packagingmaterial/"
        ids = [self.a.pk, self.b.pk]
        page = self.client.post(url, {"action": "classify_selected", "index": 0, "_selected_action": ids})
        self.assertContains(page, "BX-XS-01")
        self.assertContains(page, "L 41×31×15")
        r = self.client.post(url, {"action": "classify_selected", "_selected_action": ids, "apply": "1",
                                   f"on_{self.a.pk}": "1", f"cls_{self.a.pk}": "", f"name_{self.a.pk}": "XS 23×16×2",
                                   f"code_{self.a.pk}": "BX-XS-01",
                                   f"cls_{self.b.pk}": "XL", f"name_{self.b.pk}": "x", f"code_{self.b.pk}": "y"})
        self.assertEqual(r.status_code, 302)
        a, b = PackagingMaterial.objects.get(pk=self.a.pk), PackagingMaterial.objects.get(pk=self.b.pk)
        self.assertEqual((a.name, a.code, a.size_class), ("XS 23×16×2", "BX-XS-01", ""))
        self.assertEqual((b.name, b.code), ("Medium v2", ""))              # галочку знято — не чіпали
        lst = self.client.get(url)
        self.assertContains(lst, 'data-svl="box"')
        self.assertContains(lst, "BX-XS-01")
        self.assertContains(lst, "pkg-print-all")

    def test_card_has_label_button(self):
        card = self.client.get(f"/admin/shipping/packagingmaterial/{self.a.pk}/change/")
        self.assertContains(card, 'data-svl="box"')
        self.assertContains(card, "window.svLabel")


class BoxLabelTests(TestCase):
    def setUp(self):
        self.cube = box("L 30×30×30", 30, 30, 30, fold_heights="22, 16", code="BX-L-01",
                        tare_weight_kg=Decimal("0.25"), max_weight_kg=Decimal("10"))
        for n, (sku, qty) in enumerate((("CA-SMA-1", 10), ("AN220213-01C", 4), ("CA-SMA-1", 6))):
            p = Product.objects.get_or_create(sku=sku, defaults={"name": sku})[0]
            ProductPackaging.objects.create(product=p, packaging=self.cube, qty_per_box=qty, is_default=n == 0)

    def test_default_label_has_no_contents_block(self):
        from labels_app import box_label
        data = box_label.build(self.cube)
        self.assertEqual(data["lines"], [])
        xml = box_label.render(data)
        self.assertNotIn("ITextObject3", xml)
        self.assertNotIn("Fits:", xml)
        root = ET.fromstring(xml.encode("utf-8"))
        self.assertIn("L 30×30×30", [t.text for t in root.iter("Text")])

    def test_build_and_render(self):
        from labels_app import box_label
        data = box_label.build(self.cube, contents_line=True)
        self.assertEqual(data["code"], "BX-L-01")
        self.assertEqual(data["items"], [("CA-SMA-1", 10), ("AN220213-01C", 4)])       # найбільша місткість
        self.assertIn("30×30×30 cm · ↧ 22/16 · 27 L", data["size"])
        self.assertIn("tare 0.25 kg", data["info"])
        self.assertEqual(data["lines"], ["Fits: CA-SMA-1 × 10 · AN220213-01C × 4"])
        xml = box_label.render(data)
        self.assertNotIn("<!--", xml)                                  # DYMO не приймає коментарів
        root = ET.fromstring(xml.encode("utf-8"))
        self.assertIn("BX-L-01", [d.text for d in root.iter("DataString")])
        texts = [t.text for t in root.iter("Text")]
        self.assertIn("L 30×30×30", texts)

    def test_wrap_limits_lines(self):
        from labels_app import box_label
        items = [(f"AN2202{n:02d}-01C", 5) for n in range(40)]
        lines = box_label.wrap(items, max_lines=3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith(" more"))
        self.assertTrue(all(len(x) <= box_label.LINE_CHARS for x in lines))
        shown = sum(x.count(" × ") for x in lines)
        self.assertIn(f"+{40 - shown} more", lines[-1])

    def test_without_code_or_products(self):
        from labels_app import box_label
        bare = box("Bare", 20, 10, 5, notes="Для кабелів\nдругий рядок")
        data = box_label.build(bare, contents_line=True)
        self.assertEqual(data["code"], f"PKG-{bare.pk:04d}")
        self.assertEqual(data["lines"], ["Для кабелів"])
        self.assertEqual(box_label.build(bare)["lines"], [])

    def test_view_and_permission(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True))
        r = self.client.get(f"/labels/box/{self.cube.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('Box-BX-L-01.dymo', r["Content-Disposition"])
        self.assertIn(b"MinervaMark", r.content)
        self.assertNotIn("Fits:", r.content.decode("utf-8"))
        self.assertIn("Fits:", self.client.get(f"/labels/box/{self.cube.pk}/?contents=1").content.decode("utf-8"))
        clerk = User.objects.create_user("clerk", password="x", is_staff=True)
        clerk.profile.role = "readonly"
        clerk.profile.allowed_modules = ["crm"]
        clerk.profile.save()
        self.client.force_login(User.objects.get(pk=clerk.pk))
        self.assertEqual(self.client.get(f"/labels/box/{self.cube.pk}/").status_code, 403)
