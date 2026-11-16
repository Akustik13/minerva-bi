"""🌐 Переклади інтерфейсу (core/ui_text): збір написів, словник, AI-переклад (Claude підмінено), сторінки.

    python manage.py test core.tests_ui_text --settings=tabele.settings_test
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import UIText, UserProfile
from core.ui_text import bundled, catalog, extract, translate
from core.ui_text.text import edges

REAL_BUNDLE = bundled.PATH
NO_BUNDLE = Path(tempfile.gettempdir()) / "minerva-tests-no-bundle.jsonl"     # файлу немає — комплект порожній
CYR = re.compile("[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]")


class CacheReset(TestCase):
    def setUp(self):
        from core.shared_cache import cache
        cache.clear()                                   # хід перекладу й версія словника — у спільному кеші
        patcher = mock.patch.object(bundled, "PATH", NO_BUNDLE)       # словник — лише з того, що дає тест
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_html_attributes_in_js_strings(self):
        found = extract.from_template("""<script>var h = '<button title="Сьогодні — посередині">⌖ сьогодні</button>' +
          '<input placeholder="Назва параметру" autocomplete="off">';</script>""")
        self.assertIn("Сьогодні — посередині", found)                # підказка кнопки, зібраної в JS
        self.assertIn("Назва параметру", found)
        self.assertIn("сьогодні", found)
        self.assertFalse([f for f in found if "title=" in f or "placeholder=" in f])

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


class BundledTests(CacheReset):
    """Переклади в комплекті (core/ui_text/bundled.jsonl): заповнюють лише порожнє, без AI."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "bundled.jsonl"
        bundled.write({"Зберегти": {"en": "Save", "de": "Speichern"},
                       "Видно {0} з {1}": {"en": "Visible {0} of {1}", "de": "Sichtbar {0}"},     # бракує {1}
                       "Скасувати": {"en": "Cancel", "de": "Abbrechen"}}, self.path)

    def test_write_is_sorted_and_merges(self):
        bundled.write({"Архів": {"en": "Archive"}, "Зберегти": {"de": "Sichern"}}, self.path)
        lines = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["uk"] for l in lines], ["Архів", "Видно {0} з {1}", "Зберегти", "Скасувати"])
        self.assertEqual(lines[2], {"uk": "Зберегти", "en": "Save", "de": "Sichern"})

    def test_apply_fills_only_missing_and_valid(self):
        save = UIText.objects.create(key=catalog.key_of("Зберегти"), source="Зберегти",
                                     translations={"en": {"text": "Store", "by": "human"}})
        seen = UIText.objects.create(key=catalog.key_of("Видно {0} з {1}"), source="Видно {0} з {1}")
        gone = UIText.objects.create(key=catalog.key_of("Скасувати"), source="Скасувати", active=False)
        v1 = catalog.version()
        self.assertEqual(bundled.apply(self.path), 2)                  # de «Зберегти» + en «Видно…»
        save.refresh_from_db()
        self.assertEqual((save.text("en"), save.translations["en"]["by"]), ("Store", "human"))   # людське — як було
        self.assertEqual((save.text("de"), save.translations["de"]["model"]), ("Speichern", bundled.MODEL))
        seen.refresh_from_db()
        self.assertEqual((seen.text("en"), seen.text("de")), ("Visible {0} of {1}", ""))   # без {1} — ні
        gone.refresh_from_db()
        self.assertEqual(gone.translations, {})                        # неактивні — ні
        self.assertNotEqual(catalog.version(), v1)
        self.assertEqual(bundled.apply(self.path), 0)                  # удруге — нічого

    def test_sync_applies_bundle_and_reports_missing(self):
        tree = self.dir / "code" / "app" / "templates"
        tree.mkdir(parents=True)
        (tree / "page.html").write_text("<b>Зберегти</b><i>Новий напис</i>", encoding="utf-8")
        res = catalog.sync(self.dir / "code", bundled_path=self.path)
        self.assertEqual((res["new"], res["bundled"]), (2, 2))
        self.assertEqual(UIText.objects.get(source="Зберегти").text("de"), "Speichern")
        self.assertEqual(catalog.dictionary("en")["exact"], {"Зберегти": "Save"})
        found = extract.collect(self.dir / "code")
        self.assertEqual(bundled.missing(found, self.path), ["Новий напис"])

    def test_real_bundle_is_valid(self):
        data = bundled.load(REAL_BUNDLE)
        if not data:
            self.skipTest("bundled.jsonl ще немає")
        bad = [src for src, langs in data.items()
               if set(langs) != set(catalog.TARGETS) or not all(translate.valid(src, t) for t in langs.values())]
        self.assertEqual(bad[:10], [])

    def test_apply_updates_earlier_bundle_translations_only(self):
        """У комплекті виправили переклад («Прийняти» → не «Receive», а «Accept») — каталог його отримує; людське й
        «🤖 Перекласти» лишаються."""
        bundled.write({"Прийняти": {"en": "Accept", "de": "Übernehmen"}}, self.path)
        old = {"text": "Receive", "by": "ai", "model": bundled.MODEL}
        mine = UIText.objects.create(key=catalog.key_of("Прийняти"), source="Прийняти", translations={"en": old})
        self.assertEqual(bundled.apply(self.path), 2)                  # en оновлено, de дописано
        mine.refresh_from_db()
        self.assertEqual((mine.text("en"), mine.text("de")), ("Accept", "Übernehmen"))
        for by, model in (("human", ""), ("ai", "claude-haiku-5-5")):
            UIText.objects.filter(pk=mine.pk).update(
                translations={"en": {"text": "Take", "by": by, "model": model}})
            bundled.apply(self.path)
            mine.refresh_from_db()
            self.assertEqual(mine.text("en"), "Take")


