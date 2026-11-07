"""План проєкту: шаблони, прогноз дат і критичний шлях, базовий план і зсув, стан кроків з прив'язаних об'єктів,
«потребує уваги», Гант і доріжка шкали, календар, API й права, шаблони в адмінці.
python manage.py test projects --settings=tabele.settings_test"""
import json
import xml.etree.ElementTree as ET
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser, Permission, User
from django.urls import reverse
from django.utils import timezone

from inventory.models import PurchaseOrder
from projects import calendar_layer, gantt, overview, plan, schedule, services, timeline
from projects.models import Project, ProjectPhase, ProjectStep, ProjectTemplate, StepDependency
from projects.tests_cockpit import CockpitBase

M, C = ProjectStep.MILESTONE, ProjectStep.CUSTOMER


class PlanBase(CockpitBase):
    def setUp(self):
        super().setUp()
        self._order = 0

    def d(self, days):
        return self.today + timedelta(days=days)

    def step(self, title, days=1, start=None, end=None, after=(), kind=ProjectStep.TASK, side=ProjectStep.WE,
             phase=None, project=None, lag=0, **kw):
        self._order += 10
        s = ProjectStep.objects.create(project=project or self.project, title=title, duration_days=days, start=start,
                                       end=end, kind=kind, side=side, phase=phase, order=self._order, **kw)
        for p in after:
            StepDependency.objects.create(predecessor=p, successor=s, lag_days=lag)
        return s

    def url(self, name, *args):
        return reverse(f"projects:{name}", args=args)

    def post(self, url, body=None):
        return self.client.post(url, json.dumps(body or {}), content_type="application/json")


