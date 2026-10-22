"""Клієнт приватного пошуку RAG (/v2): python manage.py test rag_assistant --settings=tabele.settings_test

Тестовий двійник RAG перевіряє кожен запит Minerva і кожну свою відповідь закріпленими схемами
rag-private/2.0-draft.1 (rag_assistant/contract), якщо встановлено jsonschema.
"""
import hashlib
import json
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from email_assistant.models import EmailAccount, EmailSettings
from rag_assistant import knowledge, retrieval
from rag_assistant.models import KnowledgeDoc, KnowledgeSettings, RetrievalJob
from rag_assistant.tests_knowledge import KnowledgeBase

CONTRACT_DIR = pathlib.Path(settings.BASE_DIR) / "rag_assistant" / "contract" / "rag_private_2_0_draft_1"
BASE = "https://rag.test.example"
PINS = {   # integration/reviews/rag-schemas-20261020.json (package cd452d68…5f43)
    "accepted.response.schema.json": "0c21094959ec6c9d7a7547b97632a510118e8a441cdfda1c63c510df5818ab0e",
    "cancel.request.schema.json": "a1827941b20f17a169fab40fe6a6a8da03fea6dd64f72a57d23c668009184d3f",
    "capabilities.response.schema.json": "25e70826afc5efa9e15015201a46a2080d3365e2c0e90aaa9ce63d75603196de",
    "common.schema.json": "5fc3632438a89eeb65a06886449d9ac8cf5786626c2bcbab8ac4726db12c48cd",
    "context.request.schema.json": "adfad51af9d62ad3a31676c16891e7e4eff940f5760133ac123b402689d22a80",
    "error.schema.json": "7bf1907c66d06e54191709ddb43a2088bb7b2f018ff6be0ea01164372419bce2",
    "job.response.schema.json": "1e136e285968c7ac0edf8adcc72071a958cca1ad20b214253feeaf5cfa6c12bb",
    "retrieval.request.schema.json": "e243952f33a21dbce83a555c235feb50070541ec14cd1b3b9a58db97b46a6411",
    "retrieval.response.schema.json": "8d98c7540c253e8be13bfcb0886dc23bc660cdc1b3f04f73d23bd207b1fe27a2",
}

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    _SCHEMAS = {p.name[:-len(".schema.json")]: json.loads(p.read_text(encoding="utf-8"))
                for p in CONTRACT_DIR.glob("*.schema.json")}
    _REGISTRY = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in _SCHEMAS.values()])
except ImportError:
    _SCHEMAS = None


def schema_errors(name, data):
    if _SCHEMAS is None:
        return []
    v = Draft202012Validator(_SCHEMAS[name], registry=_REGISTRY, format_checker=FormatChecker())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(data)]


