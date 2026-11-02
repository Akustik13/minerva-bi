"""🧭 Спільне меню Minerva (static/admin/js/mv_menu.js, static/admin/css/mv_menu.css) — статична перевірка контракту,
яким користуються панель праворуч (mv_panel.js) і бокове меню (mv_desktop.js).

    python manage.py test core.tests_mv_menu --settings=tabele.settings_test

Вручну в браузері (не автоматизовано):
- лише клавіатурою: ↑ ↓ Home End, → / Enter / Пробіл — підменю, ← / Esc — назад, Tab — закрити, перша літера;
  фокус повертається на кнопку, що відкрила меню;
- підменю біля правого краю вікна відкривається ліворуч; довге меню прокручується (не вище 70 % вікна);
- «Розмір тексту» 125 % і 150 %: меню під курсором / під кнопкою, а не зсунуте;
- повідомлення «↶ Скасувати»: Ctrl Z, наведення зупиняє відлік; теми dark / light / hc-light; «Менше анімацій».
"""
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

STATIC = Path(settings.BASE_DIR) / "static" / "admin"
JS = STATIC / "js" / "mv_menu.js"
CSS = STATIC / "css" / "mv_menu.css"
# Класи меню «Ще» телефона (mv_mobile.*) без предка .mvm — наші правила мають бути під .mvm і перекривати їх
PHONE_SHARED = {"mvm-title", "mvm-row", "mvm-sub"}


def _first(css):
    """{селектор: тіло першого такого правила}."""
    out = {}
    for head, body in _rules(css):
        out.setdefault(head, body)
    return out