class ScheduleTests(PlanBase):
    def test_forward_pass_and_critical_path(self):
        a = self.step("A", 3, start=self.today)
        b = self.step("B", 2, after=[a])
        c = self.step("C", 1, after=[a])
        self.step("D", 0, kind=M, after=[b, c])
        p = schedule.compute(self.project, today=self.today)
        by = {x.step.title: x for x in p.steps}
        self.assertEqual((by["A"].start, by["A"].end), (self.today, self.d(2)))
        self.assertEqual((by["B"].start, by["B"].end), (self.d(3), self.d(4)))
        self.assertEqual((by["C"].start, by["C"].end), (self.d(3), self.d(3)))
        self.assertEqual((by["D"].start, by["D"].end, p.end), (self.d(5), self.d(5), self.d(5)))
        self.assertEqual([x for x in "ABCD" if by[x].critical], ["A", "B", "D"])      # у C є запас

    def test_lag_done_and_overdue_steps(self):
        a = self.step("A", 2, start=self.d(-10), status=ProjectStep.DONE, done_on=self.d(-3))
        b = self.step("B", 2, after=[a], lag=2)
        late = self.step("Late", 3, start=self.d(-8))                 # не виконано — кінець не раніше сьогодні
        p = schedule.compute(self.project, today=self.today)
        self.assertEqual((p.by_id[a.pk].start, p.by_id[a.pk].end), (self.d(-10), self.d(-3)))
        self.assertEqual(p.by_id[b.pk].start, self.today)              # −3 + 1 + 2 відступу = −0 → сьогодні
        self.assertEqual((p.by_id[late.pk].start, p.by_id[late.pk].end), (self.d(-8), self.today))
        self.assertEqual(schedule.planned_end(late), self.d(-6))

    def test_cross_project_predecessor_uses_its_forecast(self):
        other = Project.objects.create(name="Plattform")
        base = self.step("Plattform fertig", 1, project=other, start=self.today)
        ProjectStep.objects.filter(pk=base.pk).update(f_end=self.d(20))
        s = self.step("Integration", 2, after=[base])
        self.assertEqual(schedule.compute(self.project, today=self.today).by_id[s.pk].start, self.d(21))

    def test_offer_trigger_binds_and_completes_step(self):
        from offers import services as offers
        from offers.models import Offer
        s = self.step("Angebot senden", 2, start=self.today, trigger="offer_sent")
        offer = offers.new_offer(customer=self.client_co, user=self.user, project=self.project)
        c = overview.collect(self.project)["plan"].by_id[s.pk]
        self.assertEqual(c.status, ProjectStep.DOING)
        self.assertEqual(list(s.offers.all()), [offer])                # автоприв'язка
        Offer.objects.filter(pk=offer.pk).update(status=Offer.SENT, sent_at=timezone.now())
        c = overview.collect(self.project)["plan"].by_id[s.pk]
        self.assertEqual((c.status, c.end), (ProjectStep.DONE, self.today))
        self.assertIn("надіслано", c.note)
        ProjectStep.objects.filter(pk=s.pk).update(status=ProjectStep.SKIPPED)      # рішення людини — головне
        self.assertEqual(overview.collect(self.project)["plan"].by_id[s.pk].status, ProjectStep.SKIPPED)

    def test_purchase_expected_date_extends_step_and_receipt_closes_it(self):
        s = self.step("Fertigung", 2, start=self.today, trigger="po_received", side=ProjectStep.SUPPLIER)
        po = self.po(expected=self.d(10), product=self.antenna)
        services.link_object(self.project, purchase_order=po, user=self.user)
        c = overview.collect(self.project)["plan"].by_id[s.pk]
        self.assertEqual((c.status, c.end), (ProjectStep.DOING, self.d(10)))
        self.assertIn("очікується", c.note)
        self.assertEqual(s.links.get().purchase_order, po)
        PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.Status.RECEIVED, received_date=self.today)
        self.assertEqual(overview.collect(self.project)["plan"].by_id[s.pk].status, ProjectStep.DONE)
        s.refresh_from_db()
        self.assertEqual(s.eff_status, ProjectStep.DONE)               # кеш розрахунку записано

    def test_old_object_is_not_bound_to_later_step(self):
        from offers import services as offers
        offers.new_offer(customer=self.client_co, user=self.user, project=self.project)
        s = self.step("Angebot Serie", 2, start=self.d(60), trigger="offer_sent")
        overview.collect(self.project)
        self.assertFalse(s.offers.exists())                             # Angebot на розробку — не для серії

    def test_baseline_slip_and_attention(self):
        a = self.step("Simulation", 3, start=self.today)
        m = self.step("Muster freigegeben", 0, kind=M, side=C, after=[a])
        last = self.step("Serie geliefert", 0, kind=M, side=C, after=[m])
        plan.baseline(self.project, self.user)
        self.project.refresh_from_db()
        m.refresh_from_db()
        self.assertIsNotNone(self.project.baseline_at)
        self.assertEqual(m.baseline_end, self.d(3))
        ProjectStep.objects.filter(pk=a.pk).update(end=self.d(5))      # 6 днів замість 3
        facts = overview.collect(self.project)
        self.assertEqual((facts["plan"].by_id[m.pk].slip, facts["plan"].by_id[last.pk].slip), (3, 3))
        texts = [x["text"] for x in facts["attention"]]
        self.assertEqual(sum("зсунулась" in t for t in texts), 1, texts)  # лише найближча віха, без повторів
        self.assertTrue(any("на 3 дн. пізніше базового плану" in t for t in texts), texts)   # строку клієнту немає
        self.project.due_date = self.d(4)
        self.project.save()
        texts = [x["text"] for x in overview.collect(self.project)["attention"]]
        self.assertTrue(any("пізніше строку клієнту" in t for t in texts), texts)
        self.assertFalse(any("пізніше базового плану" in t for t in texts), texts)

    def test_overdue_steps_in_attention_and_briefing(self):
        gerber = self.step("Gerber senden", 2, start=self.d(-6), end=self.d(-5))
        self.step("Bestückung", 2, start=self.d(-4), end=self.d(-3), after=[gerber])   # чекає Gerber — не він винен
        self.step("Test beim Kunden", 3, side=C, start=self.d(-4), end=self.d(-2))
        facts = overview.collect(self.project)
        todo = [x["text"] for x in facts["attention"] if x["kind"] == "todo"]
        risks = [x["text"] for x in facts["attention"] if x["kind"] == "risk"]
        self.assertTrue(any("«Gerber senden» прострочено" in t for t in todo), todo)
        self.assertFalse(any("Bestückung" in t for t in todo), todo)
        self.assertTrue(any("Чекаємо клієнта: «Test beim Kunden»" in t for t in risks), risks)
        self.assertIn("Gerber senden", overview.digest_text(self.user))   # ранковий брифінг відповідального
        soon = self.step("Muster senden", 1, start=self.d(2), end=self.d(2), after=[gerber])
        self.assertFalse(any("Muster senden" in x["text"] for x in overview.collect(self.project)["attention"]))
        ProjectStep.objects.filter(pk=soon.pk).update(start=None, end=None)
        ProjectStep.objects.filter(pk=gerber.pk).update(status=ProjectStep.DONE, done_on=self.d(-1))
        todo = [x["text"] for x in overview.collect(self.project)["attention"] if x["kind"] == "todo"]
        self.assertTrue(any("«Bestückung» прострочено" in t for t in todo), todo)   # тепер можна робити — і запізнюємось


