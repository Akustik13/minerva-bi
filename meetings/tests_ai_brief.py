"""📝 Підсумок наради тезами й звірка з агендою — Claude у Minerva (meetings/ai_brief.py):
python manage.py test meetings.tests_ai_brief --settings=tabele.settings_test

Без галочки згоди, розшифровки чи ключа — відмова; у підсумку лише відомі розділи й номери рядків, що є в розшифровці;
звірка — лише пункти агенди цієї наради; ✓ пункту — ☑ і «що вирішили» (написане людиною не перезаписується), пункт
з підпунктами закривається разом з ними, «частково» — лише текст, ✕ — більше не показується; «✓ Прийняти всі»;
протокол згоди — «підсумовано й звірено з агендою»; сторінка — картка підсумку, підказки під пунктами, адреса лише
POST і власнику; галочка згоди вже стоїть, якщо учасники погодились на хмару раніше; підсумок — частина бази знань.
"""
import json
from unittest.mock import patch

from django.urls import reverse

from meetings import admin_pages, agenda, ai_brief
from meetings.models import MeetingAgendaItem, MeetingTranscript
from meetings.services import MeetingError
from meetings.tests_pages import PagesBase


def answer(ids, **over):
    a1, a2, a21, a22, a3 = ids
    data = {
        "about": "Нарада з Client GmbH про поставку антен.",
        "topics": [{"text": "Поставка антен до Кельна", "lines": [3]},
                   {"text": "Поставка антен до Кельна", "lines": [3]},                       # повтор — геть
                   {"text": "Креслення", "lines": [999]}],                                   # рядка немає — без посилання
        "agreements": [{"text": "Антени — до п’ятниці", "who": "Client GmbH", "lines": [3]}],
        "tasks": [{"text": "Надіслати креслення", "who": "Sevskiy", "due": "до понеділка", "lines": [4]}],
        "deadlines": [], "facts": [], "risks": "не список", "open": [{"text": ""}],
        "secret": [{"text": "невідомий розділ"}],
        "agenda": [
            {"id": a1, "status": "done", "result": "Антени до п’ятниці.", "lines": [3]},
            {"id": a21, "status": "done", "result": "Креслення в понеділок.", "lines": [4]},
            {"id": a22, "status": "no", "result": "щось", "lines": [4]},
            {"id": a3, "status": "partly", "result": "Ціну ще уточнюємо.", "lines": [3]},
            {"id": 999999, "status": "done", "result": "чужий пункт"},
            {"id": a2, "status": "weird"},
        ],
    }
    data.update(over)
    return data


class BriefBase(PagesBase):
    def setUp(self):
        super().setUp()
        from core.shared_cache import cache
        cache.clear()
        ai_brief.BACKGROUND = False
        self.addCleanup(setattr, ai_brief, "BACKGROUND", True)
        avail = patch("meetings.ai_moments.available", return_value=True)
        avail.start()
        self.addCleanup(avail.stop)
        self.calls = []

    def setup_agenda(self, m):
        a1 = agenda.add(self.owner, m, "Поставка антен")
        a2 = agenda.add(self.owner, m, "Креслення")
        a21 = agenda.add(self.owner, m, "Дата креслення", parent=a2.pk)
        a22 = agenda.add(self.owner, m, "Формат файлів", parent=a2.pk)
        a3 = agenda.add(self.owner, m, "Ціна")
        return [x.pk for x in (a1, a2, a21, a22, a3)]

    def claude(self, data):
        def call(prompt, *a, **k):
            self.calls.append(prompt)
            return "Ось JSON: " + json.dumps(data, ensure_ascii=False)
        return patch.multiple("email_assistant.ai_helper", _call_ai_direct=call,
                              last_usage=lambda: None, last_model=lambda: "claude-sonnet-5-5")

    def made(self, lang="uk"):
        m = self.processed(items=None)
        ids = self.setup_agenda(m)
        with self.claude(answer(ids)):
            st = ai_brief.start(m, self.owner, "claude-sonnet-5-5", consent=True, lang=lang)
        self.assertEqual(st["state"], "done", st)
        m.refresh_from_db()
        return m, ids


