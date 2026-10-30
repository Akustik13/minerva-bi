"""🌐 Переклади інтерфейсу (core/ui_text): збір написів, словник, AI-переклад (Claude підмінено), сторінки.

    python manage.py test core.tests_ui_text --settings=tabele.settings_test
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import UIText, UserProfile
from core.ui_text import catalog, extract, translate
from core.ui_text.text import edges


class CacheReset(TestCase):
    def setUp(self):
        from core.shared_cache import cache
        cache.clear()                                   # хід перекладу й версія словника — у спільному кеші
        super().setUp()


class ExtractTests(TestCase):
    def test_template_text_vars_and_edges(self):
        found = extract.from_template(
            '<h3>💾 Зберегти</h3><p title="Підказка тут">Видно {{ n }} з {{ total }}:</p>'
            '{% if x %}<b>Сховано</b>{% endif %}<script type="application/json">{"a": "Дані користувача"}</script>'
            '<script>alert("Не вдалося зберегти"); var s = \'Рядок \' + n + \' готово\';</script>')
        for phrase in ("Зберегти", "Підказка тут", "Видно {0} з {1}", "Сховано", "Не вдалося зберегти", "Рядок",
                       "готово"):
            self.assertIn(phrase, found)
        self.assertNotIn("Дані користувача", found)                     # json_script — дані, не інтерфейс

    def test_python_literals_fstrings_skip_docstrings_and_logs(self):
        src = ('"""Модуль-опис."""\nimport logging\nlogger = logging.getLogger()\n'
               'def f(n):\n    """Документація."""\n    logger.warning("Лог для розробника %s", n)\n'
               '    msg = f"Збережено {n} товарів"\n    return format_html("<b>{}</b> — замовлень", n), "Готово"\n')
        found = extract.from_python(src)
        self.assertIn("Збережено {0} товарів", found)
        self.assertIn("замовлень", found)
        self.assertIn("Готово", found)
        for gone in ("Модуль-опис.", "Документація.", "Лог для розробника {0}"):
            self.assertNotIn(gone, found)

    def test_edges_same_as_browser(self):
        self.assertEqual(edges(" 💾 Зберегти: "), (" 💾 ", "Зберегти", ": "))
        self.assertEqual(edges("✓ Готово!")[1], "Готово!")
        self.assertEqual(edges("— Видно  10 —")[1], "Видно 10")


class CatalogTests(CacheReset):
    def _tree(self, html):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "app" / "templates").mkdir(parents=True)
        (Path(tmp.name) / "app" / "templates" / "page.html").write_text(html, encoding="utf-8")
        return tmp.name

    def test_sync_keeps_translations_and_marks_gone(self):
        base = self._tree("<b>Зберегти</b><i>Скасувати</i>")
        self.assertEqual(catalog.sync(base)["new"], 2)
        row = UIText.objects.get(source="Зберегти")
        catalog.set_text(row, "en", "Save", by="human")
        Path(base, "app", "templates", "page.html").write_text("<b>Зберегти</b>", encoding="utf-8")
        res = catalog.sync(base)
        self.assertEqual((res["new"], res["gone"]), (0, 1))
        self.assertFalse(UIText.objects.get(source="Скасувати").active)
        self.assertEqual(UIText.objects.get(source="Зберегти").text("en"), "Save")

    def test_dictionary_exact_patterns_and_version(self):
        a = UIText.objects.create(key=catalog.key_of("Зберегти"), source="Зберегти")
        b = UIText.objects.create(key=catalog.key_of("Видно {0} з {1}"), source="Видно {0} з {1}")
        UIText.objects.create(key=catalog.key_of("Старе"), source="Старе", active=False,
                              translations={"en": {"text": "Old", "by": "ai"}})
        self.assertIsNone(catalog.page_info("en"))                     # перекладів ще немає — нічого не вантажимо
        v1 = catalog.version()
        catalog.set_text(a, "en", "Save")
        catalog.set_text(b, "en", "Visible {0} of {1}")
        self.assertNotEqual(catalog.version(), v1)
        d = catalog.dictionary("en")
        self.assertEqual(d["exact"], {"Зберегти": "Save"})                # неактивні — ні
        self.assertEqual(d["patterns"], [["Видно {0} з {1}", "Visible {0} of {1}"]])
        self.assertIsNotNone(catalog.page_info("en"))
        self.assertIsNone(catalog.page_info("ru"))                     # російська не перекладається
        self.assertTrue(catalog.as_js("en").startswith("window.MV_UI_DICT="))


def fake_client(reply):
    def create(**kw):
        items = json.loads(kw["messages"][0]["content"])["items"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"items": reply(items)}))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=200, cache_read_input_tokens=0))
    return SimpleNamespace(messages=SimpleNamespace(create=create))


class TranslateTests(CacheReset):
    def setUp(self):
        super().setUp()
        for src in ("Зберегти", "Видно {0} з {1}", "Скасувати"):
            UIText.objects.create(key=catalog.key_of(src), source=src)
        catalog.set_text(UIText.objects.get(source="Скасувати"), "en", "Abort (mine)", by="human")

    def test_estimate_and_validation(self):
        rows, langs = translate.todo()
        self.assertEqual((len(rows), langs), (3, ["en", "de"]))
        est = translate.estimate(rows, langs, "claude-haiku-5-5")
        self.assertGreater(est["tokens_out"], est["tokens_in"] / 10)
        self.assertLessEqual(est["usd"], translate.estimate(rows, langs, "claude-sonnet-5-5")["usd"])
        self.assertTrue(translate.valid("Видно {0} з {1}", "Visible {1} of {0}"))
        self.assertFalse(translate.valid("Видно {0} з {1}", "Visible {0}"))
        self.assertFalse(translate.valid("Зберегти", "  "))

    def test_run_saves_checks_placeholders_keeps_human(self):
        def reply(items):
            out = []
            for it in items:
                t = {"Зберегти": ("Save", "Speichern"), "Видно {0} з {1}": ("Visible {0}", "Sichtbar {0} von {1}"),
                     "Скасувати": ("Cancel", "Abbrechen")}[it["uk"]]
                out.append({"id": it["id"], "en": t[0], "de": t[1]})
            return out
        with mock.patch.object(translate, "_client", return_value=fake_client(reply)):
            res = translate.run(model="claude-haiku-5-5", who="test")
        self.assertEqual(res["status"], "done")
        self.assertEqual(UIText.objects.get(source="Зберегти").text("de"), "Speichern")
        row = UIText.objects.get(source="Видно {0} з {1}")
        self.assertEqual((row.text("en"), row.text("de")), ("", "Sichtbar {0} von {1}"))   # en без {1} — відхилено
        self.assertEqual(res["rejected"], 1)
        cancel = UIText.objects.get(source="Скасувати")
        self.assertEqual((cancel.text("en"), cancel.text("de")), ("Abort (mine)", "Abbrechen"))   # людське — лишилось
        self.assertEqual(translate.state()["status"], "done")

    def test_no_key_stops_cleanly(self):
        with mock.patch.object(translate, "_client", side_effect=RuntimeError("no_api_key")):
            res = translate.run()
        self.assertEqual(res["status"], "failed")
        self.assertIn("ключ", res["problem"])


class PagesTests(CacheReset):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.row = UIText.objects.create(key=catalog.key_of("Зберегти"), source="Зберегти")

    def test_dictionary_endpoint(self):
        catalog.set_text(self.row, "en", "Save")
        r = self.client.get("/core/ui-dict/en.js")
        self.assertIn("null", r.content.decode())                       # без входу — нічого
        self.client.force_login(self.root)
        r = self.client.get(f"/core/ui-dict/en.js?v={catalog.version()}")
        self.assertIn('"Зберегти":"Save"', r.content.decode())
        self.assertIn("immutable", r["Cache-Control"])
        self.assertIn("no-cache", self.client.get("/core/ui-dict/en.js?v=old")["Cache-Control"])

    def test_page_loads_dictionary_only_for_translated_language(self):
        self.client.force_login(self.root)
        self.assertNotIn("/core/ui-dict/", self.client.get("/admin/").content.decode())
        catalog.set_text(self.row, "en", "Save")
        UserProfile.objects.filter(user=self.root).update(interface_language="en")
        html = self.client.get("/admin/").content.decode()
        self.assertIn("/core/ui-dict/en.js?v=", html)
        self.assertIn("mv_translate.js", html)
        UserProfile.objects.filter(user=self.root).update(interface_language="ru")
        self.assertNotIn("/core/ui-dict/", self.client.get("/admin/").content.decode())

    def test_admin_page_estimate_translate_and_edit(self):
        self.client.force_login(self.root)
        r = self.client.get("/admin/core/uitext/")
        self.assertContains(r, "🤖 Перекласти 1 написів")
        self.assertContains(r, "Sonnet 5.5")
        reply = lambda items: [{"id": it["id"], "en": "Save", "de": "Speichern"} for it in items]   # noqa: E731
        with mock.patch.object(translate, "BACKGROUND", False), \
                mock.patch.object(translate, "_client", return_value=fake_client(reply)):
            self.client.post("/admin/core/uitext/translate/", {"model": "claude-haiku-5-5"})
        self.assertEqual(UIText.objects.get(pk=self.row.pk).text("de"), "Speichern")
        r = self.client.post(f"/admin/core/uitext/{self.row.pk}/change/", {"text_en": "Store", "text_de": "Speichern"})
        self.assertEqual(r.status_code, 302)
        row = UIText.objects.get(pk=self.row.pk)
        self.assertEqual((row.text("en"), row.translations["en"]["by"]), ("Store", "human"))

    def test_only_minerva_admin(self):
        u = User.objects.create_user("olga", "o@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role="manager")
        self.client.force_login(u)
        self.assertIn(self.client.get("/admin/core/uitext/").status_code, (302, 403))
        self.assertEqual(self.client.post("/admin/core/uitext/translate/").status_code in (302, 403), True)
        self.assertEqual(translate.state(), {})                         # нічого не запущено
