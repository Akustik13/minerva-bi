"""🧭 Меню на ПК у новому вигляді (core/main_menu.py, static/admin/js/mv_desktop.js): вибір людини, перевірка сторінок,
значки (і на телефоні), перемикач «Новий / Класичний».

    python manage.py test core.tests_main_menu --settings=tabele.settings_test

Вручну в браузері (не автоматизовано):
- теми dark / light / minerva / hc-dark / hc-light / custom × «Просте» / «Повне» × «Бокове меню» само / повне / значки
  при 1440, 1280 і 1024 px; «Розмір тексту» 125 % і 150 % (підменю й підказка біля свого значка);
- лише клавіатурою: Tab по групах, у ряду значків Enter / → — підменю з фокусом на першому пункті, ↑ ↓ Home End,
  ← / Esc — назад на значок, ↑ ↓ уздовж ряду; підказка одразу при фокусі;
- перетягування ⠿ і правий клік «⬆ Групу вище / ⬇ нижче», «↺ Порядок груп як був»;
- телефон 375 px і поворот iPad (1024 ↔ 768): шухляда меню як була; /email/ (меню без таблиць Django);
- людина лише з правом перегляду (без ＋); панель праворуч у списку при «Само звужується»;
- «📍 Показати» з гіда на пункт «Повного» з «Простого» і з ряду значків.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

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

    def test_groups_carry_id_and_icon(self):
        cfg = cfg_of(self.client.get("/admin/").content.decode(), "mv-menu-config")
        self.assertEqual([g["id"] for g in cfg["groups"]], ["sales", "stock", "projects", "comms", "more"])
        self.assertEqual({g["id"]: g["icon"] for g in cfg["groups"]},
                         {"sales": "🛒", "stock": "📦", "projects": "📁", "comms": "💬", "more": "⋯"})
        self.assertTrue(all(g["title"] and g["keys"] for g in cfg["groups"]))
        self.assertEqual(cfg["maxIcons"], main_menu.MAX_ICONS)
        projects = next(g for g in cfg["groups"] if g["id"] == "projects")
        self.assertIn("meetings", projects["keys"])                 # 🎙 Наради — у групі «Проєкти», не своєю групою
        self.assertNotIn("meetings", [s["key"] for s in cfg.get("search", [])])   # наради приватні — не в пошуку

    def test_expand_is_cleaned(self):
        self.assertEqual(main_menu.prefs(User.objects.get(pk=self.root.pk))["expand"], "remember")   # за замовчуванням
        cfg = cfg_of(self.client.get("/admin/").content.decode(), "mv-menu-config")
        self.assertEqual(cfg["prefs"]["expand"], "remember")
        self.assertEqual(self.post({"expand": "current"}).json()["prefs"]["expand"], "current")
        self.assertEqual(self.post({"expand": "evil"}).json()["prefs"]["expand"], "current")       # чуже — ні
        UserProfile.objects.filter(user=self.root).update(ui_prefs={"menu": {"expand": "<script>"}})
        self.assertEqual(main_menu.prefs(User.objects.get(pk=self.root.pk))["expand"], "remember")
        cfg = cfg_of(self.client.get("/admin/").content.decode(), "mv-menu-config")
        self.assertEqual(cfg["prefs"]["expand"], "remember")                               # у сторінку — лише відоме

    def test_icon_cap(self):
        icons = {f"url:/admin/x{i}/": "⭐" for i in range(main_menu.MAX_ICONS)}
        p = self.post({"icons": icons}).json()["prefs"]
        self.assertEqual(len(p["icons"]), main_menu.MAX_ICONS)
        p = self.post({"icons": {"grp:analytics": "📊"}}).json()["prefs"]            # 61-й ключ — не береться
        self.assertNotIn("grp:analytics", p["icons"])
        self.assertEqual(len(p["icons"]), main_menu.MAX_ICONS)
        p = self.post({"icons": {"url:/admin/x0/": "🔥"}}).json()["prefs"]           # наявний — можна змінити
        self.assertEqual(p["icons"]["url:/admin/x0/"], "🔥")
        p = self.post({"icons": {"url:/admin/x0/": None, "grp:analytics": "📊"}}).json()["prefs"]
        self.assertNotIn("url:/admin/x0/", p["icons"])                               # звільнили місце — новий є
        self.assertEqual(p["icons"]["grp:analytics"], "📊")
        self.assertEqual(len(p["icons"]), main_menu.MAX_ICONS)
        stored = {f"url:/admin/y{i}/": "⭐" for i in range(main_menu.MAX_ICONS + 5)}  # записано в обхід save
        UserProfile.objects.filter(user=self.root).update(ui_prefs={"menu": {"icons": stored}})
        self.assertEqual(len(main_menu.prefs(User.objects.get(pk=self.root.pk))["icons"]), main_menu.MAX_ICONS)

    def test_group_icon_round_trips(self):
        p = self.post({"icons": {"grp:analytics": "📊", "sg:stock": "🏭", "url:/admin/inventory/supplier/": "🏭",
                                 "grp:<b>": "⭐", "sg:evil": "⭐", "nope": "⭐", "url:javascript:x": "⭐"}}).json()["prefs"]
        self.assertEqual(p["icons"], {"grp:analytics": "📊", "sg:stock": "🏭", "url:/admin/inventory/supplier/": "🏭"})
        cfg = cfg_of(self.client.get("/admin/").content.decode(), "mv-menu-config")
        self.assertEqual(cfg["prefs"]["icons"]["grp:analytics"], "📊")
        self.assertEqual(cfg["prefs"]["icons"]["sg:stock"], "🏭")
        p = self.post({"icons": {"grp:analytics": None}}).json()["prefs"]               # «↺ Значок як був»
        self.assertNotIn("grp:analytics", p["icons"])
        self.assertTrue(main_menu.icon_key_ok("grp:digikey"))
        self.assertFalse(main_menu.icon_key_ok("grp:Ab c"))
        self.assertFalse(main_menu.icon_key_ok(None))

    def test_menu_save_keeps_other_ui_prefs(self):
        other = {"theme": "light", "sidebar": "rail", "panels": {"*": {"nav": "side"}}, "mobile_nav": ["home"]}
        UserProfile.objects.filter(user=self.root).update(ui_prefs=dict(other))
        self.post({"mode": "full", "expand": "current", "icons": {"grp:crm": "🎯"}})
        ui = UserProfile.objects.get(user=self.root).ui_prefs
        self.assertEqual({k: ui[k] for k in other}, other)
        self.assertEqual((ui["menu"]["mode"], ui["menu"]["expand"]), ("full", "current"))
        self.post({"reset": True})
        ui = UserProfile.objects.get(user=self.root).ui_prefs
        self.assertNotIn("menu", ui)
        self.assertEqual({k: ui[k] for k in other}, other)

    def test_menu_save_rereads_profile_row(self):
        """Застарілий профіль у пам'яті (вкладка відкрита давно) не затирає те, що тим часом записала панель."""
        u = User.objects.get(pk=self.root.pk)
        self.assertEqual(u.profile.ui_prefs or {}, {})                     # профіль уже в пам'яті
        UserProfile.objects.filter(user=self.root).update(ui_prefs={"panels": {"sales.salesorder": {"on": True}}})
        main_menu.save(u, {"mode": "full"})
        ui = UserProfile.objects.get(user=self.root).ui_prefs
        self.assertEqual(ui["panels"], {"sales.salesorder": {"on": True}})
        self.assertEqual(ui["menu"]["mode"], "full")
        self.assertEqual(u.profile.ui_prefs, ui)                            # і об'єкт у пам'яті — як у базі

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