def _ts(dt=None):
    return (dt or timezone.now()).astimezone(dt_tz.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeRag:
    """Двійник RAG /v2: перевіряє запити Minerva і власні відповіді схемами контракту."""
    INDEX = {"docs": 3, "chunks": 9, "received_seq": 10, "applied_seq": 10, "last_sync_at": None,
             "embedding_model": "embeddinggemma", "embedding_dimensions": 768}

    def __init__(self, test):
        self.test, self.calls, self.hits, self.fail = test, [], [], None
        self.mode = "200"          # 200 | 202 | error:<code>:<status>
        self.job_status = "completed"
        self.job_owner = None

    # ── відповіді ──
    def result(self, body_ctx):
        return {"contract_version": retrieval.CONTRACT, "request_id": "req-fake-0001",
                "source_instance_id": body_ctx["source_instance_id"], "generation": body_ctx["generation"],
                "user_id": body_ctx["user_id"], "status": "completed", "hits": self.hits, "took_ms": 12,
                "index": dict(self.INDEX), "warnings": []}

    def respond(self, status, body, schema):
        if schema:
            self.test.assertEqual(schema_errors(schema, body), [], f"відповідь двійника не за схемою {schema}")
        return FakeResponse(status, body)

    def error(self, code, status):
        return self.respond(status, {"code": code, "message": code, "retryable": False,
                                     "request_id": "req-fake-err1"}, "error")

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None,
                 allow_redirects=None, verify=None):
        t = self.test
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers,
                           "verify": verify})
        t.assertEqual(headers["Authorization"], "Bearer test-private-key")
        t.assertFalse(allow_redirects)
        t.assertEqual(timeout[0], retrieval.CONNECT_TIMEOUT)
        if self.fail == "connect":
            raise requests.exceptions.ConnectionError("down")
        if self.fail == "read_timeout":
            self.fail = None
            raise requests.exceptions.ReadTimeout("slow")
        path = url[len(BASE):]
        if method == "POST" and path == "/v2/retrieval/jobs":
            t.assertEqual(schema_errors("retrieval.request", json), [])
            t.assertRegex(headers["Idempotency-Key"], r"^[A-Za-z0-9_.:-]{8,128}$")
            ctx = {"source_instance_id": json["source_instance_id"], "generation": json["generation"],
                   "user_id": json["filters"]["user_id"]}
            self.last_ctx = ctx
            if self.mode.startswith("error:"):
                _, code, status = self.mode.split(":")
                return self.error(code, int(status))
            if self.mode == "202":
                return self.respond(202, {"contract_version": retrieval.CONTRACT, "request_id": "req-fake-0002",
                                          "job_id": "a" * 32, "status": "queued", "status_url": "/v2/jobs/" + "a" * 32,
                                          "poll_after_ms": 500}, "accepted.response")
            return self.respond(200, self.result(ctx), "retrieval.response")
        if method == "GET" and path.startswith("/v2/jobs/"):
            t.assertEqual(schema_errors("context.request", params), [])
            ctx = dict(params)
            if self.job_owner:
                ctx["user_id"] = self.job_owner
            body = {"contract_version": retrieval.CONTRACT, "request_id": "req-fake-0003", **ctx,
                    "job_id": path.rsplit("/", 1)[1], "status": self.job_status,
                    "stage": self.job_status if self.job_status != "running" else "search",
                    "created_at": _ts(), "updated_at": _ts(), "expires_at": _ts(timezone.now() + timedelta(hours=1))}
            if self.job_status == "completed":
                body["result"] = self.result(ctx)
            return self.respond(200, body, "job.response")
        if method == "GET" and path == "/v2/capabilities":
            t.assertEqual(schema_errors("context.request", params), [])
            return self.respond(200, {
                "contract_version": retrieval.CONTRACT, "request_id": "req-fake-0004",
                "source_instance_id": params["source_instance_id"], "generation": params["generation"],
                "operations": ["retrieval", "jobs", "cancel", "pull_index"],
                "readiness": {"retrieval": "ready", "indexing": "ready"},
                "limits": {"query_chars": 3000, "hits": 20, "wait_ms": 8000, "job_retention_seconds": 86400,
                           "job_deadline_seconds": 60, "max_pending_jobs": 32},
                "index": {**self.INDEX, "last_error": None}}, "capabilities.response")
        if method == "POST" and path.endswith("/cancel"):
            t.assertEqual(schema_errors("cancel.request", json), [])
            return self.error("not_found", 404)
        raise AssertionError(f"неочікуваний запит {method} {path}")


