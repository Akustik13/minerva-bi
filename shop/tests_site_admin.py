"""Адмінка сайту — сторона Minerva: доступ персоналу (ShopStaff), вхід через Minerva, код підключення сайту,
журнал змін на сайті (shop/site_admin.py, api/site_admin.py). python manage.py test shop --settings=tabele.settings_test"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import APIKey, Webhook

from . import site_admin
from .models import Shop, ShopPairing, ShopSiteEvent, ShopStaff


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://shop.example.com")
        self.other = Shop.objects.create(name="Other", slug="other")
        self.key = APIKey.objects.create(name="Сайт", scopes=["products:read"], shop=self.shop)
        self.anna = User.objects.create_user("anna", "anna@firma.de", "geheim-123", first_name="Anna")
        ShopStaff.objects.create(shop=self.shop, user=self.anna, role="admin")
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION="Token " + self.key.key)

    def login(self, login="anna", password="geheim-123", client=None):
        return (client or self.c).post("/api/v1/shop/staff/login/", {"login": login, "password": password,
                                                                    "site": "shop.example.com"}, format="json")


class StaffLoginTests(Base):
    def test_login_by_username_or_email(self):
        for login in ("anna", "ANNA@firma.de"):
            data = self.login(login).json()
            self.assertEqual((data["ok"], data["role"], data["name"]), (True, "admin", "Anna"))
            self.assertIn("secrets.edit", data["permissions"])
        self.assertIsNotNone(ShopStaff.objects.get(user=self.anna).last_site_login)
        self.assertTrue(ShopSiteEvent.objects.filter(shop=self.shop, action="login", user="anna").exists())

    def test_wrong_password_no_access_and_lockout(self):
        self.assertEqual(self.login(password="falsch").status_code, 401)
        bob = User.objects.create_user("bob", "bob@firma.de", "pw-bob-123")
        self.assertEqual(self.login("bob", "pw-bob-123").status_code, 401)            # без доступу до магазину
        ShopStaff.objects.create(shop=self.other, user=bob, role="admin")
        self.assertEqual(self.login("bob", "pw-bob-123").status_code, 401)            # доступ до іншого магазину
        ShopStaff.objects.filter(user=self.anna).update(is_active=False)
        self.assertEqual(self.login().status_code, 401)
        ShopStaff.objects.filter(user=self.anna).update(is_active=True)
        cache.clear()
        for _ in range(site_admin.LOCK_FAILS):
            self.login(password="falsch")
        r = self.login()   # навіть правильний пароль — після 5 невдач чекати
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))

    def test_manager_and_viewer_permissions(self):
        ShopStaff.objects.filter(user=self.anna).update(role="manager")
        self.assertNotIn("secrets.edit", self.login().json()["permissions"])
        ShopStaff.objects.filter(user=self.anna).update(role="viewer")
        self.assertEqual(self.login().json()["permissions"], ["settings.view"])

    def test_only_bound_site_key(self):
        loose = APIKey.objects.create(name="RAG", scopes=["products:read"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token " + loose.key)
        self.assertEqual(self.login(client=c).status_code, 403)
        no_scope = APIKey.objects.create(name="Склад", scopes=["stock:read"], shop=self.shop)
        c.credentials(HTTP_AUTHORIZATION="Token " + no_scope.key)
        self.assertEqual(self.login(client=c).status_code, 403)
        self.assertEqual(APIClient().post("/api/v1/shop/staff/login/", {}, format="json").status_code, 401)

    def test_site_events_without_values(self):
        r = self.c.post("/api/v1/shop/site-events/", {"user": "anna", "action": "settings", "site": "shop.example.com",
                                                      "fields": ["minerva.minerva_token", "mail.smtp_host"]}, format="json")
        self.assertEqual(r.status_code, 201)
        e = ShopSiteEvent.objects.filter(action="settings").get()
        self.assertEqual(e.details, {"fields": ["minerva.minerva_token", "mail.smtp_host"]})


class PairingTests(Base):
    def pair(self, code, site="https://neu.example.com"):
        return APIClient().post("/api/v1/shop/pair/", {"code": code, "site_url": site + "/admin/"}, format="json")

    def test_code_gives_key_and_webhook_once(self):
        code, until = site_admin.new_pairing(self.shop, self.anna)
        self.assertRegex(code, r"^[A-Z2-9]{5}-[A-Z2-9]{5}$")
        data = self.pair(code.lower()).json()   # регістр і дефіс не важливі
        key = APIKey.objects.get(key=data["api_key"])
        self.assertEqual((key.shop, key.is_active, data["shop"], data["site_url"]), (self.shop, True, "webshop",
                                                                                    "https://neu.example.com"))
        self.assertIn("orders:write", key.scopes)
        hook = Webhook.objects.get(url="https://neu.example.com/api/shop/webhook.php")
        self.assertEqual((hook.secret, hook.events), (data["webhook_secret"], ["stock.changed"]))
        self.assertTrue(data["minerva_url"].startswith("http"))
        self.assertEqual(self.pair(code).status_code, 400)   # одноразовий
        self.assertTrue(ShopSiteEvent.objects.filter(action="pair", site="neu.example.com").exists())

    def test_repair_disables_previous_pairing_key_only(self):
        first = self.pair(site_admin.new_pairing(self.shop)[0]).json()
        second = self.pair(site_admin.new_pairing(self.shop)[0]).json()
        self.assertFalse(APIKey.objects.get(key=first["api_key"]).is_active)
        self.assertTrue(APIKey.objects.get(key=second["api_key"]).is_active)
        self.assertTrue(APIKey.objects.get(pk=self.key.pk).is_active)   # ключ, створений вручну, лишається
        self.assertEqual(Webhook.objects.filter(url="https://neu.example.com/api/shop/webhook.php").count(), 1)

    def test_expired_invalid_and_http_site(self):
        code, _ = site_admin.new_pairing(self.shop)
        self.assertEqual(self.pair(code, "http://neu.example.com").json()["code"], "bad_site")
        ShopPairing.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.pair(code).json()["code"], "invalid_code")
        self.assertEqual(self.pair("AAAAA-BBBBB").status_code, 400)
        new_code, _ = site_admin.new_pairing(self.shop)
        site_admin.new_pairing(self.shop)                     # новий код скасовує попередній
        self.assertEqual(self.pair(new_code).status_code, 400)

    def test_admin_card_staff_and_code_button(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "👥 Доступ до адмінки сайту")
        self.assertContains(page, "🔗 Підключити сайт")
        self.assertContains(page, "Anna")
        r = self.client.post(f"/admin/shop/shop/{self.shop.pk}/pair-code/", follow=True)
        self.assertContains(r, "Код підключення сайту")
        self.assertEqual(ShopPairing.objects.filter(shop=self.shop, used_at__isnull=True).count(), 1)
        self.assertEqual(self.client.get(f"/admin/shop/shop/{self.shop.pk}/pair-code/").status_code, 403)
