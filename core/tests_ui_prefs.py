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
            "data-mv-fix-contrast": "0", "data-mv-font": "readable", "data-mv-size": "125",
            "data-mv-focus": "always", "data-mv-links": "underline", "data-mv-motion": "reduce"})

    def test_sidebar_switcher_saves_theme_only(self):
        self.post({"ui": {"font": "readable"}})
        self.post({"ui": {"theme": "dark"}})                           # перемикач у меню шле лише тему
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.ui_prefs, {"font": "readable", "theme": "dark"})

    def test_bad_values_ignored(self):
        self.post({"ui": {"theme": "pink", "text_size": "999", "font": "<script>"}})
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