class RetrievalBase(KnowledgeBase):
    def setUp(self):
        super().setUp()
        ks = KnowledgeSettings.get()
        ks.private_api_key, ks.private_base_url = "test-private-key", BASE
        ks.save()
        self.fake = FakeRag(self)
        p = patch("rag_assistant.retrieval.requests.request", self.fake)
        p.start()
        self.addCleanup(p.stop)
        s = patch("time.sleep", lambda *_: None)
        s.start()
        self.addCleanup(s.stop)

    def hit(self, msg, **over):
        doc = KnowledgeDoc.objects.get(obj_id=msg.pk)
        ks = KnowledgeSettings.get()
        h = {"source_instance_id": "test-suite", "generation": str(ks.generation), "document_id": f"email:{msg.pk}",
             "document_type": "email", "document_revision": doc.revision, "content_version": doc.feed_version,
             "chunk": 0, "score": 0.42, "text": "We promised delivery in week 46.", "subject": msg.subject,
             "sent_at": _ts(msg.sent_at), "from_email": msg.from_email, "thread_id": msg.thread_id,
             "mailbox_id": msg.account_id, "customer_id": None, "project_ids": [], "attachment": None,
             "text_truncated": False}
        h.update(over)
        return h


class ClientTests(RetrievalBase):
    def test_pinned_contract_files(self):
        got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in CONTRACT_DIR.glob("*.schema.json")}
        self.assertEqual(got, PINS)

    def test_not_configured_makes_no_call(self):
        ks = KnowledgeSettings.get()
        ks.private_api_key = ""
        ks.save()
        job = retrieval.start(self.user, "antenna delivery")
        self.assertEqual((job.status, self.fake.calls), (RetrievalJob.UNAVAILABLE, []))
        self.assertIn("не налаштований", job.message)

    def test_hits_are_verified_against_minerva(self):
        a, b, c = self.mail("Antenna quote"), self.mail("Cable order"), self.mail("Third")
        other_user = User.objects.create_user("bob", "b@x.y", "p", is_staff=True)
        acc2 = EmailAccount.objects.create(user=other_user, email_address="bob@sevskiy.de", imap_username="b",
                                           imap_password="x", smtp_username="b", smtp_password="x")
        foreign = self.mail("Foreign")
        foreign.account = acc2
        foreign.save()
        self.fake.hits = [
            self.hit(a),
            self.hit(a, chunk=0),                                            # дубль фрагмента
            self.hit(b, document_revision=KnowledgeDoc.objects.get(obj_id=b.pk).revision + 5),   # інша ревізія
            self.hit(c, content_version="sha256:" + "0" * 64),               # інший вміст
            self.hit(foreign, chunk=1),                                      # чужа скринька
        ]
        job = retrieval.start(self.user, "what did we promise about delivery")
        self.assertEqual(job.status, RetrievalJob.COMPLETED, job.message)
        self.assertEqual([h["document_id"] for h in job.hits], [f"email:{a.pk}"])
        self.assertEqual(job.dropped, 4)
        self.assertIn("відкинуто 4", job.message)
        body = self.fake.calls[0]["json"]
        self.assertEqual(body["filters"], {"mailbox_ids": [self.account.pk], "user_id": self.user.pk})
        self.assertEqual(body["wait_ms"], retrieval.WAIT_AUTO_MS)

    def test_whole_result_from_other_generation_is_dropped(self):
        a = self.mail()
        self.fake.hits = [self.hit(a)]
        job = retrieval.start(self.user, "q")
        KnowledgeSettings.objects.update(generation="00000000-0000-4000-8000-000000000000")
        kept, dropped = retrieval.verify_hits(self.user, self.fake.result(self.fake.last_ctx))
        self.assertEqual((kept, dropped), ([], 1))
        self.assertEqual(job.status, RetrievalJob.COMPLETED)

    def test_accepted_then_poll(self):
        a = self.mail()
        self.fake.mode, self.fake.hits = "202", [self.hit(a)]
        job = retrieval.start(self.user, "q")
        self.assertEqual((job.status, job.rag_job_id, job.poll_after_ms), (RetrievalJob.QUEUED, "a" * 32, 500))
        self.fake.job_status = "running"
        self.assertEqual(retrieval.poll(job).status, RetrievalJob.RUNNING)
        self.fake.job_status = "completed"
        self.assertEqual(retrieval.poll(job).status, RetrievalJob.COMPLETED)
        self.assertEqual(len(job.hits), 1)

    def test_job_of_other_owner_is_rejected(self):
        self.fake.mode = "202"
        job = retrieval.start(self.user, "q")
        self.fake.job_owner = self.user.pk + 100
        self.assertEqual(retrieval.poll(job).status, RetrievalJob.FAILED)
        self.assertIn("не відповідає", job.message)

    def test_breaker_opens_after_three_failures(self):
        self.fake.fail = "connect"
        for _ in range(3):
            self.assertEqual(retrieval.start(self.user, "q").status, RetrievalJob.UNAVAILABLE)
        calls = len(self.fake.calls)
        job = retrieval.start(self.user, "q")
        self.assertEqual((job.status, len(self.fake.calls)), (RetrievalJob.UNAVAILABLE, calls))   # без звернення
        self.assertIn("пауза", job.message)
        KnowledgeSettings.objects.update(rag_down_until=timezone.now() - timedelta(seconds=1))
        self.fake.fail = None
        self.assertEqual(retrieval.start(self.user, "q").status, RetrievalJob.COMPLETED)
        self.assertEqual(KnowledgeSettings.get().rag_failures, 0)

    def test_unknown_result_is_resubmitted_with_same_key(self):
        self.fake.fail = "read_timeout"
        job = retrieval.start(self.user, "q")
        self.assertTrue(job.resubmit)
        retrieval.poll(job)
        keys = [c["headers"]["Idempotency-Key"] for c in self.fake.calls if c["method"] == "POST"]
        self.assertEqual((len(keys), len(set(keys))), (2, 1))
        self.assertEqual(job.status, RetrievalJob.COMPLETED)

    def test_contract_errors_fall_back_to_minerva(self):
        self.fake.mode = "error:generation_mismatch:409"
        job = retrieval.start(self.user, "q")
        self.assertEqual(job.status, RetrievalJob.UNAVAILABLE)
        self.assertIn("перебудовується", job.message)
        self.fake.mode = "error:unavailable:503"
        self.assertEqual(retrieval.start(self.user, "q").status, RetrievalJob.UNAVAILABLE)
        self.assertEqual(KnowledgeSettings.get().rag_failures, 1)       # 5xx — збій для запобіжника, 409 — ні

    def test_capabilities_cached_and_admin_check(self):
        self.client.force_login(self.user)
        r = self.client.post("/admin/rag_assistant/knowledgesettings/check/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(KnowledgeSettings.get().last_capabilities["readiness"]["retrieval"], "ready")
        n = len(self.fake.calls)
        self.assertTrue(retrieval.capabilities(self.user)["cached"])
        self.assertEqual(len(self.fake.calls), n)
        page = self.client.get("/admin/rag_assistant/knowledgesettings/1/change/")
        self.assertContains(page, "пошук <b>ready</b>")


class ConnectionSettingsTests(RetrievalBase):
    """«🧠 База знань» → підключення RAG: адреса/порт/HTTPS, назва сервера, ключі в обидва боки."""

    def form(self, **over):
        data = {"categories": ["customer"], "attachment_types_text": "pdf", "attachment_max_mb": 10,
                "instance_id": "", "rag_host": "192.168.2.68", "rag_port": 8788, "private_verify_tls": "on"}
        data.update(over)
        return self.client.post("/admin/rag_assistant/knowledgesettings/1/change/", data)

    def test_address_fields_compose_url_and_keep_key(self):
        self.client.force_login(self.user)
        page = self.client.get("/admin/rag_assistant/knowledgesettings/1/change/")
        self.assertContains(page, 'name="rag_host" value="rag.test.example"')          # розібрано з URL
        self.assertContains(page, 'name="rag_port" value="443"')
        self.assertEqual(self.form().status_code, 302)
        ks = KnowledgeSettings.get()
        self.assertEqual((ks.private_base_url, ks.private_api_key), ("http://192.168.2.68:8788", "test-private-key"))
        self.form(rag_host="rag-tunnel", rag_port=18788, rag_https="on")
        self.assertEqual(KnowledgeSettings.get().private_base_url, "https://rag-tunnel:18788")
        for bad in ("http://192.168.2.68", "192.168.2.68:8788", "rag/v2"):
            r = self.form(rag_host=bad)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "без http://")
        self.form(rag_host="")
        self.assertEqual(KnowledgeSettings.get().private_base_url, "")
        self.assertIn("адресу", retrieval.not_configured_reason())

    def test_no_fallback_to_public_rag_address(self):
        from rag_assistant.models import RagSettings
        rs = RagSettings.get()
        rs.base_url = "https://app.public.example"
        rs.save()
        KnowledgeSettings.objects.update(private_base_url="")
        self.assertEqual(retrieval.base_url(), "")
        self.assertFalse(retrieval.available())

    def test_instance_id_from_settings_when_env_empty(self):
        self.client.force_login(self.user)
        with override_settings(MINERVA_INSTANCE_ID=""):
            self.assertEqual(self.api("get", "/api/v1/knowledge/summary/").status_code, 503)
            r = self.form(instance_id="Sevskiy Prod")
            self.assertContains(r, "Лише малі латинські літери")
            self.form(instance_id="sevskiy-prod")
            self.assertEqual(knowledge.instance_id(), "sevskiy-prod")
            self.assertEqual(knowledge.instance_id_source(), "settings")
            summary = self.api("get", "/api/v1/knowledge/summary/")
            self.assertEqual((summary.status_code, summary.json()["source_instance_id"]), (200, "sevskiy-prod"))
        self.assertEqual(knowledge.instance_id(), "test-suite")                          # змінна середовища важливіша

    def test_create_feed_key_for_rag(self):
        from api.models import APIKey
        self.client.force_login(self.user)
        before = APIKey.objects.count()
        self.assertRedirects(self.client.get("/admin/rag_assistant/knowledgesettings/feedkey/"),
                             "/admin/rag_assistant/knowledgesettings/1/change/", fetch_redirect_response=False)
        self.assertEqual(APIKey.objects.count(), before)                                 # лише POST
        r = self.client.post("/admin/rag_assistant/knowledgesettings/feedkey/")
        key = APIKey.objects.latest("created_at")
        self.assertRedirects(r, f"/admin/api/apikey/{key.pk}/change/", fetch_redirect_response=False)
        self.assertEqual((key.scopes, key.shop_id), (["knowledge:read", "knowledge:write"], None))
        page = self.client.get("/admin/rag_assistant/knowledgesettings/1/change/")
        self.assertContains(page, f"…{key.key[-4:]}")
        self.assertContains(page, "🔑 Створити ключ для RAG")

    def test_tls_verification_setting_is_used(self):
        KnowledgeSettings.objects.update(private_verify_tls=False)
        retrieval.start(self.user, "q")
        self.assertFalse(self.fake.calls[0]["verify"])


