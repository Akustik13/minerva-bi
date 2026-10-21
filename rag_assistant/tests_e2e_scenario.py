"""Синтетичний сценарій наскрізного тесту (rag_assistant/e2e_scenario.py) — частина без RAG:
seed, кроки, події стрічки і повернення вихідного стану (щоб прогони можна було повторювати)."""
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from email_assistant.models import EmailAccount, EmailMessage
from rag_assistant import e2e_scenario as sc
from rag_assistant.models import KnowledgeChange, KnowledgeDoc


@override_settings(MINERVA_INSTANCE_ID="test-e2e")
class ScenarioDataTests(TestCase):
    def setUp(self):
        background = patch("rag_assistant.knowledge._start_background", lambda fn: fn())
        background.start()
        self.addCleanup(background.stop)
        self.media = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media)
        self._media.enable()
        self.addCleanup(self._media.disable)
        self.addCleanup(shutil.rmtree, self.media, True)
        sc.seed()

    def fed(self):
        return sorted(KnowledgeDoc.objects.filter(fed=True).values_list("obj_id", flat=True))

    def baseline(self):
        return sorted(sc.message(k).pk for k in sc.INDEXED_KEYS)

    def step(self, name, **kw):
        before = sc.last_seq()
        with self.captureOnCommitCallbacks(execute=True):    # зміни скриньки — після commit
            note, _ = sc.apply_step(name, wait=False, **kw)
        return note, sc.last_seq() - before

    def last(self):
        return KnowledgeChange.objects.latest("id")

    def test_seed_is_idempotent_and_feeds_only_indexable_mail(self):
        self.assertEqual(sc.seed()["created"], 0)
        self.assertEqual(EmailMessage.objects.count(), len(sc.MAILS))
        self.assertEqual(self.fed(), self.baseline())                     # розсилка m6 не йде в стрічку
        self.assertTrue(sc.message("m3").attachments[0]["file_path"].endswith("spezifikation-ant-2400.txt"))

    def test_steps_produce_expected_feed_events(self):
        sales = EmailAccount.objects.get(email_address=sc.SALES)
        self.step("new-mail", added_key="r1")
        self.assertEqual((self.last().action, self.last().obj_id), ("upsert", sc.message("r1").pk))
        _, events = self.step("reassign")
        self.assertEqual(events, 6)                                       # 5 базових листів vertrieb + новий
        self.step("restore-owner")
        _, events = self.step("deactivate")
        self.assertEqual(set(KnowledgeChange.objects.order_by("-id")[:events].values_list("reason", flat=True)),
                         {"access_revoked"})
        self.step("activate")
        self.step("exclude")
        self.assertEqual((self.last().action, self.last().reason), ("delete", "excluded"))
        self.step("delete")
        self.assertEqual((self.last().obj_id, self.last().reason), (sc.message("m5").pk, "deleted"))
        added = sc.message("r1").pk
        self.step("purge", added_key="r1")
        self.assertEqual((self.last().obj_id, self.last().reason), (added, "deleted"))
        sales.refresh_from_db()
        self.assertTrue(sales.is_active)

    def test_edit_toggles_cab7_date(self):
        original = sc.message("m4").body_text
        self.step("edit")
        self.assertIn(sc.CAB_NEW, sc.message("m4").body_text)
        self.step("edit")
        self.assertEqual(sc.message("m4").body_text, original)

    def test_restore_baseline_after_any_state(self):
        self.step("new-mail", added_key="r1")
        self.step("edit")
        self.step("reassign")
        self.step("deactivate")
        self.step("exclude")
        self.step("delete")
        sc.message("m3").delete()                                         # як ручний purge старої версії
        with self.captureOnCommitCallbacks(execute=True):
            fixes = sc.restore_baseline()
        self.assertTrue(fixes)
        sales = EmailAccount.objects.get(email_address=sc.SALES)
        self.assertEqual((sales.user.username, sales.is_active), ("e2e_anna", True))
        self.assertIsNone(sc.message("r1"))
        self.assertIn(sc.CAB_OLD, sc.message("m4").body_text)
        self.assertFalse(sc.message("m5").is_deleted)
        self.assertFalse(sc.message("m4").thread.knowledge_excluded)
        self.assertEqual(self.fed(), self.baseline())                     # m3 відновлено з новим pk
        doc = KnowledgeDoc.objects.get(obj_id=sc.message("m1").pk)
        self.assertEqual(doc.feed_version, KnowledgeDoc.objects.get(pk=doc.pk).feed_version)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(sc.restore_baseline(), [])                   # повторно — нічого не змінює
