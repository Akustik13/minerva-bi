"""📌 Панель праворуч — сторінка списку: static/admin/js/mv_panel.js і static/admin/css/mv_panel.css.

Без браузера: статична перевірка домовленостей із сервером (core/side_panel.py, core/panel_edit.py, core/views.py),
спільним меню (static/admin/js/mv_menu.js, window.MVMenu) і бічним меню (static/admin/js/mv_desktop.js), плюс
сторінка списку замовлень, на якій скрипт і стилі панелі підключені.

    python manage.py test core.tests_panel_assets --settings=tabele.settings_test

Що перевірити в браузері (1440×900 і 1366×768; темна, світла, hc-light; текст 125 %):
- відкрити рядок, ↑ ↓ (наступний вантажиться наперед), фішки підменю й підсвічування під час прокрутки,
  «🗂 По одному», згортання розділів, розділи, що вантажаться, коли до них дійшли;
- статус з підтвердженням, ⭐ і «↶ Скасувати», 409 у двох вкладках;
- дія списку для одного запису повертає панель; оновлення сторінки лишає #mvp=; ↓ двічі — наступна сторінка;
- ширина перетягуванням, бокове меню звужується до значків (G), примітка про сховані колонки;
- помічники 🏛 і «Minerva» у низу панелі не налазять на кнопки;
- Esc закриває лише верхній шар (Ctrl K, фільтри, меню, редагування, налаштування, панель).
"""
import json
import logging
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils.html import format_html

from core import side_panel as sp
from inventory.admin import SupplierAdmin
from inventory.models import Supplier

STATIC = Path(settings.BASE_DIR) / "static" / "admin"
JS_PATH = STATIC / "js" / "mv_panel.js"
CSS_PATH = STATIC / "css" / "mv_panel.css"


def read(path):
    return path.read_text(encoding="utf-8")


