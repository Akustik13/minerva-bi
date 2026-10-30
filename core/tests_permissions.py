"""user.has_perm(...) за ролями Minerva (core/permissions.py, core.auth_backend).

python manage.py test core.tests_permissions --settings=tabele.settings_test
"""
from django.contrib.auth.models import Permission, User
from django.test import TestCase

from core.models import ModuleRegistry


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class HasPermTests(TestCase):
    def test_manager_by_role(self):
        olga = person("olga")
        self.assertTrue(olga.has_perm("crm.view_customer"))
        self.assertTrue(olga.has_perm("crm.add_customer"))          # ROLE_OPERATIONS: crm view/add/change/export
        self.assertTrue(olga.has_perm("crm.change_customer"))
        self.assertFalse(olga.has_perm("crm.delete_customer"))      # операції немає, «Може видаляти» — ні
        self.assertTrue(olga.has_perm("projects.view_project"))    # модуль ролі — перегляд
        self.assertFalse(olga.has_perm("projects.change_project"))  # операцій для модуля в ролі немає — лише перегляд
        self.assertFalse(olga.has_perm("inventory.view_product"))  # модуля немає в ролі
        self.assertTrue(olga.has_module_perms("crm"))
        self.assertFalse(olga.has_module_perms("inventory"))

    def test_delete_needs_operation_and_can_delete(self):
        tom = person("tom", module_operations={"tasks": ["view", "delete"]})
        self.assertFalse(tom.has_perm("tasks.delete_task"))
        tom = person("tom2", module_operations={"tasks": ["view", "delete"]}, can_delete=True)
        self.assertTrue(tom.has_perm("tasks.delete_task"))
        self.assertFalse(tom.has_perm("crm.delete_customer"))       # у crm операції delete немає

    def test_explicit_operations_and_denied_models(self):
        kim = person("kim", "warehouse", denied_models=["inventory:Supplier"])
        self.assertTrue(kim.has_perm("inventory.change_product"))
        self.assertFalse(kim.has_perm("inventory.view_supplier"))
        kim = person("kim2", "warehouse", module_operations={"inventory": ["view"]})
        self.assertTrue(kim.has_perm("inventory.view_product"))
        self.assertFalse(kim.has_perm("inventory.change_product"))

    def test_protected_apps_admins_and_inactive(self):
        olga = person("olga")
        self.assertFalse(olga.has_perm("auth.view_user"))
        self.assertFalse(olga.has_perm("core.change_userprofile"))
        self.assertFalse(olga.has_perm("crm.export_customer"))     # не стандартне право — лише правами Django
        boss = person("boss", "admin")
        self.assertTrue(boss.has_perm("auth.change_user"))
        self.assertTrue(boss.has_perm("inventory.delete_product"))
        olga.is_active = False
        olga.save()
        self.assertFalse(User.objects.get(pk=olga.pk).has_perm("crm.view_customer"))
        shopper = User.objects.create_user("shopper", "s@x.y", "p")
        User.objects.filter(pk=shopper.pk).update(is_staff=False)
        self.assertFalse(User.objects.get(pk=shopper.pk).has_perm("crm.view_customer"))

    def test_disabled_module_and_django_permissions(self):
        ModuleRegistry.objects.update_or_create(app_label="crm", defaults={"name": "CRM", "is_active": False})
        olga = person("olga")
        self.assertFalse(olga.has_perm("crm.view_customer"))
        reader = person("reader", "readonly")
        reader.user_permissions.add(Permission.objects.get(codename="view_product"))
        self.assertTrue(User.objects.get(pk=reader.pk).has_perm("inventory.view_product"))   # права Django — як завжди

    def test_module_hook_opens_section(self):
        from rag_assistant import access
        root = User.objects.create_superuser("root", "r@x.y", "p")
        kim = person("kim", "warehouse")
        self.assertFalse(kim.has_perm("rag_assistant.view_knowledgesource"))
        access.set_rights(kim, {"sources": True}, root)
        kim = User.objects.get(pk=kim.pk)
        self.assertTrue(kim.has_perm("rag_assistant.view_knowledgesource"))
        self.assertFalse(kim.has_perm("rag_assistant.change_knowledgeitem"))   # операції — права бази знань, не ролі
