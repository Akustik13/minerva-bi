"""«📤 Застосувати ці права до інших» на сторінці «👁 Як бачить Minerva»: збережені права людини (і роль) — рівно
такі самі кільком іншим.

python manage.py test core.tests_access_apply --settings=tabele.settings_test
"""
from django.contrib.auth.models import User

from core import access_editor
from core.models import AuditLog, UserProfile
from core.tests_access_editor import EditorBase, staff


class ApplyBase(EditorBase):
    def setUp(self):
        super().setUp()
        # джерело — складник з окремими правилами
        access_editor.save(self.wh.profile, {"modules": {"sales": "view", "crm": "work"},
                                             "extra": {"crm": ["export"]},
                                             "pages": {"inventory:Supplier": True},
                                             "special": {"export": True}}, self.root)
        self.src = UserProfile.objects.select_related("user").get(pk=self.wh.profile.pk)
        self.a = staff("anna", "manager")
        self.b = staff("bohdan", "accountant", can_delete=True, denied_models=["sales:SalesOrder"])

    def same(self, user):
        p = UserProfile.objects.get(user=user)
        return access_editor.current(p) == access_editor.current(self.src), p


class ApplyLogicTests(ApplyBase):
    def test_plan_changes_nothing(self):
        rows = access_editor.plan_to(self.root, self.src, [self.a.profile.pk, self.b.profile.pk])
        self.assertEqual([r["username"] for r in rows], ["anna", "bohdan"])
        anna = rows[0]
        self.assertEqual(anna["why"], "")
        self.assertIn("Роль: 💼 Менеджер → 📦 Складник", anna["lines"])
        self.assertTrue(any("CRM" in l for l in anna["lines"]))
        self.assertIsNone(UserProfile.objects.get(user=self.a).allowed_modules)    # у базі — як було

    def test_apply_makes_rights_identical(self):
        res = access_editor.apply_to_many(self.root, self.src, [self.a.profile.pk, self.b.profile.pk])
        self.assertEqual(sorted(n for n, _l in res["done"]), ["anna", "bohdan"])
        for user in (self.a, self.b):
            same, p = self.same(user)
            self.assertTrue(same, user.username)
            self.assertEqual(p.role, "warehouse")
            self.assertEqual(p.denied_models, ["inventory:Supplier"])
            self.assertTrue(p.can_export)
        self.assertIsNone(UserProfile.objects.get(user=self.b).can_delete)          # як у джерела — за роллю
        b = User.objects.get(pk=self.b.pk)
        self.assertTrue(b.has_perm("crm.change_customer"))
        self.assertFalse(b.has_perm("sales.change_salesorder"))                      # продажі — лише перегляд
        log = AuditLog.objects.get(action=AuditLog.Action.SETTINGS, object_id=str(self.a.pk))
        self.assertEqual(log.extra["copied_from"], "wh")
        self.assertEqual(log.extra["role"], ["manager", "warehouse"])
        # удруге — нічого нового
        again = access_editor.apply_to_many(self.root, self.src, [self.a.profile.pk])
        self.assertEqual((again["done"], again["same"]), ([], ["anna"]))

    def test_without_role(self):
        access_editor.apply_to_many(self.root, self.src, [self.a.profile.pk], with_role=False)
        same, p = self.same(self.a)
        self.assertEqual(p.role, "manager")
        self.assertEqual(access_editor.current(p)["modules"], access_editor.current(self.src)["modules"])

    def test_refused_targets_and_sources(self):
        boss = staff("boss", "admin")
        res = access_editor.apply_to_many(self.root, self.src, [boss.profile.pk, self.a.profile.pk])
        self.assertEqual([n for n, _w in res["refused"]], ["boss"])                  # адміністратор — ні
        self.assertEqual([n for n, _l in res["done"]], ["anna"])
        self.assertEqual(UserProfile.objects.get(user=boss).role, "admin")
        with self.assertRaises(access_editor.AccessError):                           # з адміністратора — ні
            access_editor.plan_to(self.root, boss.profile, [self.a.profile.pk])
        hr = staff("hr", "manager", can_manage_users=True)
        rows = access_editor.plan_to(hr, self.src, [hr.profile.pk])
        self.assertIn("Свої права", rows[0]["why"])                                  # собі — ні
        with self.assertRaises(access_editor.AccessError):                           # хто не керує юзерами — ні
            access_editor.apply_to_many(self.b, self.src, [self.a.profile.pk])

    def test_targets_list_says_why_not(self):
        staff("boss", "admin")
        rows = {r["profile"].user.username: r["why"] for r in access_editor.targets(self.root, self.src)}
        self.assertNotIn("wh", rows)
        self.assertEqual(rows["anna"], "")
        self.assertIn("адміністратор", rows["boss"])


class ApplyPageTests(ApplyBase):
    def test_button_preview_and_apply_through_the_page(self):
        url = f"/admin/core/userprofile/{self.src.pk}/access/"
        self.client.force_login(self.root)
        r = self.client.get(url)
        self.assertContains(r, "📤 Застосувати ці права до інших…")
        self.assertContains(r, f'value="{self.a.profile.pk}"')
        r = self.client.post(url, {"action": "apply_preview", "targets": [self.a.profile.pk], "with_role": "1"})
        self.assertEqual(r.json()["rows"][0]["username"], "anna")
        self.assertIsNone(UserProfile.objects.get(user=self.a).allowed_modules)
        r = self.client.post(url, {"action": "apply", "targets": [self.a.profile.pk, self.b.profile.pk],
                                   "with_role": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.same(self.a)[0] and self.same(self.b)[0])

    def test_not_allowed(self):
        url = f"/admin/core/userprofile/{self.b.profile.pk}/access/"
        self.client.force_login(self.b)                                              # свій профіль — лише перегляд
        r = self.client.get(url)
        self.assertNotContains(r, "📤 Застосувати ці права до інших")
        r = self.client.post(url, {"action": "apply", "targets": [self.a.profile.pk], "with_role": "1"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(UserProfile.objects.get(user=self.a).allowed_modules)
