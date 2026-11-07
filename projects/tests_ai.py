"""Етап 5: AI-помічник проєкту — моменти й ризики з листів (цитата перевіряється в тексті листа, лише власні
скриньки, ✓ / ✗ лише тій людині, чиї листи), чернетка плану з листування (тривалості з минулих проєктів,
застосування), забуті листи (без AI і з бази знань), доручення кроку агенту. AI, RAG і агент підмінено.
python manage.py test projects --settings=tabele.settings_test"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User

from agent.models import AgentTask
from email_assistant.models import EmailAccount
from projects import ai_plan, ai_scan, delegate, forgotten, plan as pplan, services
from projects.models import (ProjectHighlight, ProjectLink, ProjectMailScan, ProjectRisk, ProjectStep,
                             ProjectSuggestion)
from projects.tests_portfolio import PortfolioBase

C, S, M = ProjectStep.CUSTOMER, ProjectStep.SUPPLIER, ProjectStep.MILESTONE


def answer(data):
    """Підміна виклику Claude: повертає JSON і запам'ятовує промпти."""
    calls = []

    def fake(prompt, max_tokens=0, user=None, model=""):
        calls.append({"prompt": prompt, "model": model})
        return json.dumps(data, ensure_ascii=False)
    return fake, calls


class AIBase(PortfolioBase):
    def setUp(self):
        super().setUp()
        self.reply = self.mail("Re: Anfrage 5G Antenne", sent=self.ago(5), in_reply_to=self.ask.message_id,
                               references=(self.ask.message_id,),
                               body="Wir haben uns für den Stecker MHF4 entschieden.\nBitte Muster bis 25.11.\n\n"
                                    "> Alte Nachricht: wir prüfen noch die Konkurrenz")
        self.assertEqual(self.reply.thread_id, self.ask.thread_id)

    def colleague(self, name="olga", role="manager", modules=("projects", "email_assistant")):
        u = self.viewer(name, role=role, modules=list(modules))
        acc = EmailAccount.objects.create(user=u, email_address=f"{name}@sevskiy.de", imap_username=name,
                                          imap_password="x", smtp_username=name, smtp_password="x")
        return u, acc

    def scan(self, items, user=None):
        fake, calls = answer({"items": items})
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            res = ai_scan.scan(self.project, user or self.user)
        return res, calls


