"""🧠 AI-аналіз ризиків з підказкою людини, 📤 кілька файлів одним ZIP, 📋 фазу тягнуть мишкою (зсув кроків).
python manage.py test projects --settings=tabele.settings_test"""
import io
import json
import zipfile
from unittest.mock import patch

from django.urls import reverse

from projects import ai_risks, plan
from projects.models import ProjectPhase, ProjectRisk, ProjectStep, ProjectSuggestion
from projects.tests_ai import AIBase, answer
from projects.tests_parties import PartiesBase


class RiskAITests(AIBase):
    def run_ai(self, data, wish=""):
        fake, calls = answer(data)
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            res = ai_risks.analyse(self.project, self.user, wish)
        return res, calls

    def test_risks_become_suggestions_with_the_hint_in_the_prompt(self):
        ProjectRisk.objects.create(project=self.project, title="Клієнт затримає погодження", probability=2, impact=2)
        res, calls = self.run_ai({"summary": "Зразки в роботі, строк клієнту близько.", "risks": [
            {"title": "Єдине джерело модулів Cavli U3", "category": "supply", "probability": 3, "impact": 3,
             "why": "Усі U3 лише від Cavli, поставку вже переносили", "mitigation": "Знайти другого постачальника",
             "refs": [2]},
            {"title": "Клієнт затримає погодження", "category": "customer", "probability": 2, "impact": 2,
             "why": "вже є в реєстрі", "refs": []},
            {"title": "x", "category": "?", "probability": 9}]},
            wish="У Cavli єдине джерело модулів U3")
        self.assertEqual((res["found"], res["error"]), (1, ""))
        self.assertIn("Зразки в роботі", res["summary"])
        prompt = calls[0]["prompt"]
        self.assertIn("ВКАЗІВКА МЕНЕДЖЕРА", prompt)
        self.assertIn("У Cavli єдине джерело модулів U3", prompt)
        self.assertIn("УЖЕ ВІДОМІ РИЗИКИ", prompt)
        self.assertEqual(calls[0]["model"], ai_risks.AI_MODEL)
        s = ProjectSuggestion.objects.get(project=self.project)
        self.assertEqual((s.kind, s.category, s.probability, s.impact), (ProjectSuggestion.RISK, "supply", 3, 3))
        self.assertIsNotNone(s.message_id)                                       # [#2] — справжній лист
        self.assertEqual(s.created_by, self.user)
        res, _ = self.run_ai({"summary": "", "risks": [{"title": "Єдине джерело модулів Cavli U3", "refs": []}]})
        self.assertEqual(res["found"], 0)                                        # та сама пропозиція — не вдруге

    def test_accept_from_the_risks_tab_and_view(self):
        url = reverse("projects:ai_risks", args=[self.project.pk])
        fake, _ = answer({"summary": "Підсумок", "risks": [
            {"title": "Митниця затримає зразки для США", "category": "schedule", "probability": 2, "impact": 3,
             "why": "Зразки йдуть за межі ЄС", "mitigation": "Підготувати HS-коди заздалегідь"}]})
        with patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake), \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            r = self.client.post(url, json.dumps({"wish": "зразки в США"}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["summary"], "Підсумок")
        self.assertIn("Ризики від AI чекають рішення", j["html"])
        s = ProjectSuggestion.objects.get(project=self.project)
        r = self.client.post(reverse("projects:ai_suggestion", args=[self.project.pk, s.pk]),
                             json.dumps({"accept": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        risk = ProjectRisk.objects.get(project=self.project, title="Митниця затримає зразки для США")
        self.assertEqual((risk.source, risk.score), (ProjectRisk.AI, 6))

    def test_ai_failures_are_explained(self):
        res, _ = self.run_ai("not json")
        self.assertIn("не у форматі", res["error"])


class ExportZipTests(PartiesBase):
    def test_several_files_download_as_one_zip(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from projects.tests_parties import HAS_PDF, png_bytes
        self.step("Зразки", days=5, start=self.today)
        formats = "pdf,png,xlsx" if HAS_PDF else "png,xlsx"
        r = self.client.post(reverse("projects:export", args=[self.project.pk]),
                             {"what": "plan", "lang": "de", "formats": formats, "table": "1",
                              "image": SimpleUploadedFile("d.png", png_bytes(), content_type="image/png")})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn(".zip", r["Content-Disposition"])
        names = sorted(n.rsplit(".", 1)[1] for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist())
        self.assertEqual(names, sorted(formats.split(",")))


class PhaseShiftTests(PartiesBase):
    def test_dragging_a_phase_moves_its_open_steps(self):
        ph = ProjectPhase.objects.create(project=self.project, name="Прототип", order=10)
        a = self.step("Плати", days=5, start=self.d(2), end=self.d(6), phase=ph)
        done = self.step("Схема", days=2, start=self.d(-5), end=self.d(-4), phase=ph, status=ProjectStep.DONE)
        m = self.step("Зразок готовий", kind=ProjectStep.MILESTONE, end=self.d(7), phase=ph)
        r = self.client.post(reverse("projects:plan_phase_shift", args=[self.project.pk, ph.pk]),
                             json.dumps({"days": 7}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db(), done.refresh_from_db(), m.refresh_from_db()
        self.assertEqual((a.start, a.end), (self.d(9), self.d(13)))
        self.assertEqual((done.start, done.end), (self.d(-5), self.d(-4)))            # виконане — історія
        self.assertEqual(m.end, self.d(14))
        with self.assertRaises(plan.PlanError):
            plan.shift_phase(self.project, ph.pk, 0)
        self.assertEqual(self.client.post(reverse("projects:plan_phase_shift", args=[self.project.pk, ph.pk]),
                                          json.dumps({"days": "x"}), content_type="application/json").status_code,
                         400)


class HelpersTests(PartiesBase):
    def test_validate_keeps_only_known_message_refs(self):
        items = ai_risks.validate({"risks": [{"title": "Затримка плат", "refs": [1, 7], "category": "schedule"}]},
                                  [101], {})
        self.assertIsNone(items[0]["message"])                                  # #1 — не власний лист
        self.assertEqual(ai_risks.key_for("Затримка  плат"), ai_risks.key_for("затримка плат"))


class MailPeekTests(AIBase):
    """👁 Лист при наведенні в пульті: лише власні скриньки, новий текст без цитат, гілка — останній лист."""
    def test_own_message_and_thread(self):
        url = reverse("projects:mail_peek")
        j = self.client.get(url, {"m": self.reply.pk}).json()
        self.assertEqual((j["ok"], j["subject"]), (True, "Re: Anfrage 5G Antenne"))
        self.assertIn("Stecker MHF4", j["text"])
        self.assertNotIn("Alte Nachricht", j["text"])                         # цитата — ні
        j = self.client.get(url, {"t": self.reply.thread_id}).json()
        self.assertEqual(j["count"], 2)
        self.assertEqual(j["url"], f"/email/message/{self.reply.pk}/")       # останній лист гілки

    def test_foreign_mailbox_is_hidden(self):
        olga, _acc = self.colleague()
        self.client.force_login(olga)
        r = self.client.get(reverse("projects:mail_peek"), {"m": self.reply.pk})
        self.assertEqual(r.status_code, 404)


class TitledPdfTests(PartiesBase):
    def test_titled_image_skips_the_page_header(self):
        from projects import export
        from projects.tests_parties import HAS_PDF, png_bytes
        if not HAS_PDF:
            self.skipTest("reportlab не встановлено")
        from pypdf import PdfReader
        self.step("Зразки", days=5, start=self.today)
        data = export.pdf(self.project, "plan", "de", png_bytes(), table=True, titled=True)
        pages = PdfReader(io.BytesIO(data)).pages
        self.assertNotIn(self.project.code, pages[0].extract_text())          # заголовок — у картинці
        self.assertIn(self.project.code, pages[1].extract_text())             # таблиця — з назвою проєкту
