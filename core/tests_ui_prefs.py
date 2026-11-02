"""Тема й доступність (core/ui_prefs.py): збереження в профілі, атрибути <html> до першого малювання, скрипт контрасту.

    python manage.py test core.tests_ui_prefs --settings=tabele.settings_test
"""
import json
import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from config.models import SystemSettings
from core import ui_prefs
from core.models import UserProfile

User = get_user_model()


def _onboarded():
    ss = SystemSettings.objects.get_or_create(pk=1)[0]
    ss.is_onboarding_complete = True
    ss.save()


def _page_ui(html: str) -> dict:
    """JSON, який base_site.html підставляє в скрипт <head>."""
    m = re.search(r"var ui = (\{.*?\}), root = document", html)
    return json.loads(m.group(1))


class CleanTests(TestCase):
    def test_only_known_values(self):
        self.assertEqual(ui_prefs.clean({
            "theme": "hc-dark", "font": "readable", "text_size": 125, "motion": "reduce",
            "focus": "neon", "links": "<b>", "evil": "x", "fix_contrast": "0"}),
            {"theme": "hc-dark", "font": "readable", "text_size": "125", "motion": "reduce", "fix_contrast": "0"})
        self.assertEqual(ui_prefs.clean("theme=dark"), {})
        self.assertEqual(ui_prefs.clean({"theme": "</script><script>alert(1)</script>"}), {})

    def test_defaults_and_legacy_theme(self):
        self.assertEqual(ui_prefs.prefs_of(None)["fix_contrast"], "1")
        cfg = json.loads(ui_prefs.page_config(None))
        self.assertEqual(cfg["theme"], "")
        self.assertEqual(cfg["attrs"]["data-mv-fix-contrast"], "1")
        self.assertEqual(cfg["attrs"]["data-mv-size"], "100")


