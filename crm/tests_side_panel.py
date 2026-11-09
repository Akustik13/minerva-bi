"""📌 Панель праворуч для списку клієнтів CRM (crm/side_panel.py) і crm.utils.orders_of.

Замовлення клієнта — за ключем, потім за email (те саме правило, що RFM); «Продажі й RFM» — ті самі числа, що
картка CRM; листи — лише з власних скриньок; час askDANTE — чужі описи сховані, вартість — лише адміністраторам;
⭐ / статус / сегмент з панелі; права ролей; Kundennummer лише читається; документи з media/customers/<pk>/;
сторінка списку має налаштування панелі, картки клієнтів — data-pk.

    python manage.py test crm.tests_side_panel --settings=tabele.settings_test
"""
import datetime
import json
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import side_panel as sp
from core.models import UserProfile
from crm import utils
from crm.models import Customer, CustomerNote, CustomerTimeline
from sales.models import SalesOrder
from sales.revenue import sale_orders

MODEL = "crm.customer"
URL = "/core/panel/crm.customer/"
CLIENT = "einkauf@muster.example"
HIDE = ["sources_display", "avg_order_display", "repeat_badge", "rfm_display", "strategy_btn"]


def cfg_of(html):
    return json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from shop.models import ShopSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.default_currency = "EUR"
        ss.save()
        s = ShopSettings.get()
        s.fx_rates = {"USD": 0.5}
        s.save()
        saved = dict(sp._registry)

        def restore():
            sp._registry.clear()
            sp._registry.update(saved)
        self.addCleanup(restore)
        self.today = timezone.localdate()
        self.boss = User.objects.create_superuser("boss", "boss@x.y", "p")
        self.client.force_login(self.boss)
        self.c = Customer.objects.create(name="Anna Muster", company="Muster GmbH", email=CLIENT,
                                         phone="+49 30 1234", country="DE", addr_street="Hauptstr. 1",
                                         addr_zip="10115", addr_city="Berlin")
        self.n = 0
        self.uid = 0

    # ── дані ──
    def user(self, name, role, modules=None):
        u = User.objects.create_user(name, f"{name}@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role=role, allowed_modules=modules)
        return User.objects.get(pk=u.pk)

    def order(self, mine=True, days_ago=10, total="100", currency="EUR", **kw):
        self.n += 1
        values = dict(source="webshop", order_number=f"R{self.n:04d}", total_price=Decimal(total), currency=currency,
                      order_date=self.today - datetime.timedelta(days=days_ago))
        if mine:
            values.update(customer_key=self.c.external_key, email=self.c.email)
        values.update(kw)
        return SalesOrder.objects.create(**values)

    def account(self, user, address, **kw):
        from email_assistant.models import EmailAccount, EmailSettings
        acc = EmailAccount.objects.create(user=user, email_address=address, imap_username=address, imap_password="x",
                                          smtp_username=address, smtp_password="x", **kw)
        es = EmailSettings.get_for_user(user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        return acc

    def mail(self, acc, subject, **extra):
        from email_assistant.models import EmailMessage
        self.uid += 1
        values = dict(account=acc, imap_uid=self.uid, folder=EmailMessage.FOLDER_INBOX, subject=subject,
                      from_email=CLIENT, to_emails=[acc.email_address],
                      body_text=f"{subject}\n\nMit freundlichen Grüßen\nAnna Muster",
                      sent_at=timezone.now() - datetime.timedelta(hours=self.uid))
        values.update(extra)
        return EmailMessage.objects.create(**values)

    # ── запити ──
    def shell(self, pk=None):
        r = self.client.get(f"{URL}{pk or self.c.pk}/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.content.decode()

    def block(self, key, pk=None, status=200):
        r = self.client.get(f"{URL}{pk or self.c.pk}/block/{key}/")
        self.assertEqual(r.status_code, status, r.content[:300])
        return r.content.decode()

    @staticmethod
    def section(html, key):
        at = html.index(f'data-sec="{key}"')
        start = html.rindex("<section", 0, at)
        return html[start:html.index("</section>", start)]

    def post_edit(self, data):
        return self.client.post(f"{URL}{self.c.pk}/edit/", json.dumps(data), content_type="application/json")

    def fresh(self):
        return Customer.objects.get(pk=self.c.pk)


class CardAnchorTests(Base):
    """«↗ Відкрити в картці» веде на ту саму групу, хоч картка людини має й «📁 Проєкти» (з правом бачити проєкти)."""
    def test_anchor_follows_inserted_projects_group(self):
        from crm.admin import CustomerAdmin
        static = [name for name, _o in CustomerAdmin.fieldsets]
        html = self.shell()
        for key, title in (("sales", "📊 Аналітика"), ("orders", "📜 Історія замовлень")):
            i = static.index(title)                       # після «🎯 Сегментація» картка має ще «📁 Проєкти»
            self.assertIn(f'data-card="/admin/crm/customer/{self.c.pk}/change/#fieldset-0-{i + 1}-heading"',
                          self.section(html, key))
        self.assertIn(f"#fieldset-0-{static.index('📋 Контактна інформація')}-heading", self.section(html, "contact"))


class OrdersOfTests(Base):
    """crm.utils.orders_of — те саме правило, що _matching (RFM, «Тип»), але без відбору продажів."""

    def test_key_then_email_like_rfm(self):
        own = self.order()
        stale = self.order(mine=False, customer_key="stale-key-of-deleted", email="  EINKAUF@Muster.example ")
        empty = self.order(mine=False, customer_key="stale-two", email=CLIENT)
        SalesOrder.objects.filter(pk=empty.pk).update(customer_key="")          # без ключа — лише email
        other = Customer.objects.create(name="Andere", email=CLIENT)              # та сама адреса, свій ключ
        theirs = self.order(mine=False, customer_key=other.external_key, email=CLIENT)
        self.order(mine=False, customer_key="nobody-else", email="fremd@example.com")
        quote = self.order(document_type="QUOTE", affects_stock=False)
        cancelled = self.order(status="cancelled")

        got = set(utils.orders_of(self.c).values_list("pk", flat=True))
        self.assertEqual(got, {own.pk, stale.pk, empty.pk, quote.pk, cancelled.pk})   # без відбору продажів
        self.assertNotIn(theirs.pk, got)                                               # інший ключ CRM — не наше
        sales = set(utils.orders_of(self.c, base=sale_orders()).values_list("pk", flat=True))
        self.assertEqual(sales, {own.pk, stale.pk, empty.pk})
        self.assertEqual(utils.order_counts([self.c])[self.c.pk], len(sales))          # те саме, що «Тип» і RFM
        self.assertEqual(set(utils.orders_of(other).values_list("pk", flat=True)), {theirs.pk, stale.pk, empty.pk})

    def test_without_email_only_key_and_without_both_nothing(self):
        own = self.order()
        self.order(mine=False, customer_key="stale", email=CLIENT)
        no_mail = Customer(name="Ohne Mail", external_key=self.c.external_key, email="")
        self.assertEqual(list(utils.orders_of(no_mail).values_list("pk", flat=True)), [own.pk])
        self.assertFalse(utils.orders_of(Customer(name="Leer", external_key="", email="")).exists())
        self.assertFalse(utils.orders_of(None).exists())


class SalesAndOrdersTests(Base):
    def test_sales_block_numbers_equal_card_stats_and_rfm(self):
        self.order(total="100", days_ago=10)
        self.order(total="400", currency="USD", days_ago=40)                 # 200 EUR за курсом
        self.order(mine=False, customer_key="stale-key", email=CLIENT, total="50", days_ago=100)
        self.order(total="9000", status="cancelled")                          # не продаж
        html = self.block("sales")
        c = self.fresh()
        card, rfm = c.card_stats(), c.rfm_score()
        self.assertEqual(card["count"], 3)
        self.assertIn(f'<dt>Замовлень</dt><dd><span translate="no">{card["count"]}</span></dd>', html)
        self.assertIn(card["rev"], html)
        self.assertIn(card["avg"], html)
        self.assertIn(f"{card['days']} дн. тому", html)
        self.assertIn(f"R {rfm['R']} · F {rfm['F']} · M {rfm['M']} = {rfm['score']}/15", html)
        self.assertIn(rfm["segment"], html)
        self.assertIn("✅ Постійний", html)
        first = (self.today - datetime.timedelta(days=100)).strftime("%d.%m.%Y")   # перше — за ключем і email
        self.assertIn(f"<dt>Перше</dt><dd><span translate=\"no\">{first}</span></dd>", html)
        self.assertIn(rfm["segment"], self.shell().split('class="mvp-subt"')[1].split("</span>")[0])   # підзаголовок

    def test_no_sales_is_empty_state(self):
        self.order(document_type="QUOTE", affects_stock=False)
        sec = self.section(self.shell(), "sales")
        self.assertIn("data-empty", sec)
        self.assertIn("Продажів ще немає.", sec)

    def test_orders_block_counts_waiting_and_late(self):
        late = self.order(status="processing", shipping_deadline=self.today - datetime.timedelta(days=2))
        self.order(status="received", shipping_deadline=self.today + datetime.timedelta(days=5))
        self.order(status="delivered", shipped_at=self.today - datetime.timedelta(days=3))
        self.order(mine=False, customer_key="stale", email=CLIENT, document_type="QUOTE", affects_stock=False)
        sec = self.section(self.shell(), "orders")
        self.assertIn('data-count="4"', sec)
        self.assertIn('data-tone="err"', sec)
        self.assertIn("чекають відправки 2 · прострочено 1", sec)
        self.assertIn(f'href="/admin/sales/salesorder/{late.pk}/change/"', sec)
        self.assertIn("⚠ відправити до", sec)
        self.assertIn("Запит пропозиції", sec)                                   # тип документа видно
        self.assertIn("/admin/sales/salesorder/?q=einkauf%40muster.example", sec)


class MailTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailThread
        self.anna = self.user("anna", "manager")
        self.boss_acc = self.account(self.boss, "boss@firma.example", is_primary=True)
        self.boss_info = self.account(self.boss, "info@firma.example")
        self.anna_acc = self.account(self.anna, "anna@firma.example")
        thread = EmailThread.objects.create(account=self.boss_acc, subject="Lieferung", crm_customer=self.c)
        self.mine = self.mail(self.boss_acc, "Boss: Lieferung 4711", thread=thread)
        self.mail(self.boss_acc, "Boss: Werbung", is_spam=True)
        self.mail(self.boss_acc, "Boss: geloescht", is_deleted=True)
        self.second = self.mail(self.boss_info, "Info: Rechnung", is_read=True)
        self.theirs = self.mail(self.anna_acc, "Anna: privat")
        from offers.models import QuoteRequest
        QuoteRequest.objects.update_or_create(message=self.mine, defaults={"thread": thread, "status": "new"})

    def test_only_own_mailboxes_even_for_superuser(self):
        html = self.block("mail")
        self.assertIn("Boss: Lieferung 4711", html)
        self.assertIn("Info: Rechnung", html)
        for hidden in ("Anna: privat", "Boss: Werbung", "Boss: geloescht", "Mit freundlichen"):
            self.assertNotIn(hidden, html)
        self.assertIn(f'href="{reverse("email_assistant:message", args=[self.mine.pk])}"', html)   # основна
        self.assertNotIn(reverse("email_assistant:message", args=[self.second.pk]), html)          # інша — 404 там
        self.assertIn("інша скринька:", html)
        self.assertIn("📝 Запитів без Angebot: 1", html)
        self.assertIn(f"/email/?customer={self.c.pk}", html)
        sec = self.section(self.shell(), "mail")
        self.assertIn('data-count="2"', sec)                                       # лічильник — теж лише свої
        self.assertIn('data-deferred', sec)                                        # вміст — окремим запитом

        self.client.force_login(self.anna)
        html = self.block("mail")
        self.assertIn("Anna: privat", html)
        for hidden in ("Boss: Lieferung", "Info: Rechnung", "Запитів без Angebot"):
            self.assertNotIn(hidden, html)
        self.assertIn('data-count="1"', self.section(self.shell(), "mail"))

    def test_reading_in_panel_does_not_mark_as_read(self):
        from email_assistant.models import EmailMessage
        self.block("mail")
        self.assertFalse(EmailMessage.objects.get(pk=self.mine.pk).is_read)


class NotesTests(Base):
    def test_notes_without_mail_events(self):
        now = timezone.now()
        CustomerTimeline.objects.create(customer=self.c, event_type="email_in", title="Fremde Mail: geheim")
        CustomerTimeline.objects.create(customer=self.c, event_type="email_out", title="Angeheftete Mail",
                                        is_pinned=True)
        CustomerTimeline.objects.create(customer=self.c, event_type="note", title="Rückruf vereinbart", body="Montag")
        CustomerTimeline.objects.create(customer=self.c, event_type="reminder", title="Muster anrufen",
                                        remind_at=now + datetime.timedelta(days=2))
        CustomerTimeline.objects.create(customer=self.c, event_type="email_in", title="Erinnerung aus Mail",
                                        remind_at=now + datetime.timedelta(days=1))
        CustomerNote.objects.create(customer=self.c, subject="Preisliste geschickt", body="PDF 2026",
                                    created_by=self.boss)
        sec = self.section(self.shell(), "notes")
        for shown in ("Rückruf vereinbart", "Muster anrufen", "Preisliste geschickt", "PDF 2026"):
            self.assertIn(shown, sec)
        for hidden in ("Fremde Mail", "Angeheftete Mail", "Erinnerung aus Mail"):
            self.assertNotIn(hidden, sec)
        self.assertIn('data-count="1"', sec)                                       # нагадування — без листів


class HoursTests(Base):
    def setUp(self):
        super().setUp()
        from worktime.models import DanteProject, DanteUser, TimeRecord
        self.anna = self.user("anna", "manager")
        dp = DanteProject.objects.create(dante_id="363", name="Antenne", crm_customer=self.c)
        du_anna = DanteUser.objects.create(dante_id="1", first_name="Anna", user=self.anna)
        du_bob = DanteUser.objects.create(dante_id="2", first_name="Bob")
        TimeRecord.objects.create(dante_id="r1", dante_user=du_anna, person_id="1", day=self.today, duration=90,
                                  dante_project=dp, activity_name="Meeting", description="Annas eigene Notiz",
                                  hourly_rate=Decimal("80"))
        TimeRecord.objects.create(dante_id="r2", dante_user=du_bob, person_id="2", day=self.today, duration=30,
                                  dante_project=dp, activity_name="Support",
                                  description="Bobs Zusammenfassung der Mail", hourly_rate=Decimal("60"))

    def test_other_descriptions_hidden_and_cost_only_for_admins(self):
        self.client.force_login(self.anna)                                         # менеджер — не адміністратор
        html = self.block("hours")
        self.assertIn('<span translate="no">2:00</span>', html)                  # 90 + 30 хв
        self.assertIn("Annas eigene Notiz", html)
        self.assertNotIn("Bobs Zusammenfassung", html)
        self.assertIn("Support", html)                                             # вид роботи видно
        self.assertNotIn("за ставками askDANTE", html)
        self.assertNotIn("150,00", html)

        self.client.force_login(self.boss)                                         # адміністратор: вартість так,
        html = self.block("hours")                                                 # чужі описи — ні
        self.assertIn('≈ <span translate="no">150,00</span>', html)               # (80 × 90 + 60 × 30) / 60
        self.assertIn("за ставками askDANTE", html)
        self.assertNotIn("Annas eigene Notiz", html)
        self.assertNotIn("Bobs Zusammenfassung", html)


class EditTests(Base):
    def test_favorite_cell_and_undo(self):
        r = self.post_edit({"key": "favorite", "value": True, "expect": ""})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual(j["message"], "Обраний: так")
        self.assertIn("⭐", j["cells"]["favorite_col"])
        self.assertIn("crm-fav-on", j["cells"]["favorite_col"])
        self.assertEqual(j["undo"], {"key": "favorite", "value": ""})
        self.assertIn('aria-pressed="true"', j["head"])
        self.assertTrue(self.fresh().is_favorite)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.c.pk)).exists())   # як «Зберегти» в картці
        r = self.post_edit({"key": "favorite", "value": j["undo"]["value"], "expect": j["value"]})   # ↶ Скасувати
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertFalse(self.fresh().is_favorite)
        self.assertIn("☆", r.json()["cells"]["favorite_col"])

    def test_view_only_role_gets_403(self):
        viewer = self.user("viewer", "ai")                                         # CRM лише для перегляду
        self.client.force_login(viewer)
        html = self.shell()
        self.assertNotIn('data-edit="favorite"', html)
        self.assertNotIn('class="mvp-pill', html)                                  # статус — лише значок
        r = self.post_edit({"key": "favorite", "value": True, "expect": ""})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.post_edit({"key": "status", "value": "vip"}).status_code, 403)
        c = self.fresh()
        self.assertEqual((c.is_favorite, c.status), (False, "active"))

    def test_status_in_head_and_segment_in_contact(self):
        html = self.shell()
        self.assertIn('data-edit="status"', html.split("</header>")[0])
        self.assertIn('data-edit="segment"', self.section(html, "contact"))
        r = self.post_edit({"key": "status", "value": "vip", "expect": "active"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertEqual(j["message"], "Статус: VIP")
        self.assertIn("VIP", j["cells"]["status_badge"])
        self.assertIn("contact", j["blocks"])
        self.assertEqual(j["undo"], {"key": "status", "value": "active"})
        self.assertIn("mvp-pill mvp-t-warn", j["head"])
        r = self.post_edit({"key": "segment", "value": "b2b", "expect": "b2c"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertIn("B2B", r.json()["cells"]["segment_badge"])
        self.assertIn('data-value="b2b"', r.json()["blocks"]["contact"])
        r = self.post_edit({"key": "status", "value": "deleted"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_value"))
        self.assertEqual((self.fresh().status, self.fresh().segment), ("vip", "b2b"))


class ListAndRolesTests(Base):
    def test_list_page_has_config_and_card_rows_carry_pk(self):
        html = self.client.get("/admin/crm/customer/").content.decode()
        cfg = cfg_of(html)
        self.assertEqual((cfg["model"], cfg["url"], cfg["hideDefault"]), (MODEL, URL, HIDE))
        self.assertTrue(cfg["canChange"])
        self.assertEqual([b["key"] for b in cfg["blocks"]][:7],
                         ["contact", "sales", "orders", "mail", "notes", "offers", "projects"])
        self.assertEqual(cfg["prefs"]["actions"], ["mail", "inbox", "new_order", "favorite"])
        self.assertEqual(cfg["prefs"]["blocks"],
                         ["contact", "sales", "orders", "mail", "notes", "offers", "projects", "history", "fields"])
        names = [a["name"] for a in cfg["adminActions"]]
        self.assertIn("action_set_status_vip", names)
        self.assertNotIn("action_merge_customers", names)                          # злиття — щонайменше двох
        self.assertRegex(html, rf'<div class="crm-card [^"]*" id="crm-card-{self.c.pk}" data-pk="{self.c.pk}">')

    def test_sections_and_actions_follow_roles(self):
        viewer = self.user("viewer", "ai")                                         # лише CRM (+ стратегія)
        self.client.force_login(viewer)
        cfg = cfg_of(self.client.get("/admin/crm/customer/").content.decode())
        keys = [b["key"] for b in cfg["blocks"]]
        for key in ("contact", "sales", "notes", "documents"):
            self.assertIn(key, keys)
        for key in ("orders", "mail", "offers", "projects", "shipments", "money", "calendar", "hours", "shop"):
            self.assertNotIn(key, keys)
        actions = [a["key"] for a in cfg["actions"]]
        for key in ("mail", "inbox", "new_order", "favorite", "orders", "note_add"):
            self.assertNotIn(key, actions)
        self.assertFalse(cfg["canChange"])
        html = self.shell()
        self.assertNotIn('data-sec="orders"', html)
        self.assertNotIn('data-act="new_order"', html)
        self.block("orders", status=404)
        self.block("mail", status=404)

    def test_no_mail_address_disables_write_action(self):
        self.c.email = ""
        self.c.save()
        html = self.shell().split("</header>")[0]
        self.assertIn('data-act="mail" aria-disabled="true" title="Немає email"', html)


class ContactAndDocumentsTests(Base):
    def test_contact_and_kundennummer_only_read(self):
        from shop.models import CustomerNumber
        with mock.patch("shop.numbering.for_crm", side_effect=AssertionError("видав би номер")), \
                mock.patch("shop.numbering._allocate", side_effect=AssertionError("видав би номер")):
            sec = self.section(self.shell(), "contact")
        self.assertFalse(CustomerNumber.objects.exists())
        self.assertNotIn("Kundennummer", sec)
        for shown in ("Muster GmbH", "Hauptstr. 1", "10115 Berlin", "(DE)", 'href="tel:+49301234"',
                      "/email/compose/?to=einkauf%40muster.example"):
            self.assertIn(shown, sec)
        CustomerNumber.objects.create(number=31728, crm_customer=self.c)
        self.assertIn("31728", self.section(self.shell(), "contact"))

    def test_documents_list_customer_folder(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        folder = Path(media, "customers", str(self.c.pk))
        folder.mkdir(parents=True)
        (folder / "Vertrag 2026.pdf").write_bytes(b"%PDF-1.4 test")
        (folder / ".geheim").write_text("x")
        (folder / "Unterordner").mkdir()
        with override_settings(MEDIA_ROOT=media):
            html = self.block("documents")
        self.assertIn(f'href="/media/customers/{self.c.pk}/Vertrag%202026.pdf" target="_blank"', html)
        self.assertNotIn(".geheim", html)
        self.assertNotIn("Unterordner", html)
        self.assertNotIn("data-error", html)


class EverySectionTests(Base):
    """Усі розділи увімкнено — для адміністратора жоден не падає й показує свої дані."""

    def test_every_section_renders(self):
        from calendar_app.models import CalendarEvent
        from accounting.models import Invoice
        from offers.models import Offer
        from projects.models import Project
        from shipping.models import Carrier, Shipment
        from shop.models import Shop, ShopCustomer
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        late = self.order(status="processing", shipping_deadline=self.today - datetime.timedelta(days=1))
        self.order(payment_status="unpaid", payment_method="invoice")
        Offer.objects.create(customer=self.c, status=Offer.SENT, valid_until=self.today - datetime.timedelta(days=1),
                             gross_total=Decimal("119.00"), currency="EUR", sent_at=timezone.now())
        Project.objects.create(name="5G Antenne", customer=self.c, owner=self.boss, next_action="Muster schicken",
                               due_date=self.today + datetime.timedelta(days=7))
        Project.objects.create(name="Alt", customer=self.c, status=Project.DONE)
        carrier = Carrier.objects.create(name="UPS Express", carrier_type="ups")
        Shipment.objects.create(order=late, carrier=carrier, status=Shipment.Status.IN_TRANSIT,
                                tracking_number="1Z999AA10123456784", carrier_delayed=True,
                                eta_to=self.today + datetime.timedelta(days=2))
        Invoice.objects.create(customer=self.c, status="sent", due_date=self.today - datetime.timedelta(days=3))
        CalendarEvent.objects.create(user=self.boss, title="Termin Muster", crm_customer=self.c,
                                     event_type=CalendarEvent.TYPE_MEETING,
                                     start_at=timezone.now() + datetime.timedelta(days=1))
        ShopCustomer.objects.create(shop=Shop.objects.create(name="Sevskiy Shop", slug="webshop"), email=CLIENT,
                                    password="!", crm_customer=self.c)
        folder = Path(media, "customers", str(self.c.pk))
        folder.mkdir(parents=True)
        (folder / "Vertrag.pdf").write_bytes(b"%PDF-1.4 test")
        panel = sp.get(MODEL)
        keys = [b.key for b in panel.blocks]
        sp.save_prefs(self.boss, panel, {"set": {"blocks": keys, "collapsed": []}})
        expect = {"offers": "⚠ строк дії минув", "projects": "завершених і скасованих: 1",
                  "shipments": "1Z999AA10123456784", "money": "⚠ оплатити до", "calendar": "Termin Muster",
                  "shop": "Sevskiy Shop", "documents": "Vertrag.pdf", "orders": "прострочено 1",
                  "contact": "Anna Muster", "sales": "R ", "history": "", "fields": "Ще поля", "related": ""}
        with override_settings(MEDIA_ROOT=media), self.assertNoLogs(None, "WARNING"):   # і жодних збоїв розділів
            shell = self.shell()
            for key in keys:
                html = self.block(key)
                self.assertNotIn("data-error", html, key)
                if expect.get(key):
                    self.assertIn(expect[key], html, key)
        self.assertNotIn("data-error", shell)
        self.assertIn("Не оплачено", self.block("money"))
        self.assertIn("⚠ затримка", self.block("shipments"))
