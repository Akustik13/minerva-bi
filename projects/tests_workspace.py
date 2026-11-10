"""🧭 Робочий стіл проєкту, жива шкала (дані для браузера), «Коротко / Детально» на діаграмі Ганта, компактні смуги
виконаних кроків, «📅 Листи для AI — з» і план з листування з підказкою людини (заміна наявного плану).

python manage.py test projects.tests_workspace --settings=tabele.settings_test
"""
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse

from projects import ai_plan, ai_scan, gantt, overview, schedule, timeline
from projects.models import ProjectStep
from projects import tests_ai
from projects.tests_ai import AIBase, answer

M = ProjectStep.MILESTONE


class WorkspaceTests(AIBase):
    def test_workspace_page_shows_the_cockpit_full_width(self):
        self.plan_two_phases()
        url = reverse("admin:projects_project_workspace", args=[self.project.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="pj-cockpit"', html)
        self.assertIn("projects/pj.js", html)
        self.assertIn("📝 Картка проєкту", html)
        self.assertIn('name="csrfmiddlewaretoken"', html)                           # POST-дії пульта
        card = self.client.get(reverse("admin:projects_project_change", args=[self.project.pk])).content.decode()
        self.assertIn(url, card)                                                     # «⤢ Робочий стіл» з картки
        self.assertEqual(self.client.get(reverse("admin:projects_project_workspace", args=[99999])).status_code, 404)

    def test_viewer_without_projects_cannot_open_it(self):
        clerk = self.viewer("clerk2", role="readonly", modules=("crm",))
        self.client.force_login(clerk)
        r = self.client.get(reverse("admin:projects_project_workspace", args=[self.project.pk]))
        self.assertIn(r.status_code, (302, 403))


class LiveTimelineTests(AIBase):
    def test_payload_has_lanes_events_and_ranges(self):
        self.plan_two_phases()
        facts = overview.collect(self.project)
        data = timeline.payload(self.project, facts, self.user)
        self.assertEqual([k for k, _ in data["lanes"]][:3], ["moments", "plan", "mail"])
        self.assertEqual(set(data["bounds"]), {"all", "3", "6", "12"})
        lanes = {e["lane"] for e in data["events"]}
        self.assertTrue({"plan", "mail"} <= lanes)
        mail = [e for e in data["events"] if e["lane"] == "mail"][0]
        self.assertRegex(mail["start"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("mails", mail)
        olga, _ = self.colleague()                                               # чужа скринька — без тем листів
        other = timeline.payload(self.project, overview.collect(self.project), olga)
        self.assertNotIn("Anfrage 5G Antenne", str(other["events"]))

    def test_cockpit_carries_the_payload(self):
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn('id="pjt-data"', html)
        self.assertIn("data-pjt-live", html)


class BriefGanttTests(AIBase):
    def test_brief_view_keeps_phases_and_milestones_only(self):
        self.plan_two_phases()
        plan = overview.collect(self.project)["plan"]
        full = gantt.render(self.project, plan, self.today, editable=True)
        brief = gantt.render(self.project, plan, self.today, editable=True, brief=True)
        self.assertIn("Muster messen", full)
        self.assertNotIn("Muster messen", brief)
        self.assertIn("Muster freigegeben", brief)                               # ◆ віха лишається
        self.assertIn("Prototyp", brief)
        self.assertIn('data-ppd="', full)                                        # перетягування в браузері
        html = self.client.get(self.url("cockpit", self.project.pk)).json()["html"]
        self.assertIn('data-pjg-detail="brief"', html)
        self.assertIn('data-pjg-detail-btn="full"', html)

    def test_done_step_without_start_is_not_stretched_from_an_old_planned_start(self):
        old = self.d(-200)
        s = self.step("Platine layouten", 3, start=old, end=old + timedelta(days=2), status=ProjectStep.DONE,
                      done_on=self.d(-10))
        c = schedule.compute(self.project, today=self.today).by_id[s.pk]
        self.assertEqual((c.start, c.end), (self.d(-12), self.d(-10)))
        s2 = self.step("Gehäuse", 3, start=self.d(-15), status=ProjectStep.DONE, done_on=self.d(-10))   # трохи довше — як є
        self.assertEqual(schedule.compute(self.project, today=self.today).by_id[s2.pk].start, self.d(-15))


class AIMailWindowTests(AIBase):
    def test_settings_save_since_and_start_and_scan_reads_only_newer_mails(self):
        self.assertEqual(ai_scan.unread(self.project, self.user).count(), 2)
        r = self.post(self.url("ai_settings", self.project.pk), {"since": self.d(-6).isoformat(),
                                                                 "start": self.d(-30).isoformat()})
        self.assertIn("🧠 AI читає листи проєкту з", r.json()["html"])
        self.project.refresh_from_db()
        self.assertEqual((self.project.ai_mail_since, self.project.start_date), (self.d(-6), self.d(-30)))
        self.assertEqual([m.pk for m in ai_scan.unread(self.project, self.user)], [self.reply.pk])   # 9 дн. тому — ні
        self.post(self.url("ai_settings", self.project.pk), {"since": ""})
        self.project.refresh_from_db()
        self.assertIsNone(self.project.ai_mail_since)


class AIWishTests(AIBase):
    plan_json = tests_ai.AIPlanTests.plan_json          # той самий JSON відповіді AI

    def draft(self, data=None, **kw):
        fake, calls = answer(data or self.plan_json())
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            return ai_plan.draft(self.project, self.user, **kw), calls

    def test_wish_goes_into_the_prompt_and_note_marks_done(self):
        data = self.plan_json()
        data["phases"][1]["steps"][0].update(done=self.d(-1).isoformat(), ref="note")     # «за підказкою»
        d, calls = self.draft(data, wish="Зустріч учора відбулась: клієнт погодив схему.", since=self.d(-6))
        prompt = calls[0]["prompt"]
        self.assertIn("ПОБАЖАННЯ МЕНЕДЖЕРА", prompt)
        self.assertIn("клієнт погодив схему", prompt)
        self.assertIn(f"Листи в даних — з {self.d(-6):%d.%m.%Y}", prompt)
        self.assertNotIn("Wir brauchen 500 Stück", prompt)                        # лист 9 дн. тому — до дати
        step = d["phases"][1]["steps"][0]
        self.assertEqual((step["done"], step["ref"]), (self.d(-1).isoformat(), {"n": 0, "note": True}))
        self.assertTrue(d["wish"])
        d2, _calls = self.draft(data)                                              # без підказки «note» не діє
        self.assertIsNone(d2["phases"][1]["steps"][0]["done"])

    def test_existing_plan_is_replaced_only_when_asked(self):
        self.plan_two_phases()
        with self.assertRaises(ai_plan.DraftError):
            self.draft()
        d, _calls = self.draft(replace=True, wish="Переробити")
        self.assertEqual((d["replace"], d["had"]), (True, 4))
        ai_plan.apply(self.project, self.user)
        titles = set(ProjectStep.objects.filter(project=self.project).values_list("title", flat=True))
        self.assertNotIn("Muster messen", titles)                                 # старий план замінено
        self.assertIn("Уточнити роз'єм", titles)

    def test_view_passes_wish_since_and_start(self):
        fake, calls = answer(self.plan_json())
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.post(self.url("ai_plan", self.project.pk), {"wish": "Додай фазу сертифікації CE",
                                                                 "since": self.d(-6).isoformat(),
                                                                 "start": self.d(-20).isoformat()})
        self.assertTrue(r.json()["ok"], r.content)
        self.assertIn("Додай фазу сертифікації CE", calls[0]["prompt"])
        self.project.refresh_from_db()
        self.assertEqual((self.project.ai_mail_since, self.project.start_date), (self.d(-6), self.d(-20)))
        self.assertEqual(r.json()["draft"]["since"], self.d(-6).isoformat())
