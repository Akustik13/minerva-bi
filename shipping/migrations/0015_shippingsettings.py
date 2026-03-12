from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0014_shipment_recipient_state_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShippingSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("auto_tracking_enabled", models.BooleanField(
                    default=False, verbose_name="Автоматичне оновлення трекінгу",
                    help_text="Вмикає автоматичне опитування API перевізників для всіх активних відправлень.",
                )),
                ("tracking_interval_minutes", models.PositiveSmallIntegerField(
                    default=30, verbose_name="Інтервал оновлення (хвилини)",
                    help_text="Як часто оновлювати трекінг.",
                )),
                ("last_tracking_run", models.DateTimeField(
                    blank=True, null=True, verbose_name="Останній запуск",
                )),
            ],
            options={
                "verbose_name": "Налаштування доставки",
                "verbose_name_plural": "Налаштування доставки",
            },
        ),
    ]
