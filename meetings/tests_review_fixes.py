"""🎙 Виправлення після ревізії (повідомлення з прибирання, повтор і повідомлення, повтор пошуку ключових моментів,
подвійний ✓, загублена відповідь retry, завислі записи щоциклу, блокування мов і режиму, мови й позначки з RAG-ПК):
python manage.py test meetings --settings=tabele.settings_test"""
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from meetings import notify, services
from meetings.models import Meeting, MeetingSettings, MeetingTranscript
from meetings.services import MeetingError
from meetings.tests_services import ITEMS, SEGMENTS, MeetingBase
from projects.models import ProjectHighlight


class CleanupCommandNotifiesTests(MeetingBase):
    def stale(self):
        m, token = self.recording()
        Meeting.objects.filter(pk=m.pk).update(bench_updated_at=timezone.now() - timedelta(hours=25))
        services.sync([], bench={"version": "0.3.0"})
        return m

    def test_command_sends_failed_notification_in_process(self):
        """1: фоновий потік зник би разом із командою — повідомлення надсилається в самій команді."""
        m = self.stale()
        notify.BACKGROUND = True
        with patch("meetings.notify.threading.Thread") as thread, self.captureOnCommitCallbacks(execute=True):
            call_command("meetings_cleanup", stdout=StringIO())
        thread.assert_not_called()
        self.assertTrue(notify.BACKGROUND)                         # прапорець повернуто
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "failed")
        self.assertEqual(self.delivered.call_count, 1)

    def test_stale_sweep_every_loop_outside_claim(self):
        """6: завислі записи — щоциклу, навіть коли повне прибирання вже було (позначка за 20 год)."""
        self.assertTrue(services.claim_cleanup())
        m = self.stale()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("meetings_cleanup", "--if-due", stdout=StringIO())
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "failed")
        self.assertEqual(self.delivered.call_count, 1)
        script = (Path(settings.BASE_DIR) / "cron_runner.sh").read_text(encoding="utf-8")
        daily = script[script.index('if [ "$current_day" != "$last_cleanup_day" ]'):]
        daily = daily[:daily.index("\n  fi\n")]
        self.assertIn("python manage.py meetings_cleanup --if-due", script)
        self.assertNotIn("meetings_cleanup", daily)                # не лише раз на добу
        self.assertNotIn("\r\n", script)

    def test_sweep_rechecks_under_lock(self):
        m = self.stale()
        Meeting.objects.filter(pk=m.pk).update(bench_updated_at=timezone.now())
        self.assertFalse(services._mark_stale(m, timezone.now() - timedelta(hours=24)))
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "recording")


class RetryNotifiesAgainTests(MeetingBase):
    def failed(self):
        m, token = self.recording()
        with self.captureOnCommitCallbacks(execute=True):
            self.status(m, token, "error", 2, stage="transcribe", message="GPU OOM")
        self.assertEqual(self.delivered.call_count, 1)
        return Meeting.objects.get(pk=m.pk), token

    def test_retry_then_success_notifies_again(self):
        """2: після «🔄 Спробувати ще раз» повідомлення про результат приходить знову."""
        m, token = self.failed()
        services.retry(self.owner, m)
        self.assertIsNone(Meeting.objects.get(pk=m.pk).notified_at)
        with self.captureOnCommitCallbacks(execute=True):
            self.transcript(Meeting.objects.get(pk=m.pk), token)
            self.moments(Meeting.objects.get(pk=m.pk), token, ITEMS)
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "review")
        self.assertEqual(self.delivered.call_count, 2)
        self.assertIn("розшифрована", self.delivered.call_args.args[1]["head"])

    def test_rerecord_from_failed_resets_notified(self):
        """2: «🎙 Записати заново» — новий запис, нове повідомлення."""
        m, _ = self.failed()
        self.assertIsNotNone(m.notified_at)
        services.issue_ticket(m, self.owner)
        self.assertIsNone(Meeting.objects.get(pk=m.pk).notified_at)


class MomentsRetryTests(MeetingBase):
    def test_moments_failure_can_be_retried(self):
        """3: розшифровка є, ключові моменти — ні → «🔄 Спробувати ще раз» з review; розшифровка лишається."""
        m, token = self.processed()
        with self.captureOnCommitCallbacks(execute=True):
            self.status(m, token, "error", 3, stage="moments", message="ollama down")
        m.refresh_from_db()
        self.assertEqual((m.status, m.moments_error), ("review", services.MOMENTS_FAILED_TEXT))
        self.assertTrue(services.can_retry(m))
        services.retry(self.owner, m)
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_detail), ("processing", services.RETRY_TEXT))
        self.assertTrue(m.has_transcript)
        self.assertTrue(MeetingTranscript.objects.get(meeting=m).segments)
        self.assertEqual(services.sync([{"meeting": m.pk, "uid": str(m.uid), "session": "s1"}])[0]["action"],
                         "retry")
        with self.captureOnCommitCallbacks(execute=True):
            self.status(m, token, "finding_moments", 4)
            self.moments(Meeting.objects.get(pk=m.pk), token, ITEMS)
        m.refresh_from_db()
        self.assertEqual((m.status, m.moments_error, m.bench_detail), ("review", "", ""))
        self.assertIsNotNone(m.retry_sent_at)
        self.assertEqual(self.delivered.call_count, 2)
        self.assertFalse(services.can_retry(m))                  # моменти вже є

    def test_no_retry_for_normal_review_or_deleted_transcript(self):
        m, token = self.processed()
        self.moments(m, token, ITEMS)
        self.assertFalse(services.can_retry(Meeting.objects.get(pk=m.pk)))
        Meeting.objects.filter(pk=m.pk).update(moments_error="x", moments_at=None,
                                               transcript_deleted_at=timezone.now())
        self.assertFalse(services.can_retry(Meeting.objects.get(pk=m.pk)))


