"""📅 Звірити план з нарадою (projects/plan_review.py) і 🕘 Історія змін плану (projects/plan_history.py).

Історія: хто, коли, було → стало, звідки (вручну / редактор / з наради), кеш прогнозу й видалення проєкту — без записів.
Звірка: лише свої наради проєкту з «📝 Підсумком наради», без галочки згоди / з чужою нарадою / іншою моделлю — відмова,
кожна передача — у протокол згоди наради; зміна без тези наради відкидається; план змінюється лише після ✓ (нові фаза
й крок — разом із залежною зміною), ✕ — нічого; «привиди» на обох діаграмах і блок у пульті — лише тому, хто звіряв.

    python manage.py test projects.tests_plan_review --settings=tabele.settings_test
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.utils import timezone

from meetings.models import Meeting, MeetingProject, MeetingTranscript
from projects import cockpit, gantt, plan, plan_history, plan_review, schedule
from projects.models import (PlanChange, PlanReview, PlanReviewChange, Project, ProjectPhase, ProjectStep,
                             StepDependency)
from projects.tests_meetings_ui import enable_recording
from projects.tests_plan import PlanBase

MODEL = "claude-sonnet-5-5"


class ReviewBase(PlanBase):
    def setUp(self):
        super().setUp()
        enable_recording()
        plan_review.BACKGROUND = False
        self.addCleanup(setattr, plan_review, "BACKGROUND", True)
        avail = patch("meetings.ai_moments.available", return_value=True)
        avail.start()
        self.addCleanup(avail.stop)
        self.calls = []
        self.phase = ProjectPhase.objects.create(project=self.project, name="Muster", icon="🧪", order=10)
        self.a = self.step("Muster bauen", 5, start=self.d(1), end=self.d(5), phase=self.phase)
        self.b = self.step("Muster senden", 2, after=[self.a], phase=self.phase)
        PlanChange.objects.all().delete()                  # підготовка тесту — не історія

    def meeting(self, owner=None, linked=False, brief=True, consent=False):
        m = Meeting.objects.create(owner=owner or self.user, title="Kickoff Muster", status=Meeting.REVIEW,
                                   project=None if linked else self.project, started_at=self.ago(1, hour=10),
                                   duration_s=1800, transcript_at=timezone.now(),
                                   consent_log=[{"at": timezone.now().isoformat(), "kind": "cloud"}] if consent else [])
        if linked:
            MeetingProject.objects.create(meeting=m, project=self.project)
        MeetingTranscript.objects.create(meeting=m, segments=[{"n": 1, "start": 125}, {"n": 2, "start": 300}], brief={
            "v": 1, "about": "Muster und Freigabe", "label": "Sonnet", "at": timezone.now().isoformat(),
            "sections": {"agreements": [{"text": "Muster kommt eine Woche später", "lines": [1]}],
                         "tasks": [{"text": "Kunde gibt das Muster frei", "who": "Client GmbH", "due": "Ende Monat",
                                    "lines": [2]}]}} if brief else {})
        return m

    def answer(self, changes=None, summary="Muster eine Woche später, Freigabe durch den Kunden neu."):
        a, b = f"s{self.a.pk}", f"s{self.b.pk}"
        return {"summary": summary, "changes": changes if changes is not None else [
            {"op": "update", "id": a, "end": self.d(12).isoformat(), "why": "eine Woche später", "from": ["B1"]},
            {"op": "add", "ref": "a1", "phase": f"p{self.phase.pk}", "place_after": b, "title": "Freigabe Kunde",
             "kind": "milestone", "side": "customer", "end": self.d(20).isoformat(), "after": [b],
             "why": "Kunde gibt frei", "from": ["B2"]},
            {"op": "update", "id": b, "status": "done", "why": "ohne Quelle"},              # без тези — геть
            {"op": "update", "id": b, "end": self.d(9).isoformat(), "from": ["B99"]},       # чужа теза — геть
            {"op": "remove", "id": "s999999", "from": ["B1"]},                            # кроку немає — геть
        ]}

    def claude(self, data):
        def call(prompt, *a, **k):
            self.calls.append(prompt)
            return "Gerne: " + json.dumps(data, ensure_ascii=False)
        return patch.multiple("email_assistant.ai_helper", _call_ai_direct=call,
                              last_usage=lambda: None, last_model=lambda: MODEL)

    def reviewed(self, data=None, m=None):
        m = m or self.meeting()
        with self.claude(data or self.answer()):
            r = plan_review.start(self.project, self.user, m.pk, MODEL, consent=True)
        return m, r

    def change(self, r, op, target=None):
        qs = r.changes.filter(op=op)
        return qs.get(target=target) if target else qs.get()


class HistoryTests(ReviewBase):
    def test_manual_move_records_who_when_before_after(self):
        r = self.post(self.url("plan_step_dates", self.project.pk, self.a.pk),
                      {"start": self.d(3).isoformat(), "end": self.d(7).isoformat()})
        self.assertTrue(r.json()["ok"], r.content)
        ch = PlanChange.objects.get(step=self.a)
        self.assertEqual((ch.action, ch.source, ch.user), (PlanChange.EDIT, PlanChange.MANUAL, self.user))
        self.assertEqual(ch.before["end"], self.d(5).isoformat())
        self.assertEqual(ch.after["end"], self.d(7).isoformat())
        e = plan_history.entries(self.project, self.user)[0]
        self.assertEqual(e["source"], PlanChange.MANUAL)
        self.assertIn("Muster bauen", e["lines"][0])
        self.assertIn("→", e["lines"][0])
        html = cockpit.render(self.project, self.user)
        self.assertIn("🕘 Історія змін плану", html)

    def test_add_remove_and_context_source(self):
        with plan_history.context(user=self.user, source=PlanChange.EDITOR) as ctx:
            s = plan.save_step(self.project, {"title": "Test", "duration": 2, "start": self.d(2).isoformat()},
                               user=self.user)
            plan.delete_step(self.project, s.pk)
        rows = list(PlanChange.objects.order_by("pk"))
        self.assertEqual([r.action for r in rows], [PlanChange.ADD, PlanChange.REMOVE])
        self.assertEqual({r.source for r in rows}, {PlanChange.EDITOR})
        self.assertEqual({r.batch for r in rows}, {ctx["batch"]})                # одна дія — один рядок історії
        self.assertEqual(len(plan_history.entries(self.project, self.user)), 1)

    def test_forecast_cache_and_project_delete_write_nothing(self):
        cockpit.render(self.project, self.user)                    # прогноз пишеться в кеш кроків (update_fields)
        schedule.persist(schedule.compute(self.project, today=self.today))
        self.assertFalse(PlanChange.objects.exists())
        pk = self.project.pk
        Project.objects.filter(pk=pk).get().delete()               # каскад кроків — без історії й без збою
        self.assertFalse(ProjectStep.objects.filter(project_id=pk).exists())
        self.assertFalse(PlanChange.objects.filter(project_id=pk).exists())


class StartTests(ReviewBase):
    def test_proposes_only_sourced_changes_and_logs_consent(self):
        m, r = self.reviewed()
        self.assertEqual(r.status, PlanReview.READY, r.error)
        self.assertEqual(r.changes.count(), 2)
        self.assertEqual(r.dropped, 3)
        upd = self.change(r, "update", f"s{self.a.pk}")
        self.assertEqual(upd.sources, ["B1"])
        self.assertEqual(upd.before["end"], self.d(5).isoformat())     # як було на момент пропозиції
        self.assertEqual(self.change(r, "add").sources, ["B2"])
        prompt = self.calls[0]
        self.assertIn(f"s{self.a.pk}", prompt)
        self.assertIn("[B1]", prompt)
        self.assertIn("Muster kommt eine Woche später", prompt)
        self.a.refresh_from_db()
        self.assertEqual(self.a.end, self.d(5))                         # план не змінюється до ✓
        self.assertFalse(PlanChange.objects.exists())
        m.refresh_from_db()
        log = [e for e in m.consent_log if e.get("task") == "plan"]
        self.assertEqual((len(log), log[0]["kind"], log[0]["project"]), (1, "cloud", self.project.code))

    def test_change_that_changes_nothing_is_not_proposed(self):
        ProjectStep.objects.filter(pk=self.b.pk).update(status=ProjectStep.DONE)
        _m, r = self.reviewed(self.answer(changes=[
            {"op": "update", "id": f"s{self.b.pk}", "status": "done", "from": ["B1"]},                  # уже так
            {"op": "update", "id": f"s{self.a.pk}", "end": self.d(5).isoformat(), "from": ["B1"]},      # ті самі дати
            {"op": "update", "id": f"s{self.a.pk}", "status": "doing", "from": ["B1"]}]))
        self.assertEqual(list(r.changes.values_list("op", "data__set__status")), [("update", "doing")])
        self.assertEqual(r.dropped, 0)

    def test_refusals(self):
        m = self.meeting()
        with self.assertRaises(plan_review.ReviewError):            # без галочки згоди
            plan_review.start(self.project, self.user, m.pk, MODEL, consent=False)
        with self.assertRaises(plan_review.ReviewError):            # невідома модель
            plan_review.start(self.project, self.user, m.pk, "gpt-5", consent=True)
        bob = User.objects.create_user("bob", "bob@x.y", "p", is_staff=True)
        other = self.meeting(owner=bob)
        with self.assertRaises(plan_review.ReviewError):            # чужа нарада
            plan_review.start(self.project, self.user, other.pk, MODEL, consent=True)
        no_brief = self.meeting(brief=False)
        self.assertNotIn(no_brief, plan_review.meetings_for(self.project, self.user))
        self.assertIn(self.meeting(linked=True), plan_review.meetings_for(self.project, self.user))
        self.assertFalse(PlanReview.objects.exists())

    def test_nothing_to_change_and_ai_failure(self):
        _m, r = self.reviewed(self.answer(changes=[], summary="Plan passt."))
        self.assertEqual((r.status, r.summary), (PlanReview.DONE, "Plan passt."))
        self.assertIn("план узгоджений з нарадою", cockpit.render(self.project, self.user))
        with self.claude({"nonsense": 1}), patch("email_assistant.ai_helper._call_ai_direct", return_value="ні"):
            r = plan_review.start(self.project, self.user, self.meeting().pk, MODEL, consent=True)
        self.assertEqual(r.status, PlanReview.ERROR)
        self.assertTrue(r.error)

    def test_stuck_review_turns_into_error(self):
        m = self.meeting()
        r = PlanReview.objects.create(project=self.project, meeting=m, created_by=self.user, model=MODEL)
        PlanReview.objects.filter(pk=r.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        self.assertIsNone(plan_review.running(self.project, self.user))
        r.refresh_from_db()
        self.assertEqual(r.status, PlanReview.ERROR)
        _m, again = self.reviewed(m=m)                               # можна знову
        self.assertEqual(again.status, PlanReview.READY)


class ApplyTests(ReviewBase):
    def test_apply_one_change_writes_history_from_meeting(self):
        m, r = self.reviewed()
        upd = self.change(r, "update", f"s{self.a.pk}")
        out = plan_review.apply(self.project, self.user, r, [upd.pk])
        self.assertEqual(out, {"applied": 1, "failed": 0})
        self.a.refresh_from_db()
        self.assertEqual((self.a.start, self.a.end), (self.d(1), self.d(12)))
        upd.refresh_from_db()
        self.assertEqual(upd.status, PlanReviewChange.APPLIED)
        ch = PlanChange.objects.get(step=self.a)
        self.assertEqual((ch.source, ch.meeting, ch.review_id, ch.user), (PlanChange.MEETING, m, r.pk, self.user))
        self.assertEqual((ch.before["end"], ch.after["end"]), (self.d(5).isoformat(), self.d(12).isoformat()))
        e = plan_history.entries(self.project, self.user)[0]
        self.assertEqual(e["meeting"]["title"], "Kickoff Muster")
        self.assertEqual(r.changes.filter(status=PlanReviewChange.PENDING).count(), 1)
        r.refresh_from_db()
        self.assertEqual(r.status, PlanReview.READY)                  # ще одна чекає

    def test_new_step_pulls_its_new_phase_and_links_dependency(self):
        b = f"s{self.b.pk}"
        _m, r = self.reviewed(self.answer(changes=[
            {"op": "add_phase", "ref": "ap1", "place_after": f"p{self.phase.pk}", "name": "Freigabe", "icon": "✅",
             "from": ["B2"]},
            {"op": "add", "ref": "a1", "phase": "ap1", "title": "Freigabe Kunde", "kind": "milestone",
             "end": self.d(20).isoformat(), "after": [b], "from": ["B2"]}]))
        add = self.change(r, "add")
        self.assertEqual(plan_review.apply(self.project, self.user, r, [add.pk]), {"applied": 2, "failed": 0})
        step = ProjectStep.objects.get(project=self.project, title="Freigabe Kunde")
        self.assertEqual((step.kind, step.phase.name, step.end), (ProjectStep.MILESTONE, "Freigabe", self.d(20)))
        self.assertTrue(StepDependency.objects.filter(predecessor=self.b, successor=step).exists())
        phases = list(ProjectPhase.objects.filter(project=self.project).order_by("order").values_list("name", flat=True))
        self.assertEqual(phases, ["Muster", "Freigabe"])
        r.refresh_from_db()
        self.assertEqual(r.status, PlanReview.DONE)
        self.assertEqual(set(PlanChange.objects.values_list("action", flat=True)), {PlanChange.ADD, PlanChange.PHASE_ADD})

    def test_failed_change_stays_open_others_apply(self):
        _m, r = self.reviewed()
        ProjectStep.objects.filter(pk=self.a.pk).delete()             # крок прибрали після пропозиції
        out = plan_review.apply(self.project, self.user, r)
        self.assertEqual(out, {"applied": 1, "failed": 1})
        upd = self.change(r, "update", f"s{self.a.pk}")
        self.assertEqual(upd.status, PlanReviewChange.FAILED)
        self.assertIn("немає", upd.note)
        self.assertTrue(ProjectStep.objects.filter(title="Freigabe Kunde").exists())

    def test_reject_changes_nothing(self):
        _m, r = self.reviewed()
        self.assertEqual(plan_review.reject(self.project, self.user, r), 2)
        r.refresh_from_db()
        self.assertEqual(r.status, PlanReview.DONE)
        self.a.refresh_from_db()
        self.assertEqual(self.a.end, self.d(5))
        self.assertFalse(PlanChange.objects.exists())
        self.assertEqual(plan_review.ghosts(r)["steps"], {})

    def test_stale_proposal_says_what_changed(self):
        _m, r = self.reviewed()
        plan.move_step(self.project, self.a.pk, start=self.d(2), end=self.d(8))
        row = next(x for x in plan_review.view(r, self.user)["rows"] if x["op"] == "update")
        self.assertIn("після пропозиції план змінили", row["stale"])


class ShowTests(ReviewBase):
    def test_ghosts_on_both_gantts_only_for_reviewer(self):
        _m, r = self.reviewed()
        gh = plan_review.ghosts(r)
        self.assertEqual(gh["steps"][self.a.pk]["end"], self.d(12).isoformat())
        self.assertEqual(gh["added"][0]["after"], self.b.pk)
        p = schedule.compute(self.project, today=self.today)
        svg = gantt.render(self.project, p, self.today, ghosts=gh)
        self.assertIn("pjg-ghost", svg)
        self.assertIn("Freigabe Kunde", svg)
        rows = gantt.classic_data(self.project, p, self.today, ghosts=gh)["rows"]
        self.assertEqual(next(x for x in rows if x["type"] == "step" and x["id"] == self.a.pk)["prop"]["end"], self.d(12).isoformat())
        ghost = next(x for x in rows if x.get("ghost"))
        self.assertEqual((ghost["title"], ghost["phase"]), ("Freigabe Kunde", self.phase.pk))
        self.assertEqual(rows.index(ghost), rows.index(next(x for x in rows if x["type"] == "step" and x["id"] == self.b.pk)) + 1)
        html = cockpit.render(self.project, self.user)
        self.assertIn("📅 Пропозиції з наради", html)
        self.assertIn("pjg-ghost", html)
        self.assertIn("Muster kommt eine Woche später", html)
        self.assertIn("#t=125", html)                                  # місце в розшифровці наради
        self.assertNotIn("<form", html)
        bob = User.objects.create_superuser("bob", "bob@x.y", "p")
        other = cockpit.render(self.project, bob)
        self.assertNotIn("Пропозиції з наради", other)
        self.assertNotIn("pjg-ghost", other)
        self.assertNotIn("Muster kommt eine Woche später", other)
        self.assertIsNone(plan_review.view(r, bob))

    def test_meeting_choice_consent_and_url(self):
        self.meeting(consent=True)
        html = cockpit.render(self.project, self.user)
        self.assertIn("data-pjr2-meeting", html)
        self.assertIn("data-pjr2-consent checked", html)
        self.assertEqual(plan_review.review_url(self.project, 7),
                         f"/admin/projects/project/{self.project.pk}/change/?plan_meeting=7#pj-plan")

    def test_history_hides_meeting_title_from_others(self):
        _m, r = self.reviewed()
        plan_review.apply(self.project, self.user, r)
        bob = User.objects.create_superuser("bob", "bob@x.y", "p")
        e = plan_history.entries(self.project, bob)[0]
        self.assertEqual((e["meeting"]["title"], e["meeting"]["url"]), ("", ""))
        self.assertEqual(e["source"], PlanChange.MEETING)


class ViewTests(ReviewBase):
    def test_start_poll_decide(self):
        m = self.meeting()
        base = f"/projects/api/project/{self.project.pk}/plan/review/"
        self.assertEqual(self.client.get(base).status_code, 405)
        with self.claude(self.answer()):
            r = self.post(base, {"meeting": m.pk, "model": MODEL, "consent": 1})
        j = r.json()
        self.assertTrue(j["ok"], j)
        self.assertIn("Пропозиції з наради", j["html"])
        rid = j["review"]
        self.assertEqual(self.client.get(f"{base}{rid}/").json()["status"], PlanReview.READY)
        r = self.post(f"{base}{rid}/decide/", {"action": "apply", "ids": "all"})
        self.assertTrue(r.json()["ok"], r.content)
        self.assertIn("Застосовано змін: 2", r.json()["html"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.end, self.d(12))
        again = self.post(f"{base}{rid}/decide/", {"action": "apply", "ids": "all"})
        self.assertEqual(again.status_code, 400)                      # уже розглянуто

    def test_no_consent_and_foreign_review(self):
        m = self.meeting()
        base = f"/projects/api/project/{self.project.pk}/plan/review/"
        r = self.post(base, {"meeting": m.pk, "model": MODEL})
        self.assertEqual(r.status_code, 400)
        _m, review = self.reviewed(m=m)
        bob = User.objects.create_superuser("bob", "bob@x.y", "p")
        self.client.force_login(bob)
        self.assertEqual(self.client.get(f"{base}{review.pk}/").status_code, 404)
        r = self.post(f"{base}{review.pk}/decide/", {"action": "apply", "ids": "all"})
        self.assertEqual(r.status_code, 400)
        self.a.refresh_from_db()
        self.assertEqual(self.a.end, self.d(5))
        self.assertEqual(review.changes.filter(status=PlanReviewChange.PENDING).count(), 2)
