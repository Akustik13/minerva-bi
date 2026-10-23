"""Жива панель «Що зараз робить RAG», пауза стрічки: python manage.py test rag_assistant --settings=tabele.settings_test"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from rag_assistant import knowledge
from rag_assistant.models import KnowledgeActivity, KnowledgeDoc, KnowledgeSettings
from rag_assistant.tests_knowledge import KnowledgeBase


class FeedPauseTests(KnowledgeBase):
    def test_pause_answers_rate_limited_but_accepts_reports(self):
        msg = self.mail()
        knowledge.set_feed_paused(True)
        for method, path in [("get", "/api/v1/knowledge/summary/"), ("get", "/api/v1/knowledge/changes/"),
                             ("post", "/api/v1/knowledge/manifest/snapshots/"),
                             ("get", f"/api/v1/knowledge/documents/email:{msg.pk}/")]:
            r = self.api(method, path)
            self.assertEqual((r.status_code, r.json()["code"], r.json()["retryable"]), (429, "rate_limited", True), path)
            self.assertEqual(r.json()["details"]["retry_after_seconds"], knowledge.PAUSE_RETRY_SECONDS)
            self.assertEqual(r["Retry-After"], str(knowledge.PAUSE_RETRY_SECONDS))
            self.assertSchema("error", r.json())
        r = self.api("post", "/api/v1/knowledge/status/", data={"items": []})   # звіти не блокуються паузою
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_request"))
        self.assertEqual(KnowledgeActivity.get().last_call, "запит під час паузи — RAG чекає")
        self.assertEqual(knowledge.progress()["stage"], "paused")
        knowledge.set_feed_paused(False)
        self.assertEqual(self.changes()["items"][0]["document_id"], f"email:{msg.pk}")


class ActivityTests(KnowledgeBase):
    def test_snapshot_then_changes_progress(self):
        docs = [self.mail(f"Topic {i}") for i in range(4)]
        self.assertEqual(knowledge.progress()["stage"], "waiting")      # RAG ще не звертався
        snap = self.api("post", "/api/v1/knowledge/manifest/snapshots/").json()
        act = KnowledgeActivity.get()
        self.assertEqual((act.phase, act.snapshot_total, act.snapshot_served), (KnowledgeActivity.SNAPSHOT, 4, 0))
        self.api("get", f"/api/v1/knowledge/manifest/snapshots/{snap['snapshot_id']}/")
        for m in docs[:3]:
            self.assertEqual(self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/").status_code, 200)
        p = knowledge.progress()
        self.assertEqual((p["stage"], p["step"], p["served"], p["total"], p["percent"]), ("snapshot", 1, 3, 4, 75))
        self.assertIsNotNone(p["eta"])
        self.assertIn(f"email:{docs[2].pk}", p["last_call"])
        self.changes(snap["snapshot_seq"])                                # знімок завершено — RAG перейшов до змін
        self.assertEqual(KnowledgeActivity.get().phase, KnowledgeActivity.CHANGES)
        p = knowledge.progress()
        self.assertEqual((p["stage"], p["step"], p["percent"]), ("confirm", 3, None))
        self.assertEqual([st["status"] for st in p["steps"]], ["done", "done", "done", "current", "todo"])
        self.assertIn("ще 4", p["steps"][3]["detail"])
        last = knowledge.summary()["last_seq"]                            # RAG ще не застосував увесь журнал
        KnowledgeSettings.objects.update(last_capabilities={"index": {"received_seq": last, "applied_seq": last - 2}})
        p = knowledge.progress()
        self.assertEqual((p["stage"], p["step"], p["seq"]["applied"]), ("changes", 2, last - 2))
        self.assertEqual(p["steps"][2]["detail"], "ще подій 2")
        KnowledgeSettings.objects.update(last_capabilities={"index": {"received_seq": last, "applied_seq": last}})
        KnowledgeDoc.objects.update(state=KnowledgeDoc.INDEXED, status_revision=1)   # RAG підтвердив усі
        p = knowledge.progress()
        self.assertEqual((p["stage"], p["step"], p["silent"]), ("ready", 4, False))
        self.assertEqual({st["status"] for st in p["steps"]}, {"done"})
        KnowledgeActivity.objects.update(last_call_at=timezone.now() - timedelta(minutes=10))
        self.assertTrue(knowledge.progress()["silent"])                  # RAG замовк — попередження

    def test_no_eta_while_paused_or_silent(self):
        docs = [self.mail(f"Topic {i}") for i in range(5)]
        self.api("post", "/api/v1/knowledge/manifest/snapshots/")
        for m in docs[:4]:
            self.api("get", f"/api/v1/knowledge/documents/email:{m.pk}/")
        self.assertIsNotNone(knowledge.progress()["eta"])
        knowledge.set_feed_paused(True)
        p = knowledge.progress()
        self.assertEqual((p["stage"], p["percent"], p["eta"], p["rate"]), ("paused", 80, None, None))
        self.assertEqual((p["step"], p["steps"][1]["status"]), (1, "current"))   # пауза на кроці «Повна вигрузка»
        self.assertIn("«Повна вигрузка»", p["text"])
        knowledge.set_feed_paused(False)
        KnowledgeActivity.objects.update(last_call_at=timezone.now() - timedelta(minutes=10))
        p = knowledge.progress()
        self.assertEqual((p["eta"], p["silent"]), (None, True))

    def test_reports_are_counted(self):
        m = self.mail()
        self.changes()
        doc = KnowledgeDoc.objects.get(obj_id=m.pk)
        r = self.api("post", "/api/v1/knowledge/status/", data={
            "source_instance_id": "test-suite", "generation": str(KnowledgeSettings.get().generation),
            "items": [{"document_id": f"email:{m.pk}", "document_revision": doc.revision, "attempt_id": 1,
                       "state": "indexed", "content_version": doc.feed_version, "chunks": 2,
                       "reported_at": timezone.now().isoformat()}]})
        self.assertEqual(r.json()["accepted"], 1, r.content[:300])
        self.assertEqual(KnowledgeActivity.get().reported, 1)
        self.assertEqual(knowledge.progress()["stage"], "ready")

    def test_off_when_disabled_and_nothing_fed(self):
        KnowledgeSettings.objects.update(enabled=False)
        p = knowledge.progress()
        self.assertEqual((p["stage"], p["step"]), ("off", None))
        self.assertEqual({st["status"] for st in p["steps"]}, {"todo"})


class AdminLiveTests(KnowledgeBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_page_live_json_and_pause_buttons(self):
        page = self.client.get(reverse("admin:rag_assistant_knowledgesettings_change", args=[KnowledgeSettings.get().pk]))
        self.assertContains(page, "Що зараз робить RAG")
        self.assertContains(page, "⏸ Пауза")
        self.assertContains(page, 'id="kl-data"')
        live = self.client.get(reverse("admin:rag_assistant_knowledgesettings_live") + "?probe=1")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["stage"], "waiting")
        pause = reverse("admin:rag_assistant_knowledgesettings_pause")
        self.client.post(pause + "?action=pause")
        self.assertTrue(KnowledgeSettings.get().feed_paused)
        page = self.client.get(reverse("admin:rag_assistant_knowledgesettings_change", args=[1]))
        self.assertContains(page, "▶ Продовжити індексацію")
        self.client.post(pause + "?action=resume")
        self.assertFalse(KnowledgeSettings.get().feed_paused)
        self.client.get(pause + "?action=pause")                         # GET нічого не змінює
        self.assertFalse(KnowledgeSettings.get().feed_paused)

    def test_staff_without_permission_gets_no_json(self):
        from django.contrib.auth.models import User
        staff = User.objects.create_user("viewer", "v@x.y", "p", is_staff=True)
        self.client.force_login(staff)
        r = self.client.get(reverse("admin:rag_assistant_knowledgesettings_live"))
        self.assertIn(r.status_code, (302, 403))