STATIC = Path(settings.BASE_DIR) / "static" / "admin"
DESKTOP_JS = STATIC / "js" / "mv_desktop.js"
DESKTOP_CSS = STATIC / "css" / "mv_desktop.css"
SHOW_JS = STATIC / "js" / "mv_show.js"


def _css_rules(css):
    """[(селектор, тіло, у @media (min-width: 769px))] без коментарів; вкладені @media розгортаються."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i = [], 0

    def walk(text, wide):
        j = 0
        while j < len(text):
            start = text.find("{", j)
            if start < 0:
                return
            head = text[j:start].strip()
            depth, k = 1, start + 1
            while k < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            body = text[start + 1:k - 1]
            if head.startswith("@media"):
                walk(body, wide or "min-width: 769px" in head)
            elif not head.startswith("@"):
                out.append((head, body, wide))
            j = k

    walk(css, False)
    return out


class DesktopMenuAssetsTests(SimpleTestCase):
    """Статичні запобіжники mv_desktop.js / .css (поведінку перевіряє людина в браузері — див. нижче)."""

    def setUp(self):
        self.js = DESKTOP_JS.read_text(encoding="utf-8")
        self.css = DESKTOP_CSS.read_text(encoding="utf-8")

    def test_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node не встановлено")
        for f in (DESKTOP_JS, SHOW_JS):
            run = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace")
            self.assertEqual(run.returncode, 0, f"{f.name}: {run.stderr}")

    def test_public_api_and_panel_event(self):
        for name in ("reveal: reveal", "railOn: railOn", "sideWidth: sideWidth", "palette: openPalette"):
            self.assertIn(name, self.js)
        self.assertIn("document.addEventListener('mv:panel'", self.js)      # панель праворуч звужує меню
        self.assertIn("MVDesktop.reveal(el)", SHOW_JS.read_text(encoding="utf-8"))   # mv_show.js — перед підсвічуванням

    def test_menus_through_mvmenu_and_one_unread_fetch(self):
        self.assertIn("window.MVMenu.open(", self.js)
        self.assertNotIn("setAttribute('role', 'menu')", self.js)          # свої меню — лише через MVMenu
        self.assertNotIn("/email/unread-count/", self.js)                  # лічильник «Вхідні» бере base_site

    def test_css_content_has_no_words(self):
        """У content: — лише attr(), емодзі, цифри й «＋»: слова в CSS не перекладаються."""
        for head, body, _wide in _css_rules(self.css):
            for value in re.findall(r"content:\s*([^;]+)", body):
                for literal in re.findall(r"'([^']*)'|\"([^\"]*)\"", value):
                    text = "".join(literal)
                    self.assertFalse(re.search(r"[^\W\d_]", text), f"{head}: content {value!r}")

    def test_sidebar_rules_only_in_new_look_on_desktop(self):
        """Бокове меню на телефоні (шухляда mv_mobile) і в класичному вигляді — як було."""
        for head, body, wide in _css_rules(self.css):
            for sel in (s.strip() for s in head.split(",")):
                if not re.search(r"#nav-sidebar|mvd-rail|\.mg-|#mvd-snav|#mvd-foot|#mvd-top", sel):
                    continue
                if not wide:                                               # поза ПК — лише «сховати»
                    self.assertRegex(body.strip(), r"^display:\s*(none|contents);?$", sel)
                    continue
                self.assertTrue(sel.startswith('html[data-mv-design="new"]') or sel.startswith("@"),
                                f"{sel} — без html[data-mv-design=\"new\"]")

    def test_layout_fixes(self):
        self.assertIn('html[data-mv-design="new"] .main > #nav-sidebar + .content { max-width: calc(100% - var(--mvd-side-w)); }',
                      self.css)                                            # без мертвих 24 px Django
        self.assertIn('html[data-mv-design="new"][data-mv-links="underline"] #nav-sidebar a:not(.addlink):not(.mvd-add)',
                      self.css)                                            # «Підкреслювати посилання» — і в меню
        self.assertRegex(self.css, r"\.mvd-fbtn \{[^}]*order: 9")
        self.assertIn(":has(> #mvp-toggle) > .mvd-fbtn", self.css)         # «📌 Панель» — перед «⚙ Фільтри»
        for var in ("--mvd-side-w: 268px", "--mvd-rail-w: 64px", "--mvd-side-w: var(--mvd-rail-w)"):
            self.assertIn(var, self.css)
