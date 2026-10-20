"""База знань для RAG: python manage.py test rag_assistant --settings=tabele.settings_test

Реальні відповіді /api/v1/knowledge/ перевіряються тими самими JSON-схемами й семантичними правилами,
що й синтетичні приклади контракту (api/schemas/knowledge/validate.py), якщо встановлено jsonschema.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from api.models import APIKey
from crm.models import Customer
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, EmailThread
from email_assistant.sync import save_message
from rag_assistant import knowledge
from rag_assistant.models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings, KnowledgeSnapshot

ME = "sales@sevskiy.de"
SCHEMAS = pathlib.Path(settings.BASE_DIR) / "api" / "schemas" / "knowledge"

try:
    import jsonschema  # noqa: F401
    spec = importlib.util.spec_from_file_location("knowledge_schema_validate", SCHEMAS / "validate.py")
    VALIDATE = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(VALIDATE)
except ImportError:
    VALIDATE = None


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class KnowledgeBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        background = patch("rag_assistant.knowledge._start_background", lambda fn: fn())   # звірка без потоків
        background.start()
        self.addCleanup(background.stop)
        self.media = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media)
        self._media.enable()
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.user = User.objects.create_superuser("slava", "s@x.y", "p")
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX", imap_folder_sent="Sent")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        self.client_co = Customer.objects.create(name="Client GmbH", email="buyer@client.example")
        ks = KnowledgeSettings.get()
        ks.enabled = True
        ks.save()
        self.key = APIKey.objects.create(name="rag", scopes=["knowledge:read", "knowledge:write"])
        self._uid = 0

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def mail(self, subject="Antenna quote", from_email="buyer@client.example", body="Please quote 500 pcs.",
             attachments=(), list_unsubscribe=False, message_id=None, in_reply_to=""):
        self._uid += 1
        return save_message(self.account, {
            "uid": self._uid, "message_id": message_id or f"<k{self._uid}@x.example>", "in_reply_to": in_reply_to,
            "references": [], "subject": subject, "from_email": from_email, "from_name": "", "to_emails": [ME],
            "cc_emails": [], "body_text": body, "body_html": "", "attachments": list(attachments),
            "is_read": False, "list_unsubscribe": list_unsubscribe,
            "sent_at": datetime(2026, 10, 1, 9, self._uid % 60, tzinfo=dt_tz.utc)}, "INBOX")

    def api(self, method, path, key=None, **kw):
        key = self.key if key is None else key
        headers = {"HTTP_AUTHORIZATION": f"Token {key.key}"} if key else {}
        if method == "post":
            return self.client.post(path, json.dumps(kw.pop("data", {})), content_type="application/json",
                                    **headers, **kw)
        return self.client.get(path, kw.pop("data", None), **headers, **kw)

    def assertSchema(self, name, data):
        if VALIDATE is not None:
            self.assertEqual(VALIDATE.errors(name, data), [], json.dumps(data, ensure_ascii=False)[:600])

    def changes(self, after=0, **params):
        r = self.api("get", "/api/v1/knowledge/changes/", data={"after": after, **params})
        self.assertEqual(r.status_code, 200, r.content[:300])
        data = r.json()
        self.assertSchema("changes.response", data)
        return data

    def doc_state(self, msg):
        return KnowledgeDoc.objects.get(obj_id=msg.pk)


class FeedTests(KnowledgeBase):
    def test_nothing_before_enabled_then_refresh(self):
        ks = KnowledgeSettings.get()
        ks.enabled = False
        ks.save()
        m = self.mail()
        self.assertEqual(self.changes()["items"], [])
        ks.enabled = True
        ks.save()
        knowledge.refresh_all()
        items = self.changes()["items"]
        self.assertEqual([(i["document_id"], i["action"]) for i in items], [(f"email:{m.pk}", "upsert")])

    def test_new_customer_mail_has_full_context(self):
        m = self.mail()
        news = self.mail("Weekly deals", from_email="news@shop.example", list_unsubscribe=True)
        data = self.changes()
        self.assertEqual([i["document_id"] for i in data["items"]], [f"email:{m.pk}"])   # розсилка не йде
        doc = data["items"][0]["document"]
        self.assertEqual((doc["category"], doc["customer"]["name"], doc["direction"]), ("customer", "Client GmbH", "in"))
        self.assertEqual(doc["access"], {"scope": "mailbox", "mailbox_id": self.account.pk, "user_ids": [self.user.pk]})
        self.assertEqual((doc["text_new_mode"], doc["text_new"]), ("full", doc["text"]))
        self.assertEqual(data["source_instance_id"], "test-suite")
        self.assertFalse(KnowledgeDoc.objects.filter(obj_id=news.pk, fed=True).exists())

    def test_revisions_only_for_real_changes(self):
        m = self.mail()
        self.assertEqual(self.doc_state(m).revision, 1)
        last = self.changes()["next_after"]
        m.is_read = True
        m.save(update_fields=["is_read"])                 # не змінює документ
        m.save()                                           # без змін — та сама версія
        self.assertEqual(self.changes(last)["items"], [])
        m.body_text = "Please quote 800 pcs."
        m.save()
        items = self.changes(last)["items"]
        self.assertEqual((items[0]["action"], items[0]["document_revision"]), ("upsert", 2))
        self.assertIn("800", items[0]["document"]["text"])

    def test_tombstone_and_return_increase_revision(self):
        m = self.mail()
        last = self.changes()["next_after"]
        m.is_deleted = True
        m.save(update_fields=["is_deleted"])
        item = self.changes(last)["items"][0]
        self.assertEqual((item["action"], item["reason"], item["document_revision"]), ("delete", "deleted", 2))
        m.is_deleted = False
        m.save(update_fields=["is_deleted"])
        self.assertEqual(self.changes(item["seq"])["items"][0]["document_revision"], 3)
        self.assertEqual(self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/").json()["document_revision"], 3)

    def test_pause_stops_new_but_keeps_deletes(self):
        m = self.mail()
        ks = KnowledgeSettings.get()
        ks.enabled = False
        ks.save()
        last = self.changes()["next_after"]
        self.mail("New while paused")
        m.folder = "trash"
        m.save(update_fields=["folder"])
        data = self.changes(last)
        self.assertFalse(data["enabled"])
        self.assertEqual([(i["document_id"], i["action"]) for i in data["items"]], [(f"email:{m.pk}", "delete")])

    def test_private_thread_toggle(self):
        self.client.force_login(self.user)
        m = self.mail()
        last = self.changes()["next_after"]
        self.assertTrue(self.client.post(f"/email/thread/{m.thread_id}/knowledge/").json()["excluded"])
        item = self.changes(last)["items"][0]
        self.assertEqual((item["action"], item["reason"]), ("delete", "excluded"))
        self.assertFalse(self.client.post(f"/email/thread/{m.thread_id}/knowledge/").json()["excluded"])
        self.assertEqual(self.changes(item["seq"])["items"][0]["action"], "upsert")

    def test_page_has_each_document_once_with_latest_state(self):
        m = self.mail()
        for n in (2, 3, 4):
            m.body_text = f"version {n}"
            m.save()
        data = self.changes()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["document_revision"], 4)
        self.assertEqual(data["next_after"], KnowledgeChange.objects.latest("id").id)
        page = self.changes(limit=2)
        self.assertTrue(page["has_more"])
        self.assertEqual(self.changes(data["next_after"])["items"], [])

    def test_bulk_update_without_signal_self_heals(self):
        self.client.force_login(self.user)
        a, b = self.mail(), self.mail("Second")
        EmailMessage.objects.filter(pk=a.pk).update(is_spam=True)       # без сигналу
        r = self.api("get", f"/api/v1/knowledge/documents/email:{a.pk}/")
        self.assertEqual((r.status_code, r.json()["code"]), (404, "document_not_found"))
        self.assertSchema("error", r.json())
        self.assertFalse(self.doc_state(a).fed)
        self.client.post("/email/messages/bulk/", json.dumps({"action": "spam", "pks": [b.pk]}),
                         content_type="application/json")
        self.assertFalse(self.doc_state(b).fed)

    def test_project_link_creates_new_revision(self):
        from projects import services
        m = self.mail()
        p = services.create_project(name="Antennen", user=self.user, threads=[m.thread])
        doc = self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/").json()
        self.assertSchema("document", doc)
        self.assertEqual(doc["projects"], [{"id": p.pk, "code": p.code, "name": "Antennen"}])
        self.assertEqual(doc["document_revision"], 2)


class CursorAndSnapshotTests(KnowledgeBase):
    def test_cursor_errors(self):
        self.mail()
        last = self.changes()["last_seq"]
        r = self.api("get", "/api/v1/knowledge/changes/", data={"after": last + 5})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["details"]["last_seq"]), (409, "cursor_ahead", last))
        self.assertSchema("error", r.json())
        r = self.api("get", "/api/v1/knowledge/changes/", data={"generation": "00000000-0000-4000-8000-000000000000"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "generation_mismatch"))
        KnowledgeChange.objects.update(created_at=timezone.now() - timedelta(days=40))
        knowledge.prune()
        r = self.api("get", "/api/v1/knowledge/changes/", data={"after": 0})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["details"]["resume_after"]),
                         (410, "cursor_expired", last))
        self.assertSchema("error", r.json())
        self.assertEqual(self.changes(last)["items"], [])     # з межі — працює

    def test_snapshot_pages_and_expiry(self):
        docs = [self.mail(f"Topic {i}") for i in range(3)]
        r = self.api("post", "/api/v1/knowledge/manifest/snapshots/")
        self.assertEqual(r.status_code, 201)
        snap = r.json()
        self.assertSchema("snapshot.create.response", snap)
        self.assertEqual(snap["total"], 3)
        seen, cursor = [], None
        while True:
            data = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            page = self.api("get", f"/api/v1/knowledge/manifest/snapshots/{snap['snapshot_id']}/", data=data).json()
            self.assertSchema("snapshot.page.response", page)
            seen += [i["document_id"] for i in page["items"]]
            if page["complete"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(seen, [f"email:{d.pk}" for d in docs])
        KnowledgeSnapshot.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        r = self.api("get", f"/api/v1/knowledge/manifest/snapshots/{snap['snapshot_id']}/")
        self.assertEqual((r.status_code, r.json()["code"]), (410, "snapshot_expired"))

    def test_clear_and_new_generation(self):
        a, b = self.mail(), self.mail("Two")
        last = self.changes()["next_after"]
        self.assertEqual(knowledge.clear_index(), 2)
        data = self.changes(last)
        self.assertEqual({i["reason"] for i in data["items"]}, {"index_cleared"})
        self.assertFalse(data["enabled"])
        old = data["generation"]
        knowledge.new_generation()
        r = self.api("get", "/api/v1/knowledge/changes/", data={"generation": old})
        self.assertEqual(r.json()["code"], "generation_mismatch")
        self.assertTrue(a.pk and b.pk)


class StatusAndAccessTests(KnowledgeBase):
    def _status(self, items, key=None):
        payload = {"source_instance_id": "test-suite", "generation": str(KnowledgeSettings.get().generation),
                   "items": items}
        if VALIDATE is not None:
            self.assertEqual(VALIDATE.errors("status.request", payload), [])
        r = self.api("post", "/api/v1/knowledge/status/", key=key, data=payload)
        return r

    def test_status_transitions_and_summary(self):
        m = self.mail()
        doc = self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/").json()
        base = {"document_id": doc["document_id"], "document_revision": 1, "content_version": doc["content_version"],
                "reported_at": "2026-10-20T08:15:09Z"}
        r = self._status([{**base, "attempt_id": 2, "state": "indexed", "chunks": 3}])
        self.assertEqual(r.status_code, 200)
        self.assertSchema("status.response", r.json())
        self.assertEqual(r.json()["results"][0]["result"], "accepted")
        late_failed = {**base, "attempt_id": 1, "state": "failed", "error": {"code": "timeout", "message": "x"}}
        res = [x["result"] for x in self._status([{**base, "attempt_id": 2, "state": "indexed", "chunks": 3}]).json()["results"]]
        self.assertEqual(res, ["duplicate"])
        self.assertEqual(self._status([late_failed]).json()["results"][0]["result"], "stale_attempt")
        self.assertEqual(self.doc_state(m).current_state, "indexed")
        ahead = self._status([{**base, "document_revision": 5, "attempt_id": 9, "state": "indexed", "chunks": 1}])
        self.assertEqual(ahead.json()["results"][0], {"document_id": doc["document_id"], "document_revision": 5,
                                                     "attempt_id": 9, "result": "revision_ahead", "current_revision": 1})
        m.body_text = "changed"
        m.save()
        self.assertEqual(self._status([{**base, "attempt_id": 3, "state": "indexed", "chunks": 3}])
                         .json()["results"][0]["result"], "stale")
        unknown = self._status([{**base, "document_id": "email:999999", "attempt_id": 1, "state": "indexed", "chunks": 1}])
        self.assertEqual(unknown.json()["results"][0]["result"], "unknown_document")
        s = self.api("get", "/api/v1/knowledge/summary/").json()
        self.assertSchema("summary.response", s)
        self.assertEqual((s["documents"]["eligible"], s["documents"]["indexed"], s["documents"]["pending"]), (1, 0, 1))

    def test_deleted_status_needs_tombstone(self):
        m = self.mail()
        m.is_deleted = True
        m.save(update_fields=["is_deleted"])
        item = {"document_id": f"email:{m.pk}", "document_revision": 2, "attempt_id": 1, "state": "deleted",
                "reported_at": "2026-10-20T08:15:09Z"}
        self.assertEqual(self._status([item]).json()["results"][0]["result"], "accepted")
        self.assertEqual(self.api("get", "/api/v1/knowledge/summary/").json()["documents"]["pending_delete"], 0)

    def test_access_rules(self):
        from shop.models import Shop
        r = self.client.get("/api/v1/knowledge/changes/")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "unauthorized"))
        self.assertSchema("error", r.json())
        no_scope = APIKey.objects.create(name="stock", scopes=["stock:read"])
        self.assertEqual(self.api("get", "/api/v1/knowledge/changes/", key=no_scope).json()["code"], "forbidden_scope")
        read_only = APIKey.objects.create(name="ro", scopes=["knowledge:read"])
        r = self._status([{"document_id": "email:1", "document_revision": 1, "attempt_id": 1, "state": "deleted",
                           "reported_at": "2026-10-20T08:15:09Z"}], key=read_only)
        self.assertEqual((r.status_code, r.json()["code"]), (403, "forbidden_scope"))
        shop = Shop.objects.create(name="Website", slug="webshop", is_default=True)
        shop_key = APIKey.objects.create(name="shop", scopes=["knowledge:read"], shop=shop)
        self.assertEqual(self.api("get", "/api/v1/knowledge/changes/", key=shop_key).json()["code"],
                         "shop_key_forbidden")
        self.client.force_login(self.user)   # сесія адміна теж не дає доступу
        self.assertEqual(self.client.get("/api/v1/knowledge/changes/").status_code, 401)
        with override_settings(MINERVA_INSTANCE_ID=""):
            r = self.api("get", "/api/v1/knowledge/changes/")
            self.assertEqual((r.status_code, r.json()["code"]), (503, "instance_not_configured"))

    def test_attachment_download_bound_to_revision(self):
        folder = os.path.join(self.media, "email_attachments")
        os.makedirs(folder)
        with open(os.path.join(folder, "spec.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 synthetic spec")
        m = self.mail(attachments=[
            {"name": "spec.pdf", "content_type": "application/pdf", "size": 23, "file_path": "email_attachments/spec.pdf"},
            {"name": "photo.heic", "content_type": "image/heic", "size": 10, "file_path": ""}])
        doc = self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/").json()
        self.assertSchema("document", doc)
        pdf, heic = doc["attachments"]
        self.assertTrue(pdf["indexable"])
        self.assertEqual((heic["indexable"], heic["reason"]), (False, "type_not_allowed"))
        r = self.client.get(pdf["url"], HTTP_AUTHORIZATION=f"Token {self.key.key}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"%PDF-1.4 synthetic spec")
        self.assertEqual("sha256:" + r["X-Content-SHA256"], pdf["content_hash"])
        r = self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/attachments/0/", data={"revision": 99})
        self.assertEqual((r.status_code, r.json()["code"]), (412, "revision_mismatch"))
        r = self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/attachments/1/",
                     data={"revision": doc["document_revision"]})
        self.assertEqual((r.status_code, r.json()["code"]), (404, "attachment_not_found"))

    def test_admin_page(self):
        self.client.force_login(self.user)
        self.mail()
        r = self.client.get("/admin/rag_assistant/knowledgesettings/1/change/")
        self.assertContains(r, "test-suite")
        self.assertContains(r, "knowledge:read")
        version = KnowledgeSettings.get().settings_version
        r = self.client.post("/admin/rag_assistant/knowledgesettings/1/change/", {
            "enabled": "on", "categories": ["customer"], "attachment_types_text": "pdf, txt",
            "attachment_max_mb": 10, "accounts": [self.account.pk]})
        self.assertEqual(r.status_code, 302, r.content[:500])
        ks = KnowledgeSettings.get()
        self.assertEqual((ks.settings_version, ks.attachment_types), (version + 1, ["pdf", "txt"]))
        self.assertTrue(ks.last_refresh_result)


@unittest.skipUnless(connection.vendor == "postgresql", "перевірка порядку commit — лише на PostgreSQL")
@override_settings(MINERVA_INSTANCE_ID="test-suite")
class CommitOrderPostgresTests(TransactionTestCase):
    """Дві транзакції записувачів: друга чекає на блокування, тож seq не може «обігнати» commit першої."""

    def test_second_writer_waits_and_gets_higher_seq(self):
        from django.db import connections
        started, release, order = threading.Event(), threading.Event(), []

        def first():
            try:
                with knowledge.journal_writer():
                    KnowledgeChange.objects.create(obj_id=1, action="upsert")
                    started.set()
                    release.wait(5)
                    order.append(("first", KnowledgeChange.objects.order_by("-id").first().id))
            finally:
                connections.close_all()

        def second():
            try:
                started.wait(5)
                with knowledge.journal_writer():
                    order.append(("second", KnowledgeChange.objects.create(obj_id=2, action="upsert").id))
            finally:
                connections.close_all()

        t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
        t1.start()
        t2.start()
        started.wait(5)
        visible = list(KnowledgeChange.objects.values_list("obj_id", flat=True))
        release.set()
        t1.join(10)
        t2.join(10)
        self.assertEqual(visible, [])                           # нічого не закомічено, поки перша тримає lock
        self.assertEqual([who for who, _ in order], ["first", "second"])
        self.assertLess(order[0][1], order[1][1])
