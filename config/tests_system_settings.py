"""Системні налаштування: «Активні модулі» — лише посилання на 🧩 Реєстр модулів; брифінг — час у формі.

    python manage.py test config --settings=tabele.settings_test
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from config.admin import SystemSettingsForm
from config.models import SystemSettings
from core.models import ModuleRegistry

User = get_user_model()


class SystemSettingsModulesTests(TestCase):
    def setUp(self):
        self.ss = SystemSettings.objects.get_or_create(pk=1)[0]
        self.ss.is_onboarding_complete = True
        self.ss.enabled_modules = ["crm", "sales"]
        self.ss.save()
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)

    def test_page_points_to_module_registry(self):
        ModuleRegistry.objects.filter(app_label="jlcpcb").update(is_active=False)
        page = self.client.get(f"/admin/config/systemsettings/{self.ss.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "enabled_modules_choice")
        self.assertContains(page, "/admin/core/moduleregistry/")
        self.assertContains(page, "🧩 Реєстр модулів")
        self.assertContains(page, "вимкнено: JLCPCB виробництво")

    def test_form_has_no_modules_field_and_keeps_wizard_choice(self):
        self.assertNotIn("enabled_modules_choice", SystemSettingsForm.base_fields)
        self.assertNotIn("enabled_modules", SystemSettingsForm.base_fields)
        before = set(ModuleRegistry.objects.filter(is_active=True).values_list("app_label", flat=True))
        page = self.client.get(f"/admin/config/systemsettings/{self.ss.pk}/change/")
        form = page.context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None and k != "logo"}
        data.update(company_name="Neue Firma GmbH")
        r = self.client.post(f"/admin/config/systemsettings/{self.ss.pk}/change/", data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        self.ss.refresh_from_db()
        self.assertEqual(self.ss.company_name, "Neue Firma GmbH")
        self.assertEqual(self.ss.enabled_modules, ["crm", "sales"])   # вибір майстра лишився
        after = set(ModuleRegistry.objects.filter(is_active=True).values_list("app_label", flat=True))
        self.assertEqual(before, after)                               # реєстр не змінено

    def test_briefing_page_shows_schedule(self):
        page = self.client.get("/admin/config/briefingsettings/")
        page = self.client.get(page["Location"])
        self.assertContains(page, "morning_briefing --if-due")
        self.assertContains(page, "Останній брифінг за розкладом")
