"""📌 Панель замовлень (sales/side_panel.py) і захист дій списку замовлень (sales/admin.py): розділи й дії за
замовчуванням, вибір людини (і збережений ще у v1), склад позицій, листування лише з власних скриньок, нотатки без
листів, швидкі зміни тим самим шляхом, що картка, дії списку й ⭐ / 📝 лише з правом змінювати.

    python manage.py test sales --settings=tabele.settings_test
"""
import itertools
import json
import shutil
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal as D
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from core import panel_edit
from core import side_panel as sp
from core.models import AuditLog, UserProfile
from inventory.models import InventorySettings, InventoryTransaction, Location, Product, PurchaseOrder, \
    PurchaseOrderLine
from inventory.services import stock
from sales import side_panel as orders_panel
from sales.models import SalesOrder, SalesOrderLine, SalesSource

MODEL = "sales.salesorder"
URL = "/core/panel/sales.salesorder/"
NUMBER = "102497269"
DEFAULT_BLOCKS = ["summary", "lines", "shipping", "money", "documents", "mail", "notes", "links", "history", "fields"]


def cfg_of(html):
    return json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])


class _ChangeForm(HTMLParser):
    """Поля форми картки так, як їх надіслав би браузер (для «Зберегти» в картці в тесті)."""

    def __init__(self, form_id):
        super().__init__(convert_charrefs=True)
        self.form_id, self.depth, self.data = form_id, 0, []
        self._select = self._textarea = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            if self.depth or a.get("id") == self.form_id:
                self.depth += 1
            return
        if not self.depth:
            return
        name = None if "disabled" in a else a.get("name")        # вимкнених полів браузер не надсилає
        if tag == "input" and name:
            kind = (a.get("type") or "text").lower()
            if kind in ("submit", "button", "file", "image", "reset"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            value = a.get("value")
            self.data.append((name, value if value is not None else ("on" if kind in ("checkbox", "radio") else "")))
        elif tag == "select" and name:
            self._select = {"name": name, "multiple": "multiple" in a, "selected": [], "first": None}
        elif tag == "option" and self._select is not None:
            if self._select["first"] is None:
                self._select["first"] = a.get("value", "")
            if "selected" in a:
                self._select["selected"].append(a.get("value", ""))
        elif tag == "textarea" and name:
            self._textarea, self._text = name, []

    def handle_endtag(self, tag):
        if tag == "form" and self.depth:
            self.depth -= 1
        elif tag == "select" and self._select is not None:
            s = self._select
            values = s["selected"] or ([] if s["multiple"] else [s["first"] or ""])
            self.data += [(s["name"], v) for v in values]
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            text = "".join(self._text)
            self.data.append((self._textarea, text[1:] if text.startswith("\n") else text))
            self._textarea = None

    def handle_data(self, data):
        if self._textarea is not None:
            self._text.append(data)


class OrderPanelBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        saved = dict(sp._registry)

        def restore():
            sp._registry.clear()
            sp._registry.update(saved)
        self.addCleanup(restore)
        self.mode("creation")
        self.loc = Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)
        SalesSource.objects.create(slug="manual", name="Вручну")
        self.product = Product.objects.create(sku="FT220407", name="Filter 2.4 GHz")
        self.order = SalesOrder.objects.create(
            source="manual", order_number=NUMBER, client="Kunde A GmbH", contact_name="Anna Muster",
            email="a@kunde.example", phone="+49 30 1234", total_price=D("486"), currency="EUR",
            order_date=timezone.localdate(), addr_country="DE", addr_city="Berlin", addr_zip="10115",
            addr_street="Hauptstr. 1")
        SalesOrderLine.objects.create(order=self.order, product=self.product, sku_raw="FT220407", qty=D("10"),
                                      unit_price=D("48.6"), currency="EUR")
        self.panel = sp.get(MODEL)
        self._uid = itertools.count(1)

    # ── помічники ─────────────────────────────────────────────────────────────

    def mode(self, deduct_on, reservation=False):
        s = InventorySettings.get()
        s.deduct_on, s.use_reservation, s.default_location = deduct_on, reservation, "MAIN"
        s.save()

    def receive(self, product, qty):
        InventoryTransaction.objects.create(tx_type="Incoming", qty=qty, product=product, location=self.loc,
                                            external_key=f"test:in:{uuid.uuid4()}",
                                            tx_date=timezone.now() - timedelta(days=60))

    def make_order(self, number, product, qty, **kw):
        o = SalesOrder.objects.create(source="manual", order_number=number, order_date=timezone.localdate(), **kw)
        SalesOrderLine.objects.create(order=o, product=product, sku_raw=product.sku, qty=D(qty))
        return o

    def user(self, name, role, modules=None, **profile):
        u = User.objects.create_user(name, f"{name}@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role=role, allowed_modules=modules, **profile)
        return User.objects.get(pk=u.pk)

    def fresh(self, user=None):
        return User.objects.get(pk=(user or self.root).pk)

    def shell(self, pk=None, query=""):
        r = self.client.get(f"{URL}{pk or self.order.pk}/{query}")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.content.decode()

    def block(self, key, pk=None):
        r = self.client.get(f"{URL}{pk or self.order.pk}/block/{key}/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.content.decode()

    @staticmethod
    def section(html, key):
        at = html.index(f'data-sec="{key}"')
        start = html.rindex("<section", 0, at)
        return html[start:html.index("</section>", start)]

    def post_edit(self, data, pk=None):
        return self.client.post(f"{URL}{pk or self.order.pk}/edit/", json.dumps(data),
                                content_type="application/json")

    def post_prefs(self, data):
        r = self.client.post(f"{URL}prefs/", json.dumps(data), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.json()["prefs"]

    def request_for(self, user, path="/admin/sales/salesorder/"):
        request = RequestFactory().get(path)
        request.user = user
        return request

    def card_save(self, order, **changes):
        """«Зберегти» в картці замовлення: усі поля форми, як їх показала картка, + зміни."""
        url = f"/admin/sales/salesorder/{order.pk}/change/"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        form = _ChangeForm("salesorder_form")
        form.feed(page.content.decode())
        data = [(k, v) for k, v in form.data if k not in changes]
        data += list(changes.items()) + [("_save", "Зберегти")]
        post = {}
        for k, v in data:
            post.setdefault(k, []).append(v)
        r = self.client.post(url, post)
        if r.status_code != 302:
            import re
            errors = re.findall(r'<ul class="errorlist[^"]*"[^>]*>.*?</ul>', r.content.decode(), re.S)
            self.fail(f"card save: {r.status_code} {errors[:5]} {sorted(post)[:80]}")
        return r


# ── за замовчуванням і вибір людини ────────────────────────────────────────────

class DefaultsTests(OrderPanelBase):
    def test_default_sections_actions_and_edits(self):
        d = sp.defaults(self.panel)
        self.assertEqual(d["blocks"], DEFAULT_BLOCKS)
        self.assertEqual(d["collapsed"], ["history", "fields"])
        self.assertEqual(d["actions"], ["label", "mail", "shipments", "flag"])
        self.assertTrue(d["on"])
        self.assertEqual([b.key for b in self.panel.blocks],
                         ["summary", "lines", "shipping", "money", "documents", "mail", "notes", "links", "crm",
                          "moves", "history", "fields", "related"])
        self.assertEqual({b.key for b in self.panel.blocks if b.lazy},
                         {"mail", "links", "crm", "moves", "history", "fields", "related"})
        self.assertEqual([e.key for e in self.panel.edits],
                         ["status", "flag", "note", "tracking", "courier", "deadline"])
        self.assertEqual(self.panel.memo, "note")
        self.assertEqual(self.panel.hide_cols, orders_panel.HIDE_COLS)
        self.assertIn("eu_invoice_number", self.panel.extra_fields)
        self.assertEqual(self.panel.label(self.order), f"№ {NUMBER}")

    def test_list_page_config(self):
        html = self.client.get("/admin/sales/salesorder/").content.decode()
        cfg = cfg_of(html)
        self.assertEqual((cfg["model"], cfg["url"]), (MODEL, URL))
        self.assertEqual(cfg["prefs"]["blocks"], DEFAULT_BLOCKS)
        self.assertEqual(cfg["hideDefault"], list(orders_panel.HIDE_COLS))
        self.assertEqual([e["key"] for e in cfg["edits"]],
                         ["status", "flag", "note", "tracking", "courier", "deadline"])
        actions = {a["key"]: a for a in cfg["actions"]}
        self.assertEqual(list(actions), ["label", "mail", "shipments", "ship_new", "crm", "track", "inbox", "oc",
                                         "proforma", "pl_new", "ci_new", "open", "flag"])
        self.assertEqual((actions["label"]["kind"], actions["label"]["hotkey"], actions["flag"]["kind"]),
                         ("svl", "KeyL", "edit"))
        admin_names = [a["name"] for a in cfg["adminActions"]]
        for name in ("export_sales_excel", "create_offer_from_quote", "action_set_status_cancelled",
                     "action_change_source"):
            self.assertIn(name, admin_names)
        self.assertNotIn("delete_selected", admin_names)

    def test_v1_saved_prefs_get_the_new_default_sections(self):
        UserProfile.objects.filter(user=self.root).update(ui_prefs={"panels": {MODEL: {
            "on": True, "blocks": ["summary", "lines", "mail"], "actions": ["open", "label"], "width": 400}}})
        p = sp.prefs(self.fresh(), self.panel)
        self.assertEqual(p["blocks"], ["summary", "lines", "mail", "money", "links", "history", "fields"])
        self.assertEqual(p["collapsed"], ["history", "fields"])
        self.assertEqual((p["actions"], p["width"]), (["open", "label", "flag"], 400))
        html = self.shell()
        order = [html.index(f'data-sec="{k}"') for k in ("summary", "lines", "mail", "money", "links", "history")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('data-sec="shipping"', html)                      # прибрала у v1 — назад не повертається
        self.assertNotIn('data-sec="notes"', html)

    def test_v1_payload_is_cleaned_and_applied(self):
        p = self.post_prefs({"blocks": ["notes", "lines", "nope", "lines"], "actions": ["crm", "evil"],
                             "width": 480, "on": True})
        self.assertEqual((p["on"], p["blocks"], p["actions"], p["width"]), (True, ["notes", "lines"], ["crm"], 480))
        self.assertEqual(UserProfile.objects.get(user=self.root).ui_prefs["panels"][MODEL]["width"], 480)
        html = self.shell()
        self.assertLess(html.index('data-sec="notes"'), html.index('data-sec="lines"'))
        self.assertNotIn('data-sec="summary"', html)
        p = self.post_prefs({"blocks": None, "width": 999})                 # null — як було, 999 — авто
        self.assertEqual((p["blocks"], p["width"]), (DEFAULT_BLOCKS, None))


# ── оболонка, права ────────────────────────────────────────────────────────────

class ShellTests(OrderPanelBase):
    def test_header_title_status_and_actions(self):
        pk = self.order.pk
        from sales.side_panel import _status_confirm
        self.order.status = "cancelled"                       # з «Скасовано» назад — теж питати (склад рухається)
        self.assertIn("Повернути № ", _status_confirm(self.order, "received"))
        self.order.status = "shipped"
        self.assertIn("з «Відправлено» у «В обробці»", _status_confirm(self.order, "processing"))
        self.order.status = "received"
        self.assertEqual(_status_confirm(self.order, "processing"), "")   # між «робочими» — без питання
        html = self.shell()
        for part in (f'data-label="№ {NUMBER}"', f'<b>№ <span translate="no">{NUMBER}</span></b>',
                     '<span class="mvp-subt"><span translate="no">Kunde A GmbH</span> · <span translate="no">Вручну'
                     '</span> · <span translate="no">486,00 EUR</span></span>',   # дані — без перекладу, решта — 🌐
                     'class="mvp-pill mvp-t-new" data-edit="status" data-kind="choice" data-value="received"',
                     "📥 Отримано ▾", "🚫 Скасовано", 'title="Змінити: Статус · S"',
                     f"Скасувати замовлення № {NUMBER}? Товар, який воно тримає, повернеться на склад.",
                     f"Позначити № {NUMBER} як «Відправлено»? Склад зміниться так само, як при зміні статусу в картці.",
                     f'data-act="label" data-svl="order" data-order="{pk}" data-sku="{NUMBER}" data-svl-act="print" '
                     f'data-fallback="/labels/order/{pk}/" title="🏷 Етикетка · L"',
                     'data-act="flag" data-edit="flag" data-kind="bool" data-value=""',
                     'href="/email/compose/?to=a%40kunde.example&amp;subject=%E2%84%96+102497269"',
                     f'href="/admin/shipping/shipment/?order__id__exact={pk}"', "📝 Додати нотатку"):
            self.assertIn(part, html)
        bar = html[html.index('class="mvp-actbar"'):html.index("</header>")]
        self.assertLess(bar.index('data-act="label"'), bar.index('data-act="mail"'))
        self.assertLess(bar.index('data-act="shipments"'), bar.index('data-act="flag"'))
        opened = bar[bar.index('data-act="open"'):]
        self.assertIn(" hidden>", opened[:300])                             # не за замовчуванням — схована
        self.assertNotIn('data-act="ship_new"', bar[:bar.index('data-act="flag"')])
        summary = self.section(html, "summary")
        for part in ('<dt>Контакт</dt><dd><span translate="no">Anna Muster</span></dd>',
                     'href="tel:+49301234"', 'data-copy="a@kunde.example"', "Німеччина (DE), Berlin",
                     '<dt>Джерело</dt><dd><span translate="no">Вручну</span></dd>'):
            self.assertIn(part, summary)
        for key in DEFAULT_BLOCKS:
            self.assertIn(f'data-sec="{key}"', html)
        self.assertIn("data-deferred", self.section(html, "mail"))          # лист — лише за запитом
        self.assertNotIn('data-sec="crm"', html)                            # не за замовчуванням

    def test_title_marks_document_type_and_no_stock(self):
        o = self.make_order("S-2", self.product, "1", document_type="SAMPLE", affects_stock=False)
        html = self.shell(o.pk)
        self.assertIn('<b>№ <span translate="no">S-2</span></b> <span class="mvp-badge mvp-t-info">Sample</span> '
                      '<span class="mvp-badge mvp-t-off">не впливає на склад</span>', html)
        self.assertIn("Не впливає на склад і статистику", self.section(html, "summary"))

    def test_ship_new_only_for_open_orders_without_shipment(self):
        html = self.shell()
        self.assertIn(f'href="/admin/shipping/shipment/create/{self.order.pk}/"', html)
        shipped = self.make_order("S-3", self.product, "1", status="shipped")
        self.assertNotIn("/admin/shipping/shipment/create/", self.shell(shipped.pk))

    def test_view_only_role_sees_values_and_no_foreign_actions(self):
        self.client.force_login(self.user("buh", "accountant"))            # продажі: перегляд + експорт
        html = self.shell()
        self.assertIn('data-can-change="0"', html)
        self.assertNotIn("data-edit=", html)
        self.assertIn('<span class="mvp-badge mvp-t-new">📥 Отримано</span>', html)
        for key in ("label", "mail", "shipments", "flag"):                  # без DYMO, пошти, доставки, змін
            self.assertNotIn(f'data-act="{key}"', html)
        for key in ("mail", "notes"):
            self.assertNotIn(f'data-sec="{key}"', html)
        self.assertNotIn("action_set_status", html)

    def test_permissions(self):
        self.client.force_login(self.user("clerk", "readonly", ["dashboard"]))
        self.assertEqual(self.client.get(f"{URL}{self.order.pk}/").status_code, 403)
        self.assertEqual(self.client.post(f"{URL}prefs/", "{}", content_type="application/json").status_code, 403)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get("/core/panel/nope.model/1/").status_code, 404)
        self.assertEqual(self.client.get(f"{URL}999999/").status_code, 404)

    def test_broken_block_does_not_break_panel(self):
        with mock.patch.object(self.panel.block("shipping"), "render", side_effect=RuntimeError("boom")), \
                self.assertLogs("core.side_panel", "WARNING"):
            html = self.shell()
        self.assertIn("Не вдалося показати цей блок.", self.section(html, "shipping"))
        self.assertIn("FT220407", self.section(html, "lines"))


# ── 📦 позиції й склад ─────────────────────────────────────────────────────────

class LinesTests(OrderPanelBase):
    def test_short_without_stock(self):
        pk = self.order.pk
        lines = self.section(self.shell(), "lines")
        for part in ('⚠ бракує <span translate="no">10</span>', 'data-count="1"', 'data-tone="err"',
                     'data-badge="бракує 1"', 'data-brief="1 позиція · бракує 1"',
                     f'href="/admin/inventory/product/{self.product.pk}/change/" translate="no">FT220407</a>',
                     '<span translate="no">Filter 2.4 GHz</span>',
                     '<span translate="no">48,60</span> × <span translate="no">10</span> = '
                     '<span translate="no">486,00 EUR</span>',
                     f'data-svl="filter" data-sku="FT220407" data-qty="10" data-order="{pk}" data-file=""',
                     'Разом <span translate="no">10</span> шт.'):
            self.assertIn(part, lines)
        self.assertNotIn("не збігається", lines)                           # 10 × 48,60 = 486 — як сума замовлення

    def test_held_quantity_is_not_a_shortage(self):
        p = Product.objects.create(sku="AN220213", name="Antenna")
        self.receive(p, 10)
        o = self.make_order("H-1", p, "10")                                  # «при створенні» — уже списано
        self.assertEqual(stock.get_available(p), 0)
        lines = self.section(self.shell(o.pk), "lines")
        self.assertIn("✓ вистачає", lines)
        self.assertNotIn("бракує", lines)
        self.assertIn('це замовлення тримає <span translate="no">10</span>', lines)
        self.assertIn('data-tone="ok"', lines)
        self.assertIn('data-svl="antenna"', lines)

    def test_expected_stock_and_label_file(self):
        po = PurchaseOrder.objects.create(status="ordered", expected_date=timezone.localdate() + timedelta(days=9))
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.product, qty_ordered=D("6"))
        draft = PurchaseOrder.objects.create(status="draft")                # кошик — ще не замовлено
        PurchaseOrderLine.objects.create(purchase_order=draft, product=self.product, qty_ordered=D("50"))
        labels = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, labels, True)
        Path(labels, "FT220407 Alt.dymo").write_text("<x/>", encoding="utf-8")
        with override_settings(LABELS_DIR=Path(labels)):
            lines = self.section(self.shell(), "lines")
        until = (timezone.localdate() + timedelta(days=9)).strftime("%d.%m")
        self.assertIn(f'очікується <span translate="no">6</span> (<span translate="no">{po.code}</span> '
                      f'до <span translate="no">{until}</span>)', lines)
        self.assertIn('data-file="1"', lines)
        self.client.force_login(self.user("mgr2", "manager"))               # без права бачити закупівлі
        with override_settings(LABELS_DIR=Path(labels)):
            lines = self.section(self.shell(), "lines")
        self.assertNotIn("очікується", lines)
        self.assertNotIn(po.code, lines)

    def test_stock_issues_need_the_inventory_right(self):
        InventoryTransaction.objects.filter(external_key__startswith="so:").update(qty=D("-9"))   # розсинхрон
        lines = self.section(self.shell(), "lines")
        self.assertIn("FT220407: к-сть 9 → 10", lines)
        self.assertIn(f'href="/admin/sales/salesorder/{self.order.pk}/change/">🔄 Синхронізувати склад у картці',
                      lines)
        self.client.force_login(self.user("mgr", "manager"))                # без розділу «Склад»
        lines = self.section(self.shell(), "lines")
        self.assertNotIn("к-сть 9 → 10", lines)
        self.assertNotIn("/admin/inventory/product/", lines)                # товар — без посилання
        self.assertIn('<span translate="no">FT220407</span>', lines)


# ── ✉️ листування ──────────────────────────────────────────────────────────────

class MailTests(OrderPanelBase):
    def setUp(self):
        super().setUp()
        from crm.models import Customer
        from email_assistant.models import EmailAccount
        self.customer = (SalesOrder.objects.get(pk=self.order.pk).crm_customer        # створено сигналом CRM
                         or Customer.objects.create(name="Kunde A", email="a@kunde.example"))
        self.primary = EmailAccount.objects.create(user=self.root, email_address="root@firma.example",
                                                   is_primary=True)
        self.second = EmailAccount.objects.create(user=self.root, email_address="info@firma.example")
        self.olga = User.objects.create_user("olga", "o@x.y", "p", is_staff=True)
        self.other = EmailAccount.objects.create(user=self.olga, email_address="olga@firma.example", is_primary=True)

    def msg(self, account, subject, link=True, **kw):
        from crm.models import CustomerTimeline
        from email_assistant.models import EmailMessage
        kw.setdefault("from_email", "a@kunde.example")
        kw.setdefault("sent_at", timezone.now())
        m = EmailMessage.objects.create(account=account, imap_uid=next(self._uid), subject=subject, **kw)
        if link:
            CustomerTimeline.objects.create(customer=self.customer, event_type="email_in", title=subject,
                                            related_order_id=self.order.pk, email_message=m)
        else:                                       # crm_link сам прив'язує лист з номером замовлення — тут ні
            CustomerTimeline.objects.filter(email_message=m).delete()
        return m

    def test_only_own_visible_messages_and_links_for_the_primary_mailbox(self):
        ok = self.msg(self.primary, "Lieferung bitte", from_name="Kunde A", is_read=False,
                      body_text="Bitte liefern Sie bis Freitag.\n\nMit freundlichen Grüßen\nKunde A")
        side = self.msg(self.second, "Rechnung offen")
        sent = self.msg(self.primary, "Ihre Bestellung", folder="sent", is_read=True)
        hidden = [self.msg(self.primary, "Im Papierkorb", folder="trash"),
                  self.msg(self.primary, "Spam!", is_spam=True),
                  self.msg(self.primary, "Gelöscht", is_deleted=True),
                  self.msg(self.primary, "Entwurf", folder="draft"),
                  self.msg(self.other, "Privat: Gehalt")]                    # чужа скринька
        by_subject = self.msg(self.primary, f"Re: Bestellung {NUMBER} versandt", link=False,
                              from_email="noreply@carrier.example")
        self.msg(self.primary, f"Bestellung {NUMBER}0 storniert", link=False, from_email="noreply@carrier.example")
        self.msg(self.primary, f"Alte Bestellung {NUMBER}", link=False, from_email="noreply@carrier.example",
                 sent_at=timezone.now() - timedelta(days=60))
        html = self.block("mail")
        self.assertIn(f'href="/email/message/{ok.pk}/"', html)
        self.assertIn("Lieferung bitte", html)
        self.assertIn("Bitte liefern Sie bis Freitag.", html)
        self.assertNotIn("Mit freundlichen", html)                           # без підпису
        self.assertIn('class="mvp-unread"', html)
        self.assertIn("Rechnung offen", html)                                # своя, але не основна скринька
        self.assertNotIn(f'href="/email/message/{side.pk}/"', html)
        self.assertIn('href="/email/?q=Rechnung+offen">🔍 у пошті</a>', html)
        self.assertIn(f'href="/email/message/{sent.pk}/"', html)
        self.assertIn('aria-label="Надіслано"', html)
        self.assertIn(f'href="/email/message/{by_subject.pk}/"', html)      # номер замовлення в темі
        for m in hidden:
            self.assertNotIn(m.subject, html)
        self.assertNotIn(f"{NUMBER}0 storniert", html)                       # інший номер, довший
        self.assertNotIn("Alte Bestellung", html)                            # раніше ніж за 30 днів
        self.assertIn(f'href="/email/?q={NUMBER}">🔍 Усі листи з № {NUMBER}</a>', html)
        shell = self.shell()
        self.assertIn('data-count="3"', self.section(shell, "mail"))        # прив'язані: основна, друга, надіслана
        self.assertIn('data-tone="info"', self.section(shell, "mail"))

    def test_no_mail_module_no_section(self):
        self.msg(self.primary, "Lieferung bitte")
        self.client.force_login(self.user("buh", "accountant"))            # продажі є, пошти немає
        self.assertNotIn('data-sec="mail"', self.shell())
        self.assertEqual(self.client.get(f"{URL}{self.order.pk}/block/mail/").status_code, 404)


# ── 📝 нотатки й події ─────────────────────────────────────────────────────────

class NotesTests(OrderPanelBase):
    def test_notes_without_mail_events_and_only_own_calendar(self):
        from calendar_app.models import CalendarEvent
        from crm.models import Customer, CustomerTimeline
        from email_assistant.models import EmailAccount, EmailMessage
        c = (SalesOrder.objects.get(pk=self.order.pk).crm_customer
             or Customer.objects.create(name="Kunde A", email="a@kunde.example"))
        own_events = CustomerTimeline.objects.filter(related_order_id=self.order.pk).count()   # «🛒 Замовлення» CRM
        CustomerTimeline.objects.create(customer=c, event_type="note", title="Rückruf vereinbart",
                                        body="Montag 10 Uhr", related_order_id=self.order.pk)
        CustomerTimeline.objects.create(customer=c, event_type="email_in", title="Privat: Gehalt",  # старий лист
                                        related_order_id=self.order.pk)
        acc = EmailAccount.objects.create(user=self.root, email_address="root@firma.example", is_primary=True)
        m = EmailMessage.objects.create(account=acc, subject="Angebot geschickt", from_email="root@firma.example")
        CustomerTimeline.objects.create(customer=c, event_type="email_out", title="Angebot geschickt",
                                        related_order_id=self.order.pk, email_message=m)
        start = timezone.now() + timedelta(days=1)
        CalendarEvent.objects.create(user=self.root, title="Anruf Kunde A", start_at=start, sales_order=self.order)
        olga = User.objects.create_user("olga", "o@x.y", "p", is_staff=True)
        CalendarEvent.objects.create(user=olga, title="Olgas Termin", start_at=start, sales_order=self.order)
        notes = self.section(self.shell(), "notes")
        self.assertIn("Rückruf vereinbart", notes)
        self.assertIn("Montag 10 Uhr", notes)
        self.assertIn("Anruf Kunde A", notes)
        for hidden in ("Privat: Gehalt", "Angebot geschickt", "Olgas Termin"):
            self.assertNotIn(hidden, notes)
        self.assertIn(f'data-count="{own_events + 2}"', notes)              # події CRM замовлення + нотатка + свій

    def test_hidden_without_crm_right(self):
        self.client.force_login(self.user("buh", "accountant"))            # без CRM
        self.assertNotIn('data-sec="notes"', self.shell())
        self.assertEqual(self.client.get(f"{URL}{self.order.pk}/block/notes/").status_code, 404)


# ── інші розділи ───────────────────────────────────────────────────────────────

class SectionTests(OrderPanelBase):
    def test_documents_files_and_generators(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        folder = Path(media, "orders", "manual", NUMBER)
        folder.mkdir(parents=True)
        (folder / "Invoice A#1.pdf").write_bytes(b"%PDF-1.4 test")
        with override_settings(MEDIA_ROOT=media):
            docs = self.section(self.shell(), "documents")
        pk = self.order.pk
        self.assertIn(f'href="/media/orders/manual/{NUMBER}/Invoice%20A%231.pdf" target="_blank" rel="noopener"',
                      docs)
        self.assertIn('<span translate="no">Invoice A#1.pdf</span>', docs)
        self.assertIn('data-count="1"', docs)
        for path in ("doc/packing-list/", "doc/proforma/", "doc/order-confirmation/", "doc/customs/"):
            self.assertIn(f'href="/admin/sales/salesorder/{pk}/{path}" target="_blank"', docs)
        self.assertNotIn("action=save", docs)

    def test_crm_section_reads_kundennummer(self):
        from crm.models import Customer
        from shop.models import CustomerNumber
        c = (SalesOrder.objects.get(pk=self.order.pk).crm_customer              # створено сигналом CRM
             or Customer.objects.create(name="Anna Muster", company="Kunde A GmbH", email="a@kunde.example"))
        CustomerNumber.objects.create(number=31728, crm_customer=c)
        html = self.block("crm")
        self.assertIn(f'href="/admin/crm/customer/{c.pk}/change/"', html)
        self.assertIn('<dt>Kundennummer</dt><dd><span translate="no">31728</span></dd>', html)
        self.assertIn("RFM", html)
        self.assertEqual(CustomerNumber.objects.count(), 1)                 # лише прочитано, нового не видано
        summary = self.section(self.shell(), "summary")
        self.assertIn(f'href="/admin/crm/customer/{c.pk}/change/"', summary)
        self.client.force_login(self.user("buh", "accountant"))
        self.assertNotIn("/admin/crm/customer/", self.section(self.shell(), "summary"))

    def test_moves_show_cancel_compensation(self):
        o = SalesOrder.objects.get(pk=self.order.pk)
        o.status = "cancelled"
        o.save()                                                              # «при створенні» списано → повернено
        html = self.block("moves")
        self.assertIn("Витрата", html)
        self.assertIn("↩ повернено", html)
        self.client.force_login(self.user("mgr", "manager"))                # без складу — розділу немає
        self.assertEqual(self.client.get(f"{URL}{self.order.pk}/block/moves/").status_code, 404)

    def test_payment_status_tone(self):
        SalesOrder.objects.filter(pk=self.order.pk).update(payment_status="failed", payment_method="stripe")
        money = self.section(self.shell(), "money")
        self.assertIn('data-tone="err"', money)
        self.assertIn("Помилка оплати", money)


# ── швидкі зміни ───────────────────────────────────────────────────────────────

class EditTests(OrderPanelBase):
    def outcome(self, order):
        """Рухи складу замовлення (тип, к-сть) разом з компенсаціями скасування."""
        txs = list(InventoryTransaction.objects.filter(external_key__startswith=stock.order_prefix(order)))
        comps = InventoryTransaction.objects.filter(external_key__in=[f"so-cancel:{t.pk}" for t in txs])
        return sorted((t.tx_type, t.qty) for t in txs + list(comps))

    def test_status_cancel_releases_the_reservation_like_the_card(self):
        self.mode("creation", reservation=True)
        p = Product.objects.create(sku="AN220213", name="Antenna")
        self.receive(p, 20)
        by_card = self.make_order("R-1", p, "5", client="Kunde B")
        by_panel = self.make_order("R-2", p, "5", client="Kunde B")
        self.assertEqual(self.outcome(by_panel), [("Reserved", D("-5"))])
        self.assertEqual(stock.get_available(p), 10)
        self.card_save(by_card, status="cancelled")
        r = self.post_edit({"key": "status", "value": "cancelled", "expect": "received"}, pk=by_panel.pk)
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual((j["message"], j["value"], j["undo"]), ("Статус: Скасовано", "cancelled", None))
        self.assertIn('data-value="cancelled"', j["head"])
        self.assertIn('class="mvp-pill mvp-t-off"', j["head"])
        self.assertEqual(set(j["blocks"]), {"lines", "shipping"})
        self.assertEqual(set(j["cells"]), {"status_badge", "shipped_badge", "deadline_display"})
        self.assertIn("Скасовано", j["cells"]["status_badge"])
        self.assertEqual(SalesOrder.objects.get(pk=by_card.pk).status, "cancelled")
        self.assertEqual(self.outcome(by_panel), self.outcome(by_card))
        self.assertEqual(self.outcome(by_panel), [])                         # резерв знято
        self.assertEqual(stock.get_available(p), 20)
        ct = ContentType.objects.get_for_model(SalesOrder)
        entry = LogEntry.objects.filter(content_type=ct, object_id=str(by_panel.pk)).get()
        self.assertEqual((entry.action_flag, entry.user), (CHANGE, self.root))
        self.assertIn("Статус", entry.get_change_message())
        audit = AuditLog.objects.filter(content_type=ct, object_id=str(by_panel.pk), action=AuditLog.Action.UPDATE)
        self.assertEqual([a.extra.get("changed_fields") for a in audit], [["status"]])

    def test_flag_returns_the_list_cell_and_undo(self):
        r = self.post_edit({"key": "flag", "value": True, "expect": ""})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual((j["value"], j["undo"]), ("1", {"key": "flag", "value": ""}))
        self.assertIn("⭐", j["cells"]["flag_col"])
        self.assertIn("sv-flagged", j["cells"]["flag_col"])
        self.assertIn('aria-pressed="true"', j["head"])
        self.assertTrue(SalesOrder.objects.get(pk=self.order.pk).is_flagged)
        j = self.post_edit({"key": "flag", "value": j["undo"]["value"], "expect": "1"}).json()   # «↶ Скасувати»
        self.assertEqual(j["value"], "")
        self.assertFalse(SalesOrder.objects.get(pk=self.order.pk).is_flagged)

    def test_note_is_cut_to_500_characters(self):
        r = self.post_edit({"key": "note", "value": "x" * 600, "expect": ""})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual(SalesOrder.objects.get(pk=self.order.pk).internal_note, "x" * 500)
        self.assertEqual(j["undo"], {"key": "note", "value": ""})
        self.assertIn("note_col", j["cells"])
        self.assertIn('data-edit="note"', j["head"])

    def test_tracking_edit_redraws_the_shipping_section(self):
        r = self.post_edit({"key": "tracking", "value": "1Z999AA10123456784", "expect": ""})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual(list(j["blocks"]), ["shipping"])
        self.assertIn("1Z999AA10123456784", j["blocks"]["shipping"])
        self.assertEqual(j["undo"], {"key": "tracking", "value": ""})

    def test_view_only_role_gets_403(self):
        self.client.force_login(self.user("buh", "accountant"))
        for data in ({"key": "status", "value": "cancelled"}, {"key": "flag", "value": True},
                     {"key": "note", "value": "Bitte"}):
            r = self.post_edit(data)
            self.assertEqual((r.status_code, r.json()["error"]), (403, "forbidden"))
        o = SalesOrder.objects.get(pk=self.order.pk)
        self.assertEqual((o.status, o.is_flagged, o.internal_note), ("received", False, ""))


# ── дії списку й ⭐ / 📝 у списку (sales/admin.py) ──────────────────────────────

class AdminActionTests(OrderPanelBase):
    def names(self, user):
        ma = admin.site._registry[SalesOrder]
        request = self.request_for(user)
        return ([n for n, _label in ma.get_action_choices(request)],
                [a["name"] for a in panel_edit.admin_actions(self.panel, ma, request)])

    def test_view_and_export_role_sees_only_export(self):
        buh = self.user("buh", "accountant")
        choices, panel = self.names(buh)
        self.assertEqual(choices, ["", "export_sales_excel"])
        self.assertEqual(panel, ["export_sales_excel"])
        self.client.force_login(buh)
        cfg = cfg_of(self.client.get("/admin/sales/salesorder/").content.decode())
        self.assertEqual(cfg["adminActions"], [{"name": "export_sales_excel", "label": "📥 Експортувати в Excel"}])
        self.assertFalse(cfg["canChange"])
        self.assertEqual(cfg["edits"], [])

    def test_change_actions_need_change_and_export_needs_its_right(self):
        choices, panel = self.names(self.user("mgr", "manager"))
        for name in ("action_set_status_received", "action_set_status_cancelled", "action_set_currency_custom",
                     "action_set_shipping_currency_eur", "action_set_doctype_sample", "action_set_affects_stock_no",
                     "action_change_source", "export_sales_excel", "create_offer_from_quote"):
            self.assertIn(name, choices)
            self.assertIn(name, panel)
        self.assertNotIn("delete_selected", choices)                         # менеджер не видаляє
        choices, panel = self.names(self.user("mgr2", "manager", can_export=False))
        self.assertNotIn("export_sales_excel", choices)
        self.assertNotIn("export_sales_excel", panel)
        self.assertIn("action_set_status_cancelled", choices)
        choices, _panel = self.names(self.user("buh2", "accountant", can_export=False))
        self.assertEqual(choices, [""])

    def test_change_action_post_is_refused_without_the_change_right(self):
        self.client.force_login(self.user("buh", "accountant"))
        self.client.post("/admin/sales/salesorder/", {"action": "action_set_status_cancelled", "index": "0",
                                                      "select_across": "0", "_selected_action": [self.order.pk]})
        self.assertEqual(SalesOrder.objects.get(pk=self.order.pk).status, "received")
        self.client.force_login(self.user("mgr", "manager"))
        self.client.post("/admin/sales/salesorder/", {"action": "action_set_status_cancelled", "index": "0",
                                                      "select_across": "0", "_selected_action": [self.order.pk]})
        self.assertEqual(SalesOrder.objects.get(pk=self.order.pk).status, "cancelled")


class ListFlagNoteTests(OrderPanelBase):
    def test_post_and_change_right_are_required(self):
        pk = self.order.pk
        flag, note = f"/admin/sales/salesorder/{pk}/flag/", f"/admin/sales/salesorder/{pk}/note/"
        for url in (flag, note):
            r = self.client.get(url)
            self.assertEqual((r.status_code, r["Allow"]), (405, "POST"))
        self.assertFalse(SalesOrder.objects.get(pk=pk).is_flagged)          # GET більше нічого не змінює
        self.client.force_login(self.user("buh", "accountant"))
        r = self.client.post(flag)
        self.assertEqual((r.status_code, r.json()["ok"]), (403, False))
        r = self.client.post(note, json.dumps({"note": "Bitte bis Freitag"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
        o = SalesOrder.objects.get(pk=pk)
        self.assertEqual((o.is_flagged, o.internal_note), (False, ""))
        self.client.force_login(self.user("mgr", "manager"))
        self.assertEqual(self.client.post(flag).json(), {"flagged": True, "pk": pk})
        r = self.client.post(note, json.dumps({"note": "  Bitte bis Freitag  "}), content_type="application/json")
        self.assertEqual(r.json(), {"ok": True, "note": "Bitte bis Freitag"})
        self.assertEqual(self.client.post("/admin/sales/salesorder/999999/flag/").status_code, 404)