class DictionaryBundleTests(CacheReset):
    """Словник для браузера бере комплект і сам: код оновили, а «🔄 Зібрати» ще не було — фрази цілі одразу."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "bundled.jsonl"
        bundled.write({"Куди додати": {"en": "Where to add", "de": "Wohin hinzufügen"},
                       "на шкалі проєкту": {"en": "on the project timeline", "de": "auf der Projekt-Zeitleiste"},
                       "Прийняти": {"en": "Accept", "de": "Übernehmen"},
                       "Зберегти": {"en": "Save", "de": "Speichern"},
                       "Видно {0} з {1}": {"en": "Visible {0} of {1}", "de": "Sichtbar {0} von {1}"},
                       "Зламано {0}": {"en": "Broken", "de": "Kaputt {0}"}}, self.path)
        patcher = mock.patch.object(bundled, "PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bundle_fills_what_the_catalog_lacks(self):
        UIText.objects.create(key=catalog.key_of("Прийняти"), source="Прийняти",
                              translations={"en": {"text": "Receive", "by": "ai", "model": bundled.MODEL}})
        UIText.objects.create(key=catalog.key_of("Зберегти"), source="Зберегти",
                              translations={"en": {"text": "Store", "by": "human"}})
        d = catalog.dictionary("en")
        self.assertEqual(d["exact"]["Куди додати"], "Where to add")          # каталог ще не зібрав — з комплекту
        self.assertEqual(d["exact"]["на шкалі проєкту"], "on the project timeline")
        self.assertEqual(d["exact"]["Прийняти"], "Accept")                   # старий переклад комплекту — новий
        self.assertEqual(d["exact"]["Зберегти"], "Store")                    # людське — головніше
        self.assertIn(["Видно {0} з {1}", "Visible {0} of {1}"], d["patterns"])
        self.assertNotIn("Зламано {0}", dict(d["patterns"]))                 # без {0} у перекладі — ні
        self.assertIsNotNone(catalog.page_info("de"))

    def test_version_follows_bundle_file(self):
        v1 = catalog.version()
        bundled.write({"Назва": {"en": "Name", "de": "Name"}}, self.path)
        self.assertNotEqual(catalog.version(), v1)
        self.assertEqual(catalog.dictionary("en")["exact"]["Назва"], "Name")


JS = Path(settings.BASE_DIR) / "static" / "admin" / "js" / "mv_translate.js"
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const [jsPath, inPath] = process.argv.slice(2);
const ctx = { document: { documentElement: { classList: { remove() {} } } } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(jsPath, 'utf8'), ctx);
const input = JSON.parse(fs.readFileSync(inPath, 'utf8'));
const tr = ctx.MVTranslateKit.make(input.dict);
const out = input.texts.map((t, i) => {
  const key = input.drop ? input.drop[i] : null;          // «каталог ще без цієї фрази»
  const saved = key === null ? undefined : input.dict.exact[key];
  if (saved !== undefined) delete input.dict.exact[key];
  const r = tr(t);
  if (saved !== undefined) input.dict.exact[key] = saved;
  return r;
});
process.stdout.write(JSON.stringify(out));
"""


