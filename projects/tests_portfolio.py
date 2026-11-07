"""Етап 3: портфель (таблиця, шкала, дошка фаз, карта зв'язків), ⚡ впливи між проєктами, залежність кроку від кроку
іншого проєкту, блоки «📁 Проєкти» в картках, панель праворуч, група меню.
python manage.py test projects --settings=tabele.settings_test"""
import json
import xml.etree.ElementTree as ET
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from core import side_panel as sp
from core.models import UserProfile
from inventory.models import PurchaseOrder, Supplier
from jlcpcb.models import JLCOrder
from projects import impact, overview, plan, portfolio, portfolio_draw, schedule, services, state
from projects.models import Project, ProjectPhase, ProjectState, ProjectStep, StepDependency
from projects.tests_plan import PlanBase

M, C, S = ProjectStep.MILESTONE, ProjectStep.CUSTOMER, ProjectStep.SUPPLIER
PORTFOLIO = "/admin/projects/project/portfolio/"


class PortfolioBase(PlanBase):
    def setUp(self):
        super().setUp()
        saved = dict(sp._registry)

        def restore():
            sp._registry.clear()
            sp._registry.update(saved)
        self.addCleanup(restore)
        self.other = Project.objects.create(name="Antennenmodul Drohne", customer=self.client_co, owner=self.user,
                                            due_date=self.d(20))

    def viewer(self, name="noproj", role="readonly", modules=("crm", "inventory")):
        u = User.objects.create_user(name, f"{name}@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role=role, allowed_modules=list(modules))
        return User.objects.get(pk=u.pk)

    def plan_two_phases(self, project=None, start=None):
        project = project or self.project
        start = start or self.d(-10)
        p1 = ProjectPhase.objects.create(project=project, name="Prototyp", icon="🔧", order=10)
        p2 = ProjectPhase.objects.create(project=project, name="Serie", icon="🏭", order=20)
        a = self.step("Platinen", 5, start=start, end=start + timedelta(days=4), phase=p1, project=project,
                      status=ProjectStep.DONE, done_on=start + timedelta(days=4))
        b = self.step("Muster messen", 3, after=[a], phase=p1, project=project, assignee=self.user)
        m = self.step("Muster freigegeben", 0, kind=M, side=C, after=[b], phase=p1, project=project)
        s = self.step("Serienfertigung", 10, after=[m], phase=p2, project=project, side=S)
        overview.collect(project)                    # кеш плану (schedule.persist)
        return p1, p2, a, b, m, s


class PortfolioDataTests(PortfolioBase):
    def test_row_phase_next_waiting_and_reason(self):
        self.plan_two_phases()
        self.project.due_date = self.d(5)
        self.project.save()
        overview.collect(self.project)
        r = portfolio.load([self.project], self.user, self.today)[0]
        self.assertEqual((r.phase_index, r.phase_total, r.phase_name), (1, 2, "Prototyp"))
        self.assertEqual(r.phase_text, "1/2 · 🔧 Prototyp")
        self.assertEqual((r.steps, r.done), (4, 1))
        self.assertEqual(r.next["title"], "Muster messen")
        self.assertEqual(r.waiting, "ми")
        self.assertIn(">", r.reason)                              # прогноз пізніше строку клієнту
        self.assertTrue(r.forecast > self.project.due_date)
        no_plan = portfolio.load([self.other], self.user, self.today)[0]
        self.assertFalse(no_plan.has_plan)
        self.assertEqual(no_plan.phase_text, "")

    def test_board_columns_and_stuck_phase(self):
        p1, *_ = self.plan_two_phases()
        ProjectStep.objects.filter(project=self.project, title="Muster messen").update(
            start=self.d(-8), end=self.d(-3))                     # мало закінчитись — фаза затягується
        overview.collect(self.project)
        rows = portfolio.load([self.project, self.other], self.user, self.today)
        cols = portfolio.board(rows)
        self.assertEqual([c["name"] for c in cols], ["Без плану", "Prototyp"])
        self.assertTrue(cols[1]["rows"][0].stuck)
        self.assertEqual(cols[1]["rows"][0].in_phase_days, 10)

    def test_pages_render_all_views_and_filters(self):
        self.plan_two_phases()
        for view in ("table", "timeline", "board", "map"):
            r = self.client.get(PORTFOLIO, {"view": view})
            self.assertEqual(r.status_code, 200, view)
            self.assertContains(r, self.project.code)
            self.assertContains(r, "Перехресні впливи")
        r = self.client.get(PORTFOLIO, {"h": "noplan"})
        self.assertContains(r, self.other.code)
        self.assertNotContains(r, f">{self.project.code}<")
        r = self.client.get(PORTFOLIO, {"q": "Drohne"})
        self.assertContains(r, self.other.code)
        self.assertNotContains(r, f">{self.project.code}<")
        self.assertContains(self.client.get("/admin/projects/project/"), PORTFOLIO)   # кнопка в списку

    def test_viewer_without_projects_cannot_open(self):
        self.client.force_login(self.viewer())
        self.assertIn(self.client.get(PORTFOLIO).status_code, (302, 403))

    def test_svgs_are_valid(self):
        self.plan_two_phases()
        self.plan_two_phases(project=self.other, start=self.d(-3))
        a = ProjectStep.objects.get(project=self.project, title="Muster freigegeben")
        b = ProjectStep.objects.get(project=self.other, title="Muster messen")
        StepDependency.objects.create(predecessor=a, successor=b)
        rows = portfolio.load([self.project, self.other], self.user, self.today)
        rng = portfolio.span(rows, self.today)
        ET.fromstring(portfolio_draw.mini(rows[0], rng, self.today))
        ET.fromstring(portfolio_draw.axis_head(rng, 300, 2, 298))
        svg = portfolio_draw.timeline(portfolio.grouped(rows, "customer"), rng, self.today, portfolio.cross_deps(rows))
        ET.fromstring(svg)
        self.assertIn("pf-dep", svg)
        po = self.po(product=self.antenna)
        services.link_object(self.project, purchase_order=po, user=self.user)
        jlc = JLCOrder.objects.create(jlc_order_id="J-9", order_date=self.d(-2))
        services.link_object(self.other, jlc_order=jlc, user=self.user)
        impacts = impact.compute(self.today)
        svg = portfolio_draw.relation_map(portfolio.relations(rows, impacts))
        ET.fromstring(svg)
        for part in (f'data-n="c{self.client_co.pk}"', f'data-n="p{self.project.pk}"', 'data-n="jlc"',
                     f'data-n="s{po.supplier_id}"' if po.supplier_id else 'data-n="p', 'data-k="dep"'):
            self.assertIn(part, svg)


class ImpactTests(PortfolioBase):
    def test_cross_project_dependency_delays_successor(self):
        self.plan_two_phases()
        gate = ProjectStep.objects.get(project=self.project, title="Muster freigegeben")
        tooling = self.step("Werkzeug bauen", 5, start=self.d(1), end=self.d(5), project=self.other)
        StepDependency.objects.create(predecessor=gate, successor=tooling)
        items = [i for i in impact.compute(self.today) if i.kind == "dep"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].level, "medium")                 # мітка воріт пізніше за план кроку
        self.assertIn("чекає", items[0].text)
        facts = overview.collect(self.other)
        self.assertTrue(any(a["icon"] == "⛓" and a["level"] == "medium" for a in facts["attention"]))
        mine = overview.collect(self.project)["attention"]
        self.assertTrue(any(a["icon"] == "⛓" and a["level"] == "info" for a in mine))
        c = schedule.compute(self.other, today=self.today).by_id[tooling.pk]
        self.assertGreater(c.start, self.d(1))                     # прогноз іншого проєкту зсуває крок
        ProjectStep.objects.filter(pk=tooling.pk).update(start=self.d(-1))
        row = portfolio.load([self.other], self.user, self.today)[0]
        self.assertEqual(row.waiting, f"проєкт {self.project.code}")     # мав початись — чекає інший проєкт

    def test_shared_product_shortage(self):
        from offers import services as offers
        from offers.models import Offer
        for project, qty in ((self.project, 500), (self.other, 800)):
            o = offers.new_offer(customer=self.client_co, user=self.user, project=project)
            offers.add_line(o, product=self.connector, qty=qty, unit_price=Decimal("1"))
            Offer.objects.filter(pk=o.pk).update(status=Offer.SENT)
        items = [i for i in impact.compute(self.today) if i.kind == "stock"]
        self.assertEqual(len(items), 1)
        self.assertIn("1 300", items[0].text)
        self.assertEqual(items[0].data["short"], Decimal("1300"))
        attention = overview.collect(self.project)["attention"]
        self.assertTrue(any(a["icon"] == "📦" for a in attention))
        po = self.po(status=PurchaseOrder.Status.ORDERED, expected=self.d(10), product=self.connector)
        po.lines.update(qty_ordered=1300)
        self.assertFalse([i for i in impact.compute(self.today) if i.kind == "stock"])     # очікується досить

    def test_shared_supplier_and_overdue(self):
        sup = Supplier.objects.create(name="PCB-Fab Shenzhen")
        a = self.po(supplier=sup, expected=self.d(10))
        b = self.po(supplier=sup, expected=self.d(12))
        services.link_object(self.project, purchase_order=a, user=self.user)
        services.link_object(self.other, purchase_order=b, user=self.user)
        items = [i for i in impact.compute(self.today) if i.kind == "supplier"]
        self.assertEqual([(i.title, i.level) for i in items], [("PCB-Fab Shenzhen", "info")])
        PurchaseOrder.objects.filter(pk=a.pk).update(expected_date=self.d(-1))
        items = [i for i in impact.compute(self.today, focus=self.other) if i.kind == "supplier"]
        self.assertEqual(items[0].level, "medium")
        self.assertIn("Прострочено", items[0].text)

    def test_workload_across_projects(self):
        monday = self.today - timedelta(days=self.today.weekday()) + timedelta(days=7)
        for i in range(4):
            self.step(f"A{i}", 2, start=monday, end=monday + timedelta(days=1), assignee=self.user)
            self.step(f"B{i}", 2, start=monday, end=monday + timedelta(days=1), assignee=self.user,
                      project=self.other)
        items = [i for i in impact.compute(self.today) if i.kind == "load"]
        self.assertEqual(len(items), 1)
        self.assertIn("8 кроків", items[0].text)
        self.assertEqual(items[0].level, "info")


