"""Пульт проєкту: нові прив'язки (закупівля, JLC, відправлення), пропозиції, «потребує уваги», кеш стану, шкала,
📌 важливі моменти, приватність пошти, права.  python manage.py test projects --settings=tabele.settings_test"""
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.db import IntegrityError, transaction
from django.utils import timezone

from email_assistant.models import EmailAccount
from inventory.models import Product, PurchaseOrder, PurchaseOrderLine, Supplier
from jlcpcb.models import JLCOrder
from projects import cockpit, overview, services, state, timeline
from projects.models import Project, ProjectHighlight, ProjectLink, ProjectParticipant, ProjectState
from projects.tests import ME, ProjectsBase
from sales.models import SalesOrder
from shipping.models import Carrier, Shipment


class CockpitBase(ProjectsBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.antenna = Product.objects.create(sku="AN220213-01C", name="Antenna 5G n78")
        self.connector = Product.objects.create(sku="CN-MHF4", name="Connector MHF4")
        self.supplier = Supplier.objects.get(name="Parts Ltd")
        self.carrier = Carrier.objects.create(name="DHL", carrier_type="dhl")
        self.ask = self.mail("Anfrage 5G Antenne", body="Wir brauchen 500 Stück, Muster bis KW 48?",
                             sent=self.ago(9))
        self.project = services.create_project(user=self.user, threads=[self.ask.thread])
        self.project.products.add(self.antenna)

    def ago(self, days, hour=9):
        d = timezone.localdate() - timedelta(days=days)
        return timezone.make_aware(datetime(d.year, d.month, d.day, hour, 0))

    def po(self, status=PurchaseOrder.Status.ORDERED, expected=None, product=None, supplier=None, days_ago=10):
        po = PurchaseOrder.objects.create(supplier=supplier, status=status, expected_date=expected,
                                          order_date=self.today - timedelta(days=days_ago))
        if product is not None:
            PurchaseOrderLine.objects.create(purchase_order=po, product=product, qty_ordered=500)
        return po


class LinkModelTests(CockpitBase):
    def test_one_target_per_link_and_unique_per_project(self):
        po = self.po()
        services.link_object(self.project, purchase_order=po, user=self.user)
        self.assertEqual(ProjectLink.objects.get(purchase_order=po).target, po)
        with self.assertRaises(IntegrityError), transaction.atomic():      # дві цілі в одному рядку — ні
            ProjectLink.objects.create(project=self.project, purchase_order=self.po(), order=SalesOrder.objects.create(
                source="digikey", order_number="1", affects_stock=False))
        with self.assertRaises(IntegrityError), transaction.atomic():      # та сама закупівля двічі — ні
            ProjectLink.objects.create(project=self.project, purchase_order=po)

    def test_manual_link_confirms_rejected_suggestion(self):
        po = self.po(product=self.antenna)
        link = ProjectLink.objects.get(project=self.project, purchase_order=po)
        services.decide(link, False, user=self.user)
        self.assertEqual(services.link_object(self.project, purchase_order=po, user=self.user).status,
                         ProjectLink.CONFIRMED)


class SuggestionTests(CockpitBase):
    def test_purchase_with_project_product_is_suggested_once(self):
        po = self.po(product=self.antenna)
        link = ProjectLink.objects.get(project=self.project, purchase_order=po)
        self.assertEqual((link.status, link.source), (ProjectLink.SUGGESTED, ProjectLink.AUTO))
        self.assertIn("AN220213-01C", link.reason)
        services.decide(link, False, user=self.user)
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.antenna, qty_ordered=10)
        self.assertEqual(ProjectLink.objects.get(pk=link.pk).status, ProjectLink.REJECTED)   # рішення людини лишається
        other = self.po(product=self.connector)
        self.assertFalse(ProjectLink.objects.filter(purchase_order=other).exists())         # чужий товар — ні

    def test_supplier_participant_suggests_only_single_project(self):
        ProjectParticipant.objects.create(project=self.project, email="sales@parts.example",
                                          role=ProjectParticipant.SUPPLIER)
        po = self.po(supplier=self.supplier)
        self.assertEqual(ProjectLink.objects.get(purchase_order=po).reason, "постачальник закупівлі — учасник проєкту")
        second = Project.objects.create(name="Second")
        ProjectParticipant.objects.create(project=second, email="sales@parts.example", role=ProjectParticipant.SUPPLIER)
        po2 = self.po(supplier=self.supplier)
        self.assertFalse(ProjectLink.objects.filter(purchase_order=po2).exists())           # двоє — не вгадуємо

    def test_jlc_suggested_by_product_unless_its_purchase_is_linked(self):
        jlc = JLCOrder.objects.create(jlc_order_id="J-1", jlc_order_number="Y1", product=self.antenna,
                                      order_date=self.today)
        self.assertEqual(ProjectLink.objects.get(jlc_order=jlc).status, ProjectLink.SUGGESTED)
        po = self.po()
        services.link_object(self.project, purchase_order=po, user=self.user)
        jlc2 = JLCOrder.objects.create(jlc_order_id="J-2", product=self.antenna, purchase_order=po,
                                       order_date=self.today)
        self.assertFalse(ProjectLink.objects.filter(jlc_order=jlc2).exists())   # видно через закупівлю

    def test_scan_finds_existing_purchases_and_jlc(self):
        self.project.products.remove(self.antenna)
        po = self.po(product=self.antenna)
        jlc = JLCOrder.objects.create(jlc_order_id="J-3", product=self.antenna, order_date=self.today)
        old = self.po(product=self.antenna, days_ago=500)
        self.assertEqual(services.scan(self.project), 0)                         # без товарів проєкту — нічого
        self.project.products.add(self.antenna)
        self.assertEqual(services.scan(self.project), 2)
        self.assertTrue(ProjectLink.objects.filter(project=self.project, purchase_order=po).exists())
        self.assertTrue(ProjectLink.objects.filter(project=self.project, jlc_order=jlc).exists())
        self.assertFalse(ProjectLink.objects.filter(project=self.project, purchase_order=old).exists())
        self.assertEqual(services.scan(self.project), 0)                         # повтор нічого не додає


