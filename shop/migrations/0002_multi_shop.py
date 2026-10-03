"""Кілька магазинів (1/3): таблиці Shop, ShopListing, поле ShopPriceTier.listing.

Перенесення даних: створюється магазин за замовчуванням «Сайт sevskiy.de» (код webshop);
товари з галочкою «Показувати в інтернет-магазині», власною ціною магазину або ступенями
стають його позиціями; ступені цін переносяться на ці позиції.
"""
import django.core.validators
import django.db.models.deletion
from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0035_shop_fields"),
        ("shop", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Shop",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, verbose_name="Назва")),
                ("slug", models.CharField(help_text="Записується в замовлення як «Джерело» (напр. webshop) — за ним фільтруються продажі.",
                                          max_length=32, unique=True, verbose_name="Код (джерело замовлень)",
                                          validators=[django.core.validators.RegexValidator("^[a-z0-9][a-z0-9_-]*$", "Лише латиниця в нижньому регістрі, цифри, - і _")])),
                ("currency", models.CharField(default="EUR", max_length=3, verbose_name="Валюта")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активний")),
                ("is_default", models.BooleanField(default=False, help_text="Для ключів API, не прив'язаних до магазину.", verbose_name="За замовчуванням")),
                ("url", models.URLField(blank=True, default="", verbose_name="Адреса сайту")),
                ("notes", models.TextField(blank=True, default="", verbose_name="Нотатки")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name": "Магазин", "verbose_name_plural": "🏬 Магазини", "ordering": ["-is_default", "name"]},
        ),
        migrations.CreateModel(
            name="ShopListing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_visible", models.BooleanField(default=True, verbose_name="Показувати")),
                ("price", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True,
                                              help_text="Порожньо — «Ціна продажу» товару; без ціни на сайті «Ціна за запитом».",
                                              validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                                              verbose_name="Ціна (нетто, 1 шт.)")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shop_listings",
                                              to="inventory.product", verbose_name="Товар")),
                ("shop", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="listings",
                                           to="shop.shop", verbose_name="Магазин")),
            ],
            options={"verbose_name": "Позиція магазину", "verbose_name_plural": "💶 Асортимент і ціни",
                     "ordering": ["shop", "product__sku"]},
        ),
        migrations.AddConstraint(
            model_name="shoplisting",
            constraint=models.UniqueConstraint(fields=("shop", "product"), name="uniq_shop_listing"),
        ),
        migrations.AddField(
            model_name="shoppricetier",
            name="listing",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="tiers",
                                    to="shop.shoplisting", verbose_name="Позиція"),
        ),
    ]
