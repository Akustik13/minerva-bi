"""Етап 4: правила ризиків (відкривають і закривають самі, рішення людини головне), ризики людей і з листа, матриця й
реєстр, тижневий статус з AI-чернеткою, історія кольорів, щотижневий звіт відповідальним, налаштування.
python manage.py test projects --settings=tabele.settings_test"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.utils import timezone

from inventory.models import PurchaseOrder
from projects import health, overview, portfolio, risk_rules, risks, services, weekly
from projects.models import (Project, ProjectRisk, ProjectSettings, ProjectStep, ProjectUpdate, StepDependency)
from projects.tests_portfolio import PortfolioBase

M, C, S = ProjectStep.MILESTONE, ProjectStep.CUSTOMER, ProjectStep.SUPPLIER


def rule(project, key):
    return ProjectRisk.objects.filter(project=project, rule_key=key).first()


class RiskRuleTests(PortfolioBase):
    def test_due_rule_opens_closes_and_reopens(self):
        self.plan_two_phases()
        self.project.due_date = self.d(3)
        self.project.save()
        overview.collect(self.project)
        r = rule(self.project, "due")
        self.assertEqual((r.status, r.source, r.score, r.owner), (ProjectRisk.OPEN, ProjectRisk.RULE, 9, self.user))
        self.assertIn("строку клієнту", r.title)
        self.assertTrue(r.steps.exists())                         # кроки критичного шляху
        self.project.due_date = self.d(90)
        self.project.save()
        overview.collect(self.project)
        r.refresh_from_db()
        self.assertEqual((r.status, r.closed_auto), (ProjectRisk.CLOSED, True))     # умова зникла
        self.project.due_date = self.d(3)
        self.project.save()
        overview.collect(self.project)
        r.refresh_from_db()
        self.assertEqual((r.status, r.closed_auto, r.closed_at), (ProjectRisk.OPEN, False, None))

    def test_person_decides_closed_and_edited(self):
        self.plan_two_phases()
        self.project.due_date = self.d(3)
        self.project.save()
        overview.collect(self.project)
        r = rule(self.project, "due")
        risks.save(self.project, {"id": r.pk, "probability": 2, "impact": 3, "mitigation": "Express-Versand"})
        overview.collect(self.project)
        r.refresh_from_db()
        self.assertEqual((r.probability, r.edited, r.mitigation), (2, True, "Express-Versand"))   # правило не чіпає
        risks.save(self.project, {"id": r.pk, "status": ProjectRisk.CLOSED, "title": "інша назва"})
        r.refresh_from_db()
        self.assertNotEqual(r.title, "інша назва")              # назву ризику правила пише правило
        overview.collect(self.project)
        r.refresh_from_db()
        self.assertEqual((r.status, r.closed_auto), (ProjectRisk.CLOSED, False))    # закрила людина — не відкривається

    def test_slip_wait_and_purchase_rules(self):
        *_rest, m, s = self.plan_two_phases()
        ProjectStep.objects.filter(project=self.project).update(baseline_start=self.d(-20), baseline_end=self.d(-15))
        cust = self.step("Kunde testet", 3, side=C, start=self.d(-12), end=self.d(-9))
        po = self.po(expected=self.d(-4), product=self.antenna)
        services.link_object(self.project, purchase_order=po, user=self.user)
        s.links.add(self.project.links.get(purchase_order=po))
        overview.collect(self.project)
        keys = set(ProjectRisk.objects.filter(project=self.project, status=ProjectRisk.OPEN)
                   .values_list("rule_key", flat=True))
        self.assertIn("slip", keys)
        self.assertEqual(list(rule(self.project, "slip").steps.all()), [m])
        self.assertIn(f"wait:{cust.pk}", keys)
        self.assertIn(f"po:{po.pk}", keys)
        self.assertEqual(list(rule(self.project, f"po:{po.pk}").steps.all()), [s])
        self.assertIn("клієнта", rule(self.project, f"wait:{cust.pk}").title)

    def test_slip_rule_is_one_risk_for_the_whole_chain(self):
        *_rest, m, s = self.plan_two_phases()
        m2 = self.step("Serie geliefert", 0, kind=M, after=[s])
        ProjectStep.objects.filter(project=self.project).update(baseline_start=self.d(-20), baseline_end=self.d(-15))
        ProjectStep.objects.filter(pk=m2.pk).update(baseline_start=self.d(-14), baseline_end=self.d(-14))
        overview.collect(self.project)
        slips = ProjectRisk.objects.filter(project=self.project, rule_key__startswith="slip")
        self.assertEqual(slips.count(), 1)                        # одна причина — один ризик
        r = slips.get()
        self.assertTrue(r.title.startswith("«Muster freigegeben»"))   # перша зсунута віха
        self.assertIn("зсунулось віх: 2", r.title)
        self.assertIn("«Serie geliefert»", r.evidence)
        self.assertEqual(set(r.steps.values_list("pk", flat=True)), {m.pk, m2.pk})

    def test_dependency_and_shared_stock_rules(self):
        from decimal import Decimal
        from offers import services as offers
        from offers.models import Offer
        self.plan_two_phases()
        gate = ProjectStep.objects.get(project=self.project, title="Muster freigegeben")
        tool = self.step("Werkzeug", 5, start=self.d(1), end=self.d(5), project=self.other)
        StepDependency.objects.create(predecessor=gate, successor=tool)
        for project, qty in ((self.project, 500), (self.other, 800)):
            o = offers.new_offer(customer=self.client_co, user=self.user, project=project)
            offers.add_line(o, product=self.connector, qty=qty, unit_price=Decimal("1"))
            Offer.objects.filter(pk=o.pk).update(status=Offer.SENT)
        overview.collect(self.other)
        self.assertTrue(rule(self.other, f"dep:{gate.pk}:{tool.pk}"))
        self.assertTrue(rule(self.other, f"stock:{self.connector.pk}"))
        self.assertIn(self.project.code, rule(self.other, f"stock:{self.connector.pk}").title)

    def test_attention_from_people_risks_and_review(self):
        risks.save(self.project, {"title": "Kunde vergleicht mit Konkurrenz", "probability": 3, "impact": 2,
                                  "category": ProjectRisk.CUSTOMER}, user=self.user)
        risks.save(self.project, {"title": "Zoll CH", "probability": 1, "impact": 2,
                                  "review_on": self.d(-1).isoformat()}, user=self.user)
        items = overview.collect(self.project)["attention"]
        self.assertTrue(any(a["level"] == "medium" and "Kunde vergleicht" in a["text"] for a in items))
        self.assertTrue(any(a["kind"] == "todo" and "Переглянути ризик «Zoll CH»" in a["text"] for a in items))

    def test_matrix_and_counts(self):
        a = risks.save(self.project, {"title": "A", "probability": 3, "impact": 3}, user=self.user)
        b = risks.save(self.project, {"title": "B", "probability": 1, "impact": 2}, user=self.user)
        risks.save(self.project, {"title": "C", "probability": 2, "impact": 2, "status": ProjectRisk.CLOSED},
                   user=self.user)
        self.assertEqual(risk_rules.counts([self.project.pk])[self.project.pk], {"red": 1, "yellow": 0, "green": 1})
        m = risk_rules.matrix([a, b])
        self.assertEqual([row["p"] for row in m], [3, 2, 1])
        self.assertEqual(m[0]["cells"][2]["items"][0]["risk"], a)
        self.assertEqual(m[0]["cells"][2]["level"], "red")
        self.assertEqual(m[2]["cells"][1]["items"][0]["n"], 2)


class RiskViewTests(PortfolioBase):
    def url(self, name, *args):
        from django.urls import reverse
        return reverse(f"projects:{name}", args=args)

    def test_create_edit_delete_from_cockpit(self):
        r = self.post(self.url("risk", self.project.pk), {"title": "Muster zu spät", "probability": 3, "impact": 3,
                                                          "mitigation": "DHL Express", "owner": self.user.pk})
        self.assertTrue(r.json()["ok"])
        html = r.json()["html"]
        for part in ("Muster zu spät", "pjr-red", 'id="pjr-data"', "data-pjc-risk-new", "data-pjc-tab=\"risks\""):
            self.assertIn(part, html)
        risk = ProjectRisk.objects.get(title="Muster zu spät")
        self.assertEqual((risk.source, risk.created_by), (ProjectRisk.MANUAL, self.user))
        self.assertEqual(self.post(self.url("risk", self.project.pk), {"title": " "}).status_code, 400)
        self.assertTrue(self.post(self.url("risk_delete", self.project.pk, risk.pk)).json()["ok"])
        self.assertFalse(ProjectRisk.objects.filter(pk=risk.pk).exists())

    def test_rule_risk_cannot_be_deleted(self):
        self.plan_two_phases()
        self.project.due_date = self.d(3)
        self.project.save()
        overview.collect(self.project)
        r = rule(self.project, "due")
        resp = self.post(self.url("risk_delete", self.project.pk, r.pk))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("закрийте", resp.json()["error"])
        self.assertEqual(self.client.get(f"/admin/projects/projectrisk/{r.pk}/delete/").status_code, 403)

    def test_risk_from_own_mail_only(self):
        r = self.post(self.url("risk", self.project.pk), {"title": "Muster MHF4", "message": self.ask.pk,
                                                          "quote": "Muster bis KW 48"})
        self.assertTrue(r.json()["ok"])
        risk = ProjectRisk.objects.get(title="Muster MHF4")
        self.assertEqual((risk.message, risk.evidence), (self.ask, "Muster bis KW 48"))
        bob = User.objects.create_superuser("bob5", "b5@x.y", "p")
        self.client.force_login(bob)
        r = self.post(self.url("risk", self.project.pk), {"title": "fremd", "message": self.ask.pk})
        self.assertEqual(r.status_code, 400)

    def test_viewer_sees_but_cannot_change(self):
        risks.save(self.project, {"title": "Sichtbar", "probability": 2, "impact": 2}, user=self.user)
        from django.contrib.auth.models import Permission
        from core.models import UserProfile
        v = User.objects.create_user("v4", "v4@x.y", "p", is_staff=True)
        v.user_permissions.add(Permission.objects.get(codename="view_project"))
        v.profile.module_operations = {"projects": ["view"]}
        v.profile.save()
        UserProfile.objects.filter(user=v).update(role="readonly", allowed_modules=["projects"])
        self.client.force_login(User.objects.get(pk=v.pk))
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn("Sichtbar", html)
        self.assertNotIn('id="pjr-data"', html)
        self.assertNotIn("data-pjs-save", html)
        for name in ("risk", "status"):
            self.assertEqual(self.post(self.url(name, self.project.pk), {"title": "x", "health": "on_track",
                                                                        "text": "x"}).status_code, 404)
        self.assertEqual(self.post(self.url("status_draft", self.project.pk)).status_code, 404)

    def test_registry_page_with_matrix_and_filters(self):
        a = risks.save(self.project, {"title": "Offen rot", "probability": 3, "impact": 3}, user=self.user)
        risks.save(self.project, {"title": "Geschlossen", "probability": 1, "impact": 1,
                                  "status": ProjectRisk.CLOSED}, user=self.user)
        r = self.client.get("/admin/projects/projectrisk/")
        self.assertContains(r, "Offen rot")
        self.assertNotContains(r, "Geschlossen")                 # за замовчуванням — відкриті й «стежимо»
        self.assertContains(r, f"#{a.pk}")
        self.assertContains(r, "pjrm-red")
        self.assertContains(self.client.get("/admin/projects/projectrisk/", {"state": "closed"}), "Geschlossen")
        self.assertContains(self.client.get("/admin/projects/projectrisk/", {"level": "red"}), "Offen rot")
        r = self.client.post("/admin/projects/projectrisk/", {"action": "close_selected", "_selected_action": [a.pk],
                                                              "index": 0, "select_across": 0})
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db()
        self.assertEqual((a.status, a.closed_auto), (ProjectRisk.CLOSED, False))


class StatusTests(PortfolioBase):
    def url(self, name, *args):
        from django.urls import reverse
        return reverse(f"projects:{name}", args=args)

    def test_save_status_with_snapshot_and_history(self):
        self.plan_two_phases()
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn("data-pjs-save", html)
        self.assertIn("Де ми:", html)                            # чернетка без AI з даних тижня
        r = self.post(self.url("status", self.project.pk), {"health": "at_risk", "text": "Muster verspätet"})
        self.assertTrue(r.json()["ok"])
        self.assertIn("📝 🟡 Під загрозою", r.json()["html"])
        u = ProjectUpdate.objects.get(project=self.project)
        self.assertEqual((u.source, u.author, u.snapshot["phase"]), (ProjectUpdate.MANUAL, self.user, "1/2 · 🔧 Prototyp"))
        self.assertEqual(self.post(self.url("status", self.project.pk), {"health": "on_track", "text": " "}).status_code,
                         400)
        self.assertEqual(self.post(self.url("status", self.project.pk), {"health": "pink", "text": "x"}).status_code, 400)
        self.assertEqual(health.latest(self.project), u)
        row = portfolio.load([self.project], self.user, self.today)[0]
        self.assertEqual(row.status, u)

    def test_suggest_and_stale_reminder(self):
        self.assertEqual(health.suggest("bad"), ProjectUpdate.OFF_TRACK)
        self.assertEqual(health.suggest("warn"), ProjectUpdate.AT_RISK)
        self.assertEqual(health.suggest(""), ProjectUpdate.ON_TRACK)
        self.plan_two_phases()
        texts = [a["text"] for a in overview.collect(self.project)["attention"]]
        self.assertTrue(any("Тижневого статусу ще немає" in t for t in texts))
        u = health.save(self.project, self.user, ProjectUpdate.ON_TRACK, "Alles gut")
        ProjectUpdate.objects.filter(pk=u.pk).update(created_at=timezone.now() - timedelta(days=9))
        texts = [a["text"] for a in overview.collect(self.project)["attention"]]
        self.assertTrue(any("не оновлювали 9 дн." in t for t in texts))

    def test_ai_draft_uses_budget_and_facts(self):
        self.plan_two_phases()
        calls = []

        def fake(prompt, max_tokens=0, user=None, model=""):
            calls.append((prompt, model))
            return "**Prototyp** läuft, Muster messen bis Freitag."
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.post(self.url("status_draft", self.project.pk))
        self.assertEqual(r.json(), {"ok": True, "text": "Prototyp läuft, Muster messen bis Freitag.", "error": ""})
        prompt, model = calls[0]
        self.assertEqual(model, health.AI_MODEL)
        self.assertIn("<<<ДАНІ", prompt)
        self.assertIn("Muster messen", prompt)
        with patch("ai_assistant.budget_guard.check_budget", return_value=(False, "monthly_budget_exceeded")):
            r = self.post(self.url("status_draft", self.project.pk))
        self.assertEqual(r.json()["ok"], False)
        with patch("email_assistant.ai_helper._call_ai_direct", return_value=""):
            self.assertIn("AI не відповів", self.post(self.url("status_draft", self.project.pk)).json()["error"])
        self.assertFalse(ProjectUpdate.objects.exists())          # чернетка нічого не зберігає


class WeeklyTests(PortfolioBase):
    def test_due_and_claim(self):
        s = ProjectSettings.get()
        monday = timezone.make_aware(datetime(2026, 11, 9, 9, 0))
        self.assertFalse(weekly.due(monday))                      # вимкнено за замовчуванням
        s.weekly_enabled, s.weekday, s.hour = True, 0, 8
        s.save()
        self.assertTrue(weekly.due(monday))
        self.assertFalse(weekly.due(monday.replace(hour=7)))
        self.assertFalse(weekly.due(monday + timedelta(days=1)))
        self.assertTrue(weekly.claim(monday))
        self.assertFalse(weekly.claim(monday + timedelta(hours=2)))    # двічі за день — ні
        self.assertFalse(weekly.due(monday + timedelta(hours=3)))

    def test_run_snapshots_and_reports(self):
        self.plan_two_phases()
        risks.save(self.project, {"title": "Neu", "probability": 3, "impact": 3}, user=self.user)
        sent = []

        def deliver(user, m, link_text="", mail=True, telegram=True):
            sent.append((user, m, mail, telegram))
            return "🔔 Повідомлено: пошта"
        with patch("agent.notify.deliver", side_effect=deliver):
            out = weekly.run()
        self.assertEqual((out["sent"], out["people"]), (1, 1))
        self.assertEqual(out["snapshots"], 2)                     # обидва відкриті проєкти без статусу
        user, m, mail, telegram = sent[0]
        self.assertEqual(user, self.user)
        text = "\n".join(m["lines"])
        self.assertIn(self.project.code, text)
        self.assertIn("ризики 🔴1", text)
        self.assertIn("нові 1", text)
        self.assertIn("portfolio/?f=mine", m["link"])
        self.assertEqual(ProjectUpdate.objects.filter(source=ProjectUpdate.AUTO).count(), 2)
        with patch("agent.notify.deliver", side_effect=deliver):
            out = weekly.run()
        self.assertEqual(out["snapshots"], 0)                     # знімок раз на тиждень
        self.assertIn("звітів 1 з 1", ProjectSettings.get().last_result)

    def test_settings_page_and_send_to_me(self):
        r = self.client.get("/admin/projects/projectsettings/")
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r["Location"])
        self.assertContains(page, "Лише мені")
        with patch("agent.notify.deliver", return_value="🔔 Повідомлено: пошта") as d:
            r = self.client.post("/admin/projects/projectsettings/send-now/", {"who": "me"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(d.call_args[0][0], self.user)
        self.assertEqual(ProjectUpdate.objects.filter(source=ProjectUpdate.AUTO).count(), 0)   # «лише мені» — без знімків
        self.assertEqual(self.client.get("/admin/projects/projectsettings/send-now/").status_code, 302)

    def test_command_if_due(self):
        from django.core.management import call_command
        with patch("projects.weekly.run") as run:
            call_command("project_weekly", "--if-due", verbosity=0)
        run.assert_not_called()                                   # вимкнено


class MenuAndPanelTests(PortfolioBase):
    def test_menu_and_side_panel(self):
        html = self.client.get("/admin/").content.decode()
        self.assertIn("/admin/projects/projectrisk/", html)
        self.assertIn("/admin/projects/projectsettings/", html)
        risks.save(self.project, {"title": "Panel-Risiko", "probability": 3, "impact": 2}, user=self.user)
        r = self.client.get(f"/core/panel/projects.project/{self.project.pk}/")
        self.assertContains(r, "Panel-Risiko")