class BrowserTranslateTests(SimpleTestCase):
    """static/admin/js/mv_translate.js (через node): фраза перекладається цілою або лишається українською —
    окремих слів посеред українського речення не міняє (16.11.2026: «— for шкалі проєкту», «Step плану»)."""

    DICT = {"exact": {"Прийняти": "Accept", "Назва": "Name", "Куди додати": "Where to add", "Клієнт": "Customer",
                      "Строк": "Lead time", "Склад": "Stock", "Резерв": "Reserved", "Товар": "Product",
                      "Товари": "Products", "Зберегти": "Save", "Сторінок": "Pages", "зараз": "now", "на": "on",
                      "чекають": "waiting", "Надіслано": "Sent", "Номер замовлення": "Order number",
                      "Отримати": "Get"},
            "patterns": [["Крок «{0}» прострочено: мав закінчитися {1}", "Step “{0}” is overdue: it was due {1}"],
                         ["Номер замовлення: {0}", "Order number: {0}"],
                         ["для клієнта {0}", "for customer {0}"], ["Колонка «{0}»", "Column “{0}”"],
                         ["Видно {0} з {1}", "Visible {0} of {1}"], ["{0} без змін", "{0} unchanged"],
                         ["чекають {0}", "{0} waiting"], ["Крок {0}", "Step {0}"], ["на {0}", "for {0}"],
                         ["з {0}", "of {0}"], ["у {0}", "in {0}"], ["{0} з {1}", "{0} of {1}"]]}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node = shutil.which("node")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.harness = Path(cls.tmp.name) / "harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def tr(self, texts, dictionary=None, drop=False):
        if not self.node:
            self.skipTest("node не встановлено")
        data = {"dict": dictionary or self.DICT, "texts": list(texts),
                "drop": [edges(t)[1] for t in texts] if drop else None}
        inp = Path(self.tmp.name) / "in.json"
        inp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        run = subprocess.run([self.node, str(self.harness), str(JS), str(inp)], capture_output=True, text=True,
                             encoding="utf-8", timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        return dict(zip(texts, json.loads(run.stdout)))

    def test_short_patterns_do_not_swap_words_inside_sentences(self):
        out = self.tr([" — на шкалі проєкту (бачать усі в проєкті)", " — у реєстр ризиків проєкту",
                       " — з виконавцем і строком, з’явиться в календарі виконавця", " чекають ✓ / ✕",
                       "📅 Крок плану", "Надіслано без змін", "Ризик з Петром", "Номер замовлення: 1234567 або A-1"])
        self.assertEqual(out, {k: None for k in out})                        # лишаються українською

    def test_values_still_fill_patterns(self):
        out = self.tr(["Крок 3", "на 5", "Видно 10 з 20", "✓ Прийняти ▾", "Крок «Монтаж антени» прострочено: "
                       "мав закінчитися 01.12.2026", "Колонка «Зберегти»", "для клієнта Іван Петренко",
                       "Номер замовлення: 1234567"])
        self.assertEqual(out, {"Крок 3": "Step 3", "на 5": "for 5", "Видно 10 з 20": "Visible 10 of 20",
                               "✓ Прийняти ▾": "✓ Accept ▾",
                               "Крок «Монтаж антени» прострочено: мав закінчитися 01.12.2026":
                                   "Step “Монтаж антени” is overdue: it was due 01.12.2026",   # у лапках — дані
                               "Колонка «Зберегти»": "Column “Save”",                     # назва з інтерфейсу
                               "для клієнта Іван Петренко": "for customer Іван Петренко",
                               "Номер замовлення: 1234567": "Order number: 1234567"})

    def test_parts_label_with_value_but_not_half_a_sentence(self):
        out = self.tr(["Клієнт: ТОВ Антена", "Склад: 5 · Резерв: 2", "Крок 2 — Отримати Chat ID",
                       "Строк — ціле число днів", "Назва · щось нове", "Клієнт: Склад Резерв"])
        self.assertEqual(out, {"Клієнт: ТОВ Антена": "Customer: ТОВ Антена", "Склад: 5 · Резерв: 2":
                               "Stock: 5 · Reserved: 2", "Крок 2 — Отримати Chat ID": "Step 2 — Get Chat ID",
                               "Строк — ціле число днів": None, "Назва · щось нове": None,
                               "Клієнт: Склад Резерв": None})                # слова зі словника — не дані

    def test_word_endings_and_adjacent_values(self):
        d = {"exact": {"Назва": "Name", "Таймаут": "Timeout"},
             "patterns": [["{0} зон{1}", "{0} zone(s){1}"], ["{0}{1} кг", "{0}{1} kg"], ["{0} днів", "{0} days"]]}
        out = self.tr(["3 зони", "Назва зони", "Брутто, кг", "5 днів", "Таймаут 7 днів"], d)
        self.assertEqual(out, {"3 зони": "3 zone(s)",                        # закінчення «и» — геть, не «zone(s)и»
                               "Назва зони": None, "Брутто, кг": None,        # не «Name zone(s)», не «Брутто, kg»
                               "5 днів": "5 days", "Таймаут 7 днів": None})

    def test_fragments_only_all_together(self):
        out = self.tr(["Select Товар to change", "Add Товар", "5 Товари", "Зберегти (2)", "Сторінок: 12",
                       "Надіслати на DigiKey зараз", "Товар / Товари", "Товар / Послуга"])
        self.assertEqual(out, {"Select Товар to change": "Select Product to change", "Add Товар": "Add Product",
                               "5 Товари": "5 Products", "Зберегти (2)": "Save (2)", "Сторінок: 12": "Pages: 12",
                               "Надіслати на DigiKey зараз": None, "Товар / Товари": "Product / Products",
                               "Товар / Послуга": None})

    def _real(self, lang):
        data = bundled.load(REAL_BUNDLE)
        if not data:
            self.skipTest("bundled.jsonl ще немає")
        exact, patterns = {}, []
        for src, langs in data.items():
            if langs.get(lang):
                (patterns.append([src, langs[lang]]) if "{0}" in src else exact.__setitem__(src, langs[lang]))
        patterns.sort(key=lambda p: -len(p[0]))                               # як catalog.dictionary
        return {"exact": exact, "patterns": patterns}

    MEETING = ["✓ Прийняти ▾", " — на шкалі проєкту (бачать усі в проєкті)", " — у реєстр ризиків проєкту",
               " — з виконавцем і строком, з’явиться в календарі виконавця", " — бачите лише ви", " чекають ✓ / ✕",
               "📌 Важливий момент", "⚠️ Ризик", "📅 Крок плану", "⏰ Дедлайн у моєму календарі", "Куди додати",
               "Назва", "Виконавець", "✓ Додати", "Скасувати"]

    def test_meeting_page_with_real_bundle(self):
        for lang, want in (("en", {"✓ Прийняти ▾": "✓ Accept ▾", "Куди додати": "Where to add",
                                   " — на шкалі проєкту (бачать усі в проєкті)":
                                       " — on the project timeline (everyone in the project sees it)",
                                   "📅 Крок плану": "📅 Plan step", "Виконавець": "Assignee"}),
                           ("de", {"✓ Прийняти ▾": "✓ Übernehmen ▾", "Виконавець": "Bearbeiter"})):
            d = self._real(lang)
            out = self.tr(self.MEETING, d)
            for text, got in out.items():
                self.assertIsNotNone(got, text)                               # кожна фраза — ціла
                self.assertIsNone(CYR.search(got), got)
            for text, got in want.items():
                self.assertEqual(out[text], got)
            stale = self.tr(self.MEETING, d, drop=True)                       # каталог без цих фраз
            for text, got in stale.items():
                self.assertTrue(got is None or not CYR.search(got), f"{text} → {got}")

    def test_real_bundle_without_own_phrase_is_not_mixed(self):
        """Будь-яка фраза комплекту, якої в словнику немає: лишається українською або перекладена вся (до
        16.11.2026 так виходила мішанина в третини фраз)."""
        d = self._real("en")
        texts = sorted(src for src in d["exact"] if CYR.search(src))
        out = self.tr(texts, d, drop=True)
        mixed = [f"{t} → {o}" for t, o in out.items() if o is not None and CYR.search(o)]
        self.assertLessEqual(len(mixed), len(texts) // 1000, mixed[:20])


class AutoSyncTests(CacheReset):
    """Код оновили без перезапуску контейнера («🚀 Повне оновлення»: git pull + reload) — каталог збирається сам,
    раз на зміну коду; кнопка оновлення має крок «ui_texts collect»."""
    def setUp(self):
        super().setUp()
        catalog._stamp.update(at=0.0, value="")
        self.addCleanup(catalog._stamp.update, at=0.0, value="")

    def test_sync_once_per_code_change(self):
        with override_settings(UI_TEXT_AUTOSYNC=True), mock.patch.object(catalog, "BACKGROUND", False), \
                mock.patch.object(catalog, "code_stamp", return_value="v1") as stamp, \
                mock.patch.object(catalog, "sync", return_value={}) as sync:
            self.assertTrue(catalog.ensure_synced())
            self.assertFalse(catalog.ensure_synced())                   # той самий код — удруге ні
            stamp.return_value = "v2"
            self.assertTrue(catalog.ensure_synced())
        self.assertEqual(sync.call_count, 2)

    def test_off_in_tests_and_failure_waits(self):
        with mock.patch.object(catalog, "sync") as sync:
            self.assertFalse(catalog.ensure_synced())                   # settings_test: UI_TEXT_AUTOSYNC = False
        sync.assert_not_called()
        with override_settings(UI_TEXT_AUTOSYNC=True), mock.patch.object(catalog, "BACKGROUND", False), \
                mock.patch.object(catalog, "code_stamp", return_value="v1"), \
                mock.patch.object(catalog, "sync", side_effect=RuntimeError("db")) as sync:
            self.assertTrue(catalog.ensure_synced())                    # збій — у журнал
            self.assertFalse(catalog.ensure_synced())                   # повтор — коли мине SYNC_LOCK_SECONDS
        self.assertEqual(sync.call_count, 1)

    def test_page_starts_it(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        with mock.patch.object(catalog, "ensure_synced", return_value=False) as ensure:
            self.client.get("/admin/")
        ensure.assert_called()

    def test_code_stamp_follows_git_head_and_bundle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = Path(tmp.name) / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text("abc123\n", encoding="utf-8")
        self.assertEqual(catalog._git_head(tmp.name), "abc123")
        (git / "refs" / "heads" / "main").unlink()
        (git / "packed-refs").write_text("# pack-refs\ndef456 refs/heads/main\n", encoding="utf-8")
        self.assertEqual(catalog._git_head(tmp.name), "def456")
        self.assertEqual(catalog._git_head(Path(tmp.name) / "nowhere"), "")
        path = Path(tmp.name) / "bundled.jsonl"
        path.write_text("", encoding="utf-8")
        with mock.patch.object(bundled, "PATH", path), override_settings(BASE_DIR=Path(tmp.name)):
            first = catalog.code_stamp(fresh=True)
            self.assertTrue(first.endswith("|def456"))
            path.write_text('{"uk": "Зберегти", "en": "Save"}\n', encoding="utf-8")
            self.assertEqual(catalog.code_stamp(), first)                # у процесі — не частіше STAMP_MEMO
            self.assertNotEqual(catalog.code_stamp(fresh=True), first)

    def test_full_update_has_translations_step(self):
        from django.conf import settings
        from django.urls import reverse
        html = (Path(settings.BASE_DIR) / "backup/templates/admin/backup/info.html").read_text(encoding="utf-8")
        self.assertIn("{ id: 'texts',   url: UI_TEXTS_URL", html)
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        url = reverse("admin:backup_ui_texts")
        self.assertEqual(self.client.get(url).status_code, 405)
        with mock.patch("backup.utils.run_ui_texts", return_value={"ok": True, "output": "Написів: 1"}) as run:
            self.assertEqual(self.client.post(url).json()["ok"], True)
        run.assert_called_once()
