"""📅 Звірити план з нарадою (projects/plan_review.py) і 🕘 Історія змін плану (projects/plan_history.py).

Кілька нарад в одній звірці: у порядку дат [M1], [M2], тези з наскрізними номерами, пізніше рішення важливіше; згода —
для кожної наради окремо (без згоди в протоколі — лише з галочкою), запис у протокол кожної; зміна — з наради своєї тези
(у списку й в історії видно, з якої).

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

    def meeting(self, owner=None, linked=False, brief=True, consent=False, title="Kickoff Muster", days=1,
                points=None):
        m = Meeting.objects.create(owner=owner or self.user, title=title, status=Meeting.REVIEW,
                                   project=None if linked else self.project, started_at=self.ago(days, hour=10),
                                   duration_s=1800, transcript_at=timezone.now(),
                                   consent_log=[{"at": timezone.now().isoformat(), "kind": "cloud"}] if consent else [])
        if linked:
            MeetingProject.objects.create(meeting=m, project=self.project)
        sections = ({"agreements": [{"text": t, "lines": [1]} for t in points]} if points else
                    {"agreements": [{"text": "Muster kommt eine Woche später", "lines": [1]}],
                     "tasks": [{"text": "Kunde gibt das Muster frei", "who": "Client GmbH", "due": "Ende Monat",
                                "lines": [2]}]})
        MeetingTranscript.objects.create(meeting=m, segments=[{"n": 1, "start": 125}, {"n": 2, "start": 300}], brief={
            "v": 1, "about": "Muster und Freigabe", "label": "Sonnet", "at": timezone.now().isoformat(),
            "sections": sections} if brief else {})
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
        self.assertIn(f"◆ {self.d(20):%d.%m.%Y}", plan_history.describe(PlanChange.objects.get(action=PlanChange.ADD)))

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


class SeveralMeetingsTests(ReviewBase):
    def two(self, consent=(False, False)):
        client = self.meeting(title="Mit dem Kunden", days=3, consent=consent[0],
                              points=["Muster eine Woche später", "Freigabe in 5 Tagen"])
        internal = self.meeting(title="Intern", days=1, consent=consent[1],
                                points=["Freigabe doch erst nach 10 Tagen"])
        return client, internal

    def test_meetings_in_date_order_one_numbering_later_decides(self):
        client, internal = self.two()
        b = f"s{self.b.pk}"
        data = self.answer(changes=[
            {"op": "update", "id": f"s{self.a.pk}", "end": self.d(12).isoformat(), "from": ["B1"]},
            {"op": "add", "ref": "a1", "place_after": b, "title": "Freigabe Kunde", "kind": "milestone",
             "end": self.d(25).isoformat(), "after": [b], "from": ["B2", "B3"]}])
        with self.claude(data):
            r = plan_review.start(self.project, self.user, f"{internal.pk},{client.pk}", MODEL, consent=True)
        self.assertEqual(r.status, PlanReview.READY, r.error)
        self.assertEqual(r.meeting, internal)                                  # остання за датою
        self.assertEqual(set(r.meetings.all()), {client, internal})
        prompt = self.calls[0]
        self.assertLess(prompt.index("[M1] нарада"), prompt.index("[M2] нарада"))
        self.assertLess(prompt.index("Mit dem Kunden"), prompt.index("«Intern»"))
        self.assertIn("[B3] ", prompt)                                         # тези другої — далі за номером
        self.assertIn("діє пізніша", prompt)
        self.assertEqual((r.refs["B1"]["meeting"], r.refs["B3"]["meeting"], r.refs["B3"]["tag"]),
                         (client.pk, internal.pk, "M2"))
        for m in (client, internal):                                          # згода — у протоколі кожної наради
            m.refresh_from_db()
            e = [x for x in m.consent_log if x.get("task") == "plan"]
            self.assertEqual((len(e), e[0]["meetings"]), (1, 2))
        v = plan_review.view(r, self.user)
        self.assertEqual([x["tag"] for x in v["meetings"]], ["M1", "M2"])
        add = next(x for x in v["rows"] if x["op"] == "add")
        self.assertEqual([s["tag"] for s in add["sources"]], ["M1", "M2"])
        self.assertIn(f"/meeting/{internal.pk}/page/", add["sources"][1]["url"])
        html = cockpit.render(self.project, self.user)
        self.assertIn("📅 Пропозиції з нарад", html)
        plan_review.apply(self.project, self.user, r)
        self.assertEqual(PlanChange.objects.get(step=self.a).meeting, client)
        self.assertEqual(PlanChange.objects.get(title="Freigabe Kunde").meeting, internal)   # пізніша теза
        e = plan_history.entries(self.project, self.user)[0]
        self.assertEqual([(x["tag"], x["title"]) for x in e["meetings"]], [("M1", "Mit dem Kunden"), ("M2", "Intern")])
        self.assertTrue(any(line.startswith("M2 · ") for line in e["lines"]))

    def test_consent_per_meeting(self):
        client, internal = self.two(consent=(True, False))
        with self.assertRaisesMessage(plan_review.ReviewError, "«Intern»"):     # у «Intern» згоди ще немає
            plan_review.start(self.project, self.user, [client.pk, internal.pk], MODEL, consent=False)
        with self.claude(self.answer()):                                      # у протоколі вже є згода — досить
            r = plan_review.start(self.project, self.user, [client.pk], MODEL, consent=False)
        self.assertEqual(r.status, PlanReview.READY, r.error)
        internal.refresh_from_db()
        self.assertFalse(internal.consent_log)                                # не вибрана — не передавалась

    def test_limits_and_foreign_meetings(self):
        self.assertEqual(plan_review.parse_pks("3, 2,3,x"), [3, 2])
        self.assertEqual(plan_review.parse_pks([5, "6"]), [5, 6])
        ms = [self.meeting(title=f"N{i}", days=i + 1) for i in range(plan_review.REVIEW_MEETINGS_MAX + 1)]
        with self.assertRaisesMessage(plan_review.ReviewError, "до 5 нарад"):
            plan_review.start(self.project, self.user, [m.pk for m in ms], MODEL, consent=True)
        bob = User.objects.create_user("bob", "bob@x.y", "p", is_staff=True)
        other = self.meeting(owner=bob)
        with self.assertRaises(plan_review.ReviewError):
            plan_review.start(self.project, self.user, [ms[0].pk, other.pk], MODEL, consent=True)
        with self.assertRaisesMessage(plan_review.ReviewError, "Виберіть нараду"):
            plan_review.start(self.project, self.user, "", MODEL, consent=True)
        self.assertFalse(PlanReview.objects.exists())

    def test_each_meeting_its_own_colour_on_both_gantts_and_in_the_list(self):
        client, internal = self.two()
        b = f"s{self.b.pk}"
        data = self.answer(changes=[
            {"op": "update", "id": f"s{self.a.pk}", "end": self.d(12).isoformat(), "from": ["B1"]},
            {"op": "add", "ref": "a1", "place_after": b, "title": "Freigabe Kunde", "kind": "milestone",
             "end": self.d(25).isoformat(), "after": [b], "from": ["B2", "B3"]}])
        with self.claude(data):
            r = plan_review.start(self.project, self.user, [client.pk, internal.pk], MODEL, consent=True)
        gh = plan_review.ghosts(r)
        step = gh["steps"][self.a.pk]
        self.assertEqual((step["m"], step["tag"]), (1, "M1"))
        self.assertTrue(step["label"].startswith("M1 → "))
        added = gh["added"][0]
        self.assertEqual((added["m"], added["tag"]), (2, "M2"))                # обидві тези — колір пізнішої
        self.assertEqual([(x["m"], x["tag"]) for x in gh["legend"]], [(1, "M1"), (2, "M2")])
        p = schedule.compute(self.project, today=self.today)
        svg = gantt.render(self.project, p, self.today, ghosts=gh)
        self.assertIn('class="pjg-prop pjm-1"', svg)
        self.assertIn('pjg-ghost-row pjm-2"', svg)
        rows = gantt.classic_data(self.project, p, self.today, ghosts=gh)["rows"]
        self.assertEqual(next(x for x in rows if x.get("ghost"))["m"], 2)
        self.assertEqual(next(x for x in rows if x["type"] == "step" and x["id"] == self.a.pk)["prop"]["m"], 1)
        v = plan_review.view(r, self.user)
        self.assertEqual([(x["op"], x["m"]) for x in v["rows"]], [("update", 1), ("add", 2)])
        html = cockpit.render(self.project, self.user)
        self.assertIn('class="pjr2-tag pjm-2"', html)
        self.assertIn("pjr2-several", html)
        self.assertIn("Кожна нарада — своїм кольором", html)
        _m, one = self.reviewed()                                             # одна нарада — як було, без міток
        self.assertEqual({(g["m"], g["tag"]) for g in plan_review.ghosts(one)["steps"].values()}, {(1, "")})
        self.assertEqual(plan_review.ghosts(one)["legend"], [])

    def test_block_checkboxes_url_and_endpoint(self):
        client, internal = self.two(consent=(True, True))
        html = cockpit.render(self.project, self.user)
        self.assertEqual(html.count('data-pjr2-meeting value="'), 2)
        self.assertIn("☁️ згода є", html)
        self.assertEqual(plan_review.review_url(self.project, [client, internal.pk]),
                         f"/admin/projects/project/{self.project.pk}/change/?plan_meeting={client.pk},{internal.pk}#pj-plan")
        base = f"/projects/api/project/{self.project.pk}/plan/review/"
        with self.claude(self.answer(changes=[
                {"op": "update", "id": f"s{self.a.pk}", "end": self.d(12).isoformat(), "from": ["B3"]}])):
            j = self.post(base, {"meetings": [client.pk, internal.pk], "model": MODEL}).json()
        self.assertTrue(j["ok"], j)
        self.assertIn("Пропозиції з нарад", j["html"])
        self.assertEqual(PlanReview.objects.get(pk=j["review"]).meetings.count(), 2)


class ExportAndLayoutTests(ReviewBase):
    """📤 Експорт з пропозиціями з наради (сторінка PDF, аркуш Excel, рядок листа), ▸ блок пропозицій, ⛶ на весь
    екран, діаграми без пропозицій для експорту."""

    def send(self, **data):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (400, 160), "white").save(buf, "PNG")
        body = {"what": "plan", "lang": "de", "formats": "xlsx", "image": SimpleUploadedFile(
            "d.png", buf.getvalue(), content_type="image/png"), **data}
        return self.client.post(reverse("projects:export", args=[self.project.pk]), body)

    def test_proposals_in_the_document_language_only_for_the_reviewer(self):
        from projects import export
        self.reviewed()
        de = export.proposals(self.project, self.user, "de")
        self.assertEqual(len(de["rows"]), 2)
        upd, add = de["rows"]
        self.assertEqual(upd["title"], "Muster bauen")
        self.assertIn(f"Termine {self.d(1):%d.%m.%Y} – {self.d(5):%d.%m.%Y} → {self.d(1):%d.%m.%Y} – {self.d(12):%d.%m.%Y}",
                      upd["change"])
        self.assertIn(f"neuer Meilenstein ◆ {self.d(20):%d.%m.%Y}", add["change"])
        self.assertEqual(upd["quotes"], [])
        self.assertIn("Muster kommt", export.proposals(self.project, self.user, "en", quotes=True)["rows"][0]["quotes"][0])
        self.assertIn("нова віха", export.proposals(self.project, self.user, "uk")["rows"][1]["change"])
        bob = User.objects.create_superuser("bob", "bob@x.y", "p")
        self.assertIsNone(export.proposals(self.project, bob, "de"))

    def test_excel_sheet_and_mail_line_only_with_the_option(self):
        import io

        from openpyxl import load_workbook

        from email_assistant.models import EmailDraft
        self.reviewed()
        wb = load_workbook(io.BytesIO(self.send(props="1").content))
        self.assertIn("Änderungen", wb.sheetnames)
        ch = wb["Änderungen"]
        self.assertEqual(ch["B5"].value, "Muster bauen")
        self.assertIn("Termine", ch["C5"].value)
        self.assertNotIn("Änderungen", load_workbook(io.BytesIO(self.send().content)).sheetnames)
        url = self.send(props="1", send="1").json()["compose"]
        draft = EmailDraft.objects.get(pk=int(url.rsplit("=", 1)[1]))
        self.assertIn("Gestrichelt markierte Termine", draft.body)
        url = self.send(send="1").json()["compose"]
        self.assertNotIn("Gestrichelt", EmailDraft.objects.get(pk=int(url.rsplit("=", 1)[1])).body)

    def test_pdf_with_the_proposals_page(self):
        from importlib.util import find_spec
        if find_spec("reportlab") is None:
            self.skipTest("reportlab не встановлено")
        self.reviewed()
        r = self.send(formats="pdf", table="1", props="1", quotes="1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_tall_diagram_fits_the_pdf_page(self):
        """Довгий план (висока картинка) — раніше «LayoutError: … too large on page» (поля рамки не віднімались)."""
        import io
        from importlib.util import find_spec

        from PIL import Image

        from projects import export
        if find_spec("reportlab") is None:
            self.skipTest("reportlab не встановлено")
        buf = io.BytesIO()
        Image.new("RGB", (900, 1400), "white").save(buf, "PNG")
        for titled in (True, False):
            self.assertTrue(export.pdf(self.project, "plan", "de", buf.getvalue(), table=True,
                                       titled=titled).startswith(b"%PDF"))

    def test_page_collapse_full_screen_and_clean_diagrams(self):
        b = f"s{self.b.pk}"
        self.reviewed(self.answer(changes=[
            {"op": "update", "id": f"s{self.a.pk}", "end": self.d(12).isoformat(), "from": ["B1", "B2"]},
            {"op": "add", "ref": "a1", "place_after": b, "title": "Freigabe Kunde", "kind": "milestone",
             "end": self.d(25).isoformat(), "after": [b], "from": ["B2"]}]))
        html = cockpit.render(self.project, self.user)
        self.assertIn("data-pjr2-box", html)                                   # ▸ / ▾ блок пропозицій
        self.assertIn("Ще тези наради: 1", html)                              # друга теза — під «ще»
        self.assertIn("data-pjg-full", html)                                  # ⛶ на весь екран
        self.assertIn('id="pjg-clean-full"', html)
        clean = html.split('id="pjg-clean-full">', 1)[1].split("</template>", 1)[0]
        self.assertIn("<svg", clean)
        self.assertNotIn("pjg-ghost", clean)                                  # для експорту без пропозицій
        self.assertIn("lg-prop", html)
        p = schedule.compute(self.project, today=self.today)
        data = gantt.classic_data(self.project, p, self.today, ghosts=plan_review.ghosts(plan_review.active(self.project, self.user)))
        self.assertEqual([(m["m"], m["tag"]) for m in data["props"]], [(1, "")])
        plan_review.reject(self.project, self.user, plan_review.active(self.project, self.user))
        html = cockpit.render(self.project, self.user)
        self.assertNotIn('id="pjg-clean-full"', html)                         # немає пропозицій — немає й копії
        self.assertIn("data-pjg-full", html)
