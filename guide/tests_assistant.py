"""📚 Гід у віконці «Minerva»: вибір 📚 / 📦 і відповідь за сторінками гіда (Claude підмінено, без мережі).

python manage.py test guide --settings=tabele.settings_test
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from guide import assistant
from rag_assistant.models import RagConversation, RagMessage, RagSettings


def fake_response(text="Відкрийте [📂 Джерела знань](/admin/rag_assistant/knowledgesource/) і натисніть **«Зберегти»**."):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           model="claude-sonnet-5-5",
                           usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=900,
                                                 cache_creation_input_tokens=0))


class GuideWidgetTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from strategy.models import AISettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        ai = AISettings.get()
        ai.anthropic_api_key = "sk-ant-test"
        ai.save()
        self.user = User.objects.create_superuser("slava", "s@x.y", "p")
        self.client.force_login(self.user)
        inline = patch("guide.assistant._start_background", lambda fn: fn())
        inline.start()
        self.addCleanup(inline.stop)
        self.conv = RagConversation.objects.create(user=self.user)

    def ask(self, question, **extra):
        return self.client.post(f"/rag/api/conversations/{self.conv.pk}/ask/",
                                json.dumps({"question": question, **extra}), content_type="application/json")

    def test_route(self):
        from inventory.models import Product
        Product.objects.create(sku="AN050201-01C", name="Antenne")
        self.assertEqual(assistant.route("Як додати даташити з ПК у базу знань?")[0], "guide")
        self.assertEqual(assistant.route("Яка частота й підсилення AN050201-01C?")[0], "products")
        self.assertEqual(assistant.route("Які антени для LoRa є на складі?")[0], "products")
        self.assertEqual(assistant.route("Яка частота антени?", rag_ready=False)[0], "guide")   # RAG не налаштований
        self.assertEqual(assistant.route("Що таке LoRa?")[0], "products")             # «що» — не ознака гіда
        self.assertEqual(assistant.route("Де налаштувати Stripe для магазину?")[0], "guide")
        self.assertEqual(assistant.route("Як надрукувати етикетку на DYMO?")[0], "guide")
        self.assertEqual(assistant.route("А вага?", previous="products")[0], "products")   # уточнення — та сама тема
        self.assertEqual(assistant.route("А потім?", previous="guide")[0], "guide")

    def test_state_shows_window_with_guide_only(self):
        d = self.client.get("/rag/api/state/").json()
        self.assertEqual((d["enabled"], d["guide"], d["products"]), (True, True, False))

    def test_guide_answer_with_page_context(self):
        with patch("guide.assistant._call", return_value=fake_response()) as call:
            r = self.ask("Як додати файли з ПК?", page="/admin/rag_assistant/knowledgesource/add/")
        self.assertEqual(r.status_code, 202, r.content[:300])
        m = RagMessage.objects.get(pk=r.json()["assistant"]["id"])
        self.assertEqual((m.mode, m.status), (RagMessage.GUIDE, RagMessage.DONE))
        self.assertIn("/admin/rag_assistant/knowledgesource/", m.content)
        self.assertTrue(any(s["url"] == "/guide/ai/knowledge-sources/" for s in m.sources))   # сторінка, де людина
        system, payload = call.call_args.args
        self.assertIn("/guide/ai/knowledge-sources/", system)                                  # зміст гіда
        self.assertIn("=== /guide/ai/knowledge-sources/", payload)
        self.assertIn("ЗАПИТАННЯ: Як додати файли з ПК?", payload)
        msg = self.client.get(f"/rag/api/messages/{m.pk}/").json()["message"]
        self.assertEqual((msg["mode"], msg["status"]), ("guide", "done"))
        from ai_assistant.models import AIBudgetLog
        self.assertEqual(AIBudgetLog.current().total_requests, 1)                              # у бюджет AI

    def test_products_without_rag_and_failures(self):
        r = self.ask("Яка частота?", mode="products")
        self.assertEqual(r.status_code, 503)
        self.assertIn("не налаштований", r.json()["error"])
        with patch("guide.assistant._call", side_effect=RuntimeError("network")):
            r = self.ask("Як налаштувати джерела знань?", mode="guide")
        m = RagMessage.objects.get(pk=r.json()["assistant"]["id"])
        self.assertEqual(m.status, RagMessage.FAILED)
        self.assertIn("Не вдалося отримати відповідь", m.error)
        with patch("ai_assistant.budget_guard.check_budget", return_value=(False, "monthly_budget_exceeded")):
            r = self.ask("Як налаштувати джерела знань?", mode="guide")
        self.assertIn("бюджет", RagMessage.objects.get(pk=r.json()["assistant"]["id"]).error)

    def test_products_route_still_uses_rag(self):
        st = RagSettings.get()
        st.enabled, st.base_url, st.api_key = True, "https://rag.example", "k"
        st.save()
        with patch("rag_assistant.client.RagClient.ask", return_value="job-1"):
            r = self.ask("Яке підсилення в dBi у антени на 2,4 GHz?")
        self.assertEqual(r.status_code, 202, r.content[:300])
        m = RagMessage.objects.get(pk=r.json()["assistant"]["id"])
        self.assertEqual((m.mode, m.rag_job_id), (RagMessage.PRODUCTS, "job-1"))
