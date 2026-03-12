from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("config", "0007_notificationsettings_telegram"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="new_order_email",
            field=models.BooleanField(default=False, verbose_name="Email: нове замовлення",
                                      help_text="Надсилати email коли надходить нове замовлення."),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="new_order_telegram",
            field=models.BooleanField(default=False, verbose_name="Telegram: нове замовлення",
                                      help_text="Надсилати Telegram коли надходить нове замовлення."),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="status_change_email",
            field=models.BooleanField(default=False, verbose_name="Email: зміна статусу",
                                      help_text="Надсилати email при зміні статусу замовлення."),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="status_change_telegram",
            field=models.BooleanField(default=False, verbose_name="Telegram: зміна статусу",
                                      help_text="Надсилати Telegram при зміні статусу замовлення."),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="notify_on_processing",
            field=models.BooleanField(default=False, verbose_name="→ В обробці"),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="notify_on_shipped",
            field=models.BooleanField(default=True, verbose_name="→ Відправлено"),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="notify_on_delivered",
            field=models.BooleanField(default=True, verbose_name="→ Доставлено"),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="notify_on_cancelled",
            field=models.BooleanField(default=False, verbose_name="→ Скасовано"),
        ),
    ]