class FactsTests(CockpitBase):
    def test_derived_objects_and_supply_attention(self):
        po = self.po(expected=self.today - timedelta(days=2), product=self.antenna)
        services.link_object(self.project, purchase_order=po, user=self.user)
        jlc = JLCOrder.objects.create(jlc_order_id="J-4", purchase_order=po, order_date=self.today - timedelta(days=8),
                                      expected_date=self.today - timedelta(days=1),
                                      local_status=JLCOrder.LocalStatus.IN_PRODUCTION)
        parcel = Shipment.objects.create(carrier=self.carrier, direction=Shipment.IN, purchase_order=po,
                                         tracking_number="JD0001", status=Shipment.Status.DELIVERED)
        order = SalesOrder.objects.create(source="digikey", order_number="555", affects_stock=False,
                                          status="processing", order_date=self.today - timedelta(days=3))
        services.link_order(self.project, order, user=self.user)
        out = Shipment.objects.create(carrier=self.carrier, order=order, tracking_number="1Z999",
                                      status=Shipment.Status.ERROR)
        draft = self.po(status=PurchaseOrder.Status.DRAFT)
        services.link_object(self.project, purchase_order=draft, user=self.user)
        facts = overview.collect(self.project)
        self.assertEqual([p.pk for p in facts["purchases"]], [po.pk, draft.pk])
        self.assertEqual([j.pk for j in facts["jlc"]], [jlc.pk])
        self.assertEqual({s.pk for s in facts["shipments"]}, {parcel.pk, out.pk})
        by_text = {a["text"]: a for a in facts["attention"]}
        late = next(a for t, a in by_text.items() if t.startswith(f"Закупівля {po}: чекали"))
        self.assertEqual((late["level"], late["kind"]), ("high", "risk"))
        self.assertIn("оприбуткуйте", next(t for t in by_text if t.startswith("Посилка JD0001")))
        self.assertEqual(by_text["Відправлення 1Z999: помилка оформлення"]["level"], "high")
        self.assertTrue(any(t.startswith("JLC J-4") for t in by_text))
        self.assertEqual(next(a for t, a in by_text.items() if "ще чернетка" in t)["kind"], "todo")
        self.assertEqual(facts["more"]["purchases"], 2)

    def test_mail_of_colleague_is_hidden_in_attention_and_tables(self):
        bob = User.objects.create_user("bob", "b@x.y", "p", is_staff=True)
        acc = EmailAccount.objects.create(user=bob, email_address="bob@sevskiy.de", imap_username="bob",
                                          imap_password="x", smtp_username="bob", smtp_password="x")
        self.account, mine = acc, self.account
        secret = self.mail("Geheim: Preisverhandlung", sent=self.ago(4))
        self.account = mine
        services.link_thread(self.project, secret.thread, user=bob)
        facts = overview.collect(self.project)
        item = next(a for a in facts["attention"] if a.get("owner") == bob.pk)
        self.assertIn("Geheim", item["text"])
        mine_view = overview.visible(facts["attention"], self.user)
        self.assertFalse(any("Geheim" in a["text"] for a in mine_view))
        self.assertTrue(any(a["text"].endswith("лист з іншої скриньки") and not a["url"] for a in mine_view))
        self.assertTrue(any("Geheim" in a["text"] for a in overview.visible(facts["attention"], bob)))
        rows = services.timeline(self.project, viewer=self.user)
        self.assertIn("лист з іншої скриньки", [r["title"] for r in rows])
        self.assertNotIn("Geheim: Preisverhandlung", [r["title"] for r in rows])
        svg = timeline.render(self.project, facts, self.user)
        self.assertNotIn("Geheim", svg)
        self.assertIn("з інших скриньок", svg)