def css_rules(css):
    """[(селектор, оголошення)] — без коментарів, з тілами @media."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        out.append((" ".join(sel.split()), body))
    return out


def cfg_of(html):
    return json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])


class PageTestBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)


class OrderListPageTests(PageTestBase):
    """Список замовлень: налаштування панелі, її скрипт і стилі v2; спільне меню — раніше за панель."""

    def test_order_list_includes_panel_assets(self):
        r = self.client.get("/admin/sales/salesorder/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="mv-panel-config"', html)
        self.assertIn('<script src="/static/admin/js/mv_panel.js?v=5" defer></script>', html)
        self.assertIn('<link rel="stylesheet" href="/static/admin/css/mv_panel.css?v=5">', html)
        self.assertEqual(html.count("mv_panel.js?v="), 1)
        # mv_menu.js (window.MVMenu) виконується раніше: обидва defer — у порядку на сторінці
        self.assertIn("/static/admin/js/mv_menu.js", html)
        self.assertLess(html.index("/static/admin/js/mv_menu.js"), html.index("/static/admin/js/mv_panel.js"))
        cfg = cfg_of(html)
        self.assertEqual((cfg["v"], cfg["model"]), (2, "sales.salesorder"))
        self.assertTrue(cfg["url"].startswith("/core/panel/sales.salesorder/"))

    def test_config_has_every_key_the_script_reads(self):
        cfg = cfg_of(self.client.get("/admin/sales/salesorder/").content.decode())
        used = set(re.findall(r"\bcfg\.([A-Za-z]+)", read(JS_PATH)))
        self.assertTrue(used >= {"url", "model", "prefs", "prefsUrl", "blocks", "actions", "edits", "adminActions",
                                 "columns", "hideDefault", "width", "page", "shared", "canDelete", "name"}, used)
        self.assertEqual(sorted(used - set(cfg)), [], "mv_panel.js reads keys that config() does not send")
        self.assertEqual(sorted(cfg["width"]), ["max", "min", "presets"])
        self.assertEqual(sorted(cfg["page"]), ["num", "pages", "param"])
        self.assertEqual(sorted(cfg["shared"]), sorted(sp.SHARED_DEFAULTS))
        for k in sp.SHARED_DEFAULTS:                      # вигляд для всіх списків — у вибраному людиною
            self.assertIn(k, cfg["prefs"])

    def test_popup_has_no_panel(self):
        html = self.client.get("/admin/sales/salesorder/?_popup=1").content.decode()
        self.assertNotIn("mv_panel.js", html)            # вікно вибору — без панелі


def register_contract_panel():
    """Панель з усім, що розмічає сервер: розділи всіх станів, швидкі зміни, дії, дія списку."""
    return sp.register(
        "inventory.supplier",
        title=lambda s: format_html("<b>{}</b>", s.name),
        label=lambda s: s.name,
        subtitle=lambda s, r: s.email,
        blocks=[
            sp.Block("info", "Контакти", lambda s, r: sp.kv([("Email", sp.edit_html(r, s, "email"))]), icon="📇",
                     badge=lambda s, r: sp.Badge(2, "warn", "дві адреси"), summary=lambda s, r: s.email,
                     affects=("email",), fieldset=1),
            sp.Block("lazy", "Повільний", lambda s, r: format_html("<p>{}</p>", "Повільно"), lazy=True),
            sp.Block("folded", "Згорнутий", lambda s, r: format_html("<p>{}</p>", "Згорнуте"), collapsed=True),
            sp.Block("na", "Не для цього", lambda s, r: "x", applies=lambda s: False),
            sp.Block("blank", "Порожній", lambda s, r: ""),
            sp.Block("boom", "Ламається", lambda s, r: 1 / 0),
        ],
        actions=[
            sp.Action("mail", "✉️ Написати", lambda s, r: f"mailto:{s.email}", hotkey="KeyM"),
            sp.Action("label", "🏷 Етикетка", lambda s, r: f"/labels/order/{s.pk}/", kind="svl",
                      attrs=lambda s, r: {"svl": "order", "order": s.pk, "sku": s.name}),
            sp.Action("off", "📦 Недоступне", lambda s, r: "", reason=lambda s, r: "Ще немає замовлень"),
        ],
        edits=[
            sp.Edit("email", "email", "Email", "text", cells=("email",), undo=True),
            sp.Edit("notes", "notes", "Нотатки", "textarea", place="memo", maxlength=50, hotkey="KeyN"),
            sp.Edit("person", "contact_person", "Контакт", "choice", place="head",
                    choices=lambda s, r: [("Anna", "👩 Anna"), ("Bob", "👨 Bob")], confirm={"Bob": "Передати Bob?"},
                    hotkey="KeyS"),
        ],
        hide_cols=("phone",), generic_blocks=())


@admin.action(description="✉️ Нагадати")
def remind(modeladmin, request, queryset):
    return None


class ShellContractTests(PageTestBase):
    """Кожен гачок, який шукає mv_panel.js, справді є в розмітці сервера — і навпаки."""

    # (у розмітці сервера, як його шукає скрипт)
    HOOKS = [
        ('class="mvp-shell"', "mvp-shell"), ('data-label="', "data-label"), ('data-change-url="', "data-change-url"),
        ('data-history-url="', "data-history-url"), ('data-delete-url="', "data-delete-url"),
        ('<header class="mvp-head">', "mvp-head"), ('class="mvp-hrow"', "mvp-hrow"),
        ('class="mvp-title" id="mvp-title"', "mvp-title"), ('class="mvp-hedit"', "mvp-hedit"),
        ('data-mvp-nav="-1"', '[data-mvp-nav="-1"]'), ('data-mvp-nav="1"', '[data-mvp-nav="1"]'),
        ("data-mvp-open href=", "a[data-mvp-open]"), ("data-mvp-menu", "[data-mvp-menu]"),
        ("data-mvp-close", "[data-mvp-close]"), ('<div class="mvp-sub">', ".mvp-head > .mvp-sub"),
        ("data-mvp-pos hidden", "[data-mvp-pos]"), ('class="mvp-actbar"', "mvp-actbar"),
        ("data-mvp-more", "[data-mvp-more]"), ('data-act="mail"', "data-act"), ('class="mvp-act is-off"', "is-off"),
        ("data-svl=", "[data-svl]"), ("data-fallback=", "data-fallback"),
        ('data-admin-action="remind"', "data-admin-action"), ("data-confirm=", "data-confirm"),
        ('<div class="mvp-memo">', ".mvp-memo"), ('data-edit="notes"', "data-edit"), ('data-kind="textarea"', "data-kind"),
        ("data-maxlength=", "data-maxlength"), ("data-choices=", "data-choices"), ('data-value="', "data-value"),
        ('class="mvp-ed-btn"', "mvp-ed-btn"), ('<nav class="mvp-nav"', "mvp-nav"),
        ('<div class="mvp-body" tabindex="0">', "mvp-body"), ('<section class="mvp-sec"', "mvp-sec"),
        ('data-sec="info"', "data-sec"), ('data-title="', "data-title"), ('data-short="', "data-short"),
        ('data-icon="', "data-icon"), ('data-count="2"', "data-count"), ('data-tone="warn"', "data-tone"),
        ('data-badge="', "data-badge"), ('data-brief="', "data-brief"), ("data-deferred", "data-deferred"),
        ("data-collapsed", "data-collapsed"), ("data-empty", "data-empty"), ("data-na", "data-na"),
        ('data-mvp-retry="boom"', "[data-mvp-retry]"), ('class="mvp-sh-btn"', "mvp-sh-btn"),
        ('aria-expanded="', "aria-expanded"), ('class="mvp-sb"', "mvp-sb"), ('class="mvp-foot"', "mvp-foot"),
        ("data-mvp-refresh", "[data-mvp-refresh]"), ("data-mvp-keys", "[data-mvp-keys]"),
    ]

    def setUp(self):
        super().setUp()
        saved = dict(sp._registry)
        self.addCleanup(lambda: (sp._registry.clear(), sp._registry.update(saved)))
        for name in ("core.side_panel", "django.request"):   # «Ламається» і 409 пишуть попередження — так задумано
            log = logging.getLogger(name)
            self.addCleanup(log.setLevel, log.level)
            log.setLevel(logging.ERROR)
        self.supplier = Supplier.objects.create(name="Muster <GmbH>", contact_person="Anna",
                                                email="anna@muster.example", notes="Ruft an")
        register_contract_panel()
        patcher = mock.patch.object(SupplierAdmin, "actions", [remind])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = f"/core/panel/inventory.supplier/{self.supplier.pk}/"

    def test_every_hook_is_in_the_shell_and_in_the_script(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        shell, js = r.content.decode(), read(JS_PATH)
        for in_html, in_js in self.HOOKS:
            self.assertIn(in_html, shell, f"server markup lost {in_html!r}")
            self.assertIn(in_js, js, f"mv_panel.js does not use {in_js!r}")

    def test_endpoints_the_script_builds(self):
        js = read(JS_PATH)
        cfg = cfg_of(self.client.get("/admin/inventory/supplier/").content.decode())
        pk = self.supplier.pk
        # cfg.url + pk + '/' (?open=), '/block/<key>/', '/edit/'; prefsUrl — як є
        self.assertIn("cfg.url + pk + '/'", js)
        self.assertIn("'?open=' + encodeURIComponent(sec)", js)
        self.assertIn("cfg.url + pk + '/block/' + encodeURIComponent(key) + '/'", js)
        self.assertIn("cfg.url + pk + '/edit/'", js)
        self.assertIn("post(cfg.prefsUrl", js)
        self.assertEqual(self.client.get(f"{cfg['url']}{pk}/?open=lazy").status_code, 200)
        self.assertEqual(self.client.get(f"{cfg['url']}{pk}/block/lazy/").status_code, 200)
        r = self.client.post(f"{cfg['url']}{pk}/edit/", json.dumps({"key": "email", "value": "neu@x.example",
                                                                     "expect": "anna@muster.example"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue({"ok", "message", "value", "head", "blocks", "cells", "undo"} <= set(r.json()))
        self.assertIn("<header class=\"mvp-head\">", r.json()["head"])         # replaceHead: header.mvp-head
        self.assertTrue(r.json()["blocks"]["info"].startswith('<section class="mvp-sec"'))   # replaceSection
        r = self.client.post(f"{cfg['url']}{pk}/edit/", json.dumps({"key": "email", "value": "a@x.example",
                                                                     "expect": "old@x.example"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 409)
        self.assertIn('class="mvp-shell"', r.json()["html"])                   # swapShell: .mvp-shell
        r = self.client.post(cfg["prefsUrl"], json.dumps({"set": {"collapsed": ["info"]}}),
                             content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertIn("info", r.json()["prefs"]["collapsed"])                  # adopt(): відповідь — увесь вибір


class ScriptContractTests(SimpleTestCase):
    """mv_panel.js: події, адреси, дії списку, клавіші, меню — як домовлено з пакетами A, G, M."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = read(JS_PATH)

    def test_files_exist(self):
        self.assertTrue(JS_PATH.is_file())
        self.assertTrue(CSS_PATH.is_file())

    def test_required_markers(self):
        for s in ("mv:panel", "mvp-reopen", "#mvp=", "_selected_action", "select_across", "X-CSRFToken",
                  "MVMenu.open", "IntersectionObserver", "e.code"):
            self.assertIn(s, self.js)

    def test_module_shape_and_api(self):
        js = self.js
        self.assertTrue(js.lstrip().startswith("/*"))
        self.assertIn("(function () {\n  'use strict';", js)
        self.assertIn("if (!cfgEl || window.MVPanel) return;", js)
        self.assertIn("getElementById('mv-panel-config')", js)
        api = js[js.index("window.MVPanel = {"):]
        api = api[:api.index("};")]
        for name in ("open:", "close:", "isOpen:", "current:", "refresh:", "config:"):
            self.assertIn(name, api)
        self.assertIn("'mv:panel-edit'", js)

    def test_panel_event_before_reading_the_sidebar_width(self):
        js = self.js
        show = js[js.index("function showPanel("):js.index("function close()")]
        self.assertLess(show.index("emit('mv:panel', { open: true"), show.index("layout();"))
        self.assertIn("getPropertyValue('--mvd-side-w')", js)
        self.assertIn("'.main.shifted > #nav-sidebar'", js)          # класичний вигляд
        self.assertIn("emit('mv:panel', { open: false, width: 0", js)
        self.assertIn("document.addEventListener('mv:sidebar'", js)

    def test_page_variables(self):
        js = self.js
        self.assertIn("setVar(document.body, '--mvp-w'", js)
        self.assertIn("setVar(panel, '--mvp-top'", js)
        self.assertIn("setVar(panel, '--mvp-dock'", js)
        self.assertNotIn("documentElement.style", js)                 # <html> не чіпаємо (перевірка контрасту)
        self.assertNotIn("root.style", js)
        self.assertIsNone(re.search(r"setVar\(document\.body, '--mvp-(top|dock)'", js))

    def test_pill_host(self):
        js = self.js
        pill = js[js.index("function buildPill()"):js.index("function adoptToolbar()")]
        order = [pill.index("'#changelist-search > div'"), pill.index("getElementById('toolbar')"),
                 pill.index("'#changelist .changelist-form-container'"), pill.index("getElementById('content-main')")]
        self.assertEqual(order, sorted(order))
        self.assertIn("el('div', 'mvp-tbar')", pill)
        self.assertNotIn("getElementById('changelist').appendChild", js)
        self.assertNotIn("getElementById('changelist').insertBefore", js)

    def test_list_action_for_one_record(self):
        js = self.js
        run = js[js.index("function runAdmin("):js.index("function adminItems(")]
        self.assertIn("location.pathname + location.search", run)
        for field in ("['csrfmiddlewaretoken', csrf()]", "['action', name]", "['index', '0']", "['select_across', '0']",
                      "['_selected_action', pk]"):
            self.assertIn(field, run)
        self.assertIn("document.body.appendChild(f)", run)
        self.assertNotIn("changelist-form", run)
        self.assertNotIn("fetch(", run)
        self.assertLess(run.index("sSet('sessionStorage', 'mvp-reopen'"), run.index("f.submit()"))
        self.assertIn("window.confirm(msg)", run)
        self.assertIn("fmt('Виконати «{0}» для {1}?'", run)

    def test_csrf_and_edits(self):
        js = self.js
        self.assertIn("'X-CSRFToken': csrf()", js)
        self.assertIn("csrftoken=", js)
        self.assertIn("{ key: key, value: value, expect: expect }", js)
        self.assertIn("res.status === 409 && d.html", js)
        self.assertIn("td.field-' + n + ', th.field-' + n", js)

    def test_reopen_hash_and_page_edge(self):
        js = self.js
        self.assertIn("var HASH = /^#mvp=(\\d+)", js)
        self.assertIn("history.replaceState(", js)
        self.assertIn("REOPEN_TTL = 120000", js)
        self.assertIn("pk: d > 0 ? 'first' : 'last'", js)
        self.assertIn("new URLSearchParams(location.search)", js)

    def test_server_html_only_through_inner_html(self):
        """innerHTML — лише HTML з ендпойнтів панелі (сервер екранує дані); написи — textContent."""
        allowed = {"html", "String(html || '').trim()", "cells[n]"}
        found = re.findall(r"\.innerHTML\s*=\s*([^;]+);", self.js)
        self.assertTrue(found)
        for rhs in found:
            self.assertIn(rhs.strip(), allowed, f"innerHTML from {rhs!r}")
        for bad in ("insertAdjacentHTML", "outerHTML =", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, self.js)

    def test_menus_and_toasts_go_through_mvmenu(self):
        js = self.js
        self.assertIn("if (!window.MVMenu || !MVMenu.open) return null;", js)    # без MVMenu — без меню, решта працює
        self.assertIn("MVMenu.toast(", js)
        self.assertIn("MVMenu.isOpen", js)
        self.assertNotIn("mvp-ctx", js)                                           # старе власне меню прибрано
        self.assertIn("titleData: true", js)                                      # назва рядка — дані запису

    def test_right_click_rules(self):
        js = self.js
        self.assertIn("var NO_MENU = 'a[href], input, select, textarea, [contenteditable], [data-svl], [data-own-menu]';", js)
        ctx = js[js.index("function onContextMenu("):js.index("function snapKeys(")]
        self.assertIn("e.shiftKey", ctx)
        self.assertIn("#result_list thead th[class*=\"column-\"]", ctx)

    def test_keys_use_event_code(self):
        js = self.js
        for code in ("'ArrowDown'", "'KeyJ'", "'ArrowUp'", "'KeyK'", "'KeyO'", "'KeyR'", "'Period'", "'BracketLeft'",
                     "'BracketRight'", "'Slash'"):
            self.assertIn(code, js)
        self.assertIn("/^Digit[1-9]$/.test(code)", js)
        self.assertIsNone(re.search(r"e\.key\s*===\s*'[A-Za-z]'", js))         # літери — лише e.code
        self.assertIn("'#rg-panel.open, .svl-modal, #svl-menu, .mvd-ctx'", js)
        for layer in ("'mvd-pal-open'", "'mvd-fopen'"):
            self.assertIn(layer, js)

    def test_ui_texts_are_whole_ukrainian_literals(self):
        js = self.js
        for text in ("📌 Панель", "Не вдалося завантажити", "Не вдалося зберегти — перевірте зв’язок.", "Збережено",
                     "Скопійовано", "Остання на сторінці — ↓ ще раз: наступна сторінка", "{0} з {1}",
                     "🙈 Сховано колонок: {0} — вони є в панелі", "Панель «{0}» під себе", "Розділ «{0}»",
                     "Подробиці праворуч", "Дії списку для цього запису", "Усі налаштування панелі…", "Клавіші…"):
            self.assertIn(text, js)


