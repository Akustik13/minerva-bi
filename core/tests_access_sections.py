"""«👁 Як бачить Minerva»: розділи, яких людина ще не має, видно в меню редактора («➕ Ще не відкрито») з їхніми
сторінками й назвою в бічному меню — DigiKey живе в модулі «AI та Боти» (bots), а в меню це група «🛒 DigiKey».

python manage.py test core.tests_access_sections --settings=tabele.settings_test
"""
from core import access_preview
from core.models import ModuleRegistry
from core.tests_access_editor import EditorBase


class SectionsTests(EditorBase):
    def setUp(self):
        super().setUp()
        ModuleRegistry.objects.update_or_create(app_label="bots", defaults={"name": "AI та Боти", "is_active": True})

    def test_module_rows_say_where_digikey_is(self):
        bots = next(m for m in access_preview.module_rows(self.wh) if m["app"] == "bots")
        self.assertIn("🛒 DigiKey", bots["place"])
        self.assertTrue(any("DigiKey" in p for p in bots["pages"]), bots["pages"])
        self.assertFalse(bots["allowed"])                                      # складник не має модуля

    def test_closed_sections_in_preview(self):
        data = access_preview.preview(self.wh)
        closed = {m["app"] for m in data["closed"]}
        self.assertIn("bots", closed)
        self.assertIn("sales", closed)
        self.assertNotIn("inventory", closed)                                  # уже має
        self.assertNotIn("core", closed)                                       # завжди доступне
        self.assertTrue(any("🛒 DigiKey" in s for s in data["overview"]["hidden_sections"]))
        # відкрили в чернетці — розділ переходить у меню з тим самим підписом
        data = access_preview.preview(self.wh, {"modules": {"bots": "work"}})
        self.assertNotIn("bots", {m["app"] for m in data["closed"]})
        bots = next(a for a in data["menu"] if a["app"] == "bots")
        self.assertIn("🛒 DigiKey", bots["place"])

    def test_page_shows_closed_sections_with_switch_and_search(self):
        self.client.force_login(self.root)
        r = self.client.get(self.url, {"edit": "1"})
        self.assertContains(r, 'id="ap-find"')
        self.assertContains(r, "➕ Ще не відкрито")
        html = r.content.decode()
        closed = html[html.index("➕ Ще не відкрито"):]
        self.assertIn('data-ap-level="work" data-app="bots"', closed)
        self.assertIn("у меню: 🛒 DigiKey", closed)
        r = self.client.get(self.url)                                          # без редактора — лише перелік
        self.assertContains(r, "🚫 Розділів немає в її доступі")
        self.assertNotContains(r, 'data-ap-level="work" data-app="bots"')