class StateTests(CockpitBase):
    def test_states_are_cached_and_refreshed_when_dirty(self):
        st = state.states([self.project])[self.project.pk]
        self.assertEqual(st.health, "bad")                      # лист клієнта без відповіді 9 дн.
        with patch("projects.overview.collect", side_effect=AssertionError("кеш мав спрацювати")):
            self.assertEqual(state.states([self.project])[self.project.pk].pk, st.pk)
        self.mail("Re: Anfrage 5G Antenne", from_email=ME, to=("buyer@client.example",), folder="Sent",
                  in_reply_to=self.ask.message_id, sent=self.ago(0))
        self.assertTrue(ProjectState.objects.get(pk=st.pk).dirty)   # новий лист гілки проєкту
        self.assertEqual(state.states([self.project])[self.project.pk].health, "ok")   # лишилось лише «info»
        self.project.status = Project.DONE
        self.project.save()
        self.assertEqual(state.states([self.project])[self.project.pk].health, "closed")

    def test_list_column_and_filter_use_state(self):
        page = self.client.get("/admin/projects/project/")
        self.assertContains(page, "🔴 1")
        self.assertContains(page, "#pj-cockpit")
        with patch("projects.overview.collect", side_effect=AssertionError("кеш мав спрацювати")):
            self.assertContains(self.client.get("/admin/projects/project/", {"attention": "high"}), self.project.code)