def _rules(css):
    """[(селектор, тіло)] без коментарів; вкладені @media розгортаються, @keyframes пропускаються."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i = [], 0
    while i < len(css):
        start = css.find("{", i)
        if start < 0:
            break
        head = css[i:start].strip()
        depth, j = 1, start + 1
        while j < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        body = css[start + 1:j - 1]
        if head.startswith("@keyframes"):
            pass
        elif head.startswith("@"):
            out.extend(_rules(body))
        else:
            out.append((head, body))
        i = j
    return out


class MVMenuFilesTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = JS.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")

    def test_files_exist_under_static_admin(self):
        self.assertTrue(JS.is_file())
        self.assertTrue(CSS.is_file())

    def test_public_api(self):
        self.assertIn("if (window.MVMenu) return;", self.js)          # підключили двічі — діє перше
        api = re.search(r"window\.MVMenu\s*=\s*\{(.*?)\};", self.js, re.S)
        self.assertIsNotNone(api, "window.MVMenu = {…} не знайдено")
        for name in ("open", "close", "isOpen", "toast", "refresh"):
            self.assertRegex(api.group(1), r"\b%s\s*:" % name)
        for kind in ("'check'", "'radio'", "'submenu'", "'title'", "'separator'", "'hint'", "'row'"):
            self.assertIn(kind, self.js)

    def test_aria_roles_and_states(self):
        self.assertIn('role="menu"', self.js)
        self.assertRegex(self.js, r"setAttribute\('role', 'menu'\)")
        for s in ("menuitem", "menuitemcheckbox", "menuitemradio", "aria-checked", "aria-haspopup",
                  "aria-expanded", "aria-controls", "role', 'separator'", "role', 'status'"):
            self.assertIn(s, self.js)

    def test_keyboard_and_pointer(self):
        self.assertIn("stopPropagation", self.js)
        for key in ("'ArrowDown'", "'ArrowUp'", "'ArrowRight'", "'ArrowLeft'", "'Home'", "'End'", "'Enter'",
                    "' '", "'Escape'", "'Tab'"):
            self.assertIn(key, self.js)
        self.assertRegex(self.js, r"OPEN_MS\s*=\s*120\b")             # підменю від наведення — 120 мс
        self.assertRegex(self.js, r"GRACE_MS\s*=\s*300\b")            # навскіс до підменю — 300 мс запасу
        # закривається: натискання поза меню (capture), прокрутка поза ним, втрата фокусу вікном, зміна розміру
        for ev in ("'pointerdown', onDown, true", "'scroll', onScroll, true", "'blur', onBlur", "'resize', onResize"):
            self.assertIn(ev, self.js)
        self.assertIn("getComputedStyle(document.body).zoom", self.js)  # «Розмір тексту» 112–150 %

    def test_text_never_goes_through_html(self):
        self.assertIsNone(re.search(r"innerHTML[^;\n]*label|label[^;\n]*innerHTML", self.js))
        self.assertNotRegex(self.js, r"\.(?:inner|outer)HTML\s*\+?=")
        self.assertNotIn("insertAdjacentHTML", self.js)
        self.assertNotIn("document.write", self.js)
        self.assertIn("textContent", self.js)

    def test_ui_strings_are_whole_literals_for_the_translation_catalog(self):
        from core.ui_text.extract import _js_literals
        found = set(_js_literals(self.js))
        for phrase in ("Меню", "Порожньо", "Скасувати", "Закрити"):
            self.assertIn(phrase, found)
        for phrase in found:                       # зламана пара лапок дала б «фрази» з кодом
            self.assertNotRegex(phrase, r"[(){};=]", phrase)

    def test_css_layers_and_rows(self):
        rules = _first(self.css)
        self.assertRegex(rules[".mvm"], r"z-index:\s*10045\b")
        self.assertRegex(rules[".mvm-toast"], r"z-index:\s*10050\b")
        self.assertRegex(rules[".mvm-toast"], r"bottom:")
        self.assertRegex(rules[".mvm .mvm-item"], r"min-height:\s*34px")
        self.assertRegex(rules[".mvm"], r"max-height:\s*70vh")
        for cls in (".mvm-item", ".mvm-check", ".mvm-radio", ".mvm-sub", ".mvm-title", ".mvm-sep", ".mvm-hint",
                    ".mvm-row", ".mvm-toast"):
            self.assertIn(cls, self.css)

    def test_css_theme_variables_only(self):
        no_comments = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        self.assertNotRegex(no_comments, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(no_comments, r"\b(?:rgb|hsl)a?\(\s*(?!0\s*,\s*0\s*,\s*0\s*,)")   # лише тінь rgba(0,0,0,…)
        for var in ("--bg-card", "--text", "--border-strong", "--accent-subtle", "--focus-ring", "--text-muted"):
            self.assertIn("var(%s)" % var, self.css)

    def test_focus_ring_and_reduced_motion(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn('data-mv-focus="always"', self.css)
        self.assertIn('data-mv-motion="reduce"', self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(reduced)
        self.assertIn("animation: none", reduced.group(1))
        self.assertIn("transition: none", reduced.group(1))

    def test_every_menu_rule_is_scoped_under_mvm(self):
        """Меню «Ще» на телефоні (mv_mobile.css) має свої .mvm-title / .mvm-row / .mvm-sub: правила меню — лише
        всередині .mvm, тож одне одного не зачіпають; .mvm-toast — окремий блок."""
        for head, _body in _rules(self.css):
            for sel in (s.strip() for s in head.split(",")):
                classes = re.findall(r"\.(mvm-[a-z0-9-]+)", sel)
                if not classes or all(c.startswith("mvm-toast") for c in classes):
                    continue
                self.assertRegex(sel, r"(?:^|[\s>])\.mvm(?=[\s.:\[>])", sel)

    def test_inner_class_names_do_not_clash_with_the_phone_menu(self):
        phone = (STATIC / "js" / "mv_mobile.js").read_text(encoding="utf-8") + \
            (STATIC / "css" / "mv_mobile.css").read_text(encoding="utf-8")
        theirs = set(re.findall(r"\bmvm-[a-z0-9-]+", phone))
        ours = set(re.findall(r"\bmvm-[a-z0-9-]+", self.js + self.css))
        self.assertLessEqual(ours & theirs, PHONE_SHARED)
        rules = _first(self.css)
        self.assertRegex(rules[".mvm .mvm-title"], re.compile(r"font-size:.*font-weight:|font-weight:.*font-size:", re.S))
        self.assertRegex(rules[".mvm .mvm-row"], r"display:\s*flex")
        self.assertRegex(rules[".mvm .mvm-row"], r"margin:\s*0")
        for prop in ("margin: 0", "font: inherit", "letter-spacing: normal", "text-transform: none", "color: inherit"):
            self.assertIn(prop, rules[".mvm .mvm-item"])                # .mvm-sub телефона нічого не змінить

    def test_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node не встановлено")
        run = subprocess.run([node, "--check", str(JS)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