class ServiceTests(BriefBase):
    def test_refusals(self):
        m = self.processed(items=None)
        with self.assertRaisesMessage(MeetingError, "Поставте галочку"):
            ai_brief.start(m, self.owner, "claude-sonnet-5-5", consent=False)
        with self.assertRaises(MeetingError):
            ai_brief.start(m, self.owner, "gpt-5", consent=True)
        with self.assertRaises(MeetingError):
            ai_brief.start(m, self.owner, "claude-sonnet-5-5", consent=True, lang="ru")
        with patch("meetings.ai_moments.available", return_value=False), \
                self.assertRaisesMessage(MeetingError, "немає ключа Anthropic"):
            ai_brief.start(m, self.owner, "claude-sonnet-5-5", consent=True)
        with self.assertRaisesMessage(MeetingError, "Розшифровки ще немає"):
            ai_brief.start(self.meeting(), self.owner, "claude-sonnet-5-5", consent=True)
        from django.http import Http404
        with self.assertRaises(Http404):
            ai_brief.start(m, self.other, "claude-sonnet-5-5", consent=True)

    def test_brief_is_cleaned_and_logged(self):
        m, ids = self.made()
        prompt = self.calls[0]
        self.assertIn(f"[id {ids[2]}] 2.1. Дата креслення", prompt)          # агенда з номерами й id
        self.assertIn("[#3]", prompt)                                         # рядки розшифровки
        self.assertIn("Client GmbH", prompt)                                  # інша сторона
        self.assertIn("Ukrainian", prompt)
        brief = MeetingTranscript.objects.get(meeting=m).brief
        self.assertEqual(set(brief["sections"]), set(ai_brief.SECTION_KEYS))
        self.assertEqual([x["text"] for x in brief["sections"]["topics"]], ["Поставка антен до Кельна", "Креслення"])
        self.assertEqual(brief["sections"]["topics"][1]["lines"], [])
        self.assertEqual(brief["sections"]["risks"], [])
        self.assertEqual(brief["sections"]["open"], [])
        self.assertNotIn(str(999999), brief["agenda"])
        self.assertNotIn(str(ids[1]), brief["agenda"])                         # невідомий стан — геть
        self.assertEqual(brief["agenda"][str(ids[3])]["result"], "")           # «не обговорили» — без тексту
        self.assertEqual(brief["label"], "Claude Sonnet 5.5")
        cloud = [e for e in m.consent_log if e.get("kind") == "cloud"]
        self.assertEqual((cloud[-1]["task"], cloud[-1]["where"]), ("brief", "minerva"))
        self.assertTrue(any("підсумовано й звірено з агендою" in x["text"] for x in admin_pages.log_lines(m)))
        view = ai_brief.view(m)
        self.assertEqual(view["sections"][0]["rows"][0]["time"], "02:00")
        self.assertIn("• Надіслати креслення (Sevskiy · до понеділка) [02:11]", view["text"])
        self.assertIn("🤝 Про що домовились", view["text"])

    def test_language_and_bad_answer(self):
        m = self.processed(items=None)
        with patch.multiple("email_assistant.ai_helper", _call_ai_direct=lambda *a, **k: "не JSON",
                            last_usage=lambda: None, last_model=lambda: ""):
            st = ai_brief.start(m, self.owner, "claude-haiku-5-5", consent=True)
        self.assertEqual(st["state"], "error")
        self.assertIn("не відповів", st["error"])
        self.assertEqual(MeetingTranscript.objects.get(meeting=m).brief, {})
        with self.claude(answer([1, 2, 3, 4, 5])):
            ai_brief.start(m, self.owner, "claude-sonnet-5-5", consent=True, lang="en")
        self.assertIn("in English", self.calls[-1])
        self.assertEqual(MeetingTranscript.objects.get(meeting=m).brief["lang"], "en")

    def test_hints_and_accept_single(self):
        m, (a1, a2, a21, a22, a3) = self.made()
        rows = agenda.items(m)
        hints = ai_brief.hints(m, rows)
        self.assertEqual(hints[a1]["ok"], "Так, обговорили")
        self.assertEqual(hints[a1]["time"], "02:00")
        self.assertEqual(hints[a3]["ok"], "Записати")
        self.assertEqual(hints[a22]["ok"], "")                                 # не обговорили — лише довідка
        self.assertNotIn(a2, hints)
        ai_brief.decide(self.owner, m, "accept", a1)
        item = MeetingAgendaItem.objects.get(pk=a1)
        self.assertEqual((item.status, item.note), ("done", "Антени до п’ятниці."))
        ai_brief.decide(self.owner, m, "accept", a3)                            # частково — лише текст
        item = MeetingAgendaItem.objects.get(pk=a3)
        self.assertEqual((item.status, item.note), ("open", "Ціну ще уточнюємо."))
        self.assertEqual(ai_brief.hints(m, agenda.items(m)).keys() & {a1, a3}, set())
        ai_brief.decide(self.owner, m, "dismiss", a22)
        self.assertNotIn(a22, ai_brief.hints(m, agenda.items(m)))
        with self.assertRaises(MeetingError):
            ai_brief.decide(self.owner, m, "accept", 999999)
        from django.http import Http404
        with self.assertRaises(Http404):
            ai_brief.decide(self.other, m, "accept", a1)

    def test_person_note_is_kept_and_parent_follows_kids(self):
        m, (a1, a2, a21, a22, a3) = self.made()
        agenda.edit(self.owner, m, a1, note="Моє рішення")
        self.assertTrue(ai_brief.hints(m, agenda.items(m))[a1]["note_kept"])
        changed = ai_brief.decide(self.owner, m, "accept_all")
        self.assertEqual(changed, 3)
        item = MeetingAgendaItem.objects.get(pk=a1)
        self.assertEqual((item.status, item.note), ("done", "Моє рішення"))
        self.assertEqual(MeetingAgendaItem.objects.get(pk=a21).status, "done")
        self.assertEqual(MeetingAgendaItem.objects.get(pk=a22).status, "open")     # не обговорили — лишається
        self.assertEqual(MeetingAgendaItem.objects.get(pk=a2).status, "open")      # пункт — поки відкритий підпункт
        agenda.set_status(self.owner, m, a22, "done")
        self.assertEqual(MeetingAgendaItem.objects.get(pk=a2).status, "done")

    def test_new_brief_replaces_old_and_brief_goes_to_knowledge(self):
        m, ids = self.made()
        ai_brief.decide(self.owner, m, "dismiss", ids[0])
        with self.claude(answer(ids, about="Друга версія")):
            ai_brief.start(m, self.owner, "claude-sonnet-5-5", consent=True)
        brief = MeetingTranscript.objects.get(meeting=m).brief
        self.assertEqual(brief["about"], "Друга версія")
        self.assertEqual(brief["agenda"][str(ids[0])]["st"], "")
        from meetings import knowledge
        part = knowledge.brief_part(m)
        self.assertIn("· підсумок", part[0])
        self.assertIn("Друга версія", part[1])
        self.assertEqual(part[2], self.project)

    def test_merge_long_meeting(self):
        one = {"about": "A", "sections": {k: [] for k in ai_brief.SECTION_KEYS},
               "agenda": {"1": {"status": "partly", "result": "x", "lines": [1], "st": ""}}}
        two = {"about": "B", "sections": {k: [] for k in ai_brief.SECTION_KEYS},
               "agenda": {"1": {"status": "done", "result": "y", "lines": [5], "st": ""},
                          "2": {"status": "no", "result": "", "lines": [], "st": ""}}}
        one["sections"]["topics"] = [{"text": "T", "who": "", "due": "", "lines": [1]}]
        two["sections"]["topics"] = [{"text": "t", "who": "", "due": "", "lines": [5]}]
        out = ai_brief.merge([one, two])
        self.assertEqual(out["about"], "A B")
        self.assertEqual(len(out["sections"]["topics"]), 1)
        self.assertEqual(out["agenda"]["1"], {"status": "done", "result": "x; y", "lines": [1, 5], "st": ""})
        self.assertEqual(out["agenda"]["2"]["status"], "no")


