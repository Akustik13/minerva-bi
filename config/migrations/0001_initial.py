from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SystemSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("company_name", models.CharField(default="Моя компанія", max_length=255, verbose_name="Назва компанії")),
                ("logo", models.ImageField(blank=True, null=True, upload_to="config/", verbose_name="Логотип")),
                ("default_currency", models.CharField(default="EUR", max_length=3, verbose_name="Валюта за замовчуванням")),
                ("timezone", models.CharField(default="Europe/Kyiv", max_length=50, verbose_name="Часовий пояс")),
                ("enabled_modules", models.JSONField(
                    default=list,
                    help_text="Список app labels: crm, accounting, sales, shipping, inventory, bots",
                    verbose_name="Активні модулі",
                )),
                ("accounting_level", models.IntegerField(
                    choices=[(1, "Базовий (Invoice + Payment)"), (2, "Стандарт (+ Витрати, VAT)"), (3, "Розширений (+ Журнал проводок)")],
                    default=2,
                    verbose_name="Рівень бухгалтерії",
                )),
                ("is_onboarding_complete", models.BooleanField(default=False, verbose_name="Онбординг завершено")),
            ],
            options={
                "verbose_name": "Системні налаштування",
                "verbose_name_plural": "Системні налаштування",
            },
        ),
    ]
