"""«👁 Як бачить Minerva <людина>» — меню, дозволи, модулі, перевірка адреси; лише читання.

python manage.py test core.tests_access_preview --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import TestCase

from core import access_preview
from core.models import ModuleRegistry, UserProfile


def staff(username, role, **profile):
    u = User.objects.create_user(username, f"{username}@firma.example", "p", is_staff=True)
    UserProfile.objects.update_or_create(user=u, defaults={"role": role, "module_operations": None, **profile})
    return User.objects.get(pk=u.pk)


class AccessPreviewTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        for app, name in (("sales", "Продажі"), ("inventory", "Склад"), ("crm", "CRM")):
            ModuleRegistry.objects.update_or_create(app_label=app, defaults={"name": name, "is_active": True})
        self.root = User.objects.create_superuser("root", "root@firma.example", "p")
        self.ro = staff("ro", "readonly", allowed_modules=["sales"])
        self.wh = staff("wh", "warehouse")

    def test_readonly_menu_and_modules(self):
        data = access_preview.preview(self.ro)
        apps = {a["app"]: a for a in data["menu"]}
        self.assertIn("sales", apps)
        self.assertNotIn("inventory", apps)                                   # модуля немає в доступі
        order = next(m for m in apps["sales"]["models"] if m["url"].endswith("/salesorder/"))
        self.assertEqual(order["perms"], {"view": True, "add": False, "change": False, "delete": False})
        self.assertTrue(order["read_only"])
        mods = {m["app"]: m for m in data["modules"]}
        self.assertTrue(mods["sales"]["allowed"])
        self.assertFalse(mods["inventory"]["allowed"])
        self.assertIn("ручний список", mods["sales"]["why"])
        self.assertFalse(next(s for s in data["special"] if s["label"].startswith("🗑"))["value"])

    def test_check_url_explains(self):
        res = access_preview.check_url(self.ro, "https://sevskiy.minerva-bi.com/admin/sales/salesorder/")
        self.assertEqual((res["ok"], res["level"]), (True, "view"))
        res = access_preview.check_url(self.ro, "/admin/sales/salesorder/add/")
        self.assertFalse(res["ok"])
        res = access_preview.check_url(self.ro, "/admin/inventory/product/")
        self.assertFalse(res["ok"])
        self.assertIn("не має доступу", res["text"])
        self.assertEqual(access_preview.check_url(self.root, "/admin/inventory/product/add/")["level"], "edit")
        self.assertEqual(access_preview.check_url(self.ro, "/nope/zzz/")["text"], "Такої адреси в Minerva немає.")
        # адмінка Django «ловить» будь-яку /admin/… — це не сторінка, навіть для адміністратора
        self.assertFalse(access_preview.check_url(self.root, "/admin/nema/takoyi/")["ok"])

    def test_preview_does_not_leak_into_the_real_user(self):
        access_preview.preview(self.ro)
        self.ro.refresh_from_db()
        self.assertFalse(self.ro.is_superuser)                                # _get_app_list піднімає прапорець лише тимчасово

    def test_page_for_admin_and_own_profile_only(self):
        url = f"/admin/core/userprofile/{self.ro.profile.pk}/access/"
        self.client.force_login(self.root)
        r = self.client.get(url, {"url": "/admin/sales/salesorder/"})
        self.assertContains(r, "Меню, яке вона бачить")
        self.assertContains(r, "лише для перегляду")
        r = self.client.get("/admin/core/userprofile/")                       # «👁 Як бачить» у списку профілів
        self.assertContains(r, url)
        self.client.force_login(self.wh)                                      # чужий профіль — ні
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(f"/admin/core/userprofile/{self.ro.profile.pk}/debug/").status_code, 403)
        own = self.client.get(f"/admin/core/userprofile/{self.wh.profile.pk}/access/")
        self.assertEqual(own.status_code, 200)                                # свій — так
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
