"""📱 Нижня панель телефона (core/mobile_nav.py, static/admin/js/mv_mobile.js): розділи за правами, вибір людини.

    python manage.py test core.tests_mobile_nav --settings=tabele.settings_test
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import resolve

from core import mobile_nav, ui_prefs
from core.models import UserProfile


def page_cfg(html):
    return json.loads(html.split('id="mv-mobile-config" type="application/json">')[1].split("</script>")[0])


class MobileNavTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)

    def test_every_item_is_a_real_address(self):
        for key, (_icon, _label, href, _perm) in mobile_nav.ITEMS.items():
            resolve(href)                                           # 404 → Resolver404
        self.assertLessEqual(len(mobile_nav.DEFAULT), mobile_nav.MAX_PINNED)

    def test_page_gets_config_and_scripts(self):
        html = self.client.get("/admin/").content.decode()
        self.assertIn("/static/admin/js/mv_mobile.js", html)
        self.assertIn("/static/admin/css/mv_mobile.css", html)
        cfg = page_cfg(html)
        self.assertEqual(cfg["pinned"], ["home", "orders", "stock", "mail"])
        self.assertEqual(cfg["saveUrl"], "/core/mobile-nav/")
        self.assertEqual({i["key"] for i in cfg["items"]}, set(mobile_nav.ITEMS))

    def test_save_cleans_and_resets(self):
        r = self.client.post("/core/mobile-nav/", json.dumps({"pinned": ["calendar", "evil", "calendar", "mail",
                                                                          "orders", "stock", "home"]}),
                             content_type="application/json")
        self.assertEqual(r.json()["pinned"], ["calendar", "mail", "orders", "stock"])       # без чужого й повторів, до 4
        self.assertEqual(page_cfg(self.client.get("/admin/").content.decode())["pinned"],
                         ["calendar", "mail", "orders", "stock"])
        r = self.client.post("/core/mobile-nav/", json.dumps({"pinned": []}), content_type="application/json")
        self.assertEqual(r.json()["pinned"], [])                                            # лише «☰ Ще»
        r = self.client.post("/core/mobile-nav/", json.dumps({"pinned": None}), content_type="application/json")
        self.assertEqual(r.json()["pinned"], mobile_nav.DEFAULT)                            # «↺ Як було»
        self.assertNotIn("mobile_nav", UserProfile.objects.get(user=self.root).ui_prefs)
        self.assertEqual(self.client.post("/core/mobile-nav/", "[1]", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get("/core/mobile-nav/").status_code, 405)

    def test_only_sections_the_person_can_open(self):
        u = User.objects.create_user("clerk", "c@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role="readonly", allowed_modules=["dashboard"])
        u = User.objects.get(pk=u.pk)
        keys = mobile_nav.available(u)
        self.assertIn("home", keys)
        self.assertNotIn("orders", keys)
        self.assertNotIn("stock", keys)
        self.assertNotIn("orders", mobile_nav.pinned(u))
        self.client.force_login(u)
        r = self.client.post("/core/mobile-nav/", json.dumps({"pinned": ["orders", "home"]}),
                             content_type="application/json")
        self.assertEqual(r.json()["pinned"], ["home"])                                      # недоступне не зберігається
        self.assertNotIn('"orders"', self.client.get("/admin/").content.decode().split("mv-mobile-config")[1][:3000])

    def test_not_staff_gets_nothing(self):
        u = User.objects.create_user("guest", "g@x.y", "p")
        self.assertEqual(mobile_nav.available(u), [])
        self.client.logout()
        self.assertIn(self.client.post("/core/mobile-nav/", "{}", content_type="application/json").status_code,
                      (302, 403))

    def test_theme_save_keeps_panels_and_bottom_bar(self):
        p = UserProfile.objects.get(user=self.root)
        p.ui_prefs = {"panels": {"sales.salesorder": {"on": True}}, "mobile_nav": ["home"]}
        p.save(update_fields=["ui_prefs"])
        ui_prefs.save(p, {"theme": "light", "font": "readable"})
        p.refresh_from_db()
        self.assertEqual(p.ui_prefs["panels"], {"sales.salesorder": {"on": True}})
        self.assertEqual(p.ui_prefs["mobile_nav"], ["home"])
        self.assertEqual(p.ui_prefs["theme"], "light")