class TimelineTests(CockpitBase):
    def test_all_lanes_render_valid_svg(self):
        from calendar_app.models import CalendarEvent
        from offers import services as offers
        from offers.models import Offer
        from worktime.models import DanteProject, ProjectMap, TimeRecord
        offer = offers.new_offer(customer=self.client_co, user=self.user, project=self.project)
        Offer.objects.filter(pk=offer.pk).update(status=Offer.SENT, sent_at=self.ago(8),
                                                 valid_until=self.today + timedelta(days=20))
        order = SalesOrder.objects.create(source="digikey", order_number="777", affects_stock=False, status="shipped",
                                          order_date=self.today - timedelta(days=7),
                                          shipped_at=self.ago(2))
        services.link_order(self.project, order, user=self.user)
        po = self.po(expected=self.today + timedelta(days=5), product=self.antenna)
        services.link_object(self.project, purchase_order=po, user=self.user)
        CalendarEvent.objects.create(user=self.user, title="Muster senden", start_at=self.ago(-3),
                                     email_message=self.ask, event_type="deadline")
        services.add_highlight(self.project, title="Spezifikation freigegeben <OK>", kind=ProjectHighlight.APPROVAL,
                               message=self.ask, user=self.user)
        dp = DanteProject.objects.create(dante_id="D1", name="P-Test")
        ProjectMap.objects.create(project=self.project, dante_project=dp)
        TimeRecord.objects.create(dante_id="T1", day=self.today - timedelta(days=6), duration=150, dante_project=dp)
        self.project.due_date = self.today + timedelta(days=30)
        self.project.start_date = self.today - timedelta(days=12)
        self.project.save()
        facts = overview.collect(self.project)
        svg = timeline.render(self.project, facts, self.user)
        ET.fromstring(svg)                                              # коректний XML
        for lane in ("📌 Важливі моменти", "✉️ Листування", "📝 Angebote й документи", "🛒 Замовлення й доставка",
                     "🏭 Закупівлі й виробництво", "📅 Терміни й зустрічі", "⏱ Час askDANTE"):
            self.assertIn(lane, svg)
        self.assertIn("Spezifikation freigegeben &lt;OK&gt;", svg)       # текст людини — екранований
        self.assertIn("строк клієнту", svg)
        self.assertIn(f"/admin/inventory/purchaseorder/{po.pk}/change/", svg)
        self.assertIn("2,5", svg)                                       # 150 хв = 2,5 год
        short = timeline.render(self.project, facts, self.user, months=3)
        ET.fromstring(short)

    def test_empty_project_and_foreign_calendar(self):
        from calendar_app.models import CalendarEvent
        empty = Project.objects.create(name="Empty")
        self.assertIn("порожньо", timeline.render(empty, overview.collect(empty), self.user))
        bob = User.objects.create_user("bob2", "b2@x.y", "p", is_staff=True)
        CalendarEvent.objects.create(user=bob, title="Bobs Termin", start_at=self.ago(-2), email_message=self.ask)
        self.assertNotIn("Bobs Termin", timeline.render(self.project, overview.collect(self.project), self.user))