class CrossProjectPlanTests(PortfolioBase):
    def test_step_after_step_of_other_project(self):
        gate = self.step("Muster freigegeben", 0, kind=M, side=C, start=self.d(10), end=self.d(10))
        r = self.post(self.url("plan_step", self.other.pk), {"title": "Werkzeug", "duration": 3, "after": [gate.pk]})
        self.assertTrue(r.json()["ok"])
        tool = ProjectStep.objects.get(title="Werkzeug")
        self.assertTrue(StepDependency.objects.filter(predecessor=gate, successor=tool).exists())
        html = r.json()["html"]
        self.assertIn("⛓", html)                                       # позначка на Ганті
        # коло через два проєкти — ні
        r = self.post(self.url("plan_step", self.project.pk), {"id": gate.pk, "title": gate.title, "kind": M,
                                                               "after": [tool.pk]})
        self.assertEqual(r.status_code, 400)
        self.assertIn(self.other.code, r.json()["error"])
        # закритий проєкт у «Після кроків» не потрапляє
        self.project.status = Project.DONE
        self.project.save()
        self.post(self.url("plan_step", self.other.pk), {"id": tool.pk, "title": "Werkzeug", "after": [gate.pk]})
        self.assertFalse(StepDependency.objects.filter(successor=tool).exists())

    def test_editor_lists_other_projects_steps(self):
        self.step("Fremder Schritt", 2, start=self.today, project=self.other)
        self.step("Eigener Schritt", 2, start=self.today)
        data = plan.editor_data(self.project, schedule.compute(self.project, today=self.today))
        self.assertEqual([o["label"] for o in data["others"]], [f"{self.other.code} · Fremder Schritt"])

    def test_moved_predecessor_marks_successor_project_dirty(self):
        gate = self.step("Muster freigegeben", 0, kind=M, side=C, start=self.d(3), end=self.d(3))
        tool = self.step("Werkzeug", 3, project=self.other, after=[gate])
        state.states([self.other])
        ProjectState.objects.filter(project=self.other).update(dirty=False)
        ProjectStep.objects.filter(pk=gate.pk).update(end=self.d(9), start=self.d(9))
        overview.collect(self.project)
        self.assertTrue(ProjectState.objects.get(project=self.other).dirty)
        self.assertEqual(tool.pk, StepDependency.objects.get(predecessor=gate).successor_id)