class ModesAndMailTests(RetrievalBase):
    def test_decide(self):
        ctx = {"other_threads": []}
        self.assertEqual(retrieval.decide("local", "history", {"other_threads": [1]})[0], False)
        self.assertEqual(retrieval.decide("auto", "history", ctx)[0], False)
        self.assertEqual(retrieval.decide("auto", "history", {"other_threads": [1]})[0], True)
        self.assertEqual(retrieval.decide("auto", "reply", ctx, "Що ми обіцяли клієнту щодо строків?")[0], True)
        self.assertEqual(retrieval.decide("auto", "reply", ctx, "Подякуй за оплату")[0], False)
        self.assertEqual(retrieval.decide("extended", "reply", ctx)[0], True)

    def test_rag_search_button_and_owner_only_poll(self):
        self.client.force_login(self.user)
        old = self.mail("Earlier: delivery week 46")
        new = self.mail("New question about antennas")
        self.fake.hits = [self.hit(old)]
        d = self.client.post(f"/email/message/{new.pk}/rag-search/").json()
        self.assertEqual((d["status"], d["terminal"]), ("completed", True))
        self.assertEqual(d["hits"][0]["url"], f"/email/message/{old.pk}/")
        body = self.fake.calls[-1]["json"]
        self.assertEqual(body["filters"]["exclude_thread_ids"], [new.thread_id])
        self.assertEqual(body["filters"]["customer_id"], self.client_co.pk)
        self.assertEqual(body["wait_ms"], retrieval.WAIT_EXTENDED_MS)
        other = User.objects.create_user("eve", "e@x.y", "p", is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get(f"/email/rag-search/{d['id']}/").status_code, 404)

    def _ai_reply(self, msg, prompt=""):
        captured = {}

        def fake_generate(thread_messages, account, profile, instruction="", facts="", recipient=""):
            captured["facts"] = facts
            return "Draft reply"
        with patch("email_assistant.ai_helper.generate_reply", fake_generate):
            d = self.client.get(f"/email/message/{msg.pk}/ai-reply/", {"prompt": prompt}).json()
        return d, captured["facts"]

    def test_ai_reply_uses_rag_by_mode(self):
        self.client.force_login(self.user)
        old = self.mail("Earlier: delivery week 46")
        new = self.mail("Status?")
        self.fake.hits = [self.hit(old)]
        es = EmailSettings.get_for_user(self.user)
        es.rag_mode = "local"
        es.save()
        d, facts = self._ai_reply(new, "Що ми обіцяли?")
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn("Фрагменти з інших листувань", facts)
        es.rag_mode = "extended"
        es.save()
        d, facts = self._ai_reply(new)
        self.assertIn("Фрагменти з інших листувань", facts)
        self.assertIn("We promised delivery in week 46.", facts)
        self.assertTrue(any("через RAG знайдено 1" in u for u in d["used"]), d["used"])

    def test_ai_reply_works_when_rag_is_down(self):
        self.client.force_login(self.user)
        msg = self.mail("Question")
        es = EmailSettings.get_for_user(self.user)
        es.rag_mode = "extended"
        es.save()
        self.fake.fail = "connect"
        d, facts = self._ai_reply(msg)
        self.assertTrue(d["ok"])
        self.assertTrue(any("лише дані Minerva" in u for u in d["used"]), d["used"])

    def test_unconfigured_rag_is_invisible_in_mail(self):
        """Сервер без підключеного RAG: жодних кнопок, приміток і записів про пошук — лише дані Minerva."""
        self.client.force_login(self.user)
        old, new = self.mail("Earlier: delivery week 46"), self.mail("Status?")
        configured = self.client.get(f"/email/thread/{new.thread_id}/preview/")
        self.assertContains(configured, "pvRagSearch(")
        KnowledgeSettings.objects.update(private_api_key="")
        es = EmailSettings.get_for_user(self.user)
        es.rag_mode = "extended"
        es.save()
        d, facts = self._ai_reply(new, "Що ми обіцяли?")
        self.assertTrue(d["ok"])
        self.assertFalse(any("RAG" in u for u in d["used"]), d["used"])
        self.assertEqual((self.fake.calls, RetrievalJob.objects.count()), ([], 0))
        self.assertNotContains(self.client.get(f"/email/thread/{new.thread_id}/preview/"), "pvRagSearch(")
        inbox = self.client.get("/email/")
        self.assertNotContains(inbox, 'id="em-rag-mode"')
        self.assertContains(inbox, "ctxAction('kb')")                       # база знань на сервері є — «🔒» лишається
        with override_settings(MINERVA_INSTANCE_ID=""):
            self.assertNotContains(self.client.get("/email/"), "ctxAction('kb')")

    def test_mode_setting_from_mailbox(self):
        self.client.force_login(self.user)
        r = self.client.post("/email/settings/toggle/", json.dumps({"field": "rag_mode", "value": "extended"}),
                             content_type="application/json")
        self.assertEqual(r.json(), {"ok": True, "rag_mode": "extended"})
        self.assertEqual(EmailSettings.get_for_user(self.user).rag_mode, "extended")
