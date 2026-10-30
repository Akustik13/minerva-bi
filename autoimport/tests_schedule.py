"""Авто-імпорт за розкладом (cron: auto_import --all): профіль іде лише коли настав час і лише один раз.

    python manage.py test autoimport --settings=tabele.settings_test
"""
import datetime
import os
import shutil
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from autoimport import runner
from autoimport.models import AutoImportLog, AutoImportProfile


class AutoImportScheduleTests(TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.profile = AutoImportProfile.objects.create(
            name="Shop orders", import_type=AutoImportProfile.TYPE_PRODUCTS,
            source_type=AutoImportProfile.SOURCE_FOLDER, source_path=self.folder,
            file_mask="*.csv", interval_minutes=60, archive_processed=False, notify=False)
        with open(os.path.join(self.folder, "products.csv"), "w", encoding="utf-8") as f:
            f.write("sku,name\nFT1,Filter\n")
        p = mock.patch.object(runner, "_call_importer",
                              return_value={"created": 1, "updated": 0, "skipped": 0, "errors": []})
        self.importer = p.start()
        self.addCleanup(p.stop)

    def cron(self):
        out = StringIO()
        call_command("auto_import", "--all", "--verbosity", "0", stdout=out)
        return out.getvalue()

    def test_due_profile_runs_once_then_waits_for_interval(self):
        out = self.cron()
        self.assertEqual(self.importer.call_count, 1)
        self.assertIn("Shop orders", out)
        self.profile.refresh_from_db()
        self.assertGreater(self.profile.next_run_at, timezone.now() + datetime.timedelta(minutes=59))
        for _ in range(3):                                  # cron щохвилини — до кінця інтервалу нічого
            self.assertEqual(self.cron(), "")
        self.assertEqual(self.importer.call_count, 1)
        AutoImportProfile.objects.filter(pk=self.profile.pk).update(
            next_run_at=timezone.now() - datetime.timedelta(seconds=1))
        self.cron()                                         # настав час — знову (файл той самий → дублікат)
        self.assertEqual(self.importer.call_count, 1)
        self.assertTrue(AutoImportLog.objects.filter(status=AutoImportLog.STATUS_SKIPPED).exists())

    def test_claim_prevents_parallel_run(self):
        self.assertTrue(runner.claim(self.profile))
        self.assertFalse(runner.claim(self.profile))        # другий процес у ту саму хвилину — ні
        self.assertEqual(runner.run_profile(self.profile.pk), [])
        self.importer.assert_not_called()

    def test_disabled_profile_never_runs(self):
        AutoImportProfile.objects.filter(pk=self.profile.pk).update(enabled=False)
        self.assertEqual(self.cron(), "")
        self.assertFalse(runner.claim(self.profile))
        self.importer.assert_not_called()

    def test_crash_during_import_does_not_retry_every_minute(self):
        self.importer.side_effect = RuntimeError("boom")
        with mock.patch.object(AutoImportProfile, "update_schedule"):   # «упав» до кінця запуску
            self.cron()
        self.profile.refresh_from_db()
        self.assertGreater(self.profile.next_run_at, timezone.now())    # бронь уже зсунула час
        self.importer.side_effect = None
        self.cron()
        self.assertEqual(self.importer.call_count, 1)

    def test_force_button_still_runs(self):
        self.cron()
        runner.run_profile(self.profile.pk, force=True)
        self.assertEqual(self.importer.call_count, 2)