class AIScanTests(AIBase):
    def test_only_quotes_from_the_letter_itself_survive(self):
        res, calls = self.scan([
            {"type": "moment", "mail": 2, "kind": "decision", "title": "Клієнт вибрав роз'єм MHF4",
             "date": "2000-01-01", "quote": "für den Stecker MHF4 entschieden"},
            {"type": "risk", "mail": 2, "category": "customer", "title": "Клієнт порівнює з конкурентами",
             "probability": 3, "impact": 3, "quote": "wir prüfen noch die Konkurrenz"},         # лише в цитаті
            {"type": "risk", "mail": 2, "category": "schedule", "title": "Строк зразків 25.11",
             "probability": 3, "impact": 2, "mitigation": "Замовити плати експресом", "quote": "Muster bis 25.11"},
            {"type": "moment", "mail": 1, "kind": "approval", "title": "Вигадка", "quote": "steht nirgends"},
            {"type": "moment", "mail": 7, "kind": "other", "title": "Не той лист", "quote": "Hello"},
        ])
        self.assertEqual((res["found"], res["read"], res["left"], res["error"]), (2, 2, 0, ""))
        self.assertEqual(calls[0]["model"], ai_scan.AI_MODEL)
        self.assertNotIn("prüfen noch die Konkurrenz", calls[0]["prompt"])      # AI бачить лише новий текст
        moment = ProjectSuggestion.objects.get(kind=ProjectSuggestion.MOMENT)
        self.assertEqual((moment.message, moment.moment_kind, moment.created_by), (self.reply, "decision", self.user))
        self.assertEqual(moment.date, self.ago(5).date())                         # дата поза межами → дата листа
        risk = ProjectSuggestion.objects.get(kind=ProjectSuggestion.RISK)
        self.assertEqual((risk.probability, risk.impact, risk.mitigation), (3, 2, "Замовити плати експресом"))
        self.assertEqual(ProjectMailScan.objects.filter(project=self.project).count(), 2)
        with patch("email_assistant.ai_helper._call_ai_direct") as call:            # усе прочитано — AI не кличемо
            self.assertEqual(ai_scan.scan(self.project, self.user)["read"], 0)
            call.assert_not_called()

    def test_accept_and_reject_belong_to_the_mailbox_owner(self):
        self.scan([
            {"type": "moment", "mail": 2, "kind": "decision", "title": "Роз'єм MHF4",
             "quote": "für den Stecker MHF4 entschieden"},
            {"type": "risk", "mail": 2, "category": "schedule", "title": "Строк зразків",
             "probability": 3, "impact": 3, "quote": "Muster bis 25.11"}])
        moment = ProjectSuggestion.objects.get(kind=ProjectSuggestion.MOMENT)
        risk = ProjectSuggestion.objects.get(kind=ProjectSuggestion.RISK)
        olga, _ = self.colleague()
        self.assertEqual(ai_scan.pending(self.project, olga), [])
        with self.assertRaises(ai_scan.ScanError):
            ai_scan.accept(self.project, olga, moment.pk)
        ai_scan.accept(self.project, self.user, moment.pk)
        h = ProjectHighlight.objects.get(project=self.project)
        self.assertEqual((h.source, h.kind, h.message, h.quote), (ProjectHighlight.AI, "decision", self.reply,
                                                                  "für den Stecker MHF4 entschieden"))
        ai_scan.accept(self.project, self.user, risk.pk)
        r = ProjectRisk.objects.get(project=self.project, source=ProjectRisk.AI)
        self.assertEqual((r.score, r.message, r.evidence, r.owner), (9, self.reply, "Muster bis 25.11", self.user))
        self.assertIsNotNone(r.review_on)
        with self.assertRaises(ai_scan.ScanError):
            ai_scan.accept(self.project, self.user, risk.pk)                        # уже вирішено

    def test_rejected_quote_is_not_offered_again_and_known_moments_are_skipped(self):
        items = [{"type": "moment", "mail": 2, "kind": "decision", "title": "Роз'єм MHF4",
                  "quote": "für den Stecker MHF4 entschieden"}]
        self.scan(items)
        s = ProjectSuggestion.objects.get()
        ai_scan.reject(self.project, self.user, s.pk)
        ProjectMailScan.objects.all().delete()                                      # прочитати ще раз
        self.assertEqual(self.scan(items)[0]["found"], 0)
        services.add_highlight(self.project, title="Інший момент", quote="Bitte Muster bis 25.11.",
                               message=self.reply, user=self.user)
        ProjectMailScan.objects.all().delete()
        res, _ = self.scan([{"type": "moment", "mail": 2, "kind": "deadline", "title": "Зразки до 25.11",
                             "quote": "Muster bis 25.11"}])
        self.assertEqual(res["found"], 0)                                           # людина вже позначила

    def test_only_own_mailboxes_are_read(self):
        olga, acc = self.colleague()
        from email_assistant.sync import save_message
        theirs = save_message(acc, {"uid": 900, "message_id": "<olga@x>", "in_reply_to": "", "references": [],
                                    "subject": "Intern", "from_email": "buyer@client.example", "from_name": "",
                                    "to_emails": ["olga@sevskiy.de"], "cc_emails": [], "attachments": [],
                                    "body_text": "Geheime Preisliste 12,40 EUR", "body_html": "", "is_read": True,
                                    "sent_at": self.ago(3)}, "INBOX")
        services.link_thread(self.project, theirs.thread, user=olga)
        self.assertNotIn(theirs.pk, set(ai_scan.unread(self.project, self.user).values_list("pk", flat=True)))
        _, calls = self.scan([])
        self.assertNotIn("Geheime Preisliste", calls[0]["prompt"])
        self.assertEqual(set(ai_scan.unread(self.project, olga).values_list("pk", flat=True)), {theirs.pk})

    def test_budget_limit_stops_before_the_ai_call(self):
        with patch("ai_assistant.budget_guard.check_budget", return_value=(False, "Ліміт бюджету AI вичерпано")), \
                patch("email_assistant.ai_helper._call_ai_direct") as call:
            res = ai_scan.scan(self.project, self.user)
        call.assert_not_called()
        self.assertEqual(res["error"], "Ліміт бюджету AI вичерпано")

    def test_views_and_cockpit_block(self):
        fake, _ = answer({"items": [{"type": "risk", "mail": 2, "category": "schedule", "title": "Строк зразків",
                                     "probability": 3, "impact": 3, "quote": "Muster bis 25.11"}]})
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.post(self.url("ai_scan", self.project.pk))
        self.assertEqual(r.status_code, 200)
        html = r.json()["html"]
        self.assertIn("🧠 Прочитано листів: 2 · нових пропозицій: 1", html)
        self.assertIn("Строк зразків", html)
        self.assertIn("Muster bis 25.11", html)
        s = ProjectSuggestion.objects.get()
        r = self.post(self.url("ai_suggestion", self.project.pk, s.pk), {"accept": True})
        self.assertIn("⚠️ Ризик додано: Строк зразків", r.json()["html"])
        viewer = self.viewer("seer", role="readonly", modules=("projects",))
        self.client.force_login(viewer)
        self.assertEqual(self.post(self.url("ai_scan", self.project.pk)).status_code, 404)
        from projects import cockpit
        self.assertFalse(cockpit.context(self.project, viewer)["ai"]["on"])


