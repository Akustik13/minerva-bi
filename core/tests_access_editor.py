"""«✏️ Змінити доступ» на сторінці «👁 Як бачить Minerva»: чернетка → прев'ю → збереження в профіль.

python manage.py test core.tests_access_editor --settings=tabele.settings_test
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase

from core import access_editor, access_preview
from core.models import AuditLog, ModuleRegistry, UserProfile


def staff(username, role, **profile):
    u = User.objects.create_user(username, f"{username}@firma.example", "p", is_staff=True)
    UserProfile.objects.update_or_create(user=u, defaults={"role": role, "module_operations": None, **profile})
    return User.objects.select_related("profile").get(pk=u.pk)


class EditorBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        for app, name in (("sales", "Продажі"), ("inventory", "Склад"), ("crm", "CRM")):
            ModuleRegistry.objects.update_or_create(app_label=app, defaults={"name": name, "is_active": True})
        self.root = User.objects.create_superuser("root", "root@firma.example", "p")
        self.wh = staff("wh", "warehouse")                       # склад: inventory — створює й змінює
        self.url = f"/admin/core/userprofile/{self.wh.profile.pk}/access/"


class DraftTests(EditorBase):
    def test_current_levels_from_role(self):
        cur = access_editor.current(self.wh.profile)
        self.assertEqual((cur["modules"]["inventory"], cur["modules"]["sales"]), ("work", "hidden"))
        self.assertEqual(cur["extra"]["inventory"], ["import"])

    def test_preview_with_draft_changes_nothing_in_db(self):
        draft = {"modules": {"sales": "view", "inventory": "view"}, "pages": {"inventory:Supplier": True}}
        data = access_preview.preview(self.wh, draft)
        apps = {a["app"]: a for a in data["menu"]}
        order = next(m for m in apps["sales"]["models"] if m["url"].endswith("/salesorder/"))
        self.assertTrue(order["read_only"])                                 # продажі — лише перегляд
        product = next(m for m in apps["inventory"]["models"] if m["url"].endswith("/product/"))
        self.assertFalse(product["perms"]["change"])
        self.assertNotIn("inventory:Supplier", [m["key"] for m in apps["inventory"]["models"]])
        self.assertEqual([h["key"] for h in data["hidden"]], ["inventory:Supplier"])
        self.assertEqual(access_preview.check_url(self.wh, "/admin/sales/salesorder/", draft)["level"], "view")
        self.wh.profile.refresh_from_db()
        self.assertIsNone(self.wh.profile.allowed_modules)                 # у базі — як було

    def test_save_writes_profile_and_audit(self):
        lines = access_editor.save(self.wh.profile, {"modules": {"sales": "work"}, "extra": {"sales": ["export"]},
                                                     "special": {"export": True}}, self.root)
        self.assertTrue(any("Продажі" in l for l in lines))
        p = UserProfile.objects.get(pk=self.wh.profile.pk)
        self.assertIn("sales", p.allowed_modules)
        self.assertEqual(p.module_operations["sales"], ["view", "add", "change", "export"])
        self.assertEqual(p.module_operations["inventory"], ["view", "add", "change", "import"])   # як було за роллю
        self.assertTrue(p.can_export)
        log = AuditLog.objects.get(action=AuditLog.Action.SETTINGS, object_id=str(self.wh.pk))
        self.assertEqual(log.extra["access_changes"], lines)
        wh = User.objects.get(pk=self.wh.pk)
        self.assertTrue(wh.has_perm("sales.change_salesorder"))
        self.assertEqual(access_editor.save(p, {"modules": {"sales": "work"}}, self.root), [])   # нічого нового

    def test_reset_and_copy(self):
        access_editor.save(self.wh.profile, {"modules": {"sales": "delete"}, "pages": {"inventory:Supplier": True},
                                             "special": {"delete": True}}, self.root)
        p = UserProfile.objects.get(pk=self.wh.profile.pk)
        access_editor.save(p, {"reset": True}, self.root)
        p.refresh_from_db()
        self.assertEqual((p.allowed_modules, p.module_operations, p.denied_models, p.can_delete), (None, None, [], None))
        acc = staff("acc", "accountant")
        draft = access_editor.draft_from(acc.profile)
        access_editor.save(p, draft, self.root)
        self.assertEqual(access_editor.current(UserProfile.objects.get(pk=p.pk))["modules"],
                         access_editor.current(acc.profile)["modules"])

    def test_who_may_edit(self):
        self.assertEqual(access_editor.can_edit(self.root, self.wh.profile), "")
        self.assertIn("керувати юзерами", access_editor.can_edit(self.wh, self.wh.profile))
        boss = staff("boss", "admin")
        self.assertIn("адміністратор", access_editor.can_edit(self.root, boss.profile))
        hr = staff("hr", "manager", can_manage_users=True)
        self.assertEqual(access_editor.can_edit(hr, self.wh.profile), "")
        self.assertIn("Свої права", access_editor.can_edit(hr, hr.profile))      # собі — ні
        with self.assertRaises(access_editor.AccessError):                # «керувати юзерами» дає лише адміністратор
            access_editor.save(self.wh.profile, {"special": {"manage_users": True}}, hr)
        with self.assertRaises(access_editor.AccessError):
            access_editor.save(self.wh.profile, {"modules": {"sales": "work"}}, self.wh)

    def test_clean_drops_unknown(self):
        d = access_editor.clean({"modules": {"sales": "god", "core": "hidden", "crm": "view"},
                                 "special": {"superuser": True, "export": "yes"}, "pages": {"x": True}})
        self.assertEqual((d["modules"], d["special"], d["pages"]), ({"crm": "view"}, {}, {}))


class EditorPageTests(EditorBase):
    def test_edit_mode_preview_and_save_through_the_page(self):
        self.client.force_login(self.root)
        r = self.client.get(self.url, {"edit": "1"})
        self.assertContains(r, "✏️ Зміна доступу")
        self.assertContains(r, 'data-ap-level="view" data-app="inventory"')
        draft = {"modules": {"inventory": "view"}}
        r = self.client.post(self.url, {"action": "preview", "draft": json.dumps(draft)})
        self.assertContains(r, "Склад: ✏️ Працює → 👁 Лише перегляд")
        self.assertContains(r, "💾 Зберегти (1)")
        r = self.client.post(self.url, {"action": "copy", "source": staff("acc", "accountant").profile.pk})
        self.assertIn("modules", r.json()["draft"])
        r = self.client.post(self.url, {"action": "save", "draft": json.dumps(draft)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(UserProfile.objects.get(pk=self.wh.profile.pk).module_operations["inventory"], ["view", "import"])

    def test_not_allowed_to_edit(self):
        self.client.force_login(self.wh)                                   # свій профіль — лише перегляд
        r = self.client.get(f"/admin/core/userprofile/{self.wh.profile.pk}/access/", {"edit": "1"})
        self.assertNotContains(r, "✏️ Зміна доступу")
        r = self.client.post(f"/admin/core/userprofile/{self.wh.profile.pk}/access/",
                             {"action": "save", "draft": json.dumps({"modules": {"sales": "delete"}})})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(UserProfile.objects.get(pk=self.wh.profile.pk).allowed_modules)
