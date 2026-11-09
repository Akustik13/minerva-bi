"""📋 Класична діаграма Ганта: дані (номери 1 / 1.1, прогноз, базовий план, попередники, критичний шлях) і зміна
дат перетягуванням (plan.move_step, …/plan/step/<id>/dates/).
python manage.py test projects --settings=tabele.settings_test"""
from django.utils import timezone

from projects import gantt, overview, plan, schedule
from projects.models import ProjectStep, StepDependency
from projects.tests_portfolio import PortfolioBase


class ClassicGanttTests(PortfolioBase):
    def test_rows_numbers_dates_and_predecessors(self):
        p1, p2, a, b, m, s = self.plan_two_phases()
        tool = self.step("Werkzeug", 5, project=self.other, start=self.d(1))
        StepDependency.objects.create(predecessor=tool, successor=s, lag_days=2)
        data = gantt.classic_data(self.project, schedule.compute(self.project), self.today)
        rows = data["rows"]
        self.assertEqual([(r["type"], r["n"]) for r in rows],
                         [("phase", "1"), ("step", "1.1"), ("step", "1.2"), ("step", "1.3"), ("phase", "2"),
                          ("step", "2.1")])
        by = {r["title"]: r for r in rows if r["type"] == "step"}
        self.assertEqual(by["Muster messen"]["preds"], [{"id": a.pk, "n": "1.1", "lag": 0}])
        self.assertIn({"id": "", "n": self.other.code, "lag": 2, "ext": True}, by["Serienfertigung"]["preds"])
        self.assertEqual(by["Muster freigegeben"]["kind"], "milestone")
        self.assertEqual(by["Platinen"]["status"], ProjectStep.DONE)
        self.assertTrue(data["start"] <= by["Platinen"]["start"])
        self.assertEqual(data["today"], self.today.isoformat())

    def test_move_step_sets_planned_dates_and_duration(self):
        *_rest, b, m, s = self.plan_two_phases()
        step = plan.move_step(self.project, b.pk, self.d(3).isoformat(), self.d(7).isoformat())
        self.assertEqual((step.start, step.end, step.duration_days), (self.d(3), self.d(7), 5))
        ms = plan.move_step(self.project, m.pk, None, self.d(12).isoformat())
        self.assertEqual((ms.start, ms.end), (None, self.d(12)))
        with self.assertRaises(plan.PlanError):
            plan.move_step(self.project, b.pk, self.d(7).isoformat(), self.d(3).isoformat())
        c = schedule.compute(self.project).by_id[b.pk]
        self.assertEqual((c.start, c.end), (self.d(3), self.d(7)))

    def test_dates_view_and_cockpit_switch(self):
        *_rest, b, m, s = self.plan_two_phases()
        r = self.post(self.url("plan_step_dates", self.project.pk, b.pk),
                      {"start": self.d(2).isoformat(), "end": self.d(4).isoformat()})
        html = r.json()["html"]
        self.assertIn("📋 «Muster messen»:", html)
        self.assertIn('id="pjg-data"', html)
        self.assertIn("📋 Класична діаграма Ганта", html)
        self.assertEqual(self.post(self.url("plan_step_dates", self.project.pk, 999999), {"end": "x"}).status_code, 400)
        viewer = self.viewer("seer2", role="readonly", modules=("projects",))
        self.client.force_login(viewer)
        self.assertEqual(self.post(self.url("plan_step_dates", self.project.pk, b.pk),
                                   {"start": self.d(2).isoformat(), "end": self.d(4).isoformat()}).status_code, 404)
        overview.collect(self.project)