class AIPlanTests(AIBase):
    def past_steps(self):
        for days in (10, 12, 30):
            s = self.step("Виготовлення й доставка плат", days, project=self.other, side=S)
            start = self.d(-60)
            ProjectStep.objects.filter(pk=s.pk).update(status=ProjectStep.DONE, started_on=start,
                                                       done_on=start + __import__("datetime").timedelta(days=days - 1))

    def plan_json(self):
        return {"template": "Розробка антени під клієнта", "start": self.d(-20).isoformat(),
                "notes": "Клієнт хоче зразки до 25.11.",
                "phases": [
                    {"name": "Запит", "icon": "📋", "gate": "Вимоги зрозумілі", "steps": [
                        {"title": "Уточнити роз'єм", "days": 3, "side": "we", "kind": "task", "trigger": "",
                         "done": self.d(-5).isoformat(), "ref": "#2", "why": "Клієнт вибрав MHF4"},
                        {"title": "Надіслати Angebot", "days": 2, "side": "робот", "kind": "task",
                         "trigger": "offer_sent", "done": self.d(-2).isoformat(), "ref": "#99"}]},
                    {"name": "Прототип", "icon": "🔧", "steps": [
                        {"title": "Виготовлення й доставка плат", "days": 4, "side": "supplier", "kind": "task",
                         "trigger": "jlc_delivered"},
                        {"title": "Зайвий крок", "days": 1, "side": "we", "kind": "task", "trigger": "magic"},
                        {"title": "Зразки погоджено", "days": 9, "side": "customer", "kind": "milestone"}]},
                    {"name": "Порожня фаза", "steps": []}]}

    def draft(self, data=None):
        fake, calls = answer(data or self.plan_json())
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            return ai_plan.draft(self.project, self.user), calls

    def test_durations_are_medians_of_finished_steps_of_other_projects(self):
        self.past_steps()
        hist = ai_plan.durations(exclude_project=self.project)
        self.assertEqual(hist[ai_plan.norm_title("Виготовлення й доставка плат")][:2], (12, 3))
        self.assertEqual(ai_plan.durations(exclude_project=self.other), {})

    def test_draft_is_checked_and_applied_without_skipped_steps(self):
        self.past_steps()
        d, calls = self.draft()
        self.assertEqual(calls[0]["model"], ai_plan.AI_MODEL)
        self.assertIn("«Виготовлення й доставка плат»: 12 дн. · 3", calls[0]["prompt"])
        self.assertIn("Wir haben uns für den Stecker MHF4 entschieden", calls[0]["prompt"])
        self.assertEqual([p["name"] for p in d["phases"]], ["Запит", "Прототип"])          # порожня фаза — ні
        first, angebot = d["phases"][0]["steps"]
        self.assertEqual((first["done"], first["ref"]["message"]), (self.d(-5).isoformat(), self.reply.pk))
        self.assertEqual((angebot["side"], angebot["done"]), ("we", None))                  # невідомий лист — ні
        boards, extra, gate = d["phases"][1]["steps"]
        self.assertEqual((boards["days"], boards["history"]), (12, {"days": 12, "n": 3}))   # як у минулих
        self.assertEqual((extra["trigger"], gate["kind"], gate["days"]), ("", M, 0))
        n = ai_plan.apply(self.project, self.user, skip=[3], start=self.d(-20).isoformat())
        self.assertEqual(n, 4)
        steps = {s.title: s for s in ProjectStep.objects.filter(project=self.project)}
        self.assertNotIn("Зайвий крок", steps)
        self.assertEqual((steps["Уточнити роз'єм"].status, steps["Уточнити роз'єм"].done_on),
                         (ProjectStep.DONE, self.d(-5)))
        self.assertEqual(steps["Уточнити роз'єм"].notes, "🧠 Клієнт вибрав MHF4")
        self.assertEqual({s.source for s in steps.values()}, {ProjectStep.AI})
        self.assertTrue(steps["Зразки погоджено"].predecessor_links.exists())
        with self.assertRaises(ai_plan.DraftError):                     # чернетку застосовано — її більше немає
            ai_plan.apply(self.project, self.user)

    def test_draft_needs_an_empty_plan_and_own_mails_only(self):
        olga, acc = self.colleague()
        from email_assistant.sync import save_message
        theirs = save_message(acc, {"uid": 901, "message_id": "<olga2@x>", "in_reply_to": "", "references": [],
                                    "subject": "Intern", "from_email": "buyer@client.example", "from_name": "",
                                    "to_emails": ["olga@sevskiy.de"], "cc_emails": [], "attachments": [],
                                    "body_text": "Vertraulich: Rabatt 7 %", "body_html": "", "is_read": True,
                                    "sent_at": self.ago(3)}, "INBOX")
        services.link_thread(self.project, theirs.thread, user=olga)
        _, calls = self.draft()
        self.assertNotIn("Vertraulich", calls[0]["prompt"])
        ai_plan.apply(self.project, self.user)
        with self.assertRaises(ai_plan.DraftError):
            self.draft()

    def test_views(self):
        fake, _ = answer(self.plan_json())
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.post(self.url("ai_plan", self.project.pk))
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["draft"]["steps"], 5)
        r = self.post(self.url("ai_plan_apply", self.project.pk), {"skip": [0, 1], "start": self.d(-3).isoformat()})
        self.assertIn("🧠 План з листування складено: кроків 3", r.json()["html"])
        self.assertEqual(self.post(self.url("ai_plan_apply", self.project.pk)).status_code, 400)

    def test_apply_template_still_works_through_apply_plan(self):
        tpl = pplan.templates()[0]
        n = pplan.apply_template(self.project, tpl, self.d(-3), self.user)
        self.assertEqual(n, ProjectStep.objects.filter(project=self.project, source=ProjectStep.TEMPLATE).count())


