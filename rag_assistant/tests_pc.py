"""Панель «🖥 RAG-ПК зараз» (rag_assistant/pc.py): модель, активні запити, зупинка, вивантаження (RAG підмінено).
python manage.py test rag_assistant --settings=tabele.settings_test"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from . import pc
from .client import RagError
from .models import RagSettings

RUN = "a" * 32


class PcPanelTests(TestCase):
    def setUp(self):
        RagSettings.objects.update_or_create(pk=1, defaults={"base_url": "https://rag.example", "api_key": "k"})
        patcher = mock.patch("rag_assistant.pc.RagClient")
        self.rag = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.rag.health.return_value = {"status": "ok", "llm": {"provider": "ollama", "model": "qwen2.5:7b",
                                                                "hybrid_enabled": True, "analyzer_provider": "anthropic",
                                                                "analyzer_model": "claude-opus-5"}}
        self.rag.runtime.return_value = {"gpu": {"vram_used_mb": 5120, "vram_total_mb": 8192},
                                         "models": [{"name": "qwen2.5:7b"}], "ollama_available": True}
        self.rag.metrics.return_value = {"time": 100000.0, "clients": [
            {"id": "api:shop-search", "last_seen": 99990, "requests": 8, "completed": 3, "errors": 0, "busy": 5, "avg_seconds": 24.7},
            {"id": "api:web:old", "last_seen": 1, "requests": 1}],
            "runs": [{"id": RUN, "client": "api:site:x", "status": "running", "stage": "LLM", "started": 99940},
                     {"id": "b" * 32, "client": "api:shop-search", "status": "completed", "started": 99800, "finished": 99830}]}

    def test_status(self):
        d = pc.status()
        self.assertEqual((d["llm"]["provider"], d["llm"]["analyzer_provider"]), ("ollama", "anthropic"))
        self.assertEqual(d["runtime"]["models"], ["qwen2.5:7b"])
        self.assertEqual(d["active"], [{"id": RUN, "client": "Чат «Frag Minerva» на сайті", "stage": "LLM",
                                        "seconds": 60, "stopping": False}])
        self.assertEqual(d["recent"][0]["client"], "Підказки в пошуку сайту (Minerva)")
        self.assertEqual([c["client"] for c in d["clients"]], ["Підказки в пошуку сайту (Minerva)"])   # лише за 2 год
        self.rag.health.return_value = {"status": "ok"}
        self.assertIn("RAG-Integration-API", pc.status()["llm_note"])
        self.rag.health.side_effect = RagError("network", "Немає зв'язку з помічником.")
        self.assertFalse(pc.status()["ok"])

    def test_cancel_and_unload(self):
        self.rag.cancel_run.return_value = {"status": "cancelling", "message": "Зупинку запитано."}
        self.assertEqual(pc.cancel(RUN), {"ok": True, "text": "Зупинку запитано."})
        self.rag.cancel_run.side_effect = RagError("not_found", "x", 404)
        self.assertIn("rag_api.py", pc.cancel(RUN)["text"])
        self.rag.unload.side_effect = RagError("unavailable", "x", 409)
        self.assertIn("після нього", pc.unload()["text"])

    def test_admin_views(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get("/admin/rag_assistant/ragsettings/1/change/")
        self.assertContains(page, "🖥 RAG-ПК зараз")
        self.assertContains(page, "Вивантажити моделі")
        self.assertEqual(self.client.get("/admin/rag_assistant/ragsettings/pc/").json()["active"][0]["id"], RUN)
        self.assertEqual(self.client.get(f"/admin/rag_assistant/ragsettings/pc/cancel/{RUN}/").status_code, 403)   # лише POST
        self.rag.cancel_run.return_value = {"status": "cancelling", "message": "ok"}
        self.assertTrue(self.client.post(f"/admin/rag_assistant/ragsettings/pc/cancel/{RUN}/").json()["ok"])
        self.rag.cancel_run.assert_called_once_with(RUN)
        staff = User.objects.create_user("v", "v@example.com", "p", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post(f"/admin/rag_assistant/ragsettings/pc/cancel/{RUN}/").status_code, 403)