class DoubleAcceptTests(MeetingBase):
    def test_second_accept_of_same_item_refused(self):
        """4: два POST «✓» поспіль — один момент у проєкті."""
        m, token = self.processed()
        self.moments(m, token, ITEMS)
        first = m.items.get(kind="promise")
        stale_copy = m.items.get(kind="promise")                 # другий запит прочитав пункт ще «pending»
        services.accept(self.owner, first, "moment")
        with self.assertRaisesMessage(MeetingError, "уже вирішено"):
            services.accept(self.owner, stale_copy, "moment")
        self.assertEqual(ProjectHighlight.objects.filter(meeting=m).count(), 1)


class RetryAckTests(MeetingBase):
    def test_status_after_retry_acknowledges(self):
        """5: retry повторюється, доки RAG-ПК не надішле статус із at після запиту."""
        m, token = self.recording()
        self.status(m, token, "error", 2, stage="transcribe", message="x")
        services.retry(self.owner, Meeting.objects.get(pk=m.pk))
        entry = {"meeting": m.pk, "uid": str(m.uid), "session": "s1"}
        self.assertEqual(services.sync([entry])[0]["action"], "retry")
        self.assertEqual(services.sync([entry])[0]["action"], "retry")
        self.status(m, token, "transcribing", 3, progress=10)
        m.refresh_from_db()
        self.assertGreaterEqual(m.retry_sent_at, m.retry_requested_at)
        self.assertEqual(m.bench_detail, "")                      # «🔄 Ще одна спроба» знято новим кроком
        self.assertEqual(services.sync([entry])[0]["action"], "keep")


class EditLockAndDetailTests(MeetingBase):
    def test_languages_and_mode_locked_after_redeem(self):
        """7: вікно запису вже має мови й режим цієї спроби — у Minerva їх не змінити."""
        m = self.meeting()
        services.issue_ticket(m, self.owner)
        services.edit(self.owner, Meeting.objects.get(pk=m.pk), {"lang_l": "uk"})   # ще не відкрили — можна
        m, _ = self.bound(Meeting.objects.get(pk=m.pk))
        with self.assertRaisesMessage(MeetingError, "уже передано у вікно запису"):
            services.edit(self.owner, m, {"lang_r": "de"})
        with self.assertRaisesMessage(MeetingError, "уже передано"):
            services.edit(self.owner, m, {"retention": "audio_text"})
        services.edit(self.owner, m, {"lang_l": "uk", "retention": m.retention, "title": "Neu"})   # без змін — ок
        self.assertEqual(Meeting.objects.get(pk=m.pk).title, "Neu")
        services.issue_ticket(m, self.owner)                      # нове посилання — знову можна
        services.edit(self.owner, Meeting.objects.get(pk=m.pk), {"lang_r": "de"})
        self.assertEqual(Meeting.objects.get(pk=m.pk).lang_r, "de")

    def test_stale_detail_cleared_on_new_stage_only(self):
        m, token = self.recording()
        self.status(m, token, "recorded", 2, detail="⏳ черга: 2-га")
        self.status(m, token, "transcribing", 3, progress=40, detail="40 %")
        self.status(m, token, "transcribing", 4, progress=50)       # той самий крок без пояснення — лишається
        self.assertEqual(Meeting.objects.get(pk=m.pk).bench_detail, "40 %")
        self.status(m, token, "finding_moments", 5)
        self.assertEqual(Meeting.objects.get(pk=m.pk).bench_detail, "")


class BenchShapesTests(MeetingBase):
    def test_languages_with_probability(self):
        """8a: мова каналу — рядком або {language, probability}; зберігається лише код."""
        m, token = self.bound()
        self.status(m, token, "recording", 1, consent={"announced": True, "all_agreed": True},
                    languages={"L": {"language": "uk", "probability": 0.93}, "R": "de"})
        m.refresh_from_db()
        self.assertEqual((m.lang_l, m.lang_r), ("uk", "de"))
        self.transcript(m, token, languages={"L": {"language": "UK", "probability": 0.9}, "R": "de", "X": 5})
        self.assertEqual(MeetingTranscript.objects.get(meeting=m).languages, {"L": "uk", "R": "de"})

    def test_mark_wall_clock_for_consent_log(self):
        """8b: позначка з at (годинник ПК) — цей час у протоколі; поза записом — початок + t."""
        m, token = self.recording()
        start = Meeting.objects.get(pk=m.pk).started_at
        inside = (start + timedelta(seconds=95)).isoformat()
        outside = (start - timedelta(days=2)).isoformat()
        self.status(m, token, "recorded", 2, duration=300,
                    marks=[{"t": 90, "note": "consent_join", "at": inside},
                           {"t": 200, "note": "consent_ok", "at": outside}, {"t": 210, "note": "consent_ok"}])
        m.refresh_from_db()
        log = {e["t"]: e["at"] for e in m.consent_log if e["kind"] in ("join", "confirmed")}
        self.assertEqual(log[90], services._iso(start + timedelta(seconds=95)))
        self.assertEqual(log[200], services._iso(start + timedelta(seconds=200)))
        self.assertEqual(log[210], services._iso(start + timedelta(seconds=210)))
        self.assertEqual(m.marks[0]["at"], (start + timedelta(seconds=95)).isoformat(timespec="seconds"))