class ForgottenTests(AIBase):
    def test_local_search_reasons_and_decisions(self):
        lost = self.mail("Muster AN220213-01C", body="Anbei die Messwerte für AN220213-01C", sent=self.ago(40))
        noise = self.mail("Newsletter", from_email="news@shop.example", body="Angebote der Woche", sent=self.ago(2))
        rows = forgotten.local(self.project, self.user)
        by = {r["thread"]: r for r in rows}
        self.assertIn(lost.thread_id, by)
        self.assertNotIn(self.ask.thread_id, by)                                   # уже в проєкті
        self.assertNotIn(noise.thread_id, by)
        self.assertTrue(any("артикул AN220213-01C" in x for x in by[lost.thread_id]["reasons"]))
        self.assertTrue(any("листування з" in x for x in by[lost.thread_id]["reasons"]))
        forgotten.decide(self.project, self.user, lost.thread_id, False)
        self.assertNotIn(lost.thread_id, {r["thread"] for r in forgotten.local(self.project, self.user)})
        self.assertEqual(ProjectLink.objects.get(project=self.project, thread=lost.thread).status,
                         ProjectLink.REJECTED)
        forgotten.decide(self.project, self.user, lost.thread_id, True)
        self.assertEqual(ProjectLink.objects.get(project=self.project, thread=lost.thread).status,
                         ProjectLink.CONFIRMED)

    def test_other_mailboxes_are_never_offered(self):
        olga, acc = self.colleague()
        from email_assistant.sync import save_message
        theirs = save_message(acc, {"uid": 902, "message_id": "<olga3@x>", "in_reply_to": "", "references": [],
                                    "subject": "AN220213-01C", "from_email": "buyer@client.example",
                                    "from_name": "", "to_emails": ["olga@sevskiy.de"], "cc_emails": [],
                                    "attachments": [], "body_text": "AN220213-01C", "body_html": "",
                                    "is_read": True, "sent_at": self.ago(3)}, "INBOX")
        self.assertNotIn(theirs.thread_id, {r["thread"] for r in forgotten.local(self.project, self.user)})
        with self.assertRaises(forgotten.ForgottenError):
            forgotten.decide(self.project, self.user, theirs.thread_id, True)

    def test_rag_rows_skip_linked_threads(self):
        lost = self.mail("Alte Anfrage", from_email="info@elsewhere.example", body="Antennen?", sent=self.ago(300))
        job = SimpleNamespace(hits=[
            {"kind": "email", "thread_id": lost.thread_id, "message_pk": lost.pk, "text": "> Antennen für Drohnen?"},
            {"kind": "email", "thread_id": self.ask.thread_id, "message_pk": self.ask.pk, "text": "schon im Projekt"},
            {"kind": "document", "thread_id": None, "message_pk": None, "text": "Datenblatt"}])
        rows = forgotten.rag_rows(self.project, self.user, job)
        self.assertEqual([r["thread"] for r in rows], [lost.thread_id])
        self.assertTrue(rows[0]["reasons"][0].startswith("🔎 база знань"))

    def test_views_without_rag(self):
        lost = self.mail("Muster AN220213-01C", body="AN220213-01C", sent=self.ago(40))
        with patch("projects.forgotten.rag_ready", return_value=False):
            r = self.post(self.url("forgotten", self.project.pk))
        j = r.json()
        self.assertEqual((j["ok"], j["rag"], j["done"]), (True, False, True))
        self.assertIn(lost.thread_id, [x["thread"] for x in j["rows"]])
        r = self.post(self.url("forgotten_decide", self.project.pk), {"thread": lost.thread_id, "accept": True})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.post(self.url("forgotten_decide", self.project.pk), {"thread": 99999}).status_code, 400)


