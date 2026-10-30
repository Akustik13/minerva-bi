"""Реєстр модулів, «Мої налаштування», масове запрошення, розклад cron_runner.sh.

    python manage.py test core --settings=tabele.settings_test
"""
import json
import re
from io import StringIO
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command, get_commands
from django.test import TestCase

from config.models import SystemSettings
from core import modules
from core.models import ModuleBundle, ModuleRegistry, UserProfile

User = get_user_model()


def _onboarded():
    ss = SystemSettings.objects.get_or_create(pk=1)[0]
    ss.is_onboarding_complete = True
    ss.save()


# ── 8. Реєстр модулів ───────────────────────────────────────────────────────────

class ModuleRegistryTests(TestCase):
    NEW = ("projects", "offers", "worktime", "labels_app", "jlcpcb", "scraper_hub", "guide")

    def test_every_installed_app_is_a_module(self):
        """Новий застосунок без запису в core/modules.py ручний список модулів профілю мовчки б губив."""
        labels = {m["app_label"] for m in modules.MODULES}
        missing = [a.label for a in apps.get_app_configs()
                   if a.label not in labels and a.label not in modules.NOT_MODULES]
        self.assertEqual(missing, [], "додайте модуль у core/modules.py → MODULES")
        with_admin = {m._meta.app_label for m in admin.site._registry}
        self.assertEqual(sorted(with_admin - labels - modules.NOT_MODULES), [])

    def test_registered_after_migrate(self):
        """post_migrate (і для тестової бази) уже зареєстрував усі модулі."""
        have = set(ModuleRegistry.objects.values_list("app_label", flat=True))
        self.assertTrue(set(self.NEW) <= have, set(self.NEW) - have)
        self.assertEqual(have, {m["app_label"] for m in modules.MODULES})

    def test_register_missing_is_idempotent_and_keeps_admin_choices(self):
        ModuleRegistry.objects.filter(app_label="offers").update(is_active=False, name="Мої Angebote")
        ModuleRegistry.objects.filter(app_label="projects").delete()
        full = ModuleBundle.objects.create(name="⭐ Повний доступ", is_system=True)
        std = ModuleBundle.objects.create(name="📦 Стандарт", is_system=True)
        role = ModuleBundle.objects.create(name="💼 Менеджер", is_system=True)

        self.assertEqual(modules.register_missing(), ["projects"])
        self.assertEqual(modules.register_missing(), [])
        offers = ModuleRegistry.objects.get(app_label="offers")
        self.assertEqual((offers.is_active, offers.name), (False, "Мої Angebote"))   # не перезаписано
        projects = ModuleRegistry.objects.get(app_label="projects")
        self.assertTrue(projects.is_active)
        self.assertIn(projects, full.modules.all())
        self.assertIn(projects, std.modules.all())                # standard → «Стандарт»
        self.assertNotIn(projects, role.modules.all())            # рольові пакети не чіпаються

    def test_post_migrate_handler_with_historical_models(self):
        ModuleRegistry.objects.filter(app_label__in=self.NEW).delete()
        modules.register_after_migrate(sender=None, using="default", apps=apps)
        self.assertEqual(ModuleRegistry.objects.filter(app_label__in=self.NEW).count(), len(self.NEW))

    def test_setup_modules_command(self):
        call_command("setup_modules", stdout=StringIO())
        std = ModuleBundle.objects.get(name="📦 Стандарт")
        self.assertTrue(std.modules.filter(app_label="offers").exists())
        self.assertFalse(std.modules.filter(app_label="shop").exists())   # premium

    def test_manual_module_list_keeps_new_modules(self):
        _onboarded()
        root = User.objects.create_superuser("root", "r@x.y", "p")
        worker = User.objects.create_user("max", "m@x.y", "p", is_staff=True)
        profile = UserProfile.objects.get(user=worker)
        self.client.force_login(root)
        page = self.client.get(f"/admin/core/userprofile/{profile.pk}/change/")
        form = page.context["adminform"].form
        labels = set(form.fields["modules_override"].queryset.values_list("app_label", flat=True))
        self.assertTrue({"projects", "offers", "worktime", "labels_app"} <= labels)
        from core.admin import UserProfileForm
        f = UserProfileForm(instance=profile)
        picked = ModuleRegistry.objects.filter(app_label__in=["crm", "offers", "worktime"])
        f.cleaned_data = {"modules_override": list(picked)}
        inst = f.save(commit=False)
        self.assertEqual(sorted(inst.allowed_modules), ["crm", "offers", "worktime"])


