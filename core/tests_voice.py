"""🎤 Голосовий набір через RAG-ПК — адреса вікна запису для static/admin/js/mv_voice.js:
python manage.py test core.tests_voice --settings=tabele.settings_test

Адреса потрапляє в сторінку лише коли наради ввімкнено («⚙️ Налаштування нарад»), інакше — лише браузер.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from core.context_processors import VOICE_BENCH_KEY, voice_bench
from meetings.models import MeetingSettings


class VoiceBenchTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        cache.delete(VOICE_BENCH_KEY)
        self.addCleanup(cache.delete, VOICE_BENCH_KEY)

    def settings(self, enabled, url="https://192.168.2.68:8791/"):
        s = MeetingSettings.load()
        s.enabled, s.bench_url = enabled, url
        s.save()
        cache.delete(VOICE_BENCH_KEY)

    def test_only_when_meetings_are_on(self):
        self.settings(False)
        self.assertEqual(voice_bench(), "")
        self.settings(True)
        self.assertEqual(voice_bench(), "https://192.168.2.68:8791")

    def test_page_passes_it_to_the_script(self):
        self.settings(True)
        user = User.objects.create_superuser("v", "v@x.example", "p")
        self.client.force_login(user)
        page = self.client.get("/admin/").content.decode()
        self.assertIn('mv_voice.js?v=6" data-bench="https://192.168.2.68:8791"', page)
        self.settings(False)
        page = self.client.get("/admin/").content.decode()
        self.assertIn('mv_voice.js?v=6" data-bench=""', page)