class CockpitViewTests(CockpitBase):
    def url(self, name, *args):
        from django.urls import reverse
        return reverse(f"projects:{name}", args=args)

    def post(self, url, body=None, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(url, json.dumps(body or {}), content_type="application/json")

    def test_change_page_has_cockpit(self):
        services.add_highlight(self.project, title="Клієнт погодив специфікацію", kind=ProjectHighlight.APPROVAL,
                               user=self.user)
        r = self.client.get(f"/admin/projects/project/{self.project.pk}/change/")
        self.assertContains(r, 'id="pj-cockpit"')
        self.assertContains(r, "🧭 Пульт проєкту")
        self.assertContains(r, "Клієнт погодив специфікацію")
        self.assertContains(r, 'class="pjt"')
        self.assertContains(r, "Чекає нашої відповіді")
        j = self.client.get(self.url("cockpit", self.project.pk), {"range": "3"}).json()
        self.assertTrue(j["ok"])
        self.assertIn('data-range="3"', j["html"])

    def test_highlight_add_delete_and_message_check(self):
        r = self.post(self.url("highlight", self.project.pk), {"title": "Muster bestätigt", "kind": "approval",
                                                               "message": self.ask.pk, "cockpit": True})
        self.assertTrue(r.json()["ok"])
        h = ProjectHighlight.objects.get()
        self.assertEqual((h.title, h.kind, h.message_id, h.created_by), ("Muster bestätigt", "approval", self.ask.pk,
                                                                           self.user))
        self.assertEqual(h.date, timezone.localtime(self.ask.sent_at).date())   # дата — з листа
        self.assertIn("Muster bestätigt", r.json()["html"])
        self.assertEqual(self.post(self.url("highlight", self.project.pk), {"title": " "}).status_code, 400)
        bob = User.objects.create_user("bob3", "b3@x.y", "p", is_staff=True)
        acc = EmailAccount.objects.create(user=bob, email_address="bob3@sevskiy.de", imap_username="b3",
                                          imap_password="x", smtp_username="b3", smtp_password="x")
        mine, self.account = self.account, acc
        foreign = self.mail("Bob only")
        self.account = mine
        bad = self.post(self.url("highlight", self.project.pk), {"title": "x", "message": foreign.pk})
        self.assertEqual(bad.status_code, 400)                                  # чужий лист — ні
        self.assertEqual(self.post(self.url("highlight_delete", h.pk)).status_code, 200)
        self.assertFalse(ProjectHighlight.objects.exists())
        self.assertEqual(self.client.get(self.url("highlight", self.project.pk)).status_code, 405)

    def test_viewer_without_change_right(self):
        self.project.name = "Antenne n78"               # назва проєкту видна всім; тема листа — лише власнику
        self.project.save(update_fields=["name"])
        viewer = User.objects.create_user("viewer", "v@x.y", "p", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_project"))
        viewer.profile.module_operations = {"projects": ["view"]}
        viewer.profile.save()
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url("cockpit", self.project.pk)).status_code, 200)
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertNotIn("data-pjc-madd", html)                                 # без права — без форм і кнопок
        self.assertNotIn("Anfrage 5G Antenne", html)                            # чужа скринька — без теми
        for name in ("highlight", "scan"):
            self.assertEqual(self.post(self.url(name, self.project.pk), {"title": "x"}).status_code, 404)

    def test_decide_scan_and_foreign_thread(self):
        po = self.po(product=self.antenna)
        link = ProjectLink.objects.get(purchase_order=po)
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn("Пропозиції прив'язок", html)
        r = self.post(self.url("link_decide", self.project.pk, link.pk), {"accept": True})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(ProjectLink.objects.get(pk=link.pk).status, ProjectLink.CONFIRMED)
        self.assertIn("🔎", self.post(self.url("scan", self.project.pk)).json()["html"])
        bob = User.objects.create_user("bob4", "b4@x.y", "p", is_staff=True)
        acc = EmailAccount.objects.create(user=bob, email_address="bob4@sevskiy.de", imap_username="b4",
                                          imap_password="x", smtp_username="b4", smtp_password="x")
        mine, self.account = self.account, acc
        theirs = self.mail("Bob thread")
        self.account = mine
        sg, _ = ProjectLink.objects.update_or_create(project=self.project, thread=theirs.thread, defaults={
            "source": ProjectLink.AUTO, "status": ProjectLink.SUGGESTED, "reason": "test"})
        self.assertEqual(self.post(self.url("link_decide", self.project.pk, sg.pk), {"accept": True}).status_code, 403)

    def test_mail_preview_has_moment_button(self):
        pv = self.client.get(f"/email/thread/{self.ask.thread_id}/preview/")
        self.assertContains(pv, 'data-ch="1"')
        self.assertContains(pv, "pvProjMoment")

    def test_admin_inlines_save_new_links(self):
        po = self.po()
        page = self.client.get(f"/admin/projects/project/{self.project.pk}/change/")
        self.assertContains(page, "Закупівлі проєкту (PO)")
        self.assertContains(page, "Важливі моменти")