class MailPlanActionsTests(PortfolioBase):
    def test_thread_state_has_phase_health_and_open_steps(self):
        from projects.views import thread_state
        *_rest, b, m, s = self.plan_two_phases()
        c = thread_state(self.ask.thread)["confirmed"][0]
        self.assertEqual(c["brief"]["phase"], "1/2 · 🔧 Prototyp")
        self.assertEqual([x["id"] for x in c["brief"]["open"]][:3], [b.pk, m.pk, s.pk])
        self.assertEqual(c["brief"]["next"]["title"], "Muster messen")
        self.assertEqual(thread_state(self.ask.thread, self.user)["confirmed"][0]["brief"]["phase"], "1/2 · 🔧 Prototyp")
        clerk = self.viewer("clerk", role="manager", modules=["email_assistant"])          # пошта є, проєктів — ні
        self.assertEqual(thread_state(self.ask.thread, clerk)["confirmed"][0]["brief"], {})

    def test_quick_step_and_close_step_from_a_letter(self):
        *_rest, b, m, s = self.plan_two_phases()
        r = self.post(self.url("plan_quick_step", self.project.pk), {"title": "Muster nach Kundenwunsch ändern",
                                                                    "message": self.ask.pk})
        self.assertIn("➕ Крок додано: Muster nach Kundenwunsch ändern", r.json()["html"])
        new = ProjectStep.objects.get(title="Muster nach Kundenwunsch ändern")
        self.assertEqual((new.phase, new.side, new.start), (b.phase, ProjectStep.WE, self.today))
        self.assertTrue(new.notes.startswith("✉️ з листа від "))
        self.assertNotIn("Anfrage", new.notes)                                     # тема листа — ні
        r = self.post(self.url("plan_step_close", self.project.pk, b.pk), {"message": self.ask.pk})
        b.refresh_from_db()
        self.assertEqual((b.status, b.done_on), (ProjectStep.DONE, timezone.localtime(self.ask.sent_at).date()))
        self.assertIn("✓ виконано за листом від", b.notes)
        self.assertEqual(self.post(self.url("plan_quick_step", self.project.pk), {"title": " "}).status_code, 400)


class OrderOfferBlockTests(PortfolioBase):
    def test_order_and_offer_cards_show_project_and_step(self):
        from decimal import Decimal

        from offers import services as offers
        from projects import services
        from sales.models import SalesOrder
        *_rest, b, m, s = self.plan_two_phases()
        order = SalesOrder.objects.create(source="digikey", order_number="880011", affects_stock=False,
                                          email="buyer@client.example")
        link = services.link_order(self.project, order, user=self.user)
        s.links.add(link)
        r = self.client.get(f"/admin/sales/salesorder/{order.pk}/change/")
        self.assertContains(r, self.project.code)
        self.assertContains(r, "крок «Serienfertigung»")
        offer = offers.new_offer(customer=self.client_co, user=self.user, project=self.project)
        offers.add_line(offer, product=self.antenna, qty=10, unit_price=Decimal("2"))
        m.offers.add(offer)
        r = self.client.get(f"/admin/offers/offer/{offer.pk}/change/")
        self.assertContains(r, self.project.code)
        self.assertContains(r, "крок «Muster freigegeben»")
        viewer = self.viewer("noproj2", role="manager", modules=["sales"])
        self.client.force_login(viewer)
        r = self.client.get(f"/admin/sales/salesorder/{order.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.project.code)


class BriefingWeekTests(PortfolioBase):
    def test_digest_has_week_steps_and_new_risks(self):
        from projects import overview, risks
        *_rest, b, m, s = self.plan_two_phases()
        overview.collect(self.project)
        risks.save(self.project, {"title": "Zoll Schweiz", "probability": 3, "impact": 3}, user=self.user)
        text = overview.digest_text(self.user)
        self.assertIn("📋 Кроки тижня:", text)
        self.assertIn(f"• {self.project.code} · Muster messen — до ", text)
        self.assertIn("⚠️ Нові ризики за добу:", text)
        self.assertIn(f"• {self.project.code} · Zoll Schweiz — бал 9", text)
        self.assertNotIn("Platinen", text.split("📋 Кроки тижня:")[1])           # виконаний крок — ні
