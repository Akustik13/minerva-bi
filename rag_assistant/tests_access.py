"""🔐 Права на базу знань і RAG (rag_assistant/access.py) і has_perm за ролями Minerva (core/permissions.py).

python manage.py test rag_assistant.tests_access --settings=tabele.settings_test
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import AuditLog
from rag_assistant import access, retrieval
from rag_assistant.models import (KnowledgeAccess, KnowledgeItem, RagConversation, RagMessage, RagSettings,
                                  RetrievalJob)
from rag_assistant.tests_entity_help import EntityHelpBase
from rag_assistant.tests_retrieval import RetrievalBase

ACCESS_URL = "/admin/rag_assistant/knowledgesettings/access/"


def person(username, role="manager", **profile):
    """Співробітник з роллю Minerva (профіль створює core.signals)."""
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


def fresh(user):
    return User.objects.get(pk=user.pk)          # без кешу прав на об'єкті


class _Onboarded(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.root = User.objects.create_superuser("root", "r@x.y", "p")


class RulesTests(_Onboarded):
    def test_role_defaults(self):
        expected = {
            "manager": {"guide", "products", "search", "documents"},
            "warehouse": {"guide", "products"},
            "accountant": {"guide"},
            "ai": {"guide", "products", "search"},
            "readonly": {"guide"},
            "admin": set(access.RIGHT_KEYS),
            "superadmin": set(access.RIGHT_KEYS),
        }
        for role, rights in expected.items():
            user = person(f"u_{role}", role)
            self.assertEqual({r for r in access.RIGHT_KEYS if access.has(user, r)}, rights, role)
        self.assertTrue(all(access.has(self.root, r) for r in access.RIGHT_KEYS))

    def test_inactive_or_not_staff_have_nothing(self):
        user = person("ina")
        user.is_active = False
        user.save()
        self.assertFalse(any(access.has(fresh(user), r) for r in access.RIGHT_KEYS))
        customer = User.objects.create_user("shopper", "s@x.y", "p")       # не співробітник
        User.objects.filter(pk=customer.pk).update(is_staff=False)
        self.assertFalse(any(access.has(fresh(customer), r) for r in access.RIGHT_KEYS))
        with self.assertRaises(ValueError):
            access.has(self.root, "everything")

    def test_overrides_are_audited_and_reset_to_role(self):
        olga = person("olga")
        after = access.set_rights(olga, {"search": False, "sources": True, "bogus": True}, self.root, ip="10.0.0.5")
        self.assertEqual(after, {"search": False, "sources": True})
        olga = fresh(olga)
        self.assertEqual((access.has(olga, "search"), access.has(olga, "sources"), access.has(olga, "guide")),
                         (False, True, True))
        state = access.rights_of(olga)
        self.assertEqual(state["search"], {"value": False, "source": "override", "role_default": True})
        self.assertEqual(state["guide"]["source"], "role")
        log = AuditLog.objects.get(object_repr="Права бази знань: olga")
        self.assertEqual((log.user, log.ip_address, log.object_id), (self.root, "10.0.0.5", str(olga.pk)))
        self.assertEqual(log.extra["knowledge_rights"]["search"], {"було": "за роллю", "стало": False})
        access.set_rights(olga, {"search": False}, self.root)                    # нічого не змінилось — без запису
        self.assertEqual(AuditLog.objects.filter(object_repr="Права бази знань: olga").count(), 1)
        access.set_rights(olga, {"search": None, "sources": None}, self.root)    # «за роллю»
        olga = fresh(olga)
        self.assertEqual(KnowledgeAccess.objects.get(user=olga).rights, {})
        self.assertTrue(access.has(olga, "search"))
        self.assertFalse(access.has(olga, "sources"))

    def test_admins_ignore_overrides(self):
        boss = person("boss", "admin")
        access.set_rights(boss, {"search": False}, self.root)
        self.assertTrue(access.has(fresh(boss), "search"))
        self.assertEqual(access.rights_of(fresh(boss))["search"]["source"], "admin")

    def test_who_can_grant(self):
        self.assertTrue(access.can_grant(self.root))
        self.assertTrue(access.can_grant(person("adm", "admin")))
        self.assertFalse(access.can_grant(person("mgr")))
        self.assertTrue(access.can_grant(person("hr", can_manage_users=True)))


class AccessPageTests(_Onboarded):
    def test_superuser_sees_and_saves(self):
        olga = person("olga")
        self.client.force_login(self.root)
        page = self.client.get(ACCESS_URL)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "olga")
        self.assertContains(page, f'name="r_{olga.pk}_search"')
        self.assertNotContains(page, f'name="r_{self.root.pk}_search"')        # суперкористувач — завжди все
        r = self.client.post(ACCESS_URL, {f"r_{olga.pk}_search": "0", f"r_{olga.pk}_sources": "1",
                                          f"r_{olga.pk}_guide": ""})
        self.assertRedirects(r, ACCESS_URL, fetch_redirect_response=False)
        self.assertEqual(KnowledgeAccess.objects.get(user=olga).rights, {"search": False, "sources": True})
        page = self.client.get(ACCESS_URL)
        self.assertContains(page, "ka-over")                                    # ручне перевизначення видно

    def test_manager_without_rights_is_refused(self):
        olga = person("olga")
        self.client.force_login(olga)
        self.assertEqual(self.client.get(ACCESS_URL).status_code, 403)
        self.assertEqual(self.client.post(ACCESS_URL, {f"r_{olga.pk}_manage": "1"}).status_code, 403)
        self.assertFalse(KnowledgeAccess.objects.filter(rights__has_key="manage").exists())

    def test_manage_right_sees_but_only_granter_changes(self):
        olga, ivan = person("olga"), person("ivan")
        access.set_rights(olga, {"manage": True}, self.root)
        self.client.force_login(olga)
        page = self.client.get(ACCESS_URL)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "<select")                                  # лише перегляд
        self.assertEqual(self.client.post(ACCESS_URL, {f"r_{ivan.pk}_manage": "1"}).status_code, 403)
        hr = person("hr", can_manage_users=True)
        self.client.force_login(hr)
        self.client.post(ACCESS_URL, {f"r_{ivan.pk}_manage": "1"})
        self.assertTrue(access.has(fresh(ivan), "manage"))
        self.assertEqual(AuditLog.objects.get(object_repr="Права бази знань: ivan").user, hr)


class AdminSectionTests(_Onboarded):
    def test_right_opens_section_and_menu(self):
        kim = person("kim", "warehouse")                       # роль без модуля rag_assistant
        self.client.force_login(kim)
        self.assertEqual(self.client.get("/admin/rag_assistant/knowledgesource/").status_code, 403)
        access.set_rights(kim, {"sources": True}, self.root)
        r = self.client.get("/admin/rag_assistant/knowledgesource/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("rag_assistant", json.loads(r.context["allowed_modules_json"]))   # пункт меню зліва
        app = next(a for a in r.context["available_apps"] if a["app_label"] == "rag_assistant")
        self.assertEqual({m["object_name"] for m in app["models"]}, {"KnowledgeSource", "RagConversation"})  # + свої розмови
        self.assertEqual(self.client.get("/admin/rag_assistant/knowledgesettings/1/change/").status_code, 403)
        self.assertEqual(self.client.get("/admin/rag_assistant/ragsettings/").status_code, 403)

    def test_explicit_module_operations_do_not_block_granted_section(self):
        kim = person("kim", "warehouse", module_operations={"inventory": ["view"]})
        access.set_rights(kim, {"sources": True}, self.root)
        self.client.force_login(kim)
        self.assertEqual(self.client.get("/admin/rag_assistant/knowledgesource/add/").status_code, 200)

    def test_conversations_admin_shows_only_own(self):
        olga = person("olga")
        RagConversation.objects.create(user=olga, title="моя")
        RagConversation.objects.create(user=self.root, title="чужа")
        self.client.force_login(olga)
        r = self.client.get("/admin/rag_assistant/ragconversation/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "моя")
        self.assertNotContains(r, "чужа")


class WidgetTests(_Onboarded):
    def setUp(self):
        super().setUp()
        st = RagSettings.get()
        st.enabled, st.base_url, st.api_key = True, "https://rag.example", "secret-key-1234"
        st.save()
        self.olga = person("olga")
        self.client.force_login(self.olga)
        self.conv = RagConversation.objects.create(user=self.olga)

    def ask(self, question, **extra):
        return self.client.post(f"/rag/api/conversations/{self.conv.pk}/ask/",
                                json.dumps({"question": question, **extra}), content_type="application/json")

    def test_state_follows_rights(self):
        d = self.client.get("/rag/api/state/").json()
        self.assertEqual((d["enabled"], d["products"]), (True, True))
        access.set_rights(self.olga, {"products": False}, self.root)
        d = self.client.get("/rag/api/state/").json()                     # гід є за роллю, але без ключа AI
        self.assertEqual((d["enabled"], d["products"], d["guide"]), (False, False, False))
        access.set_rights(self.olga, {"guide": False}, self.root)
        self.assertEqual(self.client.get("/rag/api/state/").status_code, 403)

    def test_ask_products_needs_right(self):
        with patch("rag_assistant.client.RagClient.ask", return_value="job-1"):
            self.assertEqual(self.ask("Яка частота AN050201?", mode="products").status_code, 202)
        self.conv.messages.update(status=RagMessage.DONE)                  # відповідь прийшла
        access.set_rights(self.olga, {"products": False}, self.root)
        with patch("rag_assistant.client.RagClient.ask", return_value="job-2") as rag:
            r = self.ask("Яка частота AN050201?", mode="products")
        self.assertEqual(r.status_code, 403)
        self.assertIn("📦", r.json()["error"])
        rag.assert_not_called()

    def test_guide_needs_right_and_falls_back_to_products(self):
        from strategy.models import AISettings
        ai = AISettings.get()
        ai.anthropic_api_key = "sk-ant-test"
        ai.save()
        access.set_rights(self.olga, {"guide": False}, self.root)
        r = self.ask("Як додати даташити з ПК у базу знань?", mode="guide")
        self.assertEqual(r.status_code, 403)
        self.assertIn("📚", r.json()["error"])
        with patch("rag_assistant.client.RagClient.ask", return_value="job-3"):
            r = self.ask("Як додати даташити з ПК у базу знань?")              # вгадали гід — права немає → 📦
        self.assertEqual((r.status_code, r.json()["assistant"]["mode"]), (202, "products"))


class SearchRightTests(RetrievalBase):
    def test_start_refuses_without_search_right(self):
        reader = person("reader", "readonly")
        self.assertFalse(retrieval.available_for(reader))
        job = retrieval.start(reader, "antenna delivery")
        self.assertEqual((job.status, job.message, self.fake.calls),
                         (RetrievalJob.UNAVAILABLE, retrieval.NO_SEARCH_RIGHT, []))
        olga = person("olga")
        self.assertTrue(retrieval.available_for(olga))
        self.assertNotEqual(retrieval.start(olga, "antenna delivery").message, retrieval.NO_SEARCH_RIGHT)
        access.set_rights(olga, {"search": False}, self.user)
        self.assertEqual(retrieval.start(fresh(olga), "antenna delivery").message, retrieval.NO_SEARCH_RIGHT)


class CardRightsTests(EntityHelpBase):
    def setUp(self):
        super().setUp()
        p = patch("rag_assistant.knowledge_items._start_background", lambda fn: fn())
        p.start()
        self.addCleanup(p.stop)
        self.olga = person("olga")
        self.client.force_login(self.olga)

    def docs_url(self):
        return f"/rag/api/entity/customer/{self.client_c.pk}/documents/"

    def test_documents_right(self):
        r = self.client.post(self.docs_url(), {"file": SimpleUploadedFile("Notiz.txt", b"Kratzer 3 Filter")})
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertTrue(r.json()["can_add"])
        item = KnowledgeItem.objects.get()
        access.set_rights(self.olga, {"documents": False}, self.user)
        r = self.client.post(self.docs_url(), {"file": SimpleUploadedFile("Zweite.txt", b"x")})
        self.assertEqual(r.status_code, 403)
        d = self.client.get(self.docs_url()).json()
        self.assertEqual((d["can_add"], [i["id"] for i in d["items"]]), (False, [item.pk]))   # бачити — можна
        self.assertEqual(self.client.post(f"/rag/api/knowledge/{item.pk}/delete/").status_code, 403)
        self.assertTrue(KnowledgeItem.objects.filter(pk=item.pk).exists())

    def test_card_help_skips_rag_without_search_right(self):
        access.set_rights(self.olga, {"search": False}, self.user)
        self.ai.letter = "ok"
        b = self.run_help("customer", self.client_c, "answer", question="які були скарги?")
        stage = next(s for s in b["stages"] if s["key"] == "rag")
        self.assertEqual((stage["status"], stage["detail"]), ("skipped", "немає права на пошук у базі знань"))
        self.assertEqual(self.rag, [])

    def test_reply_brief_skips_rag_without_search_right(self):
        from email_assistant import reply_brief
        access.set_rights(self.olga, {"search": False}, self.user)
        with patch("rag_assistant.retrieval.available", return_value=True):
            got = reply_brief._rag(None, fresh(self.olga), None, {}, "", self.client_c, [])
        self.assertEqual(got, ("skipped", "немає права на пошук у базі знань", ""))

    def test_admin_documents_list_follows_card_rights(self):
        team = KnowledgeItem.objects.create(title="Team-Kunde", kind=KnowledgeItem.LINK, url="https://a.example/x",
                                            customer=self.client_c, owner=self.user)
        hidden = KnowledgeItem.objects.create(title="Privat-Chef", kind=KnowledgeItem.LINK, url="https://a.example/y",
                                              visibility=KnowledgeItem.PRIVATE, owner=self.user)
        general = KnowledgeItem.objects.create(title="Allgemein-Doku", kind=KnowledgeItem.LINK,
                                               url="https://a.example/z", owner=self.user)
        r = self.client.get("/admin/rag_assistant/knowledgeitem/")
        self.assertEqual(r.status_code, 200)
        shown = {i.pk for i in r.context["cl"].result_list}
        self.assertEqual(shown, {team.pk, general.pk})
        self.assertNotIn(hidden.pk, shown)
        reader = person("reader2", "readonly")
        access.set_rights(reader, {"documents": True}, self.user)
        self.client.force_login(reader)
        shown = {i.pk for i in self.client.get("/admin/rag_assistant/knowledgeitem/").context["cl"].result_list}
        self.assertEqual(shown, {general.pk})                     # клієнтів CRM роль не бачить
