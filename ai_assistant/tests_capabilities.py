"""🏛 Minerva AI — «Що я вмію»: роль в AI (адміністратору — завжди Admin), що вимкнено й чому, лист → «✉️ Відкрити
в пошті», знайомство з помічниками внизу праворуч.

    python manage.py test ai_assistant.tests_capabilities --settings=tabele.settings_test
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from ai_assistant import permissions
from ai_assistant.prompts import build_system_prompt
from config.models import SystemSettings
from core.models import UserProfile
from strategy.models import AISettings

User = get_user_model()


def _onboarded():
    ss = SystemSettings.objects.get_or_create(pk=1)[0]
    ss.is_onboarding_complete = True
    ss.save()


def _profile(user, **kw):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    for k, v in kw.items():
        setattr(profile, k, v)
    profile.save()
    return profile


def _on(items):
    return {c['tool'] for c in items if c['on']}


class RoleTests(TestCase):
    def setUp(self):
        _onboarded()
        self.admin = User.objects.create_superuser('boss', 'b@x.y', 'p')
        self.clerk = User.objects.create_user('clerk', 'c@x.y', 'p', is_staff=True)

    def test_admin_gets_admin_role_even_with_viewer_in_profile(self):
        profile = _profile(self.admin, ai_assistant_role='viewer', ai_enabled=True)
        self.assertEqual(permissions.effective_role(profile), 'admin')
        names = {t['name'] for t in permissions.get_allowed_tools(profile)}
        self.assertIn('compose_email', names)
        self.assertIn('get_recent_orders', names)

    def test_others_keep_profile_role(self):
        profile = _profile(self.clerk, ai_assistant_role='viewer', ai_enabled=True)
        self.assertEqual(permissions.effective_role(profile), 'viewer')
        caps = {c['tool']: c for c in permissions.capabilities(profile)}
        self.assertFalse(caps['compose_email']['on'])
        self.assertIn('Роль в AI асистенті', caps['compose_email']['why'])
        self.assertIn('Manager', caps['compose_email']['why'])
        self.assertTrue(caps['get_inventory_status']['on'])
        names = {t['name'] for t in permissions.get_allowed_tools(profile)}
        self.assertEqual(names, {'get_system_overview', 'get_inventory_status'})

    def test_global_switch_explained(self):
        s = AISettings.get()
        s.ai_allow_email_sending = False
        s.save()
        profile = _profile(self.admin, ai_enabled=True)
        caps = {c['tool']: c for c in permissions.capabilities(profile)}
        self.assertFalse(caps['compose_email']['on'])
        self.assertIn('🔒 Дозволи', caps['compose_email']['why'])
        self.assertNotIn('compose_email', {t['name'] for t in permissions.get_allowed_tools(profile)})

    def test_prompt_explains_instead_of_riddle(self):
        profile = _profile(self.clerk, ai_assistant_role='viewer', ai_enabled=True)
        prompt = build_system_prompt(profile)
        self.assertIn('НЕДОСТУПНО', prompt)
        self.assertIn('Скласти лист клієнту', prompt)
        self.assertNotIn('Ця мудрість відкрита лише обраним', prompt)


class CapabilitiesPageTests(TestCase):
    def setUp(self):
        _onboarded()
        self.admin = User.objects.create_superuser('boss', 'b@x.y', 'p')
        _profile(self.admin, ai_enabled=True)

    def test_capabilities_api(self):
        self.client.force_login(self.admin)
        d = self.client.get('/ai/capabilities/').json()
        self.assertTrue(d['enabled'])
        self.assertEqual(d['role'], 'admin')
        self.assertTrue(d['admin'])
        self.assertIn('compose_email', _on(d['items']))
        self.assertTrue(any('лист' in e for e in d['examples']))
        self.assertFalse(d['mailbox'])

    def test_capabilities_needs_login(self):
        self.assertEqual(self.client.get('/ai/capabilities/').status_code, 302)

    def test_page_has_helpers_intro(self):
        self.client.force_login(self.admin)
        page = self.client.get('/admin/')
        self.assertContains(page, '/static/admin/js/mv_ai_intro.js?v=1')
        self.assertContains(page, 'id="mv-cp-info"')
        self.assertContains(page, 'Minerva AI — питання про ваш бізнес')
