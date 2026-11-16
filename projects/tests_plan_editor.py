"""✏️ Редактор плану: уся таблиця одним запитом (projects/plan_editor.save_all) і 🧠 «Що змінити в плані» (revise —
AI лише пропонує зміни, перевіряє clean_changes); 🎤 голосовий набір на сторінках Minerva. AI підмінено.
python manage.py test projects --settings=tabele.settings_test"""
import json
from datetime import timedelta
from unittest.mock import patch

from projects import bg, plan, plan_editor
from projects.models import Project, ProjectPhase, ProjectStep, StepDependency
from projects.plan import PlanError
from projects.tests_portfolio import PortfolioBase

M, T = ProjectStep.MILESTONE, ProjectStep.TASK


class EditorBase(PortfolioBase):
    def setUp(self):
        super().setUp()
        nobg = patch.object(bg, "BACKGROUND", False)
        nobg.start()
        self.addCleanup(nobg.stop)
        self.p1 = ProjectPhase.objects.create(project=self.project, name="Prototyp", icon="🔧", order=10)
        self.p2 = ProjectPhase.objects.create(project=self.project, name="Serie", icon="🏭", order=20)
        self.a = self.step("Platinen bestellen", 5, start=self.d(0), end=self.d(4), phase=self.p1)
        self.b = self.step("Muster messen", 3, after=[self.a], phase=self.p1)
        self.c = self.step("Serienfertigung", 10, after=[self.b], phase=self.p2, side=ProjectStep.SUPPLIER)

    def row(self, s, **kw):
        deps = [{"key": f"s{d.predecessor_id}", "lag": d.lag_days}
                for d in StepDependency.objects.filter(successor=s, predecessor__project=self.project)]
        r = {"key": f"s{s.pk}", "phase": f"p{s.phase_id}" if s.phase_id else "", "title": s.title, "kind": s.kind,
             "side": s.side, "start": s.start.isoformat() if s.start else "", "end": s.end.isoformat() if s.end else "",
             "duration": s.duration_days, "status": s.status, "after": deps}
        r.update(kw)
        return r

    def phases(self, *extra):
        return [{"key": f"p{p.pk}", "name": p.name, "icon": p.icon, "gate": p.gate} for p in (self.p1, self.p2)] + list(extra)

    def table(self, steps=None, phases=None, **kw):
        out = {"phases": phases if phases is not None else self.phases(),
               "steps": steps if steps is not None else [self.row(s) for s in (self.a, self.b, self.c)]}
        out.update(kw)
        return out

    def preds(self, s):
        return set(StepDependency.objects.filter(successor=s).values_list("predecessor__title", flat=True))


