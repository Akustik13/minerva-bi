"""Посилання на фото й даташити за правилом: шаблон, спільні файли, звіт перевірки, запис вибраних.
python manage.py test inventory --settings=tabele.settings_test"""
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from inventory.models import InventorySettings, MediaLinkCheck, Product, validate_url_template
from inventory.services import media_links as ml

DS = "https://files.example.com/datasheets/download.php?file={key}.pdf"
IMG = "https://files.example.com/pictures/download.php?file={key}.jpg"


def links(ok):
    """probe-двійник: посилання з ok відповідають, решта — HTTP 404."""
    return mock.patch.object(ml, "probe", side_effect=lambda url, kind: "" if url in ok else "сервер відповів HTTP 404")


class MediaLinksTests(TestCase):
    def setUp(self):
        s = InventorySettings.get()
        s.datasheet_url_template, s.image_url_template = DS, IMG
        s.save()
        self.a = Product.objects.create(sku="AN-1", name="A")                                    # порожньо → заповнити
        self.b = Product.objects.create(sku="AN-2", name="B", datasheet_url="https://old.example.com/AN-2.pdf")  # → замінити
        self.c = Product.objects.create(sku="AN-3", name="C", datasheet_url="https://old.example.com/AN-3.pdf",
                                        image_key="AN-1")                                       # спільне фото з AN-1
        patcher = mock.patch.object(ml, "_start_background", side_effect=lambda fn: fn())
        patcher.start()
        self.addCleanup(patcher.stop)

    def url(self, template, key):
        return template.replace("{key}", key)

    def test_template_validation_and_build(self):
        validate_url_template(DS)
        validate_url_template("https://files.example.com/download.php?file=")   # досить початку адреси
        with self.assertRaises(ValidationError):
            validate_url_template("files.example.com/{key}.pdf")
        s = InventorySettings.get()
        s.datasheet_url_template = " https://docs.example.com/datasheets/download.php?file= "
        s.image_url_template = "https://docs.example.com/pictures/{key}.png"
        s.save()
        s.refresh_from_db()
        self.assertEqual(s.datasheet_url_template, "https://docs.example.com/datasheets/download.php?file={key}.pdf")
        self.assertEqual(s.image_url_template, "https://docs.example.com/pictures/{key}.png")   # {key} є — як є
        s.datasheet_url_template, s.image_url_template = DS, IMG
        s.save()
        self.assertEqual(ml.build_url(DS, "AN 1"), self.url(DS, "AN%201"))
        self.assertEqual(ml.key_for(self.c, ml.IMAGE), "AN-1")
        self.assertEqual(ml.key_for(self.c, ml.DATASHEET), "AN-3")
        self.assertEqual(ml.example(ml.DATASHEET, self.a), self.url(DS, "AN-1"))

    def test_report_decisions_and_apply(self):
        ok = {self.url(DS, "AN-1"), self.url(DS, "AN-2"), "https://old.example.com/AN-3.pdf", self.url(IMG, "AN-1")}
        with links(ok) as probe:
            self.assertTrue(ml.start("all", by="adm"))
        rows = {(r["sku"], r["kind"]): r for r in MediaLinkCheck.objects.get(pk=1).rows}
        self.assertEqual((rows[("AN-1", "datasheet")]["action"], rows[("AN-1", "datasheet")]["selected"]), (ml.FILL, True))
        self.assertEqual((rows[("AN-2", "datasheet")]["action"], rows[("AN-2", "datasheet")]["selected"]), (ml.REPLACE, True))
        self.assertEqual((rows[("AN-3", "datasheet")]["action"], rows[("AN-3", "datasheet")]["selected"]), (ml.KEEP, False))
        self.assertEqual(rows[("AN-3", "image")]["new"], self.url(IMG, "AN-1"))   # спільне фото — те саме посилання
        self.assertEqual(rows[("AN-3", "image")]["shared"], 2)
        self.assertEqual(rows[("AN-2", "image")]["action"], ml.BROKEN)
        checked = [c.args[0] for c in probe.call_args_list]
        self.assertEqual(checked.count(self.url(IMG, "AN-1")), 1)                    # спільне посилання перевіряється раз
        self.assertEqual(ml.progress()["state"], "done")
        n, warnings = ml.apply([f"{self.a.pk}:datasheet", f"{self.b.pk}:datasheet", f"{self.c.pk}:image"])
        self.assertEqual((n, warnings), (3, []))
        self.a.refresh_from_db(), self.b.refresh_from_db(), self.c.refresh_from_db()
        self.assertEqual((self.a.datasheet_url, self.b.datasheet_url), (self.url(DS, "AN-1"), self.url(DS, "AN-2")))
        self.assertEqual((self.c.image_url, self.c.datasheet_url), (self.url(IMG, "AN-1"), "https://old.example.com/AN-3.pdf"))
        rows = {(r["sku"], r["kind"]): r for r in MediaLinkCheck.objects.get(pk=1).rows}
        self.assertEqual(rows[("AN-1", "datasheet")]["action"], ml.SAME)   # записане — «без змін»

    def test_without_checking_links(self):
        with links(set()) as probe:
            ml.run("selected", ids=[self.b.pk], kinds=[ml.DATASHEET], check_links=False)
        probe.assert_not_called()
        rows = MediaLinkCheck.objects.get(pk=1).rows
        self.assertEqual([(r["sku"], r["action"], r["selected"]) for r in rows], [("AN-2", ml.REPLACE, True)])

    def test_shared_file_for_selected(self):
        with links({self.url(IMG, "AN-2")}):
            result = ml.set_shared([self.a, self.b, self.c], [ml.IMAGE], "AN-2")
        self.assertEqual(result[ml.IMAGE], (self.url(IMG, "AN-2"), ""))
        keys = dict(Product.objects.values_list("sku", "image_key"))
        self.assertEqual(keys, {"AN-1": "AN-2", "AN-2": "", "AN-3": "AN-2"})      # у самого AN-2 — порожньо (свій артикул)
        self.assertEqual(set(Product.objects.values_list("image_url", flat=True)), {self.url(IMG, "AN-2")})
        self.assertEqual(ml.shared_groups()[ml.IMAGE], [("AN-2", ["AN-1", "AN-2", "AN-3"])])


class MediaLinksAdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        st = SystemSettings.objects.get_or_create(pk=1)[0]
        st.is_onboarding_complete = True
        st.save()
        s = InventorySettings.get()
        s.datasheet_url_template = DS
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.p = Product.objects.create(sku="AN-1", name="A")
        self.q = Product.objects.create(sku="AN-2", name="B")
        patcher = mock.patch.object(ml, "_start_background", side_effect=lambda fn: fn())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_page_and_flow(self):
        r = self.client.get("/admin/inventory/inventorysettings/1/change/")
        self.assertContains(r, "Правило: посилання на даташит")
        self.assertContains(r, "AN-1.pdf")                          # приклад для першого товару
        r = self.client.post("/admin/inventory/product/", {"action": "action_media_links", "_selected_action": [self.p.pk]})
        url = r["Location"]
        self.assertIn(f"ids={self.p.pk}", url)
        self.assertContains(self.client.get(url), "вибрані в списку товарів (1)")
        with links({DS.replace("{key}", "AN-1")}):
            self.client.post(url, {"scope": "selected", "kind": ["datasheet"], "check": "1", "ids": str(self.p.pk)})
        page = self.client.get(url)
        self.assertContains(page, "заповнити")
        self.assertContains(page, "✅ відповідає")
        self.client.post(url, {"apply": "1", "row": [f"{self.p.pk}:datasheet"], "ids": str(self.p.pk)})
        self.p.refresh_from_db()
        self.assertEqual(self.p.datasheet_url, DS.replace("{key}", "AN-1"))
        self.assertEqual(self.client.get("/admin/inventory/inventorysettings/media-links/status/").json()["state"], "done")

    def test_shared_media_action(self):
        url = "/admin/inventory/product/"
        sel = [self.p.pk, self.q.pk]
        r = self.client.post(url, {"action": "action_shared_media", "_selected_action": sel})
        self.assertContains(r, "Файл якого товару")
        with links({DS.replace("{key}", "AN-1")}):
            self.client.post(url, {"action": "action_shared_media", "apply": "1", "_selected_action": sel,
                                   "kinds": ["datasheet"], "key": "AN-1"})
        self.q.refresh_from_db()
        self.assertEqual((self.q.datasheet_key, self.q.datasheet_url), ("AN-1", DS.replace("{key}", "AN-1")))