class SettingsPageTests(TestCase):
    def setUp(self):
        _onboarded()
        self.user = User.objects.create_user("olga", "o@x.y", "p", is_staff=True)
        self.profile = UserProfile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def post(self, data):
        return self.client.post("/core/my-settings/", data=json.dumps(data), content_type="application/json")

    def test_page_lists_contrast_themes_and_options(self):
        page = self.client.get("/core/my-settings/")
        self.assertContains(page, 'data-theme="hc-dark"')
        self.assertContains(page, 'data-theme="hc-light"')
        self.assertContains(page, "♿ Доступність")
        for key in ui_prefs.OPTIONS:
            self.assertContains(page, f'id="ui-{key}"')

    def test_saved_prefs_follow_to_other_pages(self):
        r = self.post({"ui": {"theme": "hc-light", "font": "readable", "text_size": "125",
                              "links": "underline", "focus": "always", "motion": "reduce", "fix_contrast": "0"}})
        self.assertEqual(r.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.theme, "hc-light")            # і старе поле — для інших місць Minerva
        self.assertEqual(self.profile.ui_prefs["font"], "readable")
        ui = _page_ui(self.client.get("/admin/").content.decode())
        self.assertEqual(ui["theme"], "hc-light")
        self.assertEqual(ui["attrs"], {
            "data-mv-design": "new",                                 # вигляд ПК — за замовчуванням новий
            "data-mv-sidebar": "auto",                               # бокове меню — само звужується до значків
            "data-mv-fix-contrast": "0", "data-mv-font": "readable", "data-mv-size": "125",
            "data-mv-focus": "always", "data-mv-links": "underline", "data-mv-motion": "reduce"})

    def test_sidebar_rail_saved_and_rendered(self):
        page = self.client.get("/core/my-settings/")
        self.assertContains(page, 'id="ui-sidebar"')                  # «📐 Бокове меню на ПК» у «♿ Доступність»
        self.assertContains(page, 'data-attr="data-mv-sidebar"')
        self.assertEqual(self.post({"ui": {"sidebar": "rail"}}).status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.ui_prefs, {"sidebar": "rail"})
        self.assertEqual(_page_ui(self.client.get("/admin/").content.decode())["attrs"]["data-mv-sidebar"], "rail")
        self.post({"ui": {"sidebar": "wide"}})                         # ⇤ / ⇥ унизу меню
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.ui_prefs["sidebar"], "wide")

    def test_sidebar_evil_ignored(self):
        self.assertEqual(ui_prefs.clean({"sidebar": "evil"}), {})
        self.assertEqual(ui_prefs.clean({"sidebar": "rail"}), {"sidebar": "rail"})
        self.post({"ui": {"sidebar": "evil"}})
        self.profile.refresh_from_db()
        self.assertNotIn("sidebar", self.profile.ui_prefs or {})
        self.assertEqual(_page_ui(self.client.get("/admin/").content.decode())["attrs"]["data-mv-sidebar"], "auto")

    def test_page_loads_menu_component_and_rail_antiflash(self):
        html = self.client.get("/admin/").content.decode()
        self.assertIn('/static/admin/js/mv_menu.js?v=1', html)
        self.assertIn('/static/admin/css/mv_menu.css?v=1', html)
        self.assertIn("root.classList.add('mvd-rail')", html)          # значки ще до першого малювання
        self.assertIn("attrs['data-mv-sidebar']", html)
        self.assertIn('/static/admin/js/mv_desktop.js?v=3', html)
        self.assertIn('/static/admin/css/mv_desktop.css?v=3', html)
        self.assertIn('/static/admin/js/mv_show.js?v=2', html)
        # спільне меню — раніше за панель праворуч і за бокове меню (обидва ним користуються)
        self.assertLess(html.index('/static/admin/js/mv_menu.js'), html.index('/static/admin/js/mv_desktop.js'))
        head = html.split("</head>")[0]
        self.assertLess(head.index("root.classList.add('mvd-rail')"), head.index('/static/admin/css/mv_desktop.css'))

    def test_save_rereads_profile_row(self):
        """Застарілий профіль у пам'яті не затирає те, що тим часом записали панель і меню (select_for_update)."""
        stale = UserProfile.objects.get(pk=self.profile.pk)
        UserProfile.objects.filter(pk=self.profile.pk).update(
            ui_prefs={"panels": {"*": {"nav": "top"}}, "menu": {"mode": "full"}})
        self.assertEqual(ui_prefs.save(stale, {"sidebar": "rail", "theme": "light"}), ["ui_prefs", "theme"])
        fresh = UserProfile.objects.get(pk=self.profile.pk)
        self.assertEqual(fresh.ui_prefs, {"panels": {"*": {"nav": "top"}}, "menu": {"mode": "full"},
                                          "sidebar": "rail", "theme": "light"})
        self.assertEqual(fresh.theme, "light")
        self.assertEqual((stale.ui_prefs, stale.theme), (fresh.ui_prefs, "light"))   # об'єкт — як у базі
        self.assertEqual(ui_prefs.save(stale, {"sidebar": "rail"}), [])              # нічого нового — без запису

    def test_sidebar_switcher_saves_theme_only(self):
        self.post({"ui": {"font": "readable"}})
        self.post({"ui": {"theme": "dark"}})                           # перемикач у меню шле лише тему
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.ui_prefs, {"font": "readable", "theme": "dark"})

    def test_bad_values_ignored(self):
        self.post({"ui": {"theme": "pink", "text_size": "999", "font": "<script>", "sidebar": "evil"}})
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.ui_prefs, self.profile.theme), ({}, "dark"))

    def test_tampered_profile_is_not_written_into_page(self):
        UserProfile.objects.filter(pk=self.profile.pk).update(
            ui_prefs={"theme": "</script><script>alert(1)</script>", "font": "x\"};alert(1);//"})
        html = self.client.get("/admin/").content.decode()
        self.assertNotIn("alert(1)", html)
        self.assertEqual(_page_ui(html)["theme"], "")

    def test_without_choice_browser_theme_is_used(self):
        """Тема ще не вибрана в «Моїх налаштуваннях» — як у цьому браузері (localStorage), далі — старе поле."""
        ui = _page_ui(self.client.get("/admin/").content.decode())
        self.assertEqual((ui["theme"], ui["legacy"]), ("", "dark"))

    def test_contrast_script_on_every_admin_page(self):
        html = self.client.get("/admin/").content.decode()
        self.assertIn("/static/admin/js/mv_contrast.js", html)
        self.assertIn('[data-theme="hc-dark"]', html)
        self.assertIn('[data-theme="hc-light"]', html)