class CardsAndPanelTests(PortfolioBase):
    def test_project_blocks_in_cards(self):
        self.plan_two_phases()
        sup = Supplier.objects.get(name="Parts Ltd")
        po = self.po(supplier=sup, product=self.antenna)
        services.link_object(self.project, purchase_order=po, user=self.user)
        step = ProjectStep.objects.get(project=self.project, title="Serienfertigung")
        step.links.add(self.project.links.get(purchase_order=po))
        self.project.products.add(self.connector)
        pages = {
            f"/admin/crm/customer/{self.client_co.pk}/change/": self.project.code,
            f"/admin/inventory/supplier/{sup.pk}/change/": "Serienfertigung",
            f"/admin/inventory/purchaseorder/{po.pk}/change/": "Serienfertigung",
            f"/admin/inventory/product/{self.connector.pk}/change/": "товар проєкту",
        }
        for url, text in pages.items():
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "field-projects_block", msg_prefix=url)
            self.assertContains(r, text, msg_prefix=url)

    def test_cards_hide_block_without_projects_right(self):
        sup = Supplier.objects.get(name="Parts Ltd")
        po = self.po(supplier=sup)
        services.link_object(self.project, purchase_order=po, user=self.user)
        u = self.viewer("buyer", role="manager", modules=["inventory"])
        self.client.force_login(u)
        r = self.client.get(f"/admin/inventory/supplier/{sup.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.project.code)
        self.assertNotContains(r, "field-projects_block")

    def test_side_panel_provider(self):
        self.plan_two_phases()
        self.assertFalse(sp.get("projects.project").generic)              # своя панель, не загальна
        r = self.client.get(f"/core/panel/projects.project/{self.project.pk}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for part in ("Prototyp", "Muster messen", "1/2", "#pj-plan"):
            self.assertIn(part, html)
        lazy = self.client.get(f"/core/panel/projects.project/{self.project.pk}/block/impacts/")
        self.assertEqual(lazy.status_code, 200)
        r = self.client.post(f"/core/panel/projects.project/{self.project.pk}/edit/",
                             json.dumps({"key": "next_action", "value": "Muster an Kunden", "expect": ""}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.next_action, "Muster an Kunden")

    def test_menu_group(self):
        html = self.client.get("/admin/").content.decode()
        self.assertIn("id:'projects'", html)
        self.assertIn("'/admin/projects/':       'projects'", html)
        self.assertIn("/admin/projects/project/portfolio/?view=map", html)
