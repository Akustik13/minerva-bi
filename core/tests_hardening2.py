"""Захист (пакет H2): дії CRM і JLCPCB, що змінюють дані, — лише POST з CSRF і з правом; GET нічого не змінює.

• 📎 Документи клієнта: завантажити / видалити — POST і право змінювати клієнта; назви файлів у картці екрановані;
• злиття дублікатів (вибрані, «⚡ Злити всі») — права змінювати й видаляти клієнтів; дії списку «Статус → …» і
  «Сегмент → …» — право змінювати;
• «🎯 Стратегія» й «🔄 Синхронізація CRM» — за посиланням лише сторінка підтвердження, дію робить POST;
• JLCPCB: «🔌 Тест з'єднання», «🔄 Синхронізувати замовлення», «📊 Звіт статусів» — POST-форми й право змінювати
  налаштування.

python manage.py test core.tests_hardening2 --settings=tabele.settings_test
"""
import os
import re
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings

NO_RIGHT = "Немає права змінювати клієнтів"
TOKEN_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


def viewers():
    """Клієнтів лише переглядають: роль AI (CRM — перегляд) і «лише перегляд» з розділами CRM і стратегій."""
    return (person("ai", "ai"), person("vera", "readonly", allowed_modules=["crm", "strategy"]))


def merger():
    """Менеджер, якому дали ще й видаляти клієнтів."""
    return person("max", "manager", module_operations={"crm": ["view", "add", "change", "delete"]}, can_delete=True)


def csrf_client(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})


