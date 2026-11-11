"""📌 Панель праворуч — загальні розділи (core/panel_generic.py): «🧾 Усі поля» (ключі й паролі сховано, методи
адмінки не виконуються, посилання — лише туди, куди людина може перейти, порожні поля згорнуто), «🔗 Пов’язане»
(queryset адмінки пов'язаної моделі, права, без пошти й календаря), «🕘 Історія» (LogEntry, без IP) і загальна
панель списків з side_panel.GENERIC. Реєстр панелей відновлюється після кожного тесту.

    python manage.py test core.tests_panel_generic --settings=tabele.settings_test
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.admin.options import get_content_type_for_model
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone
from django.utils.html import format_html

from core import panel_generic as pg
from core import side_panel as sp
from core.models import UserProfile
from core.secrets import mask


def cfg_of(html):
    return json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])


class GenericTestBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        saved = dict(sp._registry)

        def restore():
            sp._registry.clear()
            sp._registry.update(saved)
        self.addCleanup(restore)
        for model in sp.GENERIC:                       # загальні панелі — щоразу заново, з core/panel_generic.py
            sp._registry.pop(model, None)
        self.root = User.objects.create_superuser("root", "root@x.y", "p", first_name="Rita", last_name="Root")
        self.client.force_login(self.root)

    def user(self, name, role, modules=None):
        u = User.objects.create_user(name, f"{name}@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role=role, allowed_modules=modules)
        return User.objects.get(pk=u.pk)

    @staticmethod
    def request_for(user, path="/admin/"):
        request = RequestFactory().get(path)
        request.user = user
        request.session = {}                            # форма картки замовлення читає сесію
        return request

    @staticmethod
    def section(html, key):
        at = html.index(f'data-sec="{key}"')
        start = html.rindex("<section", 0, at)
        return html[start:html.index("</section>", start)]

    def fields(self, obj, user=None, extra_fields=None):
        ma = admin.site._registry[type(obj)]
        groups = pg.field_groups(ma, self.request_for(user or self.root), obj, extra_fields=extra_fields)
        return groups, str(pg.fields_html(groups, pg._change_url(obj)))

    @staticmethod
    def rows(groups):
        return {r["name"]: r for g in groups for r in g["rows"]}


class SecretTests(GenericTestBase):
    def test_jlc_keys_are_never_shown(self):
        from jlcpcb.models import JLCConfig
        values = ("614741474579288066", "JLC-ACCESS-8F3A2B", "JLC-SECRET-Q7W8E9")
        cfg = JLCConfig.objects.create(app_id=values[0], access_key=values[1], secret_key=values[2], use_sandbox=True)
        groups, html = self.fields(cfg)
        for value in values + ("579288066", "8F3A2B", "Q7W8E9"):
            self.assertNotIn(value, html)
            self.assertNotIn(value, repr(groups))
        for value in values:
            self.assertNotIn(mask(value), html)                                # і жодної маски
        self.assertEqual(html.count("🔒 збережено"), 3)
        rows = self.rows(groups)
        self.assertTrue(all(rows[n]["secret"] for n in ("app_id", "access_key", "secret_key")))
        self.assertFalse(pg.is_secret_name("app_id"))                          # лише поле-пароль у формі картки
        self.assertTrue(pg.is_secret_name("secret_key"))
        self.assertIn("✓ так", rows["use_sandbox"]["html"])

    def test_shop_stripe_and_assistant_keys_are_never_shown(self):
        from shop.models import Shop
        live, hook = "sk_" + "live_51" + "Hidden9Value", "wh" + "sec_" + "Hidden8Value"   # фальшиві, складаються тут
        shop = Shop.objects.create(name="Sevskiy", slug="webshop", stripe_secret_key=live,
                                   stripe_webhook_secret=hook, assistant_key="")
        groups, html = self.fields(shop)
        for value in (live, "Hidden9Value", hook, "Hidden8Value", mask(live), mask(hook)):
            self.assertNotIn(value, html)
            self.assertNotIn(value, repr(groups))
        self.assertEqual(html.count("🔒 збережено"), 2)
        rows = self.rows(groups)
        self.assertTrue(rows["stripe_secret_key"]["secret"] and rows["stripe_webhook_secret"]["secret"])
        self.assertIn("не задано", rows["assistant_key"]["html"])               # порожній ключ — згорнутий
        self.assertTrue(rows["assistant_key"]["empty"])
        self.assertIn("Sevskiy", html)

    def test_card_form_is_inspected_but_never_instantiated(self):
        from jlcpcb.models import JLCConfig
        cfg = JLCConfig.objects.create(app_id="614741474579288066", secret_key="JLC-SECRET-Q7W8E9")
        ma = admin.site._registry[JLCConfig]
        form_class = ma.get_form(self.request_for(self.root), cfg, fields=["app_id", "access_key", "secret_key"])

        def no_init(*args, **kwargs):
            raise AssertionError("the card form was instantiated")
        form_class.__init__ = no_init
        with mock.patch.object(type(ma), "get_form", return_value=form_class):
            groups, html = self.fields(cfg)
        self.assertNotIn("614741474579288066", html)                           # поле-пароль упізнано з класу
        self.assertTrue(self.rows(groups)["app_id"]["secret"])

    def test_json_keys_with_secret_names_are_masked(self):
        from inventory.models import Product
        out = pg.mask_json({"Frequency": "2.4 GHz", "api_token": "tok-SECRET-1", "Authorization": "Bearer xyz",
                            "nested": [{"password": "pw-2", "ok": 1}], "empty_secret": ""})
        self.assertEqual(out, {"Frequency": "2.4 GHz", "api_token": "🔒 збережено", "Authorization": "🔒 збережено",
                               "nested": [{"password": "🔒 збережено", "ok": 1}], "empty_secret": ""})
        p = Product.objects.create(sku="FT220407", name="Filter 2.4 GHz", purchase_price=Decimal("1234.5"),
                                   tech_attributes={"Frequency": "2.4 GHz", "api_token": "tok-SECRET-1",
                                                    "Connector": "SMA"})
        groups, html = self.fields(p, extra_fields=())                        # без панелі товарів (пакет D)
        self.assertNotIn("tok-SECRET-1", html)
        self.assertIn("<summary>{…} 3 ключі</summary>", html)
        self.assertIn("2.4 GHz", html)
        self.assertIn('<span translate="no">1 234,50</span>', self.rows(groups)["purchase_price"]["html"])


class FieldsTests(GenericTestBase):
    def test_fk_is_a_link_only_when_the_target_opens(self):
        from crm.models import Customer
        from inventory.models import Product
        from sales.models import SalesOrder
        from tasks.models import Task
        customer = Customer.objects.create(name="Muster GmbH", email="info@muster.example")
        product = Product.objects.create(sku="FT220407", name="Filter 2.4 GHz")
        order = SalesOrder.objects.create(source="manual", order_number="102497269")
        task = Task.objects.create(title="Muster anrufen", customer=customer, product=product, order=order,
                                   assigned_to=self.root)
        urls = {"crm": f'href="/admin/crm/customer/{customer.pk}/change/"',
                "inventory": f'href="/admin/inventory/product/{product.pk}/change/"',
                "sales": f'href="/admin/sales/salesorder/{order.pk}/change/"',
                "auth": f'href="/admin/auth/user/{self.root.pk}/change/"'}

        def fields_section(user):
            self.client.force_login(user)
            r = self.client.get(f"/core/panel/tasks.task/{task.pk}/")
            self.assertEqual(r.status_code, 200, r.content[:300])
            return self.section(r.content.decode(), "fields")

        html = fields_section(self.root)
        for part in urls.values():
            self.assertIn(part, html)
        sklad = self.user("sklad", "warehouse")                                # задачі й склад, без CRM і продажів
        html = fields_section(sklad)
        self.assertIn(urls["inventory"], html)
        for app in ("crm", "sales", "auth"):
            self.assertNotIn(urls[app], html)
        self.assertIn(f'<span translate="no">{customer}</span>', html)
        self.assertIn(f'<span translate="no">{order}</span>', html)
        guest = self.user("guest", "readonly", ["tasks"])                       # лише задачі
        html = fields_section(guest)
        for part in urls.values():
            self.assertNotIn(part, html)
        self.assertIn('<span translate="no">FT220407</span>', html)
        self.assertIn(f'<span translate="no">{customer}</span>', html)

    def test_readonly_admin_callables_are_never_called(self):
        from sales.admin import SalesOrderAdmin
        from sales.models import SalesOrder
        order = SalesOrder.objects.create(source="manual", order_number="102497269", client="Muster GmbH")
        called = []

        def boom(self_, obj):
            called.append(obj)
            raise AssertionError("readonly callable executed")
        boom.short_description = "📄 Документи (тест)"
        with mock.patch.object(SalesOrderAdmin, "documents_list", boom, create=False), \
                mock.patch.object(SalesOrderAdmin, "crm_link", boom, create=False):
            groups, html = self.fields(order)
            sp.register("sales.salesorder", title=lambda o: format_html("<b>{}</b>", o.order_number), blocks=[])
            r = self.client.get(f"/core/panel/sales.salesorder/{order.pk}/block/fields/")
        self.assertEqual(called, [])
        self.assertIn("📄 Документи (тест)", html)                             # назва — у «Ще в картці»
        self.assertIn("Ще в картці:", html)
        self.assertIn('<span translate="no">102497269</span>', html)
        self.assertEqual(r.status_code, 200)
        block = r.content.decode()
        self.assertIn('<span translate="no">Muster GmbH</span>', block)
        self.assertNotIn("Не вдалося показати цей блок.", block)
        # групи без полів моделі — одним рядком з посиланнями на групу картки
        self.assertIn(f'<a class="mvp-link" href="/admin/sales/salesorder/{order.pk}/change/#fieldset-0-6-heading">'
                      "🏷️ Етикетки DYMO</a>", html)

    def test_empty_fields_are_folded_with_their_count(self):
        from inventory.models import Supplier
        s = Supplier.objects.create(name="Muster GmbH")
        groups, html = self.fields(s)
        self.assertEqual([g["title"] for g in groups], [   # «📁 Проєкти» — projects/panels.py, решта — suppliers/
            "🏢 Постачальник", "📊 Огляд", "📁 Проєкти", "📞 Контакти", "📍 Адреса", "💼 Комерційні умови", "✉️ Пошта",
            "📷 Візитки й флаєри", "📨 Запити (RFQ)", "🧠 AI та інтернет", "🕘 Історія"])
        for n in (5, 3, 1):
            self.assertIn(f"<summary>Порожні поля ({n})</summary>", html)
        self.assertLess(html.index("Muster GmbH"), html.index("Порожні поля (5)"))
        self.assertIn('<dt>Валюта</dt><dd><span translate="no">EUR</span></dd>', html)
        self.assertEqual(html.count("data-mvp-row"), 20)
        self.assertNotIn("data-mvp-filter", html)                              # 20 рядків — без пошуку
        self.assertIn(f'href="/admin/inventory/supplier/{s.pk}/change/#fieldset-0-1-heading"', html)

    def test_long_cards_get_a_field_filter_and_card_only_notes(self):
        from shipping.models import Carrier, Shipment
        shipment = Shipment.objects.create(carrier=Carrier.objects.create(name="UPS", carrier_type="ups"),
                                           recipient_company="Muster GmbH", weight_kg=Decimal("2.500"))
        groups, html = self.fields(shipment)
        self.assertGreater(sum(len(g["rows"]) for g in groups), 20)
        self.assertIn('data-mvp-filter placeholder="🔎 Знайти поле"', html)
        self.assertIn("Ще в картці:", html)
        self.assertIn('<span translate="no">2,50</span>', self.rows(groups)["weight_kg"]["html"])

    def test_values_extra_fields_and_formats(self):
        from tasks.models import Task
        task = Task.objects.create(title="Muster anrufen", description="Zeile 1\nZeile 2", status="in_progress",
                                   due_date=date(2026, 11, 3))
        groups, html = self.fields(task)
        rows = self.rows(groups)
        self.assertIn("🔄 В роботі", rows["status"]["html"])
        self.assertNotIn("translate", rows["status"]["html"])                  # підпис з коду — перекладається
        self.assertEqual(rows["due_date"]["html"], '<span translate="no">03.11.2026</span>')
        self.assertIn("✗ ні", rows["notify_email"]["html"])
        self.assertIn('<div class="mvp-clamp" data-mvp-clamp translate="no">Zeile 1<br>Zeile 2</div>',
                      rows["description"]["html"])
        self.assertTrue(rows["order"]["empty"])
        # поля поза групами картки — «Ще поля» (з панелі моделі або явно)
        from sales.models import SalesOrder
        order = SalesOrder.objects.create(source="manual", order_number="102497270", total_price=Decimal("1234.5"),
                                          currency="EUR")
        sp.register("sales.salesorder", title=lambda o: format_html("<b>{}</b>", o.order_number), blocks=[],
                    extra_fields=("total_price", "currency", "order_number", "nope"))
        groups, html = self.fields(order)
        extra = groups[-1]
        self.assertEqual((extra["title"], extra["index"]), ("Ще поля", None))
        self.assertEqual([r["name"] for r in extra["rows"]], ["total_price", "currency"])   # номер уже є в групах
        self.assertIn('<span translate="no">1 234,50</span>', extra["rows"][0]["html"])
        self.assertIn('<h4 class="mvp-fg-h"><span>Ще поля</span></h4>', html)          # без «↗»: картка їх не має

    def test_fallback_when_the_card_groups_fail(self):
        from inventory.admin import SupplierAdmin
        from inventory.models import Supplier
        s = Supplier.objects.create(name="Muster GmbH", email="anna@muster.example")
        with mock.patch.object(SupplierAdmin, "get_fieldsets", side_effect=RuntimeError("boom")), \
                mock.patch.object(SupplierAdmin, "get_exclude", return_value=("notes",)):
            groups, html = self.fields(s)
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0]["title"], groups[0]["index"]), ("", 0))
        names = [r["name"] for r in groups[0]["rows"]]
        self.assertIn("email", names)
        self.assertNotIn("notes", names)                                       # виключене з картки — ні
        self.assertNotIn("id", names)
        self.assertIn("anna@muster.example", html)
        self.assertNotIn("<h4", html)                                          # група без назви — без заголовка

    def test_customer_card_fields_with_panel_extra_fields(self):
        from crm.models import Customer
        c = Customer.objects.create(name="Muster GmbH", email="info@muster.example", is_favorite=True)
        groups, html = self.fields(c, extra_fields=("is_favorite", "external_key"))
        extra = groups[-1]
        self.assertEqual(extra["title"], "Ще поля")
        rows = {r["name"]: r for r in extra["rows"]}
        self.assertIn("✓ так", rows["is_favorite"]["html"])
        self.assertIn(c.external_key, rows["external_key"]["html"])
        groups, html = self.fields(c, extra_fields=())
        self.assertNotIn(c.external_key, html)                                 # поза карткою — лише якщо назвали
        self.assertIn('<span translate="no">info@muster.example</span>', html)

    def test_card_only_notes_link_to_the_card(self):
        row = {"name": "code", "label": "Код", "html": sp.data("A-2026-001"), "empty": False}
        groups = [{"index": 0, "title": "", "rows": [row], "card_only": ["№ / версія"]},
                  {"index": 1, "title": "📄 Документи", "rows": [], "card_only": ["Файли"]},
                  {"index": 2, "title": "", "rows": [], "card_only": ["Кнопки"]}]
        html = str(pg.fields_html(groups, "/admin/offers/offer/1/change/"))
        self.assertIn('<p class="mvp-note"><span>Ще в картці:</span> <span>№ / версія</span> '
                      '<a class="mvp-link" href="/admin/offers/offer/1/change/">↗ у картці</a></p>', html)
        self.assertIn('<span><a class="mvp-link" href="/admin/offers/offer/1/change/#fieldset-0-1-heading">'
                      '📄 Документи</a></span> · <span>Кнопки</span>', html)
        self.assertEqual(pg.fields_html([{"index": 0, "title": "", "rows": [], "card_only": []}], "/x/"), "")

    def test_number_format(self):
        self.assertEqual(pg.number(Decimal("1234.5"), 2), "1 234,50")
        self.assertEqual(pg.number(Decimal("10.000"), 3), "10")
        self.assertEqual(pg.number(Decimal("0.1234"), 4), "0,1234")
        self.assertEqual(pg.number(Decimal("12.5000"), 4), "12,50")
        self.assertEqual(pg.number(-1234567.891), "-1 234 567,891")
        self.assertEqual(pg.number(Decimal("10.0"), 1), "10,0")


class RelatedTests(GenericTestBase):
    def setUp(self):
        super().setUp()
        from calendar_app.models import CalendarEvent
        from sales.models import SalesOrder
        from shipping.models import Carrier, Shipment
        from tasks.models import Task
        self.order = SalesOrder.objects.create(source="manual", order_number="102497269")
        carrier = Carrier.objects.create(name="UPS", carrier_type="ups")
        self.s1 = Shipment.objects.create(order=self.order, carrier=carrier, tracking_number="1ZVISIBLE001")
        self.s2 = Shipment.objects.create(order=self.order, carrier=carrier, tracking_number="1ZHIDDEN002")
        self.task = Task.objects.create(title="Rechnung prüfen", order=self.order)
        CalendarEvent.objects.create(user=self.root, title="Termin Muster", start_at=timezone.now(),
                                     sales_order=self.order)

    def related(self, user):
        from sales.models import SalesOrder
        return str(pg.related_html(self.request_for(user), SalesOrder.objects.get(pk=self.order.pk)))

    def test_order_lists_shipments_through_the_shipment_admin_queryset(self):
        from shipping.admin import ShipmentAdmin
        from shipping.models import Shipment
        hidden = self.s2.pk
        with mock.patch.object(ShipmentAdmin, "get_queryset", lambda ma, request: Shipment.objects.exclude(pk=hidden)):
            html = self.related(self.root)
        self.assertIn(f'href="/admin/shipping/shipment/{self.s1.pk}/change/"', html)
        self.assertNotIn(f"/admin/shipping/shipment/{hidden}/change/", html)
        self.assertIn(f'<a class="mvp-more-link" href="/admin/shipping/shipment/?order__id__exact={self.order.pk}">'
                      "Усі 1 →</a>", html)
        self.assertIn(f"/admin/tasks/task/{self.task.pk}/change/", html)
        self.assertNotIn("calendar_app", html)                                 # календар — ніколи
        self.assertNotIn("Termin Muster", html)
        self.assertIn("Порожні зв’язки (", html)
        self.assertIn("📝 Angebote (Замовлення з пропозиції)", html)           # два зв'язки з однією моделлю
        html = self.related(self.root)
        self.assertIn("Усі 2 →", html)
        self.assertLess(html.index("/admin/shipping/shipment/"), html.index("/admin/tasks/task/"))  # більші першими

    def test_related_follows_permissions(self):
        self.assertEqual(self.related(self.user("buh", "accountant")), "")      # без доставки й задач
        html = self.related(self.user("manager", "manager"))
        self.assertIn(f"/admin/shipping/shipment/{self.s1.pk}/change/", html)
        self.assertIn(f"/admin/tasks/task/{self.task.pk}/change/", html)
        self.assertNotIn("Рахунки-фактури", html)                              # бухгалтерії менеджер не бачить
        self.assertNotIn("Termin Muster", html)

    def test_private_and_service_relations_are_skipped(self):
        from crm.models import Customer
        from inventory.models import InventoryTransaction
        from sales.models import SalesOrder
        customer = {r.related_model._meta.label_lower for r in pg.relations_of(Customer)}
        self.assertTrue({"crm.customernote", "offers.offer", "projects.project", "tasks.task"} <= customer)
        for label in customer:
            self.assertNotIn(label.split(".")[0], {"email_assistant", "calendar_app", "worktime", "rag_assistant",
                                                   "meetings"})
        self.assertNotIn("crm.customertimeline", customer)
        self.assertNotIn("shop.shopcustomer", customer)                        # секрети / приватне (NEVER)
        order = {r.related_model._meta.label_lower for r in pg.relations_of(SalesOrder)}
        self.assertIn("shipping.shipment", order)
        for label in ("calendar_app.calendarevent", "shop.shoppayment", "sales.salesorderline",
                      "rag_assistant.knowledgeitem"):
            self.assertNotIn(label, order)
        self.assertFalse(pg.has_relations(InventoryTransaction()))             # розділ «не стосується»
        self.assertTrue(pg.has_relations(SalesOrder()))


class HistoryTests(GenericTestBase):
    def setUp(self):
        super().setUp()
        from core.models import AuditLog
        from inventory.models import Supplier
        self.supplier = Supplier.objects.create(name="Muster GmbH", email="anna@muster.example")
        ct = get_content_type_for_model(self.supplier)
        anna = User.objects.create_user("anna", "anna@x.y", "p", is_staff=True)
        now = timezone.now()
        self.added = LogEntry.objects.create(user=anna, content_type=ct, object_id=str(self.supplier.pk),
                                             object_repr="Muster GmbH", action_flag=ADDITION,
                                             change_message=json.dumps([{"added": {}}]),
                                             action_time=now - timedelta(hours=1))
        self.changed = LogEntry.objects.create(user=self.root, content_type=ct, object_id=str(self.supplier.pk),
                                               object_repr="Muster GmbH", action_flag=CHANGE,
                                               change_message="Змінено Email і Телефон.", action_time=now)
        AuditLog.objects.create(user=self.root, action="update", content_type=ct, object_id=str(self.supplier.pk),
                                object_repr="Muster GmbH", ip_address="203.0.113.77")

    def fresh(self):
        from inventory.models import Supplier
        return Supplier.objects.get(pk=self.supplier.pk)

    def test_history_shows_who_and_what_without_ip(self):
        request = self.request_for(self.root)
        supplier = self.fresh()
        html = str(pg.history_html(request, supplier))
        self.assertIn('<div class="mvp-note" translate="no">Змінено Email і Телефон.</div>', html)
        self.assertIn(self.added.get_change_message(), html)
        self.assertIn('<span translate="no">Rita Root</span>', html)            # повне ім'я
        self.assertIn('<span translate="no">anna</span>', html)                 # без імені — логін
        self.assertIn('aria-label="Змінено"', html)
        self.assertIn('aria-label="Додано"', html)
        self.assertLess(html.index("Rita Root"), html.index("anna"))          # новіші першими
        self.assertIn(sp.dt(self.changed.action_time), html)
        self.assertNotIn("203.0.113.77", html)                                 # журнал аудиту й IP — ні
        self.assertIn(f'<a class="mvp-more-link" href="/admin/inventory/supplier/{supplier.pk}/history/">'
                      "📜 Уся історія →</a>", html)
        self.assertEqual(pg._history_summary(supplier, request), f"{sp.dt(self.changed.action_time)} · Rita Root")

    def test_history_link_only_when_the_panel_allows(self):
        sp.register("inventory.supplier", title=lambda s: format_html("<b>{}</b>", s.name), blocks=[],
                    history_link=False)
        html = str(pg.history_html(self.request_for(self.root), self.fresh()))
        self.assertIn("Змінено Email і Телефон.", html)
        self.assertNotIn("📜 Уся історія", html)

    def test_history_block_through_the_panel(self):
        r = self.client.get(f"/core/panel/inventory.supplier/{self.supplier.pk}/block/history/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Змінено Email і Телефон.", html)
        self.assertNotIn("203.0.113.77", html)
        self.assertIn('data-brief="', html)
        LogEntry.objects.all().delete()
        html = self.client.get(f"/core/panel/inventory.supplier/{self.supplier.pk}/block/history/").content.decode()
        self.assertIn("Змін через картку ще не було.", html)


class GenericPanelTests(GenericTestBase):
    def test_supplier_list_gets_a_generic_panel_that_is_off_by_default(self):
        from inventory.models import PurchaseOrder, Supplier
        s = Supplier.objects.create(name="Muster GmbH", email="anna@muster.example",
                                    website="https://muster.example", notes="Ruft montags an")
        po = PurchaseOrder.objects.create(supplier=s, code="PO-12")
        panel = sp.get("inventory.supplier")
        self.assertTrue(panel.generic)
        self.assertIs(sp.get("inventory.supplier"), panel)
        self.assertEqual([b.key for b in panel.blocks], ["fields", "related", "history"])
        fields, related, history = panel.blocks
        self.assertEqual((fields.lazy, fields.collapsed, fields.default), (False, False, True))
        self.assertEqual((related.lazy, related.collapsed, related.default), (True, True, True))
        self.assertEqual((history.lazy, history.collapsed, history.default), (True, True, True))
        self.assertFalse(sp.prefs(self.root, panel)["on"])
        cfg = cfg_of(self.client.get("/admin/inventory/supplier/").content.decode())
        self.assertTrue(cfg["generic"])
        self.assertFalse(cfg["prefs"]["on"])
        self.assertEqual([b["key"] for b in cfg["blocks"]], ["fields", "related", "history"])
        r = self.client.post("/core/panel/inventory.supplier/prefs/", json.dumps({"set": {"on": True}}),
                             content_type="application/json")
        self.assertTrue(r.json()["prefs"]["on"])
        self.assertTrue(sp.prefs(User.objects.get(pk=self.root.pk), panel)["on"])
        shell = self.client.get(f"/core/panel/inventory.supplier/{s.pk}/").content.decode()
        self.assertIn('<b><span translate="no">Muster GmbH</span></b>', shell)
        self.assertIn('<span class="mvp-subt">Supplier</span>', shell)      # назва моделі — слова інтерфейсу
        body = self.section(shell, "fields")
        self.assertNotIn("data-deferred", body)
        self.assertIn('<span translate="no">anna@muster.example</span>', body)
        self.assertIn('href="https://muster.example" target="_blank" rel="noopener" translate="no">', body)
        self.assertIn('<div class="mvp-clamp" data-mvp-clamp translate="no">Ruft montags an</div>', body)
        self.assertIn("data-deferred", self.section(shell, "related"))
        self.assertIn("data-deferred", self.section(shell, "history"))
        r = self.client.get(f"/core/panel/inventory.supplier/{s.pk}/block/related/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'href="/admin/inventory/purchaseorder/{po.pk}/change/"', r.content.decode())
        self.assertIn(f"/admin/inventory/purchaseorder/?supplier__id__exact={s.pk}", r.content.decode())

    def test_never_models_and_unknown_models_get_no_generic_panel(self):
        self.assertIsNone(pg.generic_panel("shop.shop"))
        self.assertIsNone(pg.generic_panel("email_assistant.emailmessage"))
        self.assertIsNone(pg.generic_panel("nope.model"))
        self.assertIsNone(pg.generic_panel("nope"))

    def test_make_and_providers_get_the_generic_blocks(self):
        with self.assertRaises(ValueError):
            pg.make("nope")
        b = pg.make("related", default=True)
        self.assertEqual((b.key, b.title, b.icon, b.default, b.lazy, b.collapsed), ("related", "Пов’язане", "🔗",
                                                                                   True, True, True))
        self.assertEqual((pg.make("fields").title, pg.make("history").title), ("Усі поля", "Історія"))
        own = sp.register("inventory.location", title=lambda o: format_html("<b>{}</b>", o.code),
                          blocks=[sp.Block("main", "Головне", lambda o, r: "")])
        self.assertEqual([b.key for b in own.blocks], ["main", "history", "fields", "related"])
        self.assertEqual(sp.defaults(own)["blocks"], ["main", "history", "fields"])
        self.assertEqual(sp.defaults(own)["collapsed"], ["history", "fields"])