class TemplateTests(PlanBase):
    def test_apply_builtin_template(self):
        tpl = next(t for t in plan.templates() if t.name == "Перепродаж зі складу")
        self.assertEqual(plan.apply_template(self.project, tpl, self.today, self.user), 4)
        steps = list(ProjectStep.objects.filter(project=self.project).order_by("order"))
        self.assertEqual([s.start for s in steps], [self.today, self.d(1), self.d(2), self.d(4)])
        self.assertEqual([s.kind for s in steps], [ProjectStep.TASK, M, ProjectStep.TASK, M])
        self.assertEqual(StepDependency.objects.filter(successor__project=self.project).count(), 3)
        self.assertEqual(steps[1].trigger, "order_received")
        with self.assertRaises(plan.PlanError):
            plan.apply_template(self.project, tpl, self.today)        # план уже є

    def test_every_builtin_template_applies(self):
        for i, tpl in enumerate(plan.templates()):
            p = Project.objects.create(name=f"T{i}")
            n = plan.apply_template(p, tpl, self.today)
            self.assertEqual(n, plan.template_counts(tpl)[1])
            self.assertFalse(ProjectStep.objects.filter(project=p, start__isnull=True).exists(), tpl.name)

    def test_save_as_template_round_trip(self):
        p1 = ProjectPhase.objects.create(project=self.project, name="Eins", order=10)
        p2 = ProjectPhase.objects.create(project=self.project, name="Zwei", icon="🚚", gate="geliefert", order=20)
        a = self.step("A", 3, start=self.today, phase=p1)
        b = self.step("B", 2, after=[a], phase=p1)
        self.step("C", 0, kind=M, side=C, after=[a, b], phase=p2, trigger="order_delivered")
        tpl = plan.save_as_template(self.project, "Kopie", self.user)
        self.assertEqual(tpl.plan[1]["steps"][0]["after"], [0, 1])
        self.assertEqual(tpl.plan[1]["gate"], "geliefert")
        with self.assertRaises(plan.PlanError):
            plan.save_as_template(self.project, "Kopie")
        other = Project.objects.create(name="Neu")
        self.assertEqual(plan.apply_template(other, tpl, self.today), 3)
        self.assertEqual(StepDependency.objects.filter(successor__project=other).count(), 3)
        c2 = ProjectStep.objects.get(project=other, title="C")
        self.assertEqual((c2.start, c2.side, c2.trigger, c2.phase.name), (self.d(5), C, "order_delivered", "Zwei"))


