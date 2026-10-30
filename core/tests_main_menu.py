"""🧭 Меню на ПК у новому вигляді (core/main_menu.py, static/admin/js/mv_desktop.js): вибір людини, перевірка сторінок,
значки (і на телефоні), перемикач «Новий / Класичний».

    python manage.py test core.tests_main_menu --settings=tabele.settings_test
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase

from core import main_menu, ui_prefs
from core.models import UserProfile


def cfg_of(html, el_id):
    return json.loads(html.split(f'id="{el_id}" type="application/json">')[1].split("</script>")[0])


class MainMenuTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)

    def post(self, data):
        return self.client.post("/core/menu/", json.dumps(data), content_type="application/json")

    def test_page_gets_config_scripts_and_new_design(self):
        html = self.client.get("/admin/").content.decode()
        self.assertIn("/static/admin/js/mv_desktop.js", html)
        self.assertIn('"data-mv-design": "new"', html)                    # новий вигляд — за замовчуванням
        cfg = cfg_of(html, "mv-menu-config")
        self.assertEqual(cfg["prefs"]["mode"], "simple")
        self.assertEqual(cfg["prefs"]["pinned"], main_menu.DEFAULT_PINNED)
        self.assertEqual(cfg["prefs"]["filters"], "drawer")
        self.assertIn({"key": "orders", "label": "Замовлення", "href": "/admin/sales/salesorder/?q="}, cfg["search"])
        self.assertTrue(all(g["keys"] for g in cfg["groups"]))

    def test_save_cleans_everything(self):
        r = self.post({"mode": "full", "filters": "side", "pinned": ["mail", "evil", "mail", "orders"],
                       "icons": {"orders": "🧾", "mail": "abc", "stock": "<b>"}})
        p = r.json()["prefs"]
        self.assertEqual((p["mode"], p["filters"], p["pinned"]), ("full", "side", ["mail", "orders"]))
        self.assertEqual(p["icons"], {"orders": "🧾"})                    # літери й розмітка — ні
        p = self.post({"icons": {"orders": None}, "mode": "nope"}).json()["prefs"]
        self.assertEqual((p["icons"], p["mode"]), ({}, "full"))
        p = self.post({"pinned": [k for k in main_menu.mobile_nav.ITEMS] * 2}).json()["prefs"]
        self.assertEqual(len(p["pinned"]), main_menu.MAX_PINNED)
        p = self.post({"reset": True}).json()["prefs"]
        self.assertEqual((p["mode"], p["pinned"]), ("simple", main_menu.DEFAULT_PINNED))
        self.assertNotIn("menu", UserProfile.objects.get(user=self.root).ui_prefs)
        self.assertEqual(self.client.post("/core/menu/", "[1]", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get("/core/menu/").status_code, 405)

    def test_pin_page_from_full_menu_only_real_and_open(self):
        r = self.post({"pin_link": {"href": "/admin/inventory/supplier/", "label": "  Постачальники  "}}).json()
        self.assertTrue(r["ok"])
        self.assertIn("url:/admin/inventory/supplier/", r["prefs"]["pinned"])
        self.assertEqual(r["prefs"]["links"]["url:/admin/inventory/supplier/"]["label"], "Постачальники")
        for bad in ("/admin/nema-takoi/", "//evil.example/x", "javascript:alert(1)", "https://evil.example/",
                    "/admin/x\" onclick=\"y"):
            r = self.post({"pin_link": {"href": bad, "label": "X"}}).json()
            self.assertFalse(r["ok"], bad)
            self.assertNotIn("url:" + bad, r["prefs"]["pinned"])
        p = self.post({"pinned": ["home"]}).json()["prefs"]                 # відкріпили — посилання забуто
        self.assertEqual(p["links"], {})

    def test_restricted_person(self):
        u = User.objects.create_user("clerk", "c@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role="readonly", allowed_modules=["dashboard"])
        self.client.force_login(u)
        r = self.post({"pinned": ["orders", "home"],
                       "pin_link": {"href": "/admin/sales/salesorder/", "label": "Замовлення"}}).json()
        self.assertFalse(r["ok"])
        self.assertEqual(r["prefs"]["pinned"], ["home"])
        cfg = cfg_of(self.client.get("/admin/").content.decode(), "mv-menu-config")
        self.assertNotIn("orders", [i["key"] for i in cfg["items"]])
        self.assertNotIn("orders", [k for g in cfg["groups"] for k in g["keys"]])
        self.assertNotIn("orders", [s["key"] for s in cfg["search"]])

    def test_own_icon_also_on_phone(self):
        self.post({"icons": {"stock": "🏭"}})
        cfg = cfg_of(self.client.get("/admin/").content.decode(), "mv-mobile-config")
        self.assertEqual([i["icon"] for i in cfg["items"] if i["key"] == "stock"], ["🏭"])

    def test_design_switch(self):
        p = UserProfile.objects.get(user=self.root)
        p.ui_prefs = {"menu": {"mode": "full"}}
        p.save(update_fields=["ui_prefs"])
        self.client.post("/core/my-settings/", json.dumps({"ui": {"design": "classic"}}), content_type="application/json")
        p.refresh_from_db()
        self.assertEqual(p.ui_prefs["design"], "classic")
        self.assertEqual(p.ui_prefs["menu"], {"mode": "full"})                 # інше лишилось
        self.assertIn('"data-mv-design": "classic"', self.client.get("/admin/").content.decode())
        self.assertEqual(ui_prefs.clean({"design": "evil"}), {})
