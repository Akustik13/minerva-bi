"""🎙 Прибирання нарад (services.cleanup, manage.py meetings_cleanup): python manage.py test meetings
--settings=tabele.settings_test"""
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from meetings import knowledge, services
from meetings.models import Meeting, MeetingItem, MeetingSettings, MeetingTombstone, MeetingTranscript
from meetings.services import PushError
from meetings.tests_services import ITEMS, MeetingBase
from projects.models import ProjectHighlight
from rag_assistant.models import KnowledgeItem


class CleanupTests(MeetingBase):
    def reviewed(self):
        m, token = self.processed(marks=[{"t": 125, "note": "Preis klären"}])
        self.moments(m, token, ITEMS)
        return Meeting.objects.get(pk=m.pk), token

    def age(self, m, days, **extra):
        when = timezone.now() - timedelta(days=days)
        Meeting.objects.filter(pk=m.pk).update(transcript_at=when, created_at=when, **extra)

    def test_claim_once_per_20_hours(self):
        now = timezone.now()
        self.assertTrue(services.claim_cleanup(now))
        self.assertFalse(services.claim_cleanup(now + timedelta(hours=19)))
        self.assertTrue(services.claim_cleanup(now + timedelta(hours=21)))

    def test_transcript_retention_keeps_protocol_and_accepted_items(self):
        m, token = self.reviewed()
        h = services.accept(self.owner, m.items.get(kind="promise"), "moment").highlight
        services.reject(self.owner, m.items.get(kind="risk"))
        knowledge.publish(m)
        self.age(m, 366)
        out = services.cleanup()
        self.assertEqual(out["transcripts"], 1)
        m.refresh_from_db()
        self.assertIsNotNone(m.transcript_deleted_at)
        self.assertFalse(MeetingTranscript.objects.filter(meeting=m).exists())
        self.assertEqual(list(m.items.values_list("kind", "status", "title", "quote")),
                         [("promise", "accepted", "", "")])                 # решта пунктів — геть
        self.assertEqual(m.marks, [{"t": 125.0, "label": "", "kind": "user"}])
        self.assertFalse(KnowledgeItem.objects.filter(source_key__startswith=f"meeting:{m.pk}:").exists())
        self.assertTrue(m.consent_all_agreed and m.consent_text and m.consent_excerpt)   # протокол лишився
        self.assertEqual(ProjectHighlight.objects.get(pk=h.pk).quote, "liefern die Antennen bis Freitag")
        with self.assertRaises(PushError) as e:
            services.check_push(m.pk, token, "s1")
        self.assertEqual(e.exception.code, "meeting_gone")
        self.assertEqual(services.sync([{"meeting": m.pk, "uid": str(m.uid), "session": "s1"}])[0]["action"],
                         "delete")

    def test_current_setting_shortens_snapshot(self):
        m, _ = self.reviewed()
        self.age(m, 120)
        self.assertEqual(services.cleanup()["transcripts"], 0)
        MeetingSettings.objects.filter(pk=1).update(transcript_retention_days=100)
        self.assertEqual(services.cleanup()["transcripts"], 1)

    def test_retention_never_below_30_days_and_incomplete_transcripts(self):
        m, token = self.recording()
        self.transcript(m, token, parts=2, part=1)                  # частина без пари — лежить у incoming
        Meeting.objects.filter(pk=m.pk).update(retention_days=1, created_at=timezone.now() - timedelta(days=20))
        self.assertEqual(services.cleanup()["transcripts"], 0)
        Meeting.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(days=31))
        self.assertEqual(services.cleanup()["transcripts"], 1)
        self.assertFalse(MeetingTranscript.objects.filter(meeting=m).exists())

    def test_protocol_retention_deletes_row_with_tombstone(self):
        m, _ = self.reviewed()
        pk = m.pk
        self.age(m, 1100)
        out = services.cleanup()
        self.assertEqual(out["protocols"], 1)
        self.assertFalse(Meeting.objects.filter(pk=pk).exists())
        self.assertEqual(MeetingTombstone.objects.get(meeting_id=pk).reason, "retention")

    def test_abandoned_preparation_deleted_attempts_kept(self):
        never = self.meeting()
        tried, token = self.recording()
        self.status(tried, token, "discarded", 2)
        old = timezone.now() - timedelta(days=31)
        Meeting.objects.filter(pk__in=[never.pk, tried.pk]).update(created_at=old)
        self.assertEqual(services.cleanup()["abandoned"], 1)
        self.assertFalse(Meeting.objects.filter(pk=never.pk).exists())
        self.assertTrue(Meeting.objects.filter(pk=tried.pk).exists())          # протокол скасованої спроби

    def test_stale_only_when_bench_seen_recently(self):
        m, token = self.recording()
        self.status(m, token, "recorded", 2)
        Meeting.objects.filter(pk=m.pk).update(bench_updated_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(services.cleanup()["stale"], 0)                     # RAG-ПК не на зв'язку — чекаємо
        services.sync([], bench={"version": "0.3.0"})
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(services.cleanup()["stale"], 1)
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_detail), ("failed", services.STALE_TEXT))
        self.assertEqual(self.delivered.call_count, 1)

    def test_stale_with_transcript_becomes_review(self):
        m, _ = self.processed()
        services.sync([], bench={"version": "0.3.0"})
        Meeting.objects.filter(pk=m.pk).update(bench_updated_at=timezone.now() - timedelta(hours=25))
        services.cleanup()
        m.refresh_from_db()
        self.assertEqual((m.status, m.moments_error), ("review", services.MOMENTS_FAILED_TEXT))

    def test_inactive_owner(self):
        m, token = self.reviewed()
        self.owner.is_active = False
        self.owner.save()
        services.cleanup()
        m.refresh_from_db()
        self.assertIsNotNone(m.owner_inactive_since)
        self.assertIsNone(m.transcript_deleted_at)                          # ще не 30 днів
        Meeting.objects.filter(pk=m.pk).update(owner_inactive_since=timezone.now() - timedelta(days=31))
        self.assertEqual(services.sync([{"meeting": m.pk, "uid": str(m.uid), "session": "s1"}])[0]["action"],
                         "delete")
        with self.assertRaises(PushError):
            services.check_push(m.pk, token, "s1")
        self.assertEqual(services.cleanup()["owner_gone"], 1)
        m.refresh_from_db()
        self.assertIsNotNone(m.transcript_deleted_at)
        self.assertEqual(m.items.filter(status=MeetingItem.PENDING).count(), 0)
        self.owner.is_active = True
        self.owner.save()
        services.cleanup()
        self.assertIsNone(Meeting.objects.get(pk=m.pk).owner_inactive_since)

    def test_old_tombstones_and_expired_tickets(self):
        m = self.meeting()
        pk = m.pk
        m.delete()
        MeetingTombstone.objects.filter(meeting_id=pk).update(created_at=timezone.now() - timedelta(days=61))
        live = self.meeting()
        services.issue_ticket(live, self.owner, now=timezone.now() - timedelta(hours=1))
        out = services.cleanup()
        self.assertEqual((out["tombstones"], out["tickets"]), (1, 1))
        self.assertEqual(Meeting.objects.get(pk=live.pk).ticket_hash, "")

    def test_command_and_cron(self):
        out = StringIO()
        call_command("meetings_cleanup", stdout=out)
        self.assertIn("transcripts: 0", out.getvalue())
        self.assertIsNotNone(MeetingSettings.load().cleanup_at)
        out = StringIO()
        call_command("meetings_cleanup", "--if-due", stdout=out)          # щойно прибирали — нічого
        self.assertEqual(out.getvalue(), "")
        script = (Path(settings.BASE_DIR) / "cron_runner.sh").read_text(encoding="utf-8")
        self.assertIn("python manage.py meetings_cleanup --if-due", script)
        self.assertNotIn("\r\n", script)
