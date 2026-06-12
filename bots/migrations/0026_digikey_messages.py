from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0025_digikeylisting_product_nullable"),
    ]

    operations = [
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_check_enabled",
            field=models.BooleanField(
                default=False,
                verbose_name="Авто-перевірка повідомлень",
                help_text="Автоматично перевіряти нові повідомлення від покупців DigiKey",
            ),
        ),
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_check_interval",
            field=models.PositiveSmallIntegerField(
                default=15,
                verbose_name="Інтервал перевірки (хвилин)",
                help_text="Рекомендовано: 5–60 хв.",
            ),
        ),
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_notify_telegram",
            field=models.BooleanField(
                default=True,
                verbose_name="Сповіщення в Telegram",
            ),
        ),
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_notify_email",
            field=models.BooleanField(
                default=True,
                verbose_name="Сповіщення на Email",
            ),
        ),
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_last_checked_at",
            field=models.DateTimeField(
                null=True,
                blank=True,
                verbose_name="Остання перевірка повідомлень",
            ),
        ),
        migrations.CreateModel(
            name="DigiKeyMessageSeen",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("topic_id", models.CharField(max_length=64, unique=True, verbose_name="Topic ID")),
                ("last_message_id", models.CharField(blank=True, default="", max_length=64, verbose_name="Last Message ID")),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "DigiKey Message Seen",
                "verbose_name_plural": "DigiKey Messages Seen",
            },
        ),
    ]
