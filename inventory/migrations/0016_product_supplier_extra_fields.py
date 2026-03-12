from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0015_productcategory"),
    ]

    operations = [
        # ── Product ──────────────────────────────────────────────────────────
        migrations.AddField(
            model_name="product",
            name="manufacturer",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="Виробник"),
        ),
        migrations.AddField(
            model_name="product",
            name="purchase_price",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18,
                                      null=True, verbose_name="Ціна закупки"),
        ),
        migrations.AddField(
            model_name="product",
            name="sale_price",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18,
                                      null=True, verbose_name="Ціна продажу"),
        ),
        migrations.AddField(
            model_name="product",
            name="reorder_point",
            field=models.PositiveIntegerField(
                default=0,
                help_text="При залишку нижче цього значення — дозамовити",
                verbose_name="Точка дозамовлення",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="lead_time_days",
            field=models.PositiveSmallIntegerField(blank=True, null=True,
                                                   verbose_name="Термін поставки (дні)"),
        ),
        # ── Supplier ─────────────────────────────────────────────────────────
        migrations.AddField(
            model_name="supplier",
            name="contact_person",
            field=models.CharField(blank=True, default="", max_length=255,
                                   verbose_name="Контактна особа"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="email",
            field=models.EmailField(blank=True, default="", verbose_name="Email"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=50,
                                   verbose_name="Телефон"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="payment_terms",
            field=models.CharField(blank=True, default="", max_length=100,
                                   help_text="напр. Net 30, Prepayment, 50/50",
                                   verbose_name="Умови оплати"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="currency",
            field=models.CharField(blank=True, default="EUR", max_length=3,
                                   verbose_name="Валюта"),
        ),
        # Оновлюємо verbose_name полів Supplier що вже існували
        migrations.AlterField(
            model_name="supplier",
            name="name",
            field=models.CharField(max_length=255, unique=True, verbose_name="Назва"),
        ),
        migrations.AlterField(
            model_name="supplier",
            name="website",
            field=models.URLField(blank=True, default="", verbose_name="Веб-сайт"),
        ),
        migrations.AlterField(
            model_name="supplier",
            name="notes",
            field=models.TextField(blank=True, default="", verbose_name="Нотатки"),
        ),
    ]
