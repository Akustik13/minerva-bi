"""Кнопки списків замовлень і клієнтів — за правами людини (сервер і так відмовляє 403, але кнопок не видно).

• замовлення: ⭐ і 📝 у таблиці й картках — лише з правом змінювати; без нього позначка й нотатка лише видно;
• клієнти: ⭐, «🎯 Стратегія» (таблиця й картки) і «🔄 Синхронізація CRM» — лише з правом; посилання стратегії й
  синхронізації шлють POST одразу (crmPostLink), без JS — сторінка підтвердження;
• клітинки рядка, які панель праворуч перемальовує після швидкої зміни, — кнопкою, хоч би яким був останній список.

python manage.py test core.tests_list_rights --settings=tabele.settings_test
"""
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})

    def page(self, user, url):
        self.client.force_login(user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()


class OrderListTests(_Base):
    def setUp(self):
        super().setUp()
        from sales.models import SalesOrder
        self.order = SalesOrder.objects.create(source="manual", order_number="L-1", client="Kunde A",
                                               is_flagged=True, internal_note="Rückruf morgen")

    def test_manager_gets_buttons(self):
        html = self.page(person("olga"), "/admin/sales/salesorder/")
        self.assertIn(f"svFlagOrder({self.order.pk})", html)
        self.assertIn(f"svNoteToggle({self.order.pk})", html)
        self.assertIn(f'id="sv-note-panel-{self.order.pk}"', html)

    def test_view_only_sees_marks_without_buttons(self):
        html = self.page(person("vera", "readonly", allowed_modules=["sales"]), "/admin/sales/salesorder/")
        self.assertNotIn(f"svFlagOrder({self.order.pk})", html)
        self.assertNotIn(f"svNoteToggle({self.order.pk})", html)
        self.assertNotIn(f'id="sv-note-panel-{self.order.pk}"', html)
        self.assertIn('title="Позначено як важливе">⭐</span>', html)          # позначку видно
        self.assertIn("Rückruf morgen", html)                                  # нотатку теж

    def test_panel_cell_after_edit_is_a_button(self):
        """Останній список дивилась людина без права — клітинка після швидкої зміни все одно кнопкою."""
        from core import panel_edit
        from sales.models import SalesOrder
        self.page(person("vera", "readonly", allowed_modules=["sales"]), "/admin/sales/salesorder/")
        ma = admin.site._registry[SalesOrder]
        self.assertFalse(ma._list_can_change)
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root", "root@x.y", "p")

        class Edit:
            cells = ("flag_col",)

        cells = panel_edit._cells(Edit, ma, request, self.order)
        self.assertIn(f"svFlagOrder({self.order.pk})", cells["flag_col"])


class CustomerListTests(_Base):
    def setUp(self):
        super().setUp()
        from crm.models import Customer
        self.customer = Customer.objects.create(name="Muster GmbH", email="info@muster.example", is_favorite=True)

    def test_manager_gets_buttons_that_post(self):
        html = self.page(person("olga"), "/admin/crm/customer/")
        self.assertIn(f"crmFavorite({self.customer.pk})", html)
        self.assertIn("/admin/crm/customer/sync-crm/", html)
        self.assertIn("return crmPostLink(event,this)", html)                  # синхронізація — одним POST
        self.assertIn(f"/admin/crm/customer/{self.customer.pk}/assign-strategy/", html)

    def test_view_only_has_no_change_buttons(self):
        html = self.page(person("ai", "ai"), "/admin/crm/customer/")
        self.assertNotIn(f"crmFavorite({self.customer.pk})", html)
        self.assertNotIn("/admin/crm/customer/sync-crm/", html)
        self.assertNotIn(f"/admin/crm/customer/{self.customer.pk}/assign-strategy", html)
        self.assertIn('title="Обраний клієнт">⭐</span>', html)               # обраного видно