# ── 7. «Мої налаштування» ───────────────────────────────────────────────────────

class MySettingsTests(TestCase):
    def setUp(self):
        _onboarded()
        self.user = User.objects.create_user("olga", "o@x.y", "p", is_staff=True)
        self.profile = UserProfile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def test_dead_switches_not_on_page(self):
        page = self.client.get("/core/my-settings/")
        self.assertEqual(page.status_code, 200)
        for gone in ("Email сповіщення", "Telegram сповіщення", "Записів на сторінку",
                     'id="notify_email"', 'id="items_per_page"'):
            self.assertNotContains(page, gone)
        self.assertContains(page, 'id="telegram_id"')
        self.assertContains(page, "/myid")

    def test_post_saves_only_live_fields(self):
        r = self.client.post("/core/my-settings/", data=json.dumps({
            "notify_email": False, "notify_telegram": True, "items_per_page": 100,
            "theme": "light", "interface_language": "de", "telegram_id": "777"}),
            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.json()["updated"]), ["interface_language", "telegram_id", "theme"])
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.theme, self.profile.telegram_id), ("light", 777))
        self.assertEqual(self.profile.items_per_page, 25)          # не змінилось — поле ніщо не читає

    def test_profile_admin_has_no_dead_fields(self):
        root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(root)
        page = self.client.get(f"/admin/core/userprofile/{self.profile.pk}/change/")
        for gone in ('name="notify_email"', 'name="notify_telegram"', 'name="items_per_page"'):
            self.assertNotContains(page, gone)
        self.assertContains(page, 'name="theme"')


# ── 9. Масове «📨 Надіслати запрошення» ──────────────────────────────────────────

class BulkInvitationTests(TestCase):
    def setUp(self):
        _onboarded()
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.a = User.objects.create_user("a", "a@x.y", "p")
        self.b = User.objects.create_user("b", "b@x.y", "p")
        self.client.force_login(self.root)

    def test_link_built_from_request_host(self):
        from core.admin import MinervaUserAdmin
        with mock.patch.object(MinervaUserAdmin, "_do_send_invitation", return_value={"ok": True}) as send:
            r = self.client.post("/admin/auth/user/", {
                "action": "send_invitation_action", "_selected_action": [self.a.pk, self.b.pk]},
                HTTP_HOST="minerva.example.test")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(send.call_count, 2)
        urls = {c.args[3] for c in send.call_args_list}
        self.assertEqual(urls, {"http://minerva.example.test/admin/"})
        self.assertNotIn("sevskiy", Path(__file__).with_name("admin.py").read_text(encoding="utf-8"))


# ── 5. Розклад сервера (cron_runner.sh) ──────────────────────────────────────────

class CronRunnerTests(TestCase):
    def setUp(self):
        self.script = (Path(settings.BASE_DIR) / "cron_runner.sh").read_text(encoding="utf-8")

    def test_scheduled_commands_present(self):
        for cmd in ("auto_import --all", "send_alerts --if-due", "send_digest --period all",
                    "morning_briefing --if-due", "expire_offers"):
            self.assertIn(f"python manage.py {cmd}", self.script)
        self.assertNotIn('"$current_hour" = "$BRIEFING_HOUR"', self.script)   # година — з налаштувань

    def test_every_command_exists(self):
        known = get_commands()
        used = set(re.findall(r"python manage\.py ([a-z_]+)", self.script))
        self.assertEqual(sorted(used - set(known)), [])
        self.assertNotIn("\r\n", self.script)