class CustomerDocsTests(_Base):
    """📎 Документи клієнта (media/customers/<pk>/)."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        over = override_settings(MEDIA_ROOT=self.root)
        over.enable()
        self.addCleanup(over.disable)
        self.customer = Customer.objects.create(name="Muster GmbH", email="info@muster.example")
        self.folder = os.path.join(self.root, "customers", str(self.customer.pk))
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "Vertrag.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4")
        base = f"/admin/crm/customer/{self.customer.pk}"
        self.upload_url, self.delete_url, self.change_url = f"{base}/upload-docs/", f"{base}/delete-doc/", \
            f"{base}/change/"

    def _files(self):
        return sorted(os.listdir(self.folder))

    def _upload(self, url=None, name="Rechnung.pdf"):
        return self.client.post(url or self.upload_url, {"documents": SimpleUploadedFile(name, b"%PDF-1.4 new")})

    def test_get_not_allowed(self):
        self.client.force_login(person("olga"))
        for url in (self.upload_url, self.delete_url):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 405, url)
            self.assertEqual(r["Allow"], "POST")
            self.assertIn("error", r.json())
        self.assertEqual(self._files(), ["Vertrag.pdf"])

    def test_view_only_refused(self):
        for viewer in viewers():
            self.client.force_login(viewer)
            r = self._upload()
            self.assertEqual(r.status_code, 403, viewer.username)
            self.assertEqual(r.json(), {"error": NO_RIGHT})
            r = self.client.post(self.delete_url, {"filename": "Vertrag.pdf"})
            self.assertEqual(r.status_code, 403, viewer.username)
            self.assertEqual(r.json(), {"error": NO_RIGHT})
        self.assertEqual(self._files(), ["Vertrag.pdf"])

    def test_manager_uploads_and_deletes(self):
        self.client.force_login(person("olga"))
        r = self._upload()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"], 1)
        self.assertEqual(self._files(), ["Rechnung.pdf", "Vertrag.pdf"])
        r = self.client.post(self.delete_url, {"filename": "Vertrag.pdf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(self._files(), ["Rechnung.pdf"])
        self.assertEqual(self.client.post(self.delete_url, {"filename": "../Rechnung.pdf"}).status_code, 400)
        self.assertEqual(self.client.post(self.delete_url, {"filename": "Vertrag.pdf"}).status_code, 404)
        missing = self.customer.pk + 999
        self.assertEqual(self._upload(f"/admin/crm/customer/{missing}/upload-docs/").status_code, 404)
        self.assertFalse(os.path.exists(os.path.join(self.root, "customers", str(missing))))

    def test_widget_escapes_file_names(self):
        """Назва з лапками, тегом і JS — лише текстом (раніше йшла в HTML і в onclick як є: збережений XSS)."""
        evil = "x\" onmouseover=\"alert(1)<img src=x onerror=alert(2)>');alert(3);('.pdf"
        self.client.force_login(person("olga"))
        with mock.patch("crm.admin._doc_files", return_value=[(evil, 2048), ("Vertrag.pdf", 10)]):
            r = self.client.get(self.change_url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn("<img src=x", html)
        self.assertNotIn('onmouseover="alert(1)', html)
        self.assertNotIn("');alert(3)", html)
        escaped = "x&quot; onmouseover=&quot;alert(1)&lt;img src=x onerror=alert(2)&gt;&#x27;);alert(3);(&#x27;.pdf"
        self.assertIn(f'title="{escaped}"', html)
        self.assertIn(f'data-name="{escaped}"', html)
        self.assertIn('onclick="custDocDelete(this)"', html)
        self.assertIn('translate="no"', html)

    def test_widget_links_real_files(self):
        name = "Angebot O'Neil & Co.pdf"
        with open(os.path.join(self.folder, name), "wb") as fh:
            fh.write(b"%PDF")
        self.client.force_login(person("olga"))
        html = self.client.get(self.change_url).content.decode()
        self.assertIn('title="Angebot O&#x27;Neil &amp; Co.pdf"', html)
        self.assertIn(f'href="{settings.MEDIA_URL}customers/{self.customer.pk}/Angebot%20O%27Neil%20%26%20Co.pdf"',
                      html)
        self.assertIn('data-name="Angebot O&#x27;Neil &amp; Co.pdf"', html)
        self.assertIn(f'data-url="/admin/crm/customer/{self.customer.pk}/delete-doc/"', html)
        self.assertIn('id="custDocInput"', html)                       # кнопка «📎 Вибрати файли» є
        self.assertIn("📂 Файли клієнта (2 шт.)", html)

    def test_view_only_sees_files_without_upload_or_delete(self):
        self.client.force_login(person("ai", "ai"))
        r = self.client.get(self.change_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'title="Vertrag.pdf"')
        self.assertContains(r, "💾 Зберегти")
        self.assertNotContains(r, 'id="custDocInput"')
        self.assertNotContains(r, "custDocDelete")


class MergeTests(_Base):
    """Злиття видаляє дублікатів назавжди — права змінювати й видаляти клієнтів."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        self.winner = Customer.objects.create(name="Muster GmbH", company="Muster GmbH", email="info@muster.example")
        self.loser = Customer.objects.create(name="Muster", company="MUSTER GmbH", email="sales@muster.example")
        self.ids = f"{self.winner.pk},{self.loser.pk}"

    def _count(self):
        from crm.models import Customer
        return Customer.objects.count()

    def test_without_delete_right_refused(self):
        for user in (person("olga"),) + viewers():                     # менеджер: змінювати можна, видаляти — ні
            self.client.force_login(user)
            self.assertEqual(self.client.get("/admin/crm/customer/merge/", {"ids": self.ids}).status_code, 403)
            r = self.client.post("/admin/crm/customer/merge/", {"ids": self.ids, "winner": self.winner.pk})
            self.assertEqual(r.status_code, 403, user.username)
            for method in (self.client.get, self.client.post):
                self.assertEqual(method("/admin/crm/customer/find-duplicates/merge-all/").status_code, 403)
        self.assertEqual(self._count(), 2)

    def test_find_duplicates_without_rights_shows_no_merge_buttons(self):
        self.client.force_login(person("olga"))
        r = self.client.get("/admin/crm/customer/find-duplicates/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "✓ переможець")                          # групи видно
        self.assertNotContains(r, 'class="fd-auto-btn"')
        self.assertNotContains(r, 'class="fd-merge-all-btn"')
        self.assertNotContains(r, 'class="fd-manual-btn"')
        self.assertContains(r, "🔒 Зливати дублікатів можна лише з правами змінювати й видаляти клієнтів")
        self.client.force_login(person("boss", "admin"))
        r = self.client.get("/admin/crm/customer/find-duplicates/")
        self.assertContains(r, 'class="fd-auto-btn"')
        self.assertContains(r, 'class="fd-merge-all-btn"')
        self.assertNotContains(r, "🔒 Зливати дублікатів")

    def test_list_actions_follow_rights(self):
        from crm.models import Customer
        ma = admin.site._registry[Customer]

        def actions(user):
            request = RequestFactory().get("/admin/crm/customer/")
            request.user = user
            return set(ma.get_actions(request))

        changing = {"action_set_status_vip", "action_set_status_blocked", "action_set_segment_b2b",
                    "action_set_segment_other"}
        olga = actions(person("olga"))
        self.assertTrue(changing <= olga)
        self.assertNotIn("action_merge_customers", olga)
        for viewer in viewers():
            got = actions(viewer)
            self.assertFalse((changing | {"action_merge_customers"}) & got, viewer.username)
            self.assertIn("action_check_stripe_payments", got)          # оновити стан зі Stripe — як і раніше
        self.assertIn("action_merge_customers", actions(merger()))
        self.assertIn("action_merge_customers", actions(person("boss", "admin")))

    def test_changelist_tells_template_what_is_allowed(self):
        """crm_can_change / crm_can_assign / crm_can_sync — для кнопок шаблону списку (⭐, «🎯», «🔄»)."""
        flags = ("crm_can_change", "crm_can_assign", "crm_can_sync")
        self.client.force_login(person("olga"))
        r = self.client.get("/admin/crm/customer/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([r.context[f] for f in flags], [True, True, True])
        self.client.force_login(person("ai", "ai"))
        r = self.client.get("/admin/crm/customer/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([r.context[f] for f in flags], [False, False, False])

    def test_view_only_cannot_run_status_action(self):
        data = {"action": "action_set_status_vip", "_selected_action": [self.winner.pk], "index": 0}
        self.client.force_login(person("ai", "ai"))
        self.assertEqual(self.client.post("/admin/crm/customer/", data).status_code, 302)
        self.winner.refresh_from_db()
        self.assertNotEqual(self.winner.status, "vip")
        self.client.force_login(person("olga"))
        self.assertEqual(self.client.post("/admin/crm/customer/", data).status_code, 302)
        self.winner.refresh_from_db()
        self.assertEqual(self.winner.status, "vip")

    def test_with_rights_merges(self):
        from crm.models import Customer
        self.client.force_login(merger())
        self.assertEqual(self.client.get("/admin/crm/customer/merge/", {"ids": self.ids}).status_code, 200)
        r = self.client.post("/admin/crm/customer/merge/", {"ids": self.ids, "winner": self.winner.pk})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Customer.objects.filter(pk=self.loser.pk).exists())
        self.assertTrue(Customer.objects.filter(pk=self.winner.pk).exists())

    def test_admin_merges_all_with_post_only(self):
        self.client.force_login(person("boss", "admin"))
        r = self.client.get("/admin/crm/customer/find-duplicates/merge-all/")
        self.assertRedirects(r, "/admin/crm/customer/find-duplicates/", fetch_redirect_response=False)
        self.assertEqual(self._count(), 2)
        r = self.client.post("/admin/crm/customer/find-duplicates/merge-all/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._count(), 1)


class StrategyTests(_Base):
    """«🎯 Стратегія»: посилання лише показує підтвердження, призначає POST — з правом змінювати клієнта й
    додавати стратегії."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        from strategy.models import StrategyTemplate
        for behavior in ("onboarding", "reactivation", "nurturing", "retention"):     # який би не порадив RFM
            StrategyTemplate.objects.create(name=f"Plan <b>{behavior}</b>", behavior_type=behavior, is_active=True)
        self.template = StrategyTemplate.objects.get(behavior_type="nurturing")
        self.customer = Customer.objects.create(name="Muster <script>alert(1)</script>", email="info@muster.example")
        base = f"/admin/crm/customer/{self.customer.pk}"
        self.auto_url, self.manual_url = f"{base}/assign-strategy/", f"{base}/assign-strategy/manual/"
        self.change_url = f"{base}/change/"

    def _strategies(self):
        from strategy.models import CustomerStrategy
        return list(CustomerStrategy.objects.filter(customer=self.customer))

    def test_get_only_confirms(self):
        self.client.force_login(person("olga"))
        r = self.client.get(self.auto_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'<form method="post" action="{self.auto_url}"')
        self.assertContains(r, "Muster &lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(r, "<script>alert(1)")
        self.assertContains(r, 'translate="no"')
        self.assertEqual(self._strategies(), [])

    def test_view_only_refused(self):
        for viewer in viewers():
            self.client.force_login(viewer)
            for url in (self.auto_url, self.manual_url):
                self.assertEqual(self.client.get(url).status_code, 403, (viewer.username, url))
                r = self.client.post(url, {"template_id": self.template.pk})
                self.assertEqual(r.status_code, 403, (viewer.username, url))
        self.assertEqual(self._strategies(), [])

    def test_manager_assigns_with_post_and_csrf(self):
        client = csrf_client(person("olga"))
        page = client.get(self.auto_url).content.decode()
        self.assertEqual(client.post(self.auto_url).status_code, 403)          # без токена CSRF — ні
        self.assertEqual(self._strategies(), [])
        r = client.post(self.auto_url, {"csrfmiddlewaretoken": TOKEN_RE.search(page).group(1)})
        strategies = self._strategies()
        self.assertEqual(len(strategies), 1)
        self.assertRedirects(r, f"/strategy/{strategies[0].pk}/canvas/", fetch_redirect_response=False)
        r = client.get(self.auto_url)                                     # уже є активна — на її канвас
        self.assertRedirects(r, f"/strategy/{strategies[0].pk}/canvas/", fetch_redirect_response=False)

    def test_manual_page_escapes_and_posts_with_its_token(self):
        client = csrf_client(person("olga"))
        r = client.get(self.manual_url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("Muster &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("Plan &lt;b&gt;nurturing&lt;/b&gt;", html)
        r = client.post(self.manual_url, {"template_id": self.template.pk,
                                          "csrfmiddlewaretoken": TOKEN_RE.search(html).group(1)})
        strategies = self._strategies()
        self.assertEqual([s.template_id for s in strategies], [self.template.pk])
        self.assertRedirects(r, f"/strategy/{strategies[0].pk}/canvas/", fetch_redirect_response=False)

    def test_strategy_button_posts(self):
        self.client.force_login(person("olga"))
        r = self.client.get(self.change_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'href="{self.auto_url}" onclick="if(event.ctrlKey||event.metaKey')
        self.assertContains(r, "f.method=&#x27;post&#x27;")


class SyncTests(_Base):
    """«🔄 Синхронізація CRM» створює й оновлює клієнтів — лише POST і права додавати й змінювати клієнтів."""

    URL = "/admin/crm/customer/sync-crm/"

    def setUp(self):
        super().setUp()
        from sales.models import SalesOrder
        self.order = SalesOrder.objects.create(source="webshop", order_number="300001")
        # без сигналу CRM: замовлення ще не має клієнта
        SalesOrder.objects.filter(pk=self.order.pk).update(client="Muster GmbH", email="info@muster.example")

    def _state(self):
        from crm.models import Customer
        from sales.models import SalesOrder
        return Customer.objects.count(), SalesOrder.objects.get(pk=self.order.pk).customer_key

    def test_get_only_confirms(self):
        self.client.force_login(person("olga"))
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'<form method="post" action="{self.URL}"')
        self.assertContains(r, "🔄 Синхронізувати")
        self.assertEqual(self._state(), (0, ""))

    def test_view_only_refused(self):
        for viewer in viewers():
            self.client.force_login(viewer)
            self.assertEqual(self.client.get(self.URL).status_code, 403, viewer.username)
            self.assertEqual(self.client.post(self.URL).status_code, 403, viewer.username)
        self.assertEqual(self._state(), (0, ""))

    def test_manager_syncs_with_post(self):
        from crm.models import Customer
        self.client.force_login(person("olga"))
        r = self.client.post(self.URL)
        self.assertRedirects(r, "/admin/crm/customer/", fetch_redirect_response=False)
        self.assertEqual(self._state(), (1, Customer.generate_key("info@muster.example", "Muster GmbH")))


class JLCConfigActionTests(_Base):
    """Сторінка налаштувань JLCPCB: тест з'єднання, синхронізація, звіт статусів."""

    URLS = ("/admin/jlcpcb/jlcconfig/test-connection/", "/admin/jlcpcb/jlcconfig/sync-orders/",
            "/admin/jlcpcb/jlcconfig/status-report/")
    CHANGE = "/admin/jlcpcb/jlcconfig/1/change/"

    def setUp(self):
        super().setUp()
        from jlcpcb.models import JLCConfig
        JLCConfig.objects.update_or_create(pk=1, defaults={"connection_log": "старий журнал\n"})
        self.api = mock.MagicMock()
        self.api.test_connection.return_value = {"ok": True, "message": "✅ JLCPCB відповів"}
        patches = (
            mock.patch("jlcpcb.services.api.JLCAPIClient.from_config", return_value=self.api),
            mock.patch("django.core.management.call_command"),
            mock.patch("jlcpcb.notifications.notify_jlc_active_orders_summary",
                       return_value={"count": 2, "telegram": True, "email": False}),
        )
        self.from_config, self.call_command, self.notify = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def _log(self):
        from jlcpcb.models import JLCConfig
        return JLCConfig.objects.get(pk=1).connection_log

    def _nothing_ran(self):
        self.from_config.assert_not_called()
        self.call_command.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(self._log(), "старий журнал\n")

    @staticmethod
    def editor():
        return person("jan", "readonly", allowed_modules=["jlcpcb"], module_operations={"jlcpcb": ["view", "change"]})

    @staticmethod
    def viewer():
        return person("vera", "readonly", allowed_modules=["jlcpcb"])

    def test_view_only_refused(self):
        self.client.force_login(self.viewer())
        for url in self.URLS:
            self.assertEqual(self.client.get(url).status_code, 403, url)
            self.assertEqual(self.client.post(url).status_code, 403, url)
        self._nothing_ran()

    def test_get_does_nothing(self):
        self.client.force_login(self.editor())
        for url in self.URLS:
            r = self.client.get(url)
            self.assertRedirects(r, "/admin/jlcpcb/jlcconfig/", fetch_redirect_response=False)
        self._nothing_ran()

    def test_editor_runs_with_post(self):
        self.client.force_login(self.editor())
        r = self.client.post(self.URLS[0])
        self.assertRedirects(r, self.CHANGE, fetch_redirect_response=False)
        self.api.test_connection.assert_called_once_with()
        self.assertIn("Тест з'єднання", self._log())
        self.assertIn("старий журнал", self._log())
        r = self.client.post(self.URLS[1])
        self.assertRedirects(r, self.CHANGE, fetch_redirect_response=False)
        self.assertEqual(self.call_command.call_args.args, ("sync_jlc_orders", "--force"))
        r = self.client.post(self.URLS[2])
        self.assertRedirects(r, self.CHANGE, fetch_redirect_response=False)
        self.notify.assert_called_once_with()

    def test_config_page_buttons_are_post_forms(self):
        client = csrf_client(self.editor())
        page = client.get(self.CHANGE)
        self.assertEqual(page.status_code, 200)
        for url in self.URLS:
            self.assertContains(page, f'<form method="post" action="{url}">')
            self.assertNotContains(page, f'href="{url}"')
        self.assertNotContains(page, "🔒 Тест з'єднання")
        self.assertEqual(client.post(self.URLS[2]).status_code, 403)              # без токена CSRF — ні
        self.notify.assert_not_called()
        token = TOKEN_RE.search(page.content.decode()).group(1)
        r = client.post(self.URLS[2], {"csrfmiddlewaretoken": token})
        self.assertRedirects(r, self.CHANGE, fetch_redirect_response=False)
        self.notify.assert_called_once_with()

    def test_viewer_page_has_no_action_buttons(self):
        self.client.force_login(self.viewer())
        page = self.client.get(self.CHANGE)
        self.assertEqual(page.status_code, 200)
        for url in self.URLS:
            self.assertNotContains(page, url)
        self.assertContains(page, "🔒 Тест з'єднання, синхронізація й звіт статусів — лише з правом змінювати")
        self.assertContains(page, "📋 Переглянути замовлення")