class DelegateTests(AIBase):
    def supplier_step(self):
        po = self.po(supplier=self.supplier, expected=self.d(-3), product=self.antenna)
        link = services.link_object(self.project, purchase_order=po, user=self.user)
        s = self.step("Виготовлення й доставка плат", 10, start=self.d(-15), end=self.d(-6), side=S,
                      trigger="po_received")
        s.links.add(link)
        return s, po

    def test_presets_from_step_context(self):
        s, po = self.supplier_step()
        presets = {p["key"]: p for p in delegate.presets(s)}
        self.assertEqual(list(presets)[0], "remind_supplier")
        self.assertEqual(presets["remind_supplier"]["mail_to"], "sales@parts.example")
        self.assertIn(po.code or str(po.pk), presets["remind_supplier"]["instruction"])
        self.assertIn(self.project.code, presets["remind_supplier"]["instruction"])
        self.assertIn("AN220213-01C", presets["purchase"]["instruction"])
        self.assertEqual(list(presets)[-1], "custom")
        c = self.step("Тест у клієнта", 5, side=C)
        self.assertEqual(delegate.presets(c)[0]["mail_to"], "buyer@client.example")

    def test_create_links_the_task_and_starts_it(self):
        s, _ = self.supplier_step()
        with patch("agent.tasks.start", return_value=True) as start:
            task = delegate.create(s, self.user, {"instruction": "Нагадай постачальнику про плати PO-1, будь ласка",
                                                  "mail_to": "Sales <sales@parts.example>; x", "budget": "99",
                                                  "model": "gpt"})
        start.assert_called_once_with(task)
        self.assertEqual((task.owner, task.mail_to, task.budget_usd), (self.user, "sales@parts.example",
                                                                      delegate.BUDGET_MAX))
        self.assertEqual(task.model, "claude-haiku-5-5")
        self.assertEqual(list(s.agent_tasks.all()), [task])
        self.assertEqual(list(task.products.all()), [self.antenna])
        data = pplan.editor_data(self.project, __import__("projects.schedule", fromlist=["x"]).compute(self.project))
        mine = [x for x in data["steps"] if x["id"] == s.pk][0]
        self.assertEqual(mine["agent"][0]["id"], task.pk)
        with self.assertRaises(delegate.DelegateError):
            delegate.create(s, self.user, {"instruction": "Коротко"})

    def test_permission_and_views(self):
        s, _ = self.supplier_step()
        r = self.client.get(self.url("step_agent", self.project.pk, s.pk))
        self.assertTrue(r.json()["can"])
        self.assertEqual(r.json()["presets"][0]["key"], "remind_supplier")
        with patch("agent.tasks.start", return_value=True):
            r = self.post(self.url("step_agent", self.project.pk, s.pk),
                          {"instruction": "Нагадай постачальнику про плати, будь ласка", "mail_to": ""})
        j = r.json()
        task = AgentTask.objects.get()
        self.assertEqual(j["url"], f"/admin/agent/agenttask/{task.pk}/review/")
        self.assertIn(f"🤖 Завдання агенту #{task.pk}", j["html"])
        nobody = self.viewer("pm", role="manager", modules=["projects"])
        self.assertFalse(nobody.has_perm("agent.add_agenttask"))
        with self.assertRaises(delegate.DelegateError):
            delegate.create(s, nobody, {"instruction": "Нагадай постачальнику про плати, будь ласка"})
        self.assertFalse(User.objects.get(pk=nobody.pk).has_perm("agent.add_agenttask"))
