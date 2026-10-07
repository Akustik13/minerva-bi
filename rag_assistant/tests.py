"""Тести помічника по продукції: python manage.py test rag_assistant --settings=tabele.settings_test
RAG API підмінено (mock) — реальний сервер і ключ не потрібні."""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement

from .models import RagConversation, RagMessage, RagSettings


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def fake_rag(routes):
    """routes: {(method, path): (status, payload) | callable(body) → (status, payload)}; записує виклики."""
    calls = []

    def request(method, url, json=None, headers=None, timeout=None, verify=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        calls.append({"method": method, "path": path, "body": json, "headers": headers})
        handler = routes.get((method, path))
        if handler is None:
            return _Resp(404, {"error": "Not found"})
        status, payload = handler(json) if callable(handler) else handler
        return _Resp(status, payload)
    return request, calls


class RagAssistantTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p = Product.objects.create(sku="AN050201-01C-175-MHF1", name="Zweiband-Antenne", sale_price=Decimal("10"))
        create_movement(product=self.p, tx_type="Incoming", qty=12)
        self.user = User.objects.create_superuser("anna", "a@x.y", "p")
        self.client.force_login(self.user)
        st = RagSettings.get()
        st.enabled, st.base_url, st.api_key = True, "https://rag.example", "secret-key-1234"
        st.save()

    def _new_conv(self):
        return self.client.post("/rag/api/conversations/new/").json()["conversation"]["id"]

    def test_state_and_key_never_sent_to_browser(self):
        d = self.client.get("/rag/api/state/").json()
        self.assertEqual((d["enabled"], d["configured"], d["name"]), (True, True, "Minerva"))
        self.assertNotIn("secret-key", json.dumps(d))
        page = self.client.get("/admin/rag_assistant/ragsettings/1/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "secret-key-1234")
        self.assertContains(page, "…1234")
        self.assertContains(self.client.get("/admin/"), 'id="rg-pill"')

    def test_ask_poll_complete_with_products_and_history(self):
        cid = self._new_conv()
        result = {"answer": "Потужність до 5 W [1].", "warnings": [],
                  "sources": [{"id": 1, "source": "AN050201-01C-175-MHF1_r2.pdf", "page": 3, "text": "Power max 5 W",
                               "document_id": "abc"}],
                  "products": [{"position": 1, "product": "AN050201-01C-175-MHF1", "source": "x.pdf", "page": 1},
                               {"position": 2, "product": "UNKNOWN-1", "source": "y.pdf"}],
                  "trace": [{"key": "llm", "title": "LLM: формування відповіді", "status": "completed",
                             "elapsed_seconds": 2.5, "details": ["ok"]}],
                  "usage": {"total_tokens": 100}}
        state = {"n": 0}

        def job(_):
            state["n"] += 1
            if state["n"] == 1:
                return 200, {"id": "job1", "status": "running", "stage": "Пошук у даташитах",
                             "trace": [{"title": "Векторний пошук", "status": "running", "elapsed_seconds": 1}]}
            return 200, {"id": "job1", "status": "completed", "result": result}
        req, calls = fake_rag({("POST", "/v1/ask"): (202, {"job_id": "job1", "status_url": "/v1/jobs/job1"}),
                               ("GET", "/v1/jobs/job1"): job})
        with mock.patch("rag_assistant.client.requests.request", req):
            r = self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "Яка потужність?"},
                                 content_type="application/json")
            self.assertEqual(r.status_code, 202, r.content)
            mid = r.json()["assistant"]["id"]
            m = self.client.get(f"/rag/api/messages/{mid}/").json()["message"]
            self.assertEqual((m["status"], m["stage"]), ("pending", "Пошук у даташитах"))
            m = self.client.get(f"/rag/api/messages/{mid}/").json()["message"]
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["content"], "Потужність до 5 W [1].")
        self.assertEqual(m["sources"][0]["page"], 3)
        known = m["products"][0]["minerva"]
        self.assertEqual((known["available"], known["url"]), (12.0, f"/admin/inventory/product/{self.p.pk}/change/"))
        self.assertNotIn("minerva", m["products"][1])
        ask_call = calls[0]
        self.assertEqual(ask_call["headers"]["Authorization"], "Bearer secret-key-1234")
        self.assertEqual(ask_call["headers"]["X-Client-ID"], f"minerva:u{self.user.pk}")
        self.assertEqual(ask_call["body"]["history"], [])
        self.assertEqual(RagConversation.objects.get(pk=cid).title, "Яка потужність?")
        # друге запитання — з історією розмови
        req2, calls2 = fake_rag({("POST", "/v1/ask"): (202, {"job_id": "job2"})})
        with mock.patch("rag_assistant.client.requests.request", req2):
            self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "А вага?"},
                             content_type="application/json")
        self.assertEqual([h["role"] for h in calls2[0]["body"]["history"]], ["user", "assistant"])

    def test_busy_is_not_saved_and_pending_blocks_second_question(self):
        cid = self._new_conv()
        req, _ = fake_rag({("POST", "/v1/ask"): (429, {"error": "Busy"})})
        with mock.patch("rag_assistant.client.requests.request", req):
            r = self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "Q"}, content_type="application/json")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "busy"))
        self.assertEqual(RagMessage.objects.count(), 0)
        req, _ = fake_rag({("POST", "/v1/ask"): (202, {"job_id": "j"})})
        with mock.patch("rag_assistant.client.requests.request", req):
            self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "Q"}, content_type="application/json")
            r = self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "Q2"}, content_type="application/json")
        self.assertEqual(r.status_code, 409)

    def test_lost_job_and_errors(self):
        cid = self._new_conv()
        req, _ = fake_rag({("POST", "/v1/ask"): (202, {"job_id": "gone"})})
        with mock.patch("rag_assistant.client.requests.request", req):
            mid = self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "Q"},
                                   content_type="application/json").json()["assistant"]["id"]
            m = self.client.get(f"/rag/api/messages/{mid}/").json()["message"]  # GET job → 404
        self.assertEqual(m["status"], "failed")
        req, _ = fake_rag({("POST", "/v1/ask"): (200, "<html>parking page</html>")})
        with mock.patch("rag_assistant.client.requests.request", req):
            r = self.client.post(f"/rag/api/conversations/{cid}/ask/", {"question": "Q"}, content_type="application/json")
        self.assertEqual(r.status_code, 503)
        self.assertIn("не JSON", r.json()["error"])

    def test_ownership_and_access(self):
        cid = self._new_conv()
        other = User.objects.create_superuser("bob", "b@x.y", "p")  # має доступ до модуля, але не до чужої розмови
        self.client.force_login(other)
        self.assertEqual(self.client.get(f"/rag/api/conversations/{cid}/").status_code, 404)
        self.assertEqual(self.client.post(f"/rag/api/conversations/{cid}/delete/").status_code, 404)
        plain = User.objects.create_user("eve", "e@x.y", "p")
        self.client.force_login(plain)
        self.assertEqual(self.client.get("/rag/api/state/").status_code, 403)

    def test_admin_check_connection(self):
        req, _ = fake_rag({("GET", "/v1/health"): (200, {"status": "ok", "api_version": "1", "hybrid_enabled": True}),
                           ("GET", "/v1/documents"): (200, {"documents": [{"title": "a"}, {"title": "b"}]})})
        with mock.patch("rag_assistant.client.requests.request", req):
            self.client.post("/admin/rag_assistant/ragsettings/check/")
        st = RagSettings.get()
        self.assertIn("документів: 2", st.last_check_result)
        req, _ = fake_rag({("GET", "/v1/health"): (401, {"error": "Unauthorized"})})
        with mock.patch("rag_assistant.client.requests.request", req):
            self.client.post("/admin/rag_assistant/ragsettings/check/")
        self.assertIn("ключ", RagSettings.get().last_check_result)

    def test_settings_form_keeps_key_when_blank(self):
        url = "/admin/rag_assistant/ragsettings/1/change/"
        data = {"enabled": "on", "name": "Minerva", "base_url": "https://app.example", "api_key": "",
                "verify_tls": "on", "timeout": "20", "language": "uk", "latest_only": "on"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        st = RagSettings.get()
        self.assertEqual((st.api_key, st.base_url), ("secret-key-1234", "https://app.example"))
        self.client.post(url, {**data, "api_key": "new-key-9999"})
        self.assertEqual(RagSettings.get().api_key, "new-key-9999")
