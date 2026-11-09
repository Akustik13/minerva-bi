"""📌 Панель праворуч — каркас (core/side_panel.py, core/panel_edit.py, core/views.py, тег side_panel_config):
реєстр і права, вибір людини, оболонка й розділи, окремий розділ, швидкі зміни, дії списку для одного запису,
помічники HTML. Тестова панель — для постачальників (inventory.supplier); реєстр відновлюється після тесту.
Панелі замовлень, товарів і клієнтів — sales/, inventory/, crm/tests_side_panel.py.

    python manage.py test core.tests_side_panel --settings=tabele.settings_test
"""
import json
import logging
import sys
import types
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone
from django.utils.html import format_html

from core import panel_edit
from core import side_panel as sp
from core.models import UserProfile
from core.templatetags.side_panel_tags import side_panel_config
from inventory.admin import SupplierAdmin
from inventory.models import Location, Supplier

MODEL = "inventory.supplier"
URL = "/core/panel/inventory.supplier/"


def _boom(obj, request):
    raise RuntimeError("boom")


def _person_choices(obj, request):
    return [("Anna", "👩 Anna"), ("Bob", "👨 Bob")]


def register_test_panel():
    return sp.register(
        MODEL,
        title=lambda s: format_html("<b>{}</b>", s.name),
        label=lambda s: s.name,
        subtitle=lambda s, r: s.email or "без email",
        blocks=[
            sp.Block("info", "Контакти", lambda s, r: sp.kv([("Email", s.email), ("Телефон", s.phone)]), icon="📇",
                     badge=lambda s, r: sp.Badge(2, "ok", "дві адреси"),
                     summary=lambda s, r: f"{s.contact_person} · {s.email}", affects=("email",), fieldset=1),
            sp.Block("secret", "Для продажів", lambda s, r: format_html("<p>{}</p>", "Таємні продажі"),
                     perm="sales.view_salesorder"),
            sp.Block("lazy", "Повільний", lambda s, r: format_html("<p>{}</p>", "Повільно пораховано"), lazy=True),
            sp.Block("folded", "Згорнутий", lambda s, r: format_html("<p>{}</p>", "Згорнуте всередині"),
                     collapsed=True),
            sp.Block("na", "Не для цього", lambda s, r: format_html("<p>{}</p>", "Ніколи"), applies=lambda s: False),
            sp.Block("boom", "Ламається", _boom),
            sp.Block("blank", "Порожній", lambda s, r: "", empty="Тут нічого немає."),
            sp.Block("extra", "Додатковий", lambda s, r: format_html("<p>{}</p>", "Додаткове"), default=False),
        ],
        actions=[
            sp.Action("mail", "✉️ Написати", lambda s, r: f"mailto:{s.email}" if s.email else "",
                      reason=lambda s, r: "" if s.email else "Немає email", hotkey="KeyM"),
            sp.Action("orders", "📦 Замовлення", lambda s, r: "/admin/sales/salesorder/", perm="sales.view_salesorder"),
            sp.Action("site", "🌐 Сайт", lambda s, r: s.website, new_tab=True, default=False),
            sp.Action("card", "📋 Відкрити картку", lambda s: f"/admin/inventory/supplier/{s.pk}/change/"),  # як у v1
        ],
        edits=[
            sp.Edit("email", "email", "Email", "text", cells=("email",), undo=True),
            sp.Edit("notes", "notes", "Нотатки", "textarea", place="memo", maxlength=50, undo=True, hotkey="KeyN"),
            sp.Edit("person", "contact_person", "Контакт", "choice", place="head", choices=_person_choices,
                    confirm={"Bob": "Передати Bob?"}, tones={"Anna": "ok"}, hotkey="KeyS"),
        ],
        hide_cols=("phone",), generic_blocks=())


def register_location_panel():
    return sp.register(
        "inventory.location", title=lambda o: format_html("<b>{}</b>", o.code),
        blocks=[sp.Block("main", "Головне", lambda o, r: sp.kv([("Назва", o.name)]), affects=("active",))],
        edits=[sp.Edit("active", "is_active", "Активна", "bool", place="bar", undo=True, cells=("is_active",),
                       hotkey="KeyA", icon_on="✅", icon_off="⛔"),
               sp.Edit("type", "location_type", "Тип складу", "choice", place="head", tones={"finished": "done"})],
        generic_blocks=())


@admin.action(description="✉️ Нагадати")
def remind(modeladmin, request, queryset):
    return None


@admin.action(description="👁 Звіт", permissions=["view"])
def report(modeladmin, request, queryset):
    return None


@admin.action(description="🔁 Валюта", permissions=["change"])
def recur(modeladmin, request, queryset):
    return None


def cfg_of(html):
    return json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])


class PanelTestBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        saved = dict(sp._registry)

        def restore():
            sp._registry.clear()
            sp._registry.update(saved)
        self.addCleanup(restore)
        log = logging.getLogger("core.side_panel")      # розділ «Ламається» пише попередження в кожній панелі
        self.addCleanup(log.setLevel, log.level)
        log.setLevel(logging.ERROR)
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)
        self.supplier = Supplier.objects.create(name="Muster GmbH", contact_person="Anna", email="anna@muster.example",
                                                phone="+49 30 1234", website="https://muster.example",
                                                notes="Ruft montags an")
        self.panel = register_test_panel()

    def user(self, name, role, modules=None):
        u = User.objects.create_user(name, f"{name}@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role=role, allowed_modules=modules)
        return User.objects.get(pk=u.pk)

    def login(self, user):
        self.client.force_login(user)

    def shell(self, pk=None, query=""):
        r = self.client.get(f"{URL}{pk or self.supplier.pk}/{query}")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.content.decode()

    @staticmethod
    def section(html, key):
        at = html.index(f'data-sec="{key}"')
        start = html.rindex("<section", 0, at)
        return html[start:html.index("</section>", start)]

    def post_edit(self, data, pk=None, model=MODEL):
        return self.client.post(f"/core/panel/{model}/{pk or self.supplier.pk}/edit/", json.dumps(data),
                                content_type="application/json")

    def post_prefs(self, data, status=200):
        r = self.client.post(f"{URL}prefs/", json.dumps(data), content_type="application/json")
        self.assertEqual(r.status_code, status, r.content[:300])
        return r.json().get("prefs")

    def stored(self, user=None):
        return UserProfile.objects.get(user=user or self.root).ui_prefs

    def fresh(self, user=None):
        return User.objects.get(pk=(user or self.root).pk)

    def request_for(self, user, path="/admin/inventory/supplier/"):
        request = RequestFactory().get(path)
        request.user = user
        return request

    def tag_config(self, user):
        request = self.request_for(user)
        ma = admin.site._registry[Supplier]
        cl = ma.get_changelist_instance(request)
        out = side_panel_config({"request": request}, cl)
        return cfg_of(out) if out else None, out


class RegistryTests(PanelTestBase):
    def test_registered_panel_wins_and_keys_are_unique(self):
        self.assertIs(sp.get(MODEL), self.panel)
        self.assertEqual(self.panel.memo, "notes")                             # зміна з place="memo"
        self.assertEqual(self.panel.action("card").href(self.supplier, None),   # v1: href лише від запису
                         f"/admin/inventory/supplier/{self.supplier.pk}/change/")
        render = lambda o, r: ""  # noqa: E731
        with self.assertRaises(ValueError):
            sp.register("inventory.location", title=str, blocks=[sp.Block("a", "A", render), sp.Block("a", "B", render)])
        with self.assertRaises(ValueError):
            sp.register("inventory.location", title=str, blocks=[sp.Block("a", "A", render)],
                        actions=[sp.Action("x", "X", lambda o, r: "/")], edits=[sp.Edit("x", "name", "X", "text")])
        with self.assertRaises(ValueError):
            sp.register("inventory.location", title=str, blocks=[], edits=[sp.Edit("e", "name", "X", "nope")])

    def test_never_models_and_apps_get_no_panel(self):
        block = sp.Block("x", "X", lambda o, r: format_html("{}", "x"))
        sp.register("core.auditlog", title=str, blocks=[block], generic_blocks=())
        sp.register("email_assistant.emailmessage", title=str, blocks=[block], generic_blocks=())
        self.assertIsNone(sp.get("core.auditlog"))
        self.assertIsNone(sp.get("email_assistant.emailmessage"))
        self.assertIsNone(sp.get("jlcpcb.jlcconfig"))
        r = self.client.get("/admin/core/auditlog/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("mv-panel-config", r.content.decode())
        self.assertEqual(self.client.get("/core/panel/core.auditlog/1/").status_code, 404)

    def test_missing_provider_module_gives_no_panel_and_no_error(self):
        sp._registry.pop("inventory.productcategory", None)
        with mock.patch.dict(sp.PROVIDERS, {"inventory.productcategory": "core.no_such_provider_module"}):
            with self.assertLogs("core.side_panel", "WARNING"):
                self.assertIsNone(sp.get("inventory.productcategory"))
            r = self.client.get("/admin/inventory/productcategory/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("mv-panel-config", r.content.decode())

    def test_generic_panels_need_core_panel_generic(self):
        sp._registry.pop("inventory.purchaseorder", None)
        with mock.patch.dict(sys.modules, {"core.panel_generic": None}):
            self.assertIsNone(sp.get("inventory.purchaseorder"))
            r = self.client.get("/admin/inventory/purchaseorder/")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("mv-panel-config", r.content.decode())
            # свій постачальник без загальних розділів реєструється й так
            self.assertEqual([b.key for b in register_location_panel().blocks], ["main"])
        fake = types.ModuleType("core.panel_generic")
        fake.generic_panel = lambda model: sp.Panel(
            model, title=lambda o: format_html("<b>{}</b>", o),
            blocks=[sp.Block("fields", "Усі поля", lambda o, r: sp.kv([("Код", o.code)]))], generic=True)
        fake.make = lambda key: sp.Block(key, key.title(), lambda o, r: "", lazy=True, collapsed=True)
        with mock.patch.dict(sys.modules, {"core.panel_generic": fake}):
            panel = sp.get("inventory.purchaseorder")
            self.assertTrue(panel.generic)
            self.assertIs(sp.get("inventory.purchaseorder"), panel)            # один раз
            self.assertFalse(sp.prefs(self.root, panel)["on"])                 # загальна — вимкнена
            own = sp.register("inventory.location", title=str,
                              blocks=[sp.Block("history", "Моя історія", lambda o, r: "")])
        self.assertEqual([b.key for b in own.blocks], ["history", "fields", "related"])
        self.assertEqual(own.block("history").title, "Моя історія")           # свій розділ не замінюється

    def test_v1_registration_and_names_still_work(self):
        loc = Location.objects.create(code="MAIN", name="Головний склад")
        sp.register("inventory.location", title=lambda o: format_html("<b>{}</b>", o.code),
                    blocks=[sp.Block("main", "Головне", lambda o, r: format_html("<p>{}</p>", o.name), True)],
                    actions=[sp.Action("open", "📋 Відкрити картку", lambda o: f"/admin/inventory/location/{o.pk}/change/",
                                       True, False)])
        UserProfile.objects.filter(user=self.root).update(ui_prefs={"panels": {"inventory.location": {
            "on": True, "blocks": ["main"], "actions": ["open"], "width": 400}}})
        r = self.client.get(f"/core/panel/inventory.location/{loc.pk}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Головний склад", html)
        self.assertIn(f'href="/admin/inventory/location/{loc.pk}/change/"', html)
        self.assertEqual(sp.prefs(self.fresh(), sp.get("inventory.location"))["width"], 400)
        self.assertEqual(sp.WIDTHS, sp.PRESETS)
        self.assertIsNone(sp.DEFAULT_WIDTH)


class ConfigTests(PanelTestBase):
    def test_list_page_has_config_and_v2_assets(self):
        html = self.client.get("/admin/inventory/supplier/").content.decode()
        self.assertIn('id="mv-panel-config"', html)
        self.assertIn("/static/admin/js/mv_panel.js?v=6", html)
        self.assertIn("/static/admin/css/mv_panel.css?v=6", html)
        cfg = cfg_of(html)
        self.assertEqual((cfg["v"], cfg["model"], cfg["url"], cfg["prefsUrl"]),
                         (2, MODEL, URL, URL + "prefs/"))
        self.assertTrue(cfg["canChange"])
        self.assertFalse(cfg["generic"])
        self.assertEqual(cfg["page"], {"num": 1, "pages": 1, "param": "p"})

    def test_config_contents_follow_permissions(self):
        cfg, out = self.tag_config(self.root)
        self.assertIn("mv_panel.js?v=6", out)
        self.assertEqual([b["key"] for b in cfg["blocks"]],
                         ["info", "secret", "lazy", "folded", "na", "boom", "blank", "extra"])
        info = cfg["blocks"][0]
        self.assertEqual((info["short"], info["icon"], info["lazy"], info["collapsed"]), ("Контакти", "📇", False, False))
        mail = cfg["actions"][0]
        self.assertEqual((mail["key"], mail["icon"], mail["short"], mail["kind"], mail["hotkey"]),
                         ("mail", "✉️", "Написати", "link", "KeyM"))
        self.assertEqual([e["key"] for e in cfg["edits"]], ["email", "notes", "person"])
        self.assertEqual([c["name"] for c in cfg["columns"]],
                         ["name", "contact_person", "email", "phone", "currency", "website"])
        self.assertEqual(cfg["columns"][1]["label"], "Контактна особа")
        self.assertEqual(cfg["hideDefault"], ["phone"])
        self.assertEqual(cfg["width"], {"min": 320, "max": 960, "presets": [360, 440, 560, 720]})
        self.assertEqual(cfg["shared"], ["nav", "view", "chips", "density", "actions_at", "place", "helpers", "empty"])
        self.assertEqual(cfg["prefs"]["nav"], "top")
        self.assertTrue(cfg["canDelete"])
        # склад: продажів не бачить — розділу й дії з правом продажів немає, змінювати може
        cfg, _ = self.tag_config(self.user("sklad", "warehouse"))
        self.assertNotIn("secret", [b["key"] for b in cfg["blocks"]])
        self.assertNotIn("orders", [a["key"] for a in cfg["actions"]])
        self.assertTrue(cfg["canChange"])
        self.assertFalse(cfg["canDelete"])
        # бухгалтер: склад лише переглядає — швидких змін немає
        cfg, _ = self.tag_config(self.user("buh", "accountant"))
        self.assertFalse(cfg["canChange"])
        self.assertEqual(cfg["edits"], [])
        # без розділу «Склад» — нічого
        cfg, out = self.tag_config(self.user("guest", "readonly"))
        self.assertIsNone(cfg)
        self.assertEqual(out, "")

    def test_saved_prefs_reach_config(self):
        self.post_prefs({"set": {"blocks": ["lazy", "info"], "width": 480}, "shared": {"view": "tabs"}})
        cfg, _ = self.tag_config(self.fresh())
        self.assertEqual(cfg["prefs"]["blocks"], ["lazy", "info"])
        self.assertEqual(cfg["prefs"]["width"], 480)
        self.assertEqual(cfg["prefs"]["view"], "tabs")


class ShellTests(PanelTestBase):
    def test_shell_header_and_sections(self):
        pk = self.supplier.pk
        with self.assertLogs("core.side_panel", "WARNING") as logs:
            html = self.shell()
        self.assertTrue(any("boom" in line for line in logs.output))
        for part in ('class="mvp-shell"', f'data-model="{MODEL}"', f'data-pk="{pk}"', 'data-label="Muster GmbH"',
                     f'data-change-url="/admin/inventory/supplier/{pk}/change/"',
                     f'data-history-url="/admin/inventory/supplier/{pk}/history/"',
                     f'data-delete-url="/admin/inventory/supplier/{pk}/delete/"', 'data-can-change="1"',
                     'data-mvp-nav="-1"', 'data-mvp-nav="1"', 'tabindex="-1"><span><b>Muster GmbH</b></span></h2>',
                     'data-mvp-open href="/admin/inventory/supplier/', 'data-mvp-menu', 'data-mvp-close',
                     '<span translate="no">anna@muster.example</span>', 'data-mvp-pos hidden',
                     '<nav class="mvp-nav" aria-label="Розділи запису"></nav>', 'class="mvp-body" tabindex="0"',
                     'data-mvp-refresh', 'data-mvp-keys', 'оновлено '):
            self.assertIn(part, html)
        order = [html.index(f'data-sec="{k}"') for k in ("info", "secret", "lazy", "folded", "na", "boom", "blank")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('data-sec="extra"', html)                              # не за замовчуванням
        info = self.section(html, "info")
        for part in ('data-count="2"', 'data-tone="ok"', 'data-badge="дві адреси"',
                     'data-brief="Anna · anna@muster.example"', 'aria-expanded="true"',
                     # група 1 адмінки («📞 Контакти») — у картці 2: після «🏢 Постачальник» ще «📁 Проєкти»
                     f'data-card="/admin/inventory/supplier/{pk}/change/#fieldset-0-2-heading"', "mvp-sh-card",
                     '<dt>Email</dt><dd><span translate="no">anna@muster.example</span></dd>'):
            self.assertIn(part, info)
        self.assertIn("Таємні продажі", self.section(html, "secret"))
        lazy = self.section(html, "lazy")
        self.assertIn("data-lazy", lazy)
        self.assertIn("data-deferred", lazy)
        self.assertIn('class="mvp-skel"', lazy)
        self.assertNotIn("Повільно пораховано", html)
        folded = self.section(html, "folded")
        self.assertIn("data-collapsed", folded)
        self.assertIn("data-deferred", folded)
        self.assertIn('aria-expanded="false"', folded)
        self.assertNotIn("Згорнуте всередині", html)
        na = self.section(html, "na")
        self.assertIn("data-na", na)
        self.assertIn("Не стосується цього запису.", na)
        self.assertNotIn("Ніколи", html)
        boom = self.section(html, "boom")
        self.assertIn("data-error", boom)
        self.assertIn("Не вдалося показати цей блок.", boom)
        self.assertIn('data-mvp-retry="boom"', boom)
        blank = self.section(html, "blank")
        self.assertIn("data-empty", blank)
        self.assertIn("Тут нічого немає.", blank)

    def test_open_renders_a_lazy_body_now(self):
        html = self.shell(query="?open=lazy")
        self.assertIn("Повільно пораховано", html)
        self.assertNotIn("data-deferred", self.section(html, "lazy"))
        html = self.shell(query="?open=folded")                                 # згорнутий лишається згорнутим
        self.assertIn("data-deferred", self.section(html, "folded"))

    def test_header_edits_actions_and_memo(self):
        html = self.shell()
        hedit = html[html.index('class="mvp-hedit"'):html.index('class="mvp-subt"')]   # статус — у другому рядку шапки
        self.assertLess(html.index("data-mvp-close"), html.index('class="mvp-hedit"'))
        for part in ('class="mvp-pill mvp-t-ok"', 'data-edit="person"', 'data-kind="choice"', 'data-value="Anna"',
                     "👨 Bob", "Передати Bob?", 'aria-haspopup="menu"', 'title="Змінити: Контакт · S"', "👩 Anna ▾"):
            self.assertIn(part, hedit)
        memo = html[html.index('class="mvp-memo"'):html.index("</header>")]
        for part in ('data-edit="notes"', 'data-kind="textarea"', 'data-maxlength="50"',
                     '<span class="mvp-ed-v" translate="no">📝 Ruft montags an</span>'):
            self.assertIn(part, memo)
        bar = html[html.index('class="mvp-actbar"'):html.index('class="mvp-memo"')]
        self.assertIn('<a class="mvp-act" data-act="mail" href="mailto:anna@muster.example" '
                      'title="✉️ Написати · M">✉️ Написати</a>', bar)
        self.assertIn(f'href="/admin/inventory/supplier/{self.supplier.pk}/change/"', bar)    # дія v1 (лише запис)
        self.assertIn('data-act="orders"', bar)
        site = bar[bar.index('data-act="site"') - 30:]
        self.assertIn('target="_blank" rel="noopener"', site[:200])
        self.assertIn("hidden", site[:250])                                     # не вибрана — схована
        self.assertLess(bar.index('data-act="card"'), bar.index('data-act="site"'))
        self.assertIn('data-mvp-more', bar)

    def test_role_without_change_sees_values_only(self):
        self.login(self.user("buh", "accountant"))
        html = self.shell()
        self.assertIn('data-can-change="0"', html)
        self.assertNotIn("data-edit=", html)
        self.assertNotIn("data-delete-url", html)
        self.assertIn('<span class="mvp-badge mvp-t-ok">👩 Anna</span>', html)
        self.assertIn("📝 Ruft montags an", html)

    def test_role_without_sales_has_no_sales_block_or_action(self):
        self.login(self.user("sklad", "warehouse"))
        html = self.shell()
        self.assertNotIn('data-sec="secret"', html)
        self.assertNotIn("Таємні продажі", html)
        self.assertNotIn('data-act="orders"', html)
        self.assertIn('data-edit="person"', html)                               # склад змінювати може

    def test_unavailable_action_shows_reason(self):
        other = Supplier.objects.create(name="Ohne Mail AG")
        html = self.shell(pk=other.pk)
        self.assertIn('<span class="mvp-act is-off" data-act="mail" aria-disabled="true" title="Немає email">'
                      '✉️ Написати</span>', html)
        self.assertNotIn('data-act="site"', html)                               # немає адреси й причини — немає кнопки
        self.assertIn("📝 Додати нотатку", html)                                # нотатки немає — кнопка додати

    def test_unknown_record_or_model(self):
        self.assertEqual(self.client.get(f"{URL}999999/").status_code, 404)
        self.assertEqual(self.client.get("/core/panel/nope.model/1/").status_code, 404)

    def test_roles_without_the_module(self):
        self.login(self.user("guest", "readonly"))
        pk = self.supplier.pk
        for url in (f"{URL}{pk}/", f"{URL}{pk}/block/lazy/"):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(f"{URL}prefs/", "{}", content_type="application/json").status_code, 403)
        self.assertEqual(self.post_edit({"key": "email", "value": "x@y.z"}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(f"{URL}{pk}/").status_code, 302)
        self.assertEqual(self.client.get(f"{URL}{pk}/block/lazy/").status_code, 302)


class BlockEndpointTests(PanelTestBase):
    def test_block_endpoint_renders_one_section(self):
        r = self.client.get(f"{URL}{self.supplier.pk}/block/lazy/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertTrue(html.startswith('<section class="mvp-sec"'))
        self.assertIn('data-sec="lazy"', html)
        self.assertIn("Повільно пораховано", html)
        self.assertNotIn("data-deferred", html)
        self.assertNotIn("mvp-shell", html)
        html = self.client.get(f"{URL}{self.supplier.pk}/block/folded/").content.decode()
        self.assertIn("Згорнуте всередині", html)
        self.assertIn("data-collapsed", html)

    def test_unknown_or_forbidden_key_is_404(self):
        pk = self.supplier.pk
        self.assertEqual(self.client.get(f"{URL}{pk}/block/nope/").status_code, 404)
        self.assertEqual(self.client.get(f"{URL}999999/block/info/").status_code, 404)
        self.login(self.user("sklad", "warehouse"))
        self.assertEqual(self.client.get(f"{URL}{pk}/block/secret/").status_code, 404)
        self.assertEqual(self.client.get(f"{URL}{pk}/block/info/").status_code, 200)


class PrefsTests(PanelTestBase):
    def test_legacy_v1_payload(self):
        p = self.post_prefs({"blocks": ["lazy", "info", "nope", "info"], "actions": ["site", "evil"],
                             "width": 480, "on": True})
        self.assertEqual((p["blocks"], p["actions"], p["width"], p["on"]), (["lazy", "info"], ["site"], 480, True))
        stored = self.stored()["panels"][MODEL]
        self.assertEqual(stored["seen"]["blocks"], [b.key for b in self.panel.blocks])
        p = self.post_prefs({"blocks": None, "actions": None, "width": None, "on": True, "_reset": True})
        self.assertEqual(p["blocks"], sp.defaults(self.panel)["blocks"])
        self.assertEqual(p["actions"], ["mail", "orders", "card"])
        self.assertIsNone(p["width"])

    def test_set_shared_and_reset_variants(self):
        p = self.post_prefs({"set": {"collapsed": ["info"], "start": "lazy", "bar": 2, "width": 999}})
        self.assertEqual((p["collapsed"], p["start"], p["bar"], p["width"]), (["info"], "lazy", 2, None))
        p = self.post_prefs({"set": {"width": 480, "start": ["x"]}})
        self.assertEqual((p["width"], p["start"], p["bar"]), (480, "last", 2))
        p = self.post_prefs({"set": {"bar": True}})                            # bool — не число: як було
        self.assertEqual(p["bar"], 4)
        self.assertEqual(self.post_prefs({"set": {"bar": 99}})["bar"], 8)
        p = self.post_prefs({"shared": {"nav": "side", "view": "tabs", "density": "evil"}})
        self.assertEqual((p["nav"], p["view"], p["density"]), ("side", "tabs", "normal"))
        self.assertEqual(self.stored()["panels"]["*"], {"nav": "side", "view": "tabs"})
        p = self.post_prefs({"reset": "blocks"})
        self.assertEqual((p["collapsed"], p["start"], p["width"], p["nav"]), (["folded"], "last", 480, "side"))
        p = self.post_prefs({"set": {"actions": ["admin:remind", "card", "admin:bad name", "mail", "nope"], "bar": 3}})
        self.assertEqual((p["actions"], p["bar"]), (["admin:remind", "card", "mail"], 3))
        p = self.post_prefs({"reset": "actions"})
        self.assertEqual((p["actions"], p["bar"]), (["mail", "orders", "card"], 4))
        p = self.post_prefs({"reset": "layout"})
        self.assertEqual((p["width"], p["nav"], p["view"]), (None, "top", "scroll"))
        self.assertNotIn("*", self.stored()["panels"])
        self.post_prefs({"set": {"on": False, "blocks": ["info"]}, "shared": {"empty": "show"}})
        p = self.post_prefs({"set": {"blocks": None}})                          # null — як було
        self.assertEqual(p["blocks"], sp.defaults(self.panel)["blocks"])
        self.assertFalse(p["on"])
        p = self.post_prefs({"reset": "list", "set": {"width": 560}})           # reset, потім set
        self.assertEqual((p["on"], p["width"], p["empty"]), (True, 560, "show"))
        p = self.post_prefs({"reset": "all"})
        self.assertEqual((p["width"], p["empty"]), (None, "compact"))
        self.assertEqual(set(self.stored()["panels"][MODEL]), {"seen"})
        self.assertNotIn("*", self.stored()["panels"])

    def test_shared_key_sent_with_set_goes_to_shared(self):
        p = self.post_prefs({"set": {"nav": "none", "width": 440}})
        self.assertEqual((p["nav"], p["width"]), ("none", 440))
        self.assertEqual(self.stored()["panels"]["*"], {"nav": "none"})
        self.assertNotIn("nav", self.stored()["panels"][MODEL])

    def test_hide_cols_are_checked(self):
        p = self.post_prefs({"set": {"hide_cols": ["status_badge", "bad-name", "X", "<script>", "ok_col", "ok_col"]}})
        self.assertEqual(p["hide_cols"], ["status_badge", "ok_col"])
        self.assertEqual(self.post_prefs({"set": {"hide_cols": []}})["hide_cols"], [])
        self.assertIsNone(self.post_prefs({"set": {"hide_cols": None}})["hide_cols"])
        self.assertIsNone(self.post_prefs({"set": {"hide_cols": "phone"}})["hide_cols"])

    def test_seen_merge_brings_new_default_blocks(self):
        self.post_prefs({"set": {"blocks": ["info", "lazy"]}})
        self.panel.blocks.append(sp.Block("fresh", "Нове", lambda o, r: "", collapsed=True))
        self.panel.blocks.append(sp.Block("fresh_off", "Нове вимкнене", lambda o, r: "", default=False))
        p = sp.prefs(self.fresh(), self.panel)
        self.assertEqual(p["blocks"], ["info", "lazy", "fresh"])
        self.assertIn("fresh", p["collapsed"])
        p = self.post_prefs({"set": {"width": 440}})                            # уже бачене лишається вибраним
        self.assertEqual(p["blocks"], ["info", "lazy", "fresh"])
        p = self.post_prefs({"set": {"blocks": ["info"]}})                      # прибрала — назад не повертається
        self.assertEqual(p["blocks"], ["info"])
        self.assertEqual(sp.prefs(self.fresh(), self.panel)["blocks"], ["info"])

    def test_v1_saved_prefs_use_seen_v1(self):
        UserProfile.objects.filter(user=self.root).update(ui_prefs={"panels": {MODEL: {
            "on": True, "blocks": ["info"], "actions": ["mail"], "width": 400}}})
        self.panel.seen_v1 = {"blocks": ("info", "secret", "lazy", "na", "boom", "blank", "extra"),
                              "actions": ("mail", "orders", "site", "card")}
        p = sp.prefs(self.fresh(), self.panel)
        self.assertEqual(p["blocks"], ["info", "folded"])                       # «folded» з'явився після v1
        self.assertEqual(p["collapsed"], ["folded"])
        self.assertEqual((p["actions"], p["width"]), (["mail"], 400))

    def test_other_ui_prefs_stay(self):
        UserProfile.objects.filter(user=self.root).update(ui_prefs={
            "theme": "light", "menu": {"mode": "full"}, "panels": {"sales.salesorder": {"on": False}}})
        self.post_prefs({"set": {"on": False}})
        ui = self.stored()
        self.assertEqual((ui["theme"], ui["menu"]), ("light", {"mode": "full"}))
        self.assertEqual(ui["panels"]["sales.salesorder"], {"on": False})
        self.assertIs(ui["panels"][MODEL]["on"], False)

    def test_bad_json(self):
        for body in ("{nope", "[1, 2]"):
            r = self.client.post(f"{URL}prefs/", body, content_type="application/json")
            self.assertEqual(r.status_code, 400)
            self.assertFalse(r.json()["ok"])

    def test_clean_drops_unknown_and_wrong_types(self):
        out = sp.clean(self.panel, {"on": "yes", "blocks": "info", "collapsed": ["folded", 5], "width": "480",
                                    "start": "first", "evil": 1, "seen": {"blocks": ["info", "gone"]}})
        self.assertEqual(out, {"collapsed": ["folded"], "width": 480, "start": "first",
                               "seen": {"blocks": ["info"], "actions": []}})
        self.assertEqual(sp.clean_shared({"nav": "side", "view": "evil", "x": "top"}), {"nav": "side"})


class EditTests(PanelTestBase):
    def test_edit_saves_logs_and_returns_parts(self):
        r = self.post_edit({"key": "email", "value": "neu@muster.example", "expect": "anna@muster.example"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual((j["value"], j["message"]), ("neu@muster.example", "Email: neu@muster.example"))
        self.assertTrue(j["head"].startswith('<header class="mvp-head">'))
        self.assertEqual(list(j["blocks"]), ["info"])                           # розділ, що залежить від email
        self.assertIn("neu@muster.example", j["blocks"]["info"])
        self.assertEqual(j["cells"], {"email": "neu@muster.example"})
        self.assertEqual(j["undo"], {"key": "email", "value": "anna@muster.example"})
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.email, "neu@muster.example")
        entry = LogEntry.objects.get(object_id=str(self.supplier.pk))
        self.assertEqual(entry.user, self.root)
        self.assertIn("Email", entry.get_change_message())

    def test_same_value_changes_nothing(self):
        j = self.post_edit({"key": "email", "value": "anna@muster.example"}).json()
        self.assertEqual((j["message"], j["undo"]), ("Без змін.", None))
        self.assertFalse(LogEntry.objects.exists())

    def test_expect_with_windows_line_breaks_is_not_stale(self):
        # браузер у атрибуті data-value міняє CRLF на LF — це той самий текст, не чужа зміна
        self.supplier.notes = "Ruft\r\nmontags an"
        self.supplier.save(update_fields=["notes"])
        r = self.post_edit({"key": "notes", "value": "Ruft dienstags an", "expect": "Ruft\nmontags an"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.notes, "Ruft dienstags an")

    def test_stale_expect_gives_409_with_fresh_panel(self):
        r = self.post_edit({"key": "email", "value": "x@muster.example", "expect": "alt@muster.example"})
        self.assertEqual(r.status_code, 409)
        j = r.json()
        self.assertEqual((j["ok"], j["error"]), (False, "stale"))
        self.assertEqual(j["message"], "Запис щойно змінив інший користувач — показано свіже.")
        self.assertIn('class="mvp-shell"', j["html"])
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.email, "anna@muster.example")

    def test_invalid_and_not_editable(self):
        r = self.post_edit({"key": "email", "value": "kein-email"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid"))
        with mock.patch.object(SupplierAdmin, "get_readonly_fields", return_value=("email",)):
            r = self.post_edit({"key": "email", "value": "neu@muster.example"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "not_editable"))
        with mock.patch.object(SupplierAdmin, "get_fieldsets", return_value=[(None, {"fields": ["name"]})]):
            r = self.post_edit({"key": "notes", "value": "Neu"})
            self.assertEqual((r.status_code, r.json()["error"]), (400, "not_editable"))
            self.panel.extra_fields = ("notes",)
            self.assertEqual(self.post_edit({"key": "notes", "value": "Neu"}).status_code, 200)
        self.assertEqual((self.post_edit({"key": "person", "value": "Carl"}).status_code), 400)
        self.assertEqual(self.post_edit({"key": "person", "value": "Carl"}).json()["error"], "bad_value")
        r = self.client.post(f"{URL}{self.supplier.pk}/edit/", "{nope", content_type="application/json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_json"))
        self.supplier.refresh_from_db()
        self.assertEqual((self.supplier.email, self.supplier.contact_person), ("anna@muster.example", "Anna"))

    def test_choice_and_maxlength(self):
        r = self.post_edit({"key": "person", "value": "Bob", "expect": "Anna"})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["message"], "Контакт: Bob")
        self.assertIn('data-value="Bob"', j["head"])
        self.assertIsNone(j["undo"])                                            # без undo
        r = self.post_edit({"key": "notes", "value": "x" * 80})
        self.assertEqual(r.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual((self.supplier.contact_person, self.supplier.notes), ("Bob", "x" * 50))

    def test_permissions_and_queryset(self):
        self.assertEqual(self.post_edit({"key": "nope", "value": "x"}).status_code, 404)
        self.assertEqual(self.post_edit({"key": "email", "value": "x@y.z"}, pk=999999).status_code, 404)
        with mock.patch.object(SupplierAdmin, "get_queryset",
                               lambda self_, request: Supplier.objects.exclude(name="Muster GmbH")):
            self.assertEqual(self.post_edit({"key": "email", "value": "x@y.z"}).status_code, 404)
        self.panel.edits.append(sp.Edit("phone", "phone", "Телефон", "text", perm=lambda request, obj: False))
        self.assertEqual(self.post_edit({"key": "phone", "value": "1"}).status_code, 403)
        self.login(self.user("buh", "accountant"))
        r = self.post_edit({"key": "email", "value": "x@y.z"})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "forbidden"))
        self.login(self.user("sklad", "warehouse"))
        self.assertEqual(self.post_edit({"key": "email", "value": "sklad@muster.example"}).status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.email, "sklad@muster.example")

    def test_bool_toggle_choice_from_model_and_cells(self):
        register_location_panel()
        loc = Location.objects.create(code="MAIN", name="Головний", location_type="other", is_active=True)
        html = self.client.get(f"/core/panel/inventory.location/{loc.pk}/").content.decode()
        self.assertIn('class="mvp-act mvp-tog" data-act="active" data-edit="active" data-kind="bool" data-value="1" '
                      'aria-pressed="true" data-on="✅" data-off="⛔" title="Активна · A"', html)
        self.assertIn("Готова продукція 🏭", html)                              # choices поля моделі
        self.assertIn("Інше (загальний склад) ▾", html)
        r = self.post_edit({"key": "active", "value": False, "expect": "1"}, pk=loc.pk, model="inventory.location")
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual((j["message"], j["value"], j["undo"]), ("Активна: ні", "", {"key": "active", "value": "1"}))
        self.assertIn("icon-no", j["cells"]["is_active"])
        self.assertIn('aria-pressed="false"', j["head"])
        self.assertEqual(list(j["blocks"]), ["main"])
        loc.refresh_from_db()
        self.assertFalse(loc.is_active)
        r = self.post_edit({"key": "active", "value": "1", "expect": ""}, pk=loc.pk, model="inventory.location")
        self.assertEqual(r.json()["value"], "1")                                # «↶ Скасувати» — те саме з undo.value
        r = self.post_edit({"key": "type", "value": "nonsense"}, pk=loc.pk, model="inventory.location")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid"))
        r = self.post_edit({"key": "type", "value": "finished"}, pk=loc.pk, model="inventory.location")
        self.assertEqual(r.json()["message"], "Тип складу: Готова продукція 🏭")
        self.assertIn('class="mvp-pill mvp-t-done"', r.json()["head"])


class AdminActionTests(PanelTestBase):
    def actions_for(self, user):
        ma = admin.site._registry[Supplier]
        return [a["name"] for a in panel_edit.admin_actions(self.panel, ma, self.request_for(user))]

    def test_actions_filtered_by_permission(self):
        with mock.patch.object(SupplierAdmin, "actions", [remind, report, recur]):
            self.assertEqual(self.actions_for(self.root), ["remind", "report", "recur"])   # без «Видалити обрані»
            buh = self.user("buh", "accountant")
            self.assertEqual(self.actions_for(buh), ["report"])
            self.panel.admin_perms = {"remind": "module:inventory"}
            self.assertEqual(self.actions_for(self.fresh(buh)), ["remind", "report"])
            self.panel.admin_perms = {"report": lambda request, obj: False}
            self.assertEqual(self.actions_for(self.fresh(buh)), [])

    def test_pinned_action_is_a_bar_button(self):
        with mock.patch.object(SupplierAdmin, "actions", [remind, report]):
            self.post_prefs({"set": {"actions": ["admin:remind", "mail"]}})
            html = self.shell()
            cfg, _ = self.tag_config(self.fresh())
        self.assertIn('<button type="button" class="mvp-act" data-act="admin:remind" data-admin-action="remind" '
                      'data-confirm="Виконати «✉️ Нагадати» для Muster GmbH?" title="✉️ Нагадати">✉️ Нагадати</button>',
                      html)
        self.assertLess(html.index('data-act="admin:remind"'), html.index('data-act="mail"'))
        report_btn = html[html.index('data-act="admin:report"'):]
        self.assertIn(" hidden>", report_btn[:250])
        self.assertNotIn("delete_selected", html)
        self.assertEqual(cfg["adminActions"], [{"name": "remind", "label": "✉️ Нагадати"},
                                               {"name": "report", "label": "👁 Звіт"}])


class HelperTests(PanelTestBase):
    def test_values_are_escaped_and_marked_as_data(self):
        self.assertEqual(sp.data("<b>"), '<span translate="no">&lt;b&gt;</span>')
        self.assertEqual((sp.data(""), sp.data(None)), ("", ""))
        self.assertEqual(sp.data(0), '<span translate="no">0</span>')
        html = sp.kv([("Email", "<x>"), ("Порожньо", ""), ("Немає", None), ("Список", []), ("Тон", "так", "warn"),
                      ("Посилання", sp.link("/admin/", "Огляд"))])
        self.assertTrue(html.startswith('<dl class="mvp-kv">'))
        self.assertIn('<dt>Email</dt><dd><span translate="no">&lt;x&gt;</span></dd>', html)
        self.assertNotIn("Порожньо", html)
        self.assertNotIn("Немає", html)
        self.assertNotIn("Список", html)
        self.assertIn('<div class="mvp-kv-r t-warn">', html)
        self.assertIn('<a class="mvp-link" href="/admin/">Огляд</a>', html)
        self.assertEqual(sp.kv([("A", None)]), "")
        self.assertEqual(sp.link("javascript:alert(1)", "x"), "x")
        self.assertIn('href="mailto:a@b.example"', sp.link("mailto:a@b.example", "a"))
        self.assertIn('target="_blank" rel="noopener"', sp.link("https://x.example", "x", new_tab=True))
        self.assertIn('data-copy="a&amp;b"', sp.copy("a&b"))
        self.assertIn('aria-label="Копіювати: Email"', sp.copy("a@b", "Email"))
        self.assertEqual(sp.copy(""), "")
        self.assertEqual(sp.badge("x", "evil"), '<span class="mvp-badge">x</span>')
        self.assertEqual(sp.badge("Нове", "new"), '<span class="mvp-badge mvp-t-new">Нове</span>')
        self.assertIn("<td><span translate=\"no\">&lt;i&gt;</span></td>", sp.table(["A"], [["<i>"]]))
        self.assertEqual(sp.table(["A"], []), "")
        self.assertEqual(sp.more("/admin/", "Усі →"), '<a class="mvp-more-link" href="/admin/">Усі →</a>')

    def test_number_and_date_formats(self):
        self.assertEqual(sp.money(Decimal("1234.5"), "EUR"), "1 234,50 EUR")
        self.assertEqual(sp.money(-1234567.891), "-1 234 567,89")
        self.assertEqual(sp.money(None, "EUR"), "")
        self.assertEqual((sp.qty(Decimal("10.000")), sp.qty(Decimal("2.50")), sp.qty(None)), ("10", "2,5", ""))
        self.assertEqual(sp.date(date(2026, 10, 31)), "31.10.2026")
        aware = timezone.make_aware(datetime(2026, 10, 31, 14, 32))
        self.assertEqual(sp.dt(aware), "31.10.2026 14:32")
        self.assertEqual(sp.date(aware), "31.10.2026")
        self.assertEqual([sp.value_text(v) for v in (None, True, False, date(2026, 11, 3), Decimal("1.50"), 7)],
                         ["", "1", "", "2026-11-03", "1.50", "7"])
        self.assertEqual(sp.split_icon("🏷 Етикетка"), ("🏷", "Етикетка"))
        self.assertEqual(sp.split_icon("✉️ Лист клієнту"), ("✉️", "Лист клієнту"))
        self.assertEqual(sp.split_icon("Статус"), ("", "Статус"))
        self.assertEqual((sp.key_name("KeyF"), sp.key_name("Digit1"), sp.key_name("Slash")), ("F", "1", "/"))

    def test_memo_runs_once_per_record(self):
        calls = []
        for _ in range(3):
            self.assertEqual(sp.memo(self.supplier, "stats", lambda: calls.append(1) or 42), 42)
        self.assertEqual(len(calls), 1)

    def test_admin_link_only_when_the_card_opens(self):
        link = sp.admin_link(self.request_for(self.root), self.supplier)
        self.assertEqual(link, f'<a class="mvp-link" href="/admin/inventory/supplier/{self.supplier.pk}/change/" '
                               'translate="no">Muster GmbH</a>')
        guest = self.user("guest", "readonly")
        self.assertEqual(sp.admin_link(self.request_for(guest), self.supplier, "Muster"),
                         '<span translate="no">Muster</span>')
        self.assertEqual(sp.admin_link(self.request_for(self.root), None), "")

    def test_edit_html_without_bound_request(self):
        request = self.request_for(self.root)
        html = sp.edit_html(request, self.supplier, "email")
        self.assertIn('data-edit="email"', html)
        self.assertIn('data-maxlength="254"', html)                            # max_length поля моделі
        request = self.request_for(self.user("buh", "accountant"))
        self.assertEqual(sp.edit_html(request, self.supplier, "email"), '<span translate="no">anna@muster.example</span>')
        self.assertEqual(sp.edit_html(request, self.supplier, "nope"), "")