class PlanViewTests(PlanBase):
    def test_template_step_phase_and_plan_actions(self):
        pk = self.project.pk
        tpl = next(t for t in plan.templates() if t.name == "Перепродаж зі складу")
        r = self.post(self.url("plan_template", pk), {"template": tpl.pk, "start": self.today.isoformat()})
        html = r.json()["html"]
        for part in ('class="pjg"', "pjc-strip", 'id="pjp-data"', 'tabindex="0"', "data-pjc-step-new"):
            self.assertIn(part, html)
        self.assertEqual(self.post(self.url("plan_template", pk), {"template": tpl.pk}).status_code, 400)
        steps = list(ProjectStep.objects.filter(project=self.project).order_by("order"))

        r = self.post(self.url("plan_step", pk), {"title": "Rechnung", "duration": 2, "after": [steps[-1].pk],
                                                  "side": "we", "status": "todo"})
        self.assertTrue(r.json()["ok"])
        new = ProjectStep.objects.get(title="Rechnung")
        self.assertTrue(StepDependency.objects.filter(predecessor=steps[-1], successor=new).exists())
        r = self.post(self.url("plan_step", pk), {"id": steps[0].pk, "title": steps[0].title, "after": [new.pk]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("коло", r.json()["error"])
        self.assertEqual(self.post(self.url("plan_step", pk), {"title": " "}).status_code, 400)

        self.post(self.url("plan_step_delete", pk, steps[1].pk))       # ланцюжок не рветься
        self.assertFalse(ProjectStep.objects.filter(pk=steps[1].pk).exists())
        self.assertTrue(StepDependency.objects.filter(predecessor=steps[0], successor=steps[2]).exists())

        r = self.post(self.url("plan_phase", pk), {"name": "Nacharbeit", "icon": "🔁", "gate": "OK"})
        self.assertTrue(r.json()["ok"])
        ph = ProjectPhase.objects.get(name="Nacharbeit")
        ProjectStep.objects.filter(pk=new.pk).update(phase=ph)
        self.post(self.url("plan_phase_delete", pk, ph.pk))
        self.assertIsNone(ProjectStep.objects.get(pk=new.pk).phase)

        self.assertTrue(self.post(self.url("plan_baseline", pk)).json()["ok"])
        self.assertFalse(ProjectStep.objects.filter(project=self.project, baseline_end__isnull=True).exists())
        self.assertTrue(self.post(self.url("plan_save_template", pk), {"name": "Mein Ablauf"}).json()["ok"])
        self.assertEqual(plan.template_counts(ProjectTemplate.objects.get(name="Mein Ablauf"))[1], 4)
        self.assertEqual(self.post(self.url("plan_save_template", pk), {"name": "Mein Ablauf"}).status_code, 400)

        self.assertTrue(self.post(self.url("plan_clear", pk)).json()["ok"])
        self.project.refresh_from_db()
        self.assertFalse(ProjectStep.objects.filter(project=self.project).exists())
        self.assertIsNone(self.project.baseline_at)
        self.assertEqual(self.client.get(self.url("plan_clear", pk)).status_code, 405)

    def test_step_links_only_confirmed_objects_of_project(self):
        po = self.po(product=self.antenna)                              # пропозиція, ще не підтверджена
        link = self.project.links.get(purchase_order=po)
        s = self.step("Teile", 2, start=self.today)
        self.post(self.url("plan_step", self.project.pk), {"id": s.pk, "title": "Teile", "links": [link.pk]})
        self.assertFalse(s.links.exists())
        services.link_object(self.project, purchase_order=po, user=self.user)
        self.post(self.url("plan_step", self.project.pk), {"id": s.pk, "title": "Teile", "links": [link.pk],
                                                           "trigger": "po_received"})
        self.assertEqual(list(s.links.all()), [link])

    def test_viewer_sees_plan_but_cannot_change(self):
        tpl = plan.templates()[0]
        plan.apply_template(self.project, tpl, self.today, self.user)
        viewer = User.objects.create_user("viewer", "v@x.y", "p", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_project"))
        viewer.profile.module_operations = {"projects": ["view"]}
        viewer.profile.save()
        self.client.force_login(viewer)
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn('class="pjg"', html)
        for part in ("data-pjc-step-new", 'id="pjp-data"', 'tabindex="0"', "data-pjc-tpl-apply"):
            self.assertNotIn(part, html)
        for name in ("plan_template", "plan_step", "plan_phase", "plan_baseline", "plan_save_template", "plan_clear"):
            self.assertEqual(self.post(self.url(name, self.project.pk), {"title": "x", "name": "x"}).status_code, 404)
        self.assertEqual(ProjectStep.objects.filter(project=self.project, title="x").count(), 0)


class DrawingTests(PlanBase):
    def test_gantt_and_timeline_lane_are_valid_svg(self):
        phase = ProjectPhase.objects.create(project=self.project, name="Prototyp <1>", icon="🔧", order=10,
                                            gate="Muster <OK>")
        a = self.step("Platinen & Bestückung", 5, start=self.d(-2), phase=phase, assignee=self.user)
        m = self.step("Muster OK", 0, kind=M, side=C, after=[a], phase=phase)
        self.step("Ohne Phase", 2, after=[m])
        plan.baseline(self.project)
        self.project.due_date = self.d(30)
        self.project.save()
        facts = overview.collect(self.project)
        svg = gantt.render(self.project, facts["plan"], self.today, editable=True)
        ET.fromstring(svg)
        for part in (f'data-step="{a.pk}"', f'data-phase="{phase.pk}"', "pjg-crit", "pjg-dep", "pjg-due",
                     "Platinen &amp; Bestückung", "Prototyp &lt;1&gt;", "Без фази"):
            self.assertIn(part, svg)
        self.assertNotIn("tabindex", gantt.render(self.project, facts["plan"], self.today))
        tl = timeline.render(self.project, facts, self.user)
        ET.fromstring(tl)
        self.assertIn("📋 Фази й віхи", tl)
        self.assertIn("◆ Muster OK", tl)
        self.assertIn("Muster &lt;OK&gt;", tl)


class CalendarLayerTests(PlanBase):
    def test_own_open_steps_in_calendar(self):
        bob = User.objects.create_user("bob9", "b9@x.y", "p", is_staff=True)
        self.step("Mein Schritt", 1, start=self.today, end=self.today, assignee=self.user)
        self.step("Bobs Schritt", 1, start=self.today, end=self.today, assignee=bob)
        self.step("Erledigt", 1, start=self.today, end=self.today, assignee=self.user, status=ProjectStep.DONE)
        self.step("Freigabe", 0, kind=M, side=C, start=self.today, end=self.today)       # віха проєкту власника
        self.step("Kunde testet", 3, side=C, start=self.today, end=self.today)             # не наш крок
        days = calendar_layer.month_steps(self.user, self.today.year, self.today.month)
        self.assertEqual(sorted(x["title"] for x in days[self.today.day]), ["Freigabe", "Mein Schritt"])
        self.assertEqual(calendar_layer.month_steps(AnonymousUser(), self.today.year, self.today.month), {})
        r = self.client.get("/calendar/", {"year": self.today.year, "month": self.today.month})
        self.assertContains(r, "Mein Schritt")
        self.assertContains(r, f"/admin/projects/project/{self.project.pk}/change/#pj-plan")
        self.assertNotContains(r, "Bobs Schritt")
        self.project.status = Project.DONE
        self.project.save()
        self.assertEqual(calendar_layer.month_steps(self.user, self.today.year, self.today.month), {})


class TemplateAdminTests(PlanBase):
    def test_builtin_templates_listed_hidden_not_deleted(self):
        self.assertEqual(self.client.get("/admin/projects/projecttemplate/").status_code, 200)
        self.assertEqual(ProjectTemplate.objects.filter(builtin=True).count(), len(plan.BUILTIN))
        tpl = ProjectTemplate.objects.get(name=plan.BUILTIN[0]["name"])
        r = self.client.get(f"/admin/projects/projecttemplate/{tpl.pk}/change/")
        self.assertContains(r, "умова переходу")
        self.assertEqual(self.client.post(f"/admin/projects/projecttemplate/{tpl.pk}/delete/",
                                          {"post": "yes"}).status_code, 403)
        ProjectTemplate.objects.filter(pk=tpl.pk).update(is_active=False)
        self.assertNotIn(tpl, plan.templates())
        self.assertFalse(ProjectTemplate.objects.get(pk=tpl.pk).is_active)     # прихований не повертається

    def test_form_validates_plan_json(self):
        from projects.admin import ProjectTemplateForm

        def form(value):
            return ProjectTemplateForm(data={"name": "X", "description": "", "is_active": "on", "order": 0,
                                             "plan": json.dumps(value)})
        self.assertFalse(form([{"steps": []}]).is_valid())
        self.assertFalse(form([{"name": "A", "steps": [{"title": "t", "trigger": "nope"}]}]).is_valid())
        self.assertFalse(form([{"name": "A", "steps": [{"title": "t", "side": "boss"}]}]).is_valid())
        self.assertFalse(form([{"name": "A", "steps": []}]).is_valid())
        self.assertTrue(form([{"name": "A", "steps": [{"title": "t", "days": 2, "trigger": "po_received"}]}]).is_valid())
