"""Аудит безпеки: ключі в адмінці, налаштування AI, профілі колег, закрито при помилці, IP у журналі аудиту.

python manage.py test core.tests_security --settings=tabele.settings_test
"""
from django import forms
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from core.models import UserProfile
from core.utils import client_ip, user_has_operation
from shipping.models import Carrier

KEY = "SECRET-API-KEY-123456789"
MASKED = "SECR••••••••6789"


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


def form_data(response):
    """Дані форми адмінки так, як їх надішле браузер (поля-паролі порожні — сторінка їх не підставляє)."""
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if isinstance(field.widget, forms.PasswordInput):
            data[name] = ""
        elif isinstance(field, forms.BooleanField):
            if value:
                data[name] = "on"
        else:
            data[name] = "" if value is None else value
    for formset in response.context.get("inline_admin_formsets") or []:
        mf = formset.formset.management_form
        data.update({f"{mf.prefix}-{k}": v for k, v in mf.initial.items()})
    return data


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.root = User.objects.create_superuser("root", "r@x.y", "p")


class SecretFieldTests(_Base):
    def setUp(self):
        super().setUp()
        self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups", api_key=KEY, api_secret="pw-secret-987654")
        self.url = f"/admin/shipping/carrier/{self.carrier.pk}/change/"

    def test_editor_sees_mask_and_empty_field_keeps_key(self):
        self.client.force_login(self.root)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, KEY)
        self.assertNotContains(r, "pw-secret-987654")
        self.assertContains(r, MASKED)                                    # підказка «збережено: …»
        data = form_data(r)
        data["name"] = "UPS Express"
        r = self.client.post(self.url, data)
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.carrier.refresh_from_db()
        self.assertEqual((self.carrier.name, self.carrier.api_key, self.carrier.api_secret),
                         ("UPS Express", KEY, "pw-secret-987654"))         # порожнє поле — ключ лишився
        data["api_key"] = "NEW-KEY-000000000000"
        self.client.post(self.url, data)
        self.carrier.refresh_from_db()
        self.assertEqual(self.carrier.api_key, "NEW-KEY-000000000000")

    def test_view_only_sees_masked(self):
        kim = person("kim", "readonly", allowed_modules=["shipping"])   # модуль є, операцій немає — лише перегляд
        self.client.force_login(kim)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, KEY)
        self.assertContains(r, MASKED)


class AISettingsTests(_Base):
    def test_only_admins(self):
        from strategy.models import AISettings
        ai = AISettings.get() if hasattr(AISettings, "get") else AISettings.objects.get_or_create(pk=1)[0]
        ai.anthropic_api_key = "sk-ant-api03-SECRETSECRET-abcd"
        ai.save()
        olga = person("olga")                                  # роль має модуль «Стратегії»
        self.client.force_login(olga)
        self.assertEqual(self.client.get("/admin/strategy/aisettings/").status_code, 403)
        self.assertEqual(self.client.get(f"/admin/strategy/aisettings/{ai.pk}/change/").status_code, 403)
        self.client.force_login(person("boss", "admin"))
        r = self.client.get(f"/admin/strategy/aisettings/{ai.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "sk-ant-api03-SECRETSECRET-abcd")


class UserProfileTests(_Base):
    def setUp(self):
        super().setUp()
        self.olga = person("olga", smtp_password="olga-mail-pass-1")
        self.ivan = person("ivan", smtp_password="ivan-mail-pass-2")

    def test_manager_sees_only_own_profile_without_passwords(self):
        self.client.force_login(self.olga)
        r = self.client.get("/admin/core/userprofile/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p.user.username for p in r.context["cl"].result_list], ["olga"])
        other = self.client.get(f"/admin/core/userprofile/{self.ivan.profile.pk}/change/")
        self.assertNotEqual(other.status_code, 200)                       # чужого профілю немає
        own = self.client.get(f"/admin/core/userprofile/{self.olga.profile.pk}/change/")
        self.assertEqual(own.status_code, 200)
        self.assertNotContains(own, "olga-mail-pass-1")
        self.assertNotContains(own, "ivan-mail-pass-2")

    def test_user_manager_sees_all_and_cannot_be_escalated_by_others(self):
        hr = person("hr", can_manage_users=True)
        self.client.force_login(hr)
        r = self.client.get("/admin/core/userprofile/")
        self.assertEqual({p.user.username for p in r.context["cl"].result_list}, {"root", "olga", "ivan", "hr"})
        self.client.force_login(self.olga)
        self.client.post(f"/admin/core/userprofile/{self.olga.profile.pk}/change/", {"role": "admin"})
        self.assertEqual(UserProfile.objects.get(pk=self.olga.profile.pk).role, "manager")


class FailClosedTests(_Base):
    def test_no_profile_blocks_modules(self):
        ghost = person("ghost")
        UserProfile.objects.filter(user=ghost).delete()
        ghost = User.objects.get(pk=ghost.pk)
        self.assertFalse(user_has_operation(ghost, "crm", "view"))
        self.client.force_login(ghost)
        self.assertEqual(self.client.get("/admin/crm/customer/").status_code, 403)


class ClientIpTests(TestCase):
    def test_proxy_rules(self):
        rf = RequestFactory()
        self.assertEqual(client_ip(rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9")), "203.0.113.9")
        self.assertEqual(client_ip(rf.get("/", HTTP_X_REAL_IP="198.51.100.4", HTTP_X_FORWARDED_FOR="6.6.6.6")),
                         "198.51.100.4")
        self.assertEqual(client_ip(rf.get("/", HTTP_X_FORWARDED_FOR="not-an-ip")), "127.0.0.1")
        self.assertIsNone(client_ip(None))