class SaveAllTests(EditorBase):
    def test_nothing_changed_nothing_written(self):
        before = ProjectStep.objects.get(pk=self.b.pk).updated_at
        out = plan_editor.save_all(self.project, self.table(), self.user)
        self.assertEqual(out, {"added": 0, "changed": 0, "removed": 0})
        self.assertEqual(ProjectStep.objects.get(pk=self.b.pk).updated_at, before)
        self.assertEqual(plan_editor.saved_text(out), "✏️ План збережено — змін не було")

    def test_edit_add_move_and_delete_in_one_go(self):
        new_phase = {"key": "np1", "name": "Test", "icon": "🧪", "gate": "Muster ok"}
        steps = [
            self.row(self.a, title="Platinen bestellen (JLC)", end=self.d(6), duration=7),
            self.row(self.c, phase=f"p{self.p1.pk}", after=[{"key": f"s{self.a.pk}", "lag": 2}]),   # нагору, після A +2
            {"key": "n1", "phase": "np1", "title": "Klimatest", "kind": T, "side": "we", "start": "", "end": "",
             "duration": 4, "status": "todo", "after": [{"key": f"s{self.c.pk}"}]},
            {"key": "n2", "phase": "np1", "title": "Freigabe", "kind": M, "side": "customer", "start": "",
             "end": self.d(30).isoformat(), "duration": 0, "status": "todo", "after": [{"key": "n1"}]},
        ]
        out = plan_editor.save_all(self.project, self.table(steps, self.phases(new_phase),
                                                            delete_steps=[self.b.pk]), self.user)
        self.assertEqual(out, {"added": 3, "changed": 2, "removed": 1})
        self.assertFalse(ProjectStep.objects.filter(pk=self.b.pk).exists())
        a, c = ProjectStep.objects.get(pk=self.a.pk), ProjectStep.objects.get(pk=self.c.pk)
        self.assertEqual((a.title, a.end, a.duration_days), ("Platinen bestellen (JLC)", self.d(6), 7))
        self.assertEqual(c.phase_id, self.p1.pk)
        self.assertEqual(list(StepDependency.objects.filter(successor=c).values_list("predecessor_id", "lag_days")),
                         [(a.pk, 2)])
        test = ProjectPhase.objects.get(project=self.project, name="Test")
        self.assertEqual((test.icon, test.gate, test.order), ("🧪", "Muster ok", 30))
        k = ProjectStep.objects.get(title="Klimatest")
        f = ProjectStep.objects.get(title="Freigabe")
        self.assertEqual((k.phase_id, f.kind, f.end, f.side), (test.pk, M, self.d(30), "customer"))
        self.assertEqual(self.preds(f), {"Klimatest"})
        order = list(ProjectStep.objects.filter(project=self.project).order_by("order").values_list("title", flat=True))
        self.assertEqual(order, ["Platinen bestellen (JLC)", "Serienfertigung", "Klimatest", "Freigabe"])

    def test_new_phase_on_top_moves_others_without_counting_them(self):
        top = {"key": "np1", "name": "Vorbereitung", "icon": "📝", "gate": ""}
        out = plan_editor.save_all(self.project, self.table(phases=[top] + self.phases()), self.user)
        self.assertEqual(out, {"added": 1, "changed": 0, "removed": 0})
        self.assertEqual(list(ProjectPhase.objects.filter(project=self.project).order_by("order")
                              .values_list("name", flat=True)), ["Vorbereitung", "Prototyp", "Serie"])

    def test_swap_is_not_a_cycle_but_a_real_cycle_saves_nothing(self):
        steps = [self.row(self.a, after=[{"key": f"s{self.b.pk}"}]), self.row(self.b, after=[]), self.row(self.c)]
        plan_editor.save_all(self.project, self.table(steps), self.user)
        self.assertEqual((self.preds(self.a), self.preds(self.b)), ({"Muster messen"}, set()))
        steps = [self.row(self.a, title="Neu"), self.row(self.b, after=[{"key": f"s{self.c.pk}"}]), self.row(self.c)]
        with self.assertRaisesMessage(PlanError, "вийшло б коло"):
            plan_editor.save_all(self.project, self.table(steps), self.user)
        self.assertEqual(ProjectStep.objects.get(pk=self.a.pk).title, "Platinen bestellen")      # усе відкочено

    def test_other_project_dependency_and_hidden_fields_stay(self):
        other = Project.objects.create(name="Інший", status=Project.ACTIVE)
        x = self.step("Gehäuse fertig", 3, project=other)
        StepDependency.objects.create(predecessor=x, successor=self.c)
        ProjectStep.objects.filter(pk=self.c.pk).update(notes="важливо", trigger="po_received", assignee=self.user)
        self.c.refresh_from_db()
        plan_editor.save_all(self.project, self.table(
            [self.row(self.a), self.row(self.b), self.row(self.c, title="Serie 1500 Stk", after=[])]), self.user)
        c = ProjectStep.objects.get(pk=self.c.pk)
        self.assertEqual((c.title, c.notes, c.trigger, c.assignee_id), ("Serie 1500 Stk", "важливо", "po_received",
                                                                         self.user.pk))
        self.assertEqual(self.preds(c), {"Gehäuse fertig"})                       # ⛓ інший проєкт — як був

    def test_errors_name_the_row(self):
        with self.assertRaisesMessage(PlanError, "Рядок 2"):
            plan_editor.save_all(self.project, self.table([self.row(self.a), self.row(self.b, title="  ")]), self.user)
        with self.assertRaisesMessage(PlanError, "кінець раніше за початок".capitalize()):
            plan_editor.save_all(self.project, self.table(
                [self.row(self.a, start=self.d(5), end=self.d(1))]), self.user)
        ProjectStep.objects.filter(pk=self.b.pk).delete()
        with self.assertRaisesMessage(PlanError, "уже прибрано"):
            plan_editor.save_all(self.project, self.table(), self.user)

    def test_view_and_rights(self):
        url = self.url("plan_save_all", self.project.pk)
        r = self.post(url, self.table([self.row(self.a, title="Bestellen"), self.row(self.b), self.row(self.c)]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("✏️ План збережено: змінено 1", r.json()["html"])
        r = self.post(url, self.table([self.row(self.a, title="")]))
        self.assertEqual(r.status_code, 400)
        self.client.force_login(self.viewer("ro", role="readonly", modules=["projects"]))
        self.assertEqual(self.post(url, self.table()).status_code, 404)        # без права змінювати — проєкту «немає»

    def test_editor_data_and_buttons(self):
        p = plan.editor_data(self.project, __import__("projects.schedule", fromlist=["x"]).compute(self.project))
        c = next(s for s in p["steps"] if s["id"] == self.c.pk)
        self.assertEqual(c["deps"], [(self.b.pk, 0)])
        self.assertIn("order", c)
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn("data-pje-open", html)
        self.assertIn(self.url("plan_save_all", self.project.pk), html)


class ReviseTests(EditorBase):
    def revise_with(self, changes, wish="Nach dem Messen 2 Wochen Klimatest einplanen", summary="Klimatest eingeplant"):
        calls = []

        def fake(prompt, max_tokens=0, user=None, model=""):
            calls.append({"prompt": prompt, "model": model})
            return json.dumps({"summary": summary, "changes": changes}, ensure_ascii=False)
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.post(self.url("plan_revise", self.project.pk),
                          {"wish": wish, "table": {"phases": self.phases(), "steps": [
                              self.row(self.a), self.row(self.b), self.row(self.c),
                              {"key": "n1", "phase": "", "title": "Ще не збережений", "kind": T, "duration": 2,
                               "after": ["s%d" % self.c.pk]}]}})
        return r, calls

    def test_changes_are_checked_and_nothing_is_written(self):
        b, c = f"s{self.b.pk}", f"s{self.c.pk}"
        r, calls = self.revise_with([
            {"op": "add", "ref": "a1", "place_after": b, "title": "Klimatest", "days": 14, "side": "we",
             "after": [b], "why": "просили 2 тижні"},
            {"op": "update", "id": c, "after": ["a1"], "why": "серія після тесту"},
            {"op": "update", "id": "s999999", "title": "чужий"},                       # немає в таблиці
            {"op": "update", "id": b, "kind": "milestone", "days": 5, "start": self.d(2).isoformat(), "why": "віха"},
            {"op": "update", "id": b, "status": "lost"},                               # невідомий стан
            {"op": "remove", "id": "n1", "why": "дубль"},
            {"op": "add_phase", "ref": "ap1", "name": "Test", "icon": "🧪", "place_after": f"p{self.p1.pk}"},
            {"op": "delete_everything"},
        ])
        self.assertEqual(r.status_code, 200)
        res = r.json()["result"]
        self.assertEqual(res["summary"], "Klimatest eingeplant")
        self.assertEqual(res["dropped"], 3)
        ops = [(x["op"], x.get("id") or x.get("ref")) for x in res["changes"]]
        self.assertEqual(ops, [("add", "a1"), ("update", c), ("update", b), ("remove", "n1"), ("add_phase", "ap1")])
        self.assertEqual(res["changes"][0]["set"], {"title": "Klimatest", "side": "we", "duration": 14, "after": [b]})
        self.assertEqual(res["changes"][1]["set"], {"after": ["a1"]})
        self.assertEqual(res["changes"][2]["set"], {"kind": M, "duration": 0})           # віха — без тривалості й початку
        prompt = calls[0]["prompt"]
        self.assertIn("Nach dem Messen 2 Wochen Klimatest einplanen", prompt)
        self.assertIn(f"2 · {b} · p{self.p1.pk} · Muster messen · крок", prompt)
        self.assertIn("n1 · без фази · Ще не збережений", prompt)                  # незбережене теж бачить
        self.assertEqual(calls[0]["model"], plan_editor.AI_MODEL)
        self.assertEqual(ProjectStep.objects.filter(project=self.project).count(), 3)    # у план нічого не записано
        self.assertEqual(self.client.get(self.url("plan_revise", self.project.pk)).json()["result"]["dropped"], 3)

    def test_empty_wish_budget_and_rights(self):
        r = self.post(self.url("plan_revise", self.project.pk), {"wish": " ", "table": {}})
        self.assertEqual(r.status_code, 400)
        with patch("ai_assistant.budget_guard.check_budget", return_value=(False, "Ліміт бюджету AI вичерпано")), \
                patch("email_assistant.ai_helper._call_ai_direct") as call:
            r = self.post(self.url("plan_revise", self.project.pk), {"wish": "x", "table": {}})
        self.assertEqual((r.status_code, call.called), (400, False))
        self.client.force_login(self.viewer("ro2", role="readonly", modules=["projects"]))
        self.assertEqual(self.post(self.url("plan_revise", self.project.pk), {"wish": "x"}).status_code, 404)

    def test_not_json_is_an_error(self):
        with patch("email_assistant.ai_helper._call_ai_direct", return_value="Ich würde es so machen…"), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.post(self.url("plan_revise", self.project.pk), {"wish": "check", "table": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("не у форматі змін", r.json()["error"])


class VoiceTests(EditorBase):
    def test_voice_script_on_pages_and_setting(self):
        page = self.client.get("/admin/").content.decode()
        self.assertIn("/static/admin/js/mv_voice.js", page)
        self.assertContains(self.client.get("/core/my-settings/"), 'id="ui-voice"')
        from core import ui_prefs
        self.assertEqual(ui_prefs.clean({"voice": "off"}).get("voice"), "off")
        self.assertNotIn("voice", ui_prefs.clean({"voice": "sometimes"}))
