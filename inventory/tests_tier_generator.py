"""📈 Генератор ступенів і колонка «Сума» в базових цінах (картка товару, масовий редактор): сторінки рендеряться зі
спільним скриптом admin/inventory/product/_tier_gen.js. Сама арифметика — у браузері.

    python manage.py test inventory.tests_tier_generator --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import Product


class TierGeneratorPagesTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.p = Product.objects.create(sku="AN-GEN", name="Gen", base_price_currency="EUR",
                                        base_prices=[{"min_qty": 1, "unit_price": "0.3"},
                                                     {"min_qty": 1000, "unit_price": "0.24"}])

    def test_card_has_generator_and_sum(self):
        page = self.client.get(f"/admin/inventory/product/{self.p.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "📈 Генератор ступенів…")
        self.assertContains(page, "function tierPlan(")            # спільний скрипт вставлено
        self.assertContains(page, "var TIER_PRESETS")
        self.assertContains(page, "bp-main")                        # основна таблиця — окремо від перегляду генератора
        self.assertContains(page, "Скільки коштує вся ця кількість")

    def test_editor_has_generator_and_sum(self):
        page = self.client.get(f"/admin/inventory/product/base-prices/?ids={self.p.pk}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Згенерувати знижки")
        self.assertContains(page, "function tierPlan(")
        self.assertContains(page, "Заповнити шаблон")
