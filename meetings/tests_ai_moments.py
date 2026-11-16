"""🔄 Ключові моменти ще раз — Claude у Minerva (meetings/ai_moments.py) і хмара на RAG-ПК у протоколі згоди:
python manage.py test meetings.tests_ai_moments --settings=tabele.settings_test

Без галочки згоди, розшифровки чи ключа — відмова; пункти лише з дослівною цитатою; пункти, що чекають ✓ / ✕,
замінюються новими, прийняті лишаються; протокол згоди — «☁️ … у Minerva / на RAG-ПК»; сторінка — блок з моделями,
адреса лише POST, власнику з правом змінювати; «не шукали на RAG-ПК» (model skip).
"""
import json
from unittest.mock import patch

from django.urls import reverse

from meetings import admin_pages, ai_moments, services
from meetings.models import Meeting, MeetingItem
from meetings.services import MeetingError
from meetings.tests_pages import ITEMS, PagesBase, SEGMENTS

ANSWER = {
    "summary": "Domovleno: antenny do piatnytsi.",
    "items": [
        {"kind": "decision", "text": "Антени до п’ятниці", "line": 3, "quote": "liefern die Antennen bis Freitag"},
        {"kind": "task", "text": "Креслення до понеділка", "line": 4, "quote": "schicke die Zeichnung bis Montag"},
        {"kind": "risk", "text": "Вигадане", "line": 2, "quote": "das wurde nie gesagt in der Besprechung"},
    ],
}


class AiMomentsBase(PagesBase):
    def setUp(self):
        super().setUp()
        from core.shared_cache import cache
        cache.clear()                                   # хід пошуку — за номером наради, номери в тестах повторюються
        ai_moments.BACKGROUND = False
        self.addCleanup(setattr, ai_moments, "BACKGROUND", True)
        avail = patch("meetings.ai_moments.available", return_value=True)
        avail.start()
        self.addCleanup(avail.stop)

    def claude(self, answer=ANSWER):
        return patch.multiple("email_assistant.ai_helper", _call_ai_direct=lambda *a, **k: json.dumps(answer),
                              last_usage=lambda: None, last_model=lambda: "claude-sonnet-5-5")


class ServiceTests(AiMomentsBase):
    def test_refusals(self):
        m = self.processed()
        with self.assertRaisesMessage(MeetingError, "Поставте галочку"):
            ai_moments.start(m, self.owner, "claude-sonnet-5-5", consent=False)
        with self.assertRaises(MeetingError):
            ai_moments.start(m, self.owner, "gpt-5", consent=True)
        with patch("meetings.ai_moments.available", return_value=False), \
                self.assertRaisesMessage(MeetingError, "немає ключа Anthropic"):
            ai_moments.start(m, self.owner, "claude-sonnet-5-5", consent=True)
        fresh = self.meeting()
        with self.assertRaisesMessage(MeetingError, "Розшифровки ще немає"):
            ai_moments.start(fresh, self.owner, "claude-sonnet-5-5", consent=True)
        from django.http import Http404
        with self.assertRaises(Http404):
            ai_moments.start(m, self.other, "claude-sonnet-5-5", consent=True)

    def test_run_replaces_pending_keeps_decided_and_logs_consent(self):
        m = self.processed(items=ITEMS)
        first = m.items.order_by("pk").first()
        MeetingItem.objects.filter(pk=first.pk).update(status=MeetingItem.REJECTED)
        old_pending = list(m.items.filter(status=MeetingItem.PENDING).values_list("pk", flat=True))
        with self.claude():
            st = ai_moments.start(m, self.owner, "claude-sonnet-5-5", consent=True)
        self.assertEqual(st["state"], "done")
        m.refresh_from_db()
        self.assertFalse(MeetingItem.objects.filter(pk__in=old_pending).exists())
        self.assertTrue(MeetingItem.objects.filter(pk=first.pk, status=MeetingItem.REJECTED).exists())
        quotes = set(m.items.values_list("quote", flat=True))
        self.assertIn("schicke die Zeichnung bis Montag", quotes)
        self.assertNotIn("das wurde nie gesagt in der Besprechung", quotes)            # цитати немає — не пункт
        self.assertEqual(m.transcript.summary, ANSWER["summary"])
        self.assertEqual(m.transcript.moments_model, "Claude Sonnet 5.5")
        cloud = [e for e in m.consent_log if e.get("kind") == "cloud"]
        self.assertEqual((cloud[-1]["where"], cloud[-1]["model"], cloud[-1]["user"]),
                         ("minerva", "claude-sonnet-5-5", self.owner.pk))
        lines = [x["text"] for x in admin_pages.log_lines(m)]
        self.assertTrue(any(t.startswith("☁️ Текст розшифровки оброблено в Claude Sonnet 5.5") and "у Minerva" in t
                            for t in lines))

    def test_bad_answer_keeps_items(self):
        m = self.processed(items=ITEMS)
        before = m.items.count()
        with patch.multiple("email_assistant.ai_helper", _call_ai_direct=lambda *a, **k: "",
                            last_usage=lambda: None, last_model=lambda: ""):
            st = ai_moments.start(m, self.owner, "claude-haiku-5-5", consent=True)
        self.assertEqual(st["state"], "error")
        self.assertIn("не відповів", st["error"])
        self.assertEqual(m.items.count(), before)

    def test_rag_pc_cloud_and_skip_reach_the_protocol(self):
        m = self.processed(items=None)
        token = Meeting.objects.get(pk=m.pk)
        services.add_moments(token, {"items": [], "summary": "", "model": "skip"})
        m.refresh_from_db()
        self.assertEqual(m.transcript.moments_model, "skip")
        services.add_moments(m, {"items": [], "summary": "", "model": "claude-opus-5-5",
                                 "cloud": {"provider": "anthropic", "model": "claude-opus-5-5", "at": "2026-11-16 18:00:00"}})
        m.refresh_from_db()
        self.assertEqual([e.get("where") for e in m.consent_log if e.get("kind") == "cloud"], ["rag_pc"])
        self.assertTrue(any("на RAG-ПК" in x["text"] for x in admin_pages.log_lines(m)))


class PageTests(AiMomentsBase):
    def test_box_endpoint_and_status(self):
        m = self.processed(items=None)
        services.add_moments(m, {"items": [], "summary": "", "model": "skip"})
        self.login(self.owner)
        page = self.client.get(self.url("page", m))
        self.assertContains(page, "🔄 Знайти ключові моменти ще раз")
        self.assertContains(page, "На RAG-ПК ключові моменти не шукали")
        self.assertContains(page, ai_moments.CONSENT_TEXT)
        self.assertContains(page, 'value="claude-opus-5-5"')
        url = self.url("moments", m)
        self.assertEqual(self.client.get(url).status_code, 405)
        r = self.client.post(url, json.dumps({"model": "claude-sonnet-5-5"}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("галочку", r.json()["error"])
        with self.claude():
            r = self.client.post(url, json.dumps({"model": "claude-sonnet-5-5", "consent": True}),
                                 content_type="application/json")
        self.assertEqual(r.json()["state"]["state"], "done")
        status = self.client.get(self.url("status", m)).json()
        self.assertEqual(status["ai_moments"]["state"], "done")
        self.login(self.other)
        self.assertEqual(self.client.post(url, json.dumps({"model": "claude-sonnet-5-5", "consent": True}),
                                          content_type="application/json").status_code, 404)

    def test_url_name(self):
        self.assertEqual(reverse("admin:meetings_meeting_moments", args=[3]), "/admin/meetings/meeting/3/moments/")
