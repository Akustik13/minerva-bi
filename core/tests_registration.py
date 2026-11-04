"""Самореєстрація (/register/, tabele/views.py): за замовчуванням закрита; увімкнена — без доступу до Minerva,
доки адміністратор не відкриє його вручну; адміністратор дізнається про кожну реєстрацію.

python manage.py test core.tests_registration --settings=tabele.settings_test
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from config.models import NotificationSettings, SystemSettings
from core.models import TenantAccount
from core.shared_cache import cache
from core.utils import is_minerva_admin


def form(**over):
    data = {
        "company_name": "Evil GmbH", "company_country": "DE", "contact_phone": "", "package": "business",
        "owner_name": "Mal Lory", "email": "mal@example.com",
        "password1": "Very-long-pass-123", "password2": "Very-long-pass-123", "agree_terms": "on",
    }
    data.update(over)
    return data


def pending_account(email="mal@example.com", sent=None):
    """Як після атаки: неактивний користувач з роллю «Адміністратор» і неперевіреним токеном."""
    user = User.objects.create_user("mal", email, "x", is_active=False, is_staff=False)
    user.profile.role = "admin"
    user.profile.save()
    tenant = TenantAccount.objects.create(company_name="Evil GmbH", owner_email=email, owner_user=user,
                                          status="pending", verification_token="tok-" + "a" * 40,
                                          verification_sent_at=sent or timezone.now())
    return user, tenant


class ClosedByDefaultTests(TestCase):
    def test_setting_is_off(self):
        from django.conf import settings
        self.assertFalse(settings.PUBLIC_REGISTRATION)

    def test_register_page_closed(self):
        response = self.client.get("/register/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Реєстрацію закрито", status_code=404)

    def test_post_creates_nothing(self):
        response = self.client.post("/register/", form())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(User.objects.filter(email="mal@example.com").exists())
        self.assertFalse(TenantAccount.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_pending_page_closed(self):
        self.assertEqual(self.client.get("/register/pending/").status_code, 404)

    def test_old_link_does_not_activate(self):
        user, tenant = pending_account()
        response = self.client.get(f"/verify/{tenant.verification_token}/")
        self.assertEqual(response.status_code, 404)
        user.refresh_from_db()
        tenant.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(tenant.email_verified)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_register_links(self):
        for url in ("/admin/login/", "/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotContains(response, 'href="/register/', msg_prefix=url)


@override_settings(PUBLIC_REGISTRATION=True)
class OpenRegistrationTests(TestCase):
    def setUp(self):
        cache.clear()
        s = SystemSettings.get()
        s.company_email = "boss@firma.de"
        s.save()
        ns = NotificationSettings.get()
        ns.email_to = "alerts@firma.de, Boss@firma.de"
        ns.save()

    def register(self, ip="203.0.113.7", **over):
        return self.client.post("/register/", form(**over), REMOTE_ADDR=ip)

    def test_account_has_no_access(self):
        response = self.register()
        self.assertRedirects(response, "/register/pending/", fetch_redirect_response=False)
        user = User.objects.get(email="mal@example.com")
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.profile.role, "readonly")
        self.assertFalse(is_minerva_admin(user))

    def test_vendor_is_told(self):
        self.register()
        to_vendor = [m for m in mail.outbox if "mal@example.com" not in m.to]
        self.assertEqual(len(to_vendor), 1)
        self.assertEqual(to_vendor[0].to, ["boss@firma.de", "alerts@firma.de"])
        self.assertIn("203.0.113.7", to_vendor[0].body)
        to_owner = [m for m in mail.outbox if m.to == ["mal@example.com"]]
        self.assertEqual(len(to_owner), 1)

    def test_newline_in_company_does_not_drop_mail(self):
        self.register(company_name="Evil\nBcc: x@y.z")
        subjects = [m.subject for m in mail.outbox]
        self.assertTrue(any("Evil Bcc: x@y.z" in s for s in subjects), subjects)
        self.assertTrue(all("\n" not in s for s in subjects))

    def test_telegram_text_is_escaped(self):
        ns = NotificationSettings.get()
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "123:abc", "42"
        ns.save()
        with mock.patch("dashboard.notifications._send_telegram") as send:
            self.register(company_name="<b>R&D</b>")
        text = send.call_args[0][1]
        self.assertIn("&lt;b&gt;R&amp;D&lt;/b&gt;", text)
        self.assertIn("203.0.113.7", text)

    def test_verify_confirms_email_only(self):
        self.register()
        tenant = TenantAccount.objects.get()
        mail.outbox.clear()
        token = tenant.verification_token
        response = self.client.get(f"/verify/{token}/")
        self.assertRedirects(response, "/verify/success/", fetch_redirect_response=False)
        tenant.refresh_from_db()
        user = tenant.owner_user
        self.assertTrue(tenant.email_verified)
        self.assertEqual(tenant.verification_token, "")
        self.assertEqual(tenant.status, "pending")
        self.assertFalse(user.is_active)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 1)              # лише адміністратору
        self.assertIn("Email підтвердив", mail.outbox[0].subject)
        # посилання одноразове
        self.assertEqual(self.client.get(f"/verify/{token}/").status_code, 400)
        tenant.owner_user.refresh_from_db()
        self.assertFalse(tenant.owner_user.is_active)

    def test_expired_link(self):
        user, tenant = pending_account(sent=timezone.now() - timedelta(hours=49))
        response = self.client.get(f"/verify/{tenant.verification_token}/")
        self.assertEqual(response.status_code, 400)
        tenant.refresh_from_db()
        self.assertFalse(tenant.email_verified)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_throttle_per_ip(self):
        for i in range(3):
            self.assertEqual(self.register(email=f"a{i}@example.com").status_code, 302)
        response = self.register(email="a9@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Забагато реєстрацій")
        self.assertFalse(User.objects.filter(email="a9@example.com").exists())
        # інша адреса — можна
        self.assertEqual(self.register(ip="198.51.100.1", email="b@example.com").status_code, 302)

    def test_login_page_shows_link(self):
        self.assertContains(self.client.get("/admin/login/"), 'href="/register/"')