class StyleContractTests(SimpleTestCase):
    """mv_panel.css: змінні теми, шари, помічники, класи вмісту, доступність."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = read(CSS_PATH)
        cls.rules = css_rules(cls.css)

    def rule(self, selector):
        bodies = [b for s, b in self.rules if selector in [x.strip() for x in s.split(",")]]
        self.assertTrue(bodies, f"no rule for {selector}")
        return " ".join(bodies)

    def test_required_markers(self):
        for s in ("body.mvp-open.mvp-dock #mv-fab", "#mv-chat-panel", ".mvp-sec", ".mvp-nav"):
            self.assertIn(s, self.css)
        self.assertNotIn("@media (max-width: 1099px)", self.css)
        self.assertIsNone(re.search(r"@media\s*\(\s*max-width:\s*1099px", self.css))

    def test_layers(self):
        self.assertIn("z-index: 900", self.rule("#mv-panel"))
        self.assertIn("top: var(--mvp-top", self.rule("#mv-panel"))
        self.assertIn("width: var(--mvp-w", self.rule("#mv-panel"))
        self.assertNotIn("mvp-ctx", self.css)
        for bare in (".mvm-title", ".mvm-row", ".mvm-sub", ".mvm-ico", ".mvm-lbl"):   # меню — лише mv_menu.css
            self.assertNotIn(bare, self.css)

    def test_theme_variables_only(self):
        for sel, body in self.rules:
            self.assertNotRegex(body, r"#[0-9a-fA-F]{3,8}\b", f"hard-coded colour in {sel}")
        for var in ("--bg-card", "--bg-card-2", "--bg-hover", "--border", "--border-strong", "--text", "--text-muted",
                    "--accent", "--accent-subtle", "--ok", "--warn", "--err", "--link-fg", "--default-button-bg",
                    "--button-fg"):
            self.assertIn(f"var({var}", self.css)
        self.assertIsNone(re.search(r"content:\s*\"[^\"]*[A-Za-zА-Яа-яІіЇїЄєҐґ]", self.css))   # без слів у content:

    def test_helpers_dock_and_beside(self):
        fab = self.rule("body.mvp-open.mvp-dock #mv-fab")
        for decl in ("right: 10px", "bottom: 6px", "width: 36px", "height: 36px", "font-size: 17px", "box-shadow: none"):
            self.assertIn(decl, fab)
        pill = self.rule("body.mvp-open.mvp-dock #rg-pill")
        for decl in ("right: 52px", "bottom: 6px", "width: 36px", "height: 36px", "padding: 0", "border-radius: 50%"):
            self.assertIn(decl, pill)
        self.assertIn("display: none", self.rule("body.mvp-open.mvp-dock #rg-pill .rg-name"))
        self.assertIn("padding-right: var(--mvp-dock", self.rule("body.mvp-open.mvp-dock .mvp-foot"))
        self.assertIn("right: calc(var(--mvp-w) + 20px)", self.rule("body.mvp-open.mvp-beside #mv-fab"))
        self.assertIn("right: calc(var(--mvp-w) + 84px)", self.rule("body.mvp-open.mvp-beside #rg-pill"))
        chat = self.rule("body.mvp-open #mv-chat-panel")
        self.assertIn("right: calc(var(--mvp-w) + 16px)", chat)
        self.assertIn("bottom: 16px", chat)
        self.assertIn("right: calc(var(--mvp-w) + 18px)", self.rule("body.mvp-open #mv-toast-container"))

    def test_list_beside_the_panel(self):
        push = self.rule("body.mvp-open.mvp-push #content")
        self.assertIn("margin-right: var(--mvp-w)", push)
        self.assertIn("box-shadow: -14px 0 40px rgba(0, 0, 0, .45)", self.rule("body.mvp-open.mvp-overlay #mv-panel"))
        self.assertIn("display: flex", self.rule('html:not([data-mv-design="new"]) body.mvp-open ul.object-tools'))
        pill = self.rule("#mvp-toggle.mvp-pill-toggle")
        self.assertIn("order: 8", pill)
        self.assertIn("margin: 0 0 0 auto", pill)
        self.assertIn("display: none !important", self.rule("#mvp-toggle.mvp-pill-toggle[hidden]"))

    def test_classes_the_script_builds_are_styled(self):
        js = read(JS_PATH)
        for cls in ("mvp-wrap", "mvp-grip", "mvp-load", "mvp-vh", "mvp-pill-toggle", "mvp-tbar", "mvp-colnote",
                    "mvp-colnote-b", "mvp-ch", "mvp-ci", "mvp-cl", "mvp-cn", "mvp-ch-more", "mvp-nav-icons", "mvp-ai",
                    "mvp-al", "mvp-icons", "mvp-actbar-bottom", "mvp-snone", "mvp-ed-form", "mvp-ed-in", "mvp-ed-ok",
                    "mvp-ed-no", "mvp-ed-cnt", "mvp-nofind", "mvp-sheet", "mvp-sheet-h", "mvp-sg", "mvp-sg-note",
                    "mvp-sl", "mvp-sli", "mvp-drag", "mvp-sdiv", "mvp-sopts", "mvp-sopt", "mvp-sopt-t", "mvp-srow",
                    "mvp-srange", "mvp-sout", "mvp-btn", "mvp-btn-sm", "mvp-keys", "mvp-wlabel", "mvp-dropline",
                    "mvp-busy", "mvp-sel", "mvp-stale", "mvp-on", "mvp-open", "mvp-push", "mvp-overlay", "mvp-dock",
                    "mvp-beside", "mvp-resizing", "mvp-d-compact", "mvp-empty-compact", "mvp-view-tabs",
                    "mvp-nav-side", "mvp-sheet-on"):
            # у рядку JS: 'mvp-x', 'mvp-a mvp-x', 'mvp-x ' + …
            self.assertTrue(re.search(rf"['\s]{re.escape(cls)}['\s]", js), f"mv_panel.js does not build {cls}")
            self.assertTrue(re.search(rf"\.{re.escape(cls)}(?![\w-])", self.css), f"no CSS for .{cls}")
        self.assertIn("c.toggle('mvp-nav-' + v, nav === v)", js)                 # mvp-nav-top / -side / -none
        self.assertTrue(re.search(r"\.mvp-nav-none(?![\w-])", self.css))

    def test_provider_content_classes_are_styled(self):
        for sel in (".mvp-kv", ".mvp-kv-r", ".mvp-row", ".mvp-line", ".mvp-note", ".mvp-ok", ".mvp-warn", ".mvp-err",
                    ".mvp-muted", ".mvp-link", ".mvp-chips", ".mvp-chip", ".mvp-quote", ".mvp-empty", ".mvp-badge",
                    ".mvp-t-new", ".mvp-t-work", ".mvp-t-ship", ".mvp-t-done", ".mvp-t-off", ".mvp-t-ok", ".mvp-t-warn",
                    ".mvp-t-err", ".mvp-t-info", ".mvp-table", ".mvp-thumb", ".mvp-spark", "details.mvp-det",
                    ".mvp-clamp", "input[data-mvp-filter]", "button.mvp-copy", "a.mvp-more-link", ".mvp-fg",
                    ".mvp-fg-h", "pre.mvp-json", ".mvp-pill", ".mvp-tog", ".mvp-ed", ".mvp-ed-memo", ".mvp-ro",
                    ".mvp-sbrief", ".mvp-sh-card", ".mvp-skel", ".mvp-stamp"):
            self.assertIn(sel, self.css)
        self.assertIn("white-space: pre-wrap", self.rule("#mv-panel pre.mvp-json"))
        self.assertIn("display: none", self.rule(".mvp-sec[data-collapsed] .mvp-sb"))
        self.assertIn("display: none", self.rule("#mv-panel.mvp-empty-compact .mvp-sec[data-empty] .mvp-sb"))

    def test_accessibility(self):
        css = self.css
        self.assertIn(':root[data-mv-focus="always"] #mv-panel', css)
        self.assertIn(":focus-visible", css)
        self.assertIn('html[data-mv-motion="reduce"] #mv-panel', css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("@media (forced-colors: active)", css)
        self.assertIn("@media print", css)
        self.assertIn("text-transform: none", self.rule("#mv-panel h2"))
        sh = self.rule(".mvp-sh-btn")
        self.assertIn("font-size: 13px", sh)
        self.assertIn("font-weight: 600", sh)
        self.assertIn("height: 34px", self.rule(".mvp-icon"))
        self.assertIn("height: 32px", self.rule(".mvp-ch"))
        self.assertIn("font-size: 12.5px", self.rule("#mv-panel.mvp-d-compact"))
        self.assertIn("min-height: 28px", self.rule("#mv-panel.mvp-d-compact .mvp-sli"))
        self.assertIn("user-select: none", self.rule("html.mvp-resizing"))
