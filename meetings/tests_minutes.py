"""✉️ Підсумок наради листом (meetings/minutes.py):
python manage.py test meetings.tests_minutes --settings=tabele.settings_test

Лист — HTML з таблицею «Пункт агенди → Результат» (стан як відмічено на сторінці) і таблицями завдань та термінів,
без посилань на розшифровку, усе екрановано; частини — на вибір; мова: без перекладу — як склали, з «🌐» — переклад
підсумку й агенди (запам'ятовується, у протоколі згоди), другий раз без запиту AI; чернетка у власній скриньці з
підписом; без скриньки — відмова; адреса лише POST і власнику.
"""
import json
from unittest.mock import patch

from meetings import agenda, ai_brief, minutes
from meetings.models import MeetingTranscript
from meetings.services import MeetingError
from meetings.tests_ai_brief import BriefBase, answer


class MinutesBase(BriefBase):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.owner, email_address="slava@sevskiy.example",
                                                   signature="Slava <b>Sevskiy</b>")

    def ready(self):
        m, ids = self.made()
        ai_brief.decide(self.owner, m, "accept_all")
        agenda.edit(self.owner, m, ids[4], note="Ціну <уточнюємо>")
        m.refresh_from_db()
        return m, ids

    def fake_translate(self, lang_tag):
        calls = []

        def tr(user, texts, lang, model=None, about=""):
            calls.append(list(texts))
            return [f"[{lang_tag}] {t}" for t in texts]
        return patch("core.ai_texts.translate_list", side_effect=tr), calls


class ComposeTests(MinutesBase):
    def test_html_has_agenda_results_tasks_and_no_links(self):
        m, (a1, a2, a21, a22, a3) = self.ready()
        mail = minutes.compose(self.owner, m, "uk", minutes.PARTS)
        html = mail["html"]
        self.assertIn("Підсумок наради:", mail["subject"])
        self.assertIn("📋 Агенда й результати", html)
        self.assertIn("✅ обговорили", html)                        # пункт 1 — ☑
        self.assertIn("Антени до п’ятниці.", html)
        self.assertIn("🟡 ще відкрито", html)                        # пункт 3 — відкритий з «що вирішили»
        self.assertIn("⏭ не обговорили — на наступну нараду", html)  # 2.2
        self.assertIn("Ціну &lt;уточнюємо&gt;", html)               # екрановано
        self.assertNotIn("<уточнюємо>", html)
        self.assertIn(">Хто<", html)
        self.assertIn("Sevskiy", html)
        self.assertIn("до понеділка", html)
        self.assertNotIn("#t=", html)                               # без посилань на розшифровку
        self.assertNotIn("class=", html)
        self.assertIn("2.1. Дата креслення — ✅ обговорили: Креслення в понеділок.", mail["text"])

    def test_parts_choice(self):
        m, _ids = self.ready()
        mail = minutes.compose(self.owner, m, "uk", ["agenda"])
        self.assertIn("📋 Агенда й результати", mail["html"])
        self.assertNotIn("🤝 Про що домовились", mail["html"])
        self.assertNotIn("Нарада з Client GmbH", mail["html"])
        with self.assertRaisesMessage(MeetingError, "Виберіть, що буде"):
            minutes.compose(self.owner, m, "uk", ["nonsense"])
        with self.assertRaises(MeetingError):
            minutes.compose(self.owner, m, "fr", ["agenda"])

    def test_language_without_and_with_translation(self):
        m, _ids = self.ready()
        mail = minutes.compose(self.owner, m, "en", ["about", "agenda", "tasks"])
        self.assertIn("Meeting summary:", mail["subject"])
        self.assertIn("✅ discussed", mail["html"])
        self.assertIn("Нарада з Client GmbH", mail["html"])          # без перекладу — як склали
        fake, calls = self.fake_translate("EN")
        with fake:
            mail = minutes.compose(self.owner, m, "en", ["about", "agenda", "tasks"], ai=True)
        self.assertIn("[EN] Нарада з Client GmbH", mail["html"])
        self.assertIn("[EN] Поставка антен", mail["html"])            # пункт агенди — теж
        self.assertTrue(any(e.get("task") == "translate" for e in
                            type(m).objects.get(pk=m.pk).consent_log if e.get("kind") == "cloud"))
        brief = MeetingTranscript.objects.get(meeting=m).brief
        self.assertIn("en", brief["tr"])
        before = len(calls)
        with fake:
            minutes.compose(self.owner, type(m).objects.get(pk=m.pk), "en", ["about", "tasks"], ai=True)
        self.assertEqual(len(calls), before)                           # уже перекладено — без запиту
        form = minutes.form(self.owner, type(m).objects.get(pk=m.pk))
        self.assertTrue(next(x for x in form["langs"] if x["code"] == "en")["ready"])

    def test_form_defaults(self):
        m, _ids = self.made()
        form = minutes.form(self.owner, m)
        parts = {p["key"]: p["on"] for p in form["parts"]}
        self.assertTrue(parts["about"] and parts["agenda"] and parts["agreements"] and parts["tasks"])
        self.assertFalse(parts["topics"])                               # агенда є — «що обговорили» не за замовчуванням
        self.assertNotIn("risks", parts)                                # ризиків немає — і галочки немає
        self.assertEqual(form["waiting"], 3)
        self.assertEqual([p["email"] for p in form["people"]], ["max@client.example"])


class SendTests(MinutesBase):
    def test_draft_with_signature_and_refusals(self):
        from email_assistant.models import EmailDraft
        m, _ids = self.ready()
        url = minutes.send(self.owner, m, ["max@client.example", "boss@x.example"], "de",
                           ["about", "agenda", "deadlines"])
        draft = EmailDraft.objects.get(pk=int(url.rsplit("=", 1)[1]))
        self.assertEqual(draft.to_emails, ["max@client.example", "boss@x.example"])
        self.assertIn("Besprechungsprotokoll", draft.subject)
        self.assertIn("Tagesordnungspunkt", draft.body_html)
        self.assertIn('class="em-sig-in"', draft.body_html)
        self.assertIn("Slava <b>Sevskiy</b>", draft.body_html)
        self.assertIn("✅ besprochen", draft.body)
        with self.assertRaisesMessage(MeetingError, "неправильна"):
            minutes.send(self.owner, m, ["not an address"], "uk", ["agenda"])
        self.account.delete()
        with self.assertRaisesMessage(MeetingError, "немає вашої поштової скриньки"):
            minutes.send(self.owner, m, [], "uk", ["agenda"])
        from django.http import Http404
        with self.assertRaises(Http404):
            minutes.send(self.other, m, [], "uk", ["agenda"])

    def test_page_form_and_endpoint(self):
        m, _ids = self.ready()
        self.login(self.owner)
        page = self.client.get(self.url("page", m))
        self.assertContains(page, "✉️ Надіслати підсумок листом…")
        self.assertContains(page, 'name="part" value="agenda" checked')
        url = self.url("brief_send", m)
        self.assertEqual(self.client.get(url).status_code, 405)
        r = self.client.post(url, json.dumps({"to": ["max@client.example"], "extra": "", "lang": "uk",
                                              "parts": ["agenda", "tasks"]}), content_type="application/json")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertTrue(r.json()["compose"].startswith("/email/compose/?draft="))
        self.login(self.other)
        self.assertEqual(self.client.post(url, json.dumps({"lang": "uk", "parts": ["agenda"]}),
                                          content_type="application/json").status_code, 404)