class PageTests(BriefBase):
    def test_card_hints_endpoints_and_consent_precheck(self):
        m = self.processed()
        ids = self.setup_agenda(m)
        self.login(self.owner)
        page = self.client.get(self.url("page", m))
        self.assertContains(page, "Kurz: Lieferung")                           # короткий підсумок RAG-ПК
        self.assertContains(page, "📝 Підсумок наради")
        self.assertContains(page, "📝 Скласти підсумок і звірити з агендою")
        self.assertNotContains(page, 'id="mt-brief-consent" checked')
        url = self.url("brief", m)
        self.assertEqual(self.client.get(url).status_code, 405)
        with self.claude(answer(ids)):
            r = self.client.post(url, json.dumps({"model": "claude-sonnet-5-5", "consent": True, "lang": "uk"}),
                                 content_type="application/json")
        self.assertEqual(r.json()["state"]["state"], "done")
        self.assertEqual(self.client.get(self.url("status", m)).json()["ai_brief"]["state"], "done")
        page = self.client.get(self.url("page", m))
        self.assertContains(page, "Нарада з Client GmbH про поставку антен.")
        self.assertContains(page, "🔄 Скласти підсумок ще раз")
        self.assertContains(page, "🤖 <b>Звірка з розшифровкою</b>")
        self.assertContains(page, "✓ Прийняти всі пропозиції (3)")
        self.assertContains(page, 'data-ag-act="ai_ok"')
        self.assertContains(page, 'id="mt-brief-consent" checked')            # згода вже в протоколі
        self.assertContains(page, 'id="mt-ai-consent" checked')
        self.assertNotContains(page, "Kurz: Lieferung")                        # короткий підсумок RAG-ПК сховано
        r = self.client.post(self.url("agenda", m), json.dumps({"action": "ai_all"}), content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["changed"], 3)
        self.assertNotIn('data-ag-act="ai_ok"', r.json()["html"])
        self.login(self.other)
        self.assertEqual(self.client.post(url, json.dumps({"model": "claude-sonnet-5-5", "consent": True}),
                                          content_type="application/json").status_code, 404)

    def test_url_name(self):
        self.assertEqual(reverse("admin:meetings_meeting_brief", args=[3]), "/admin/meetings/meeting/3/brief/")
