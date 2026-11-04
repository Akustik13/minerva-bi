from django.db import migrations, models


def forwards(apps, schema_editor):
    """Наявні теми — сповіщено до того, що стара перевірка вже обробила (нове з того часу прийде, архів — ні);
    авто-перевірку й сповіщення в Telegram і на пошту вмикаємо (просив користувач, 11.2026)."""
    Seen = apps.get_model("bots", "DigiKeyMessageSeen")
    for s in Seen.objects.all():
        s.notified_upto = s.last_seen_at
        s.notified_message_id = s.last_message_id
        s.save(update_fields=["notified_upto", "notified_message_id"])
    Config = apps.get_model("bots", "DigiKeyConfig")
    Config.objects.update(msg_check_enabled=True, msg_notify_telegram=True, msg_notify_email=True)


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0042_digikey_image_update"),
    ]

    operations = [
        migrations.AddField(
            model_name="digikeymessageseen",
            name="notified_upto",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Сповіщено до"),
        ),
        migrations.AddField(
            model_name="digikeymessageseen",
            name="notified_message_id",
            field=models.CharField(blank=True, default="", max_length=64,
                                   verbose_name="Останнє перевірене повідомлення"),
        ),
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_last_result",
            field=models.CharField(blank=True, default="", max_length=300,
                                   help_text="Що знайшла остання перевірка повідомлень і куди надіслала (або чому ні)",
                                   verbose_name="Підсумок останньої перевірки"),
        ),
        migrations.AlterField(
            model_name="digikeyconfig",
            name="msg_check_enabled",
            field=models.BooleanField(default=True,
                                      help_text="Автоматично перевіряти нові повідомлення від покупців DigiKey",
                                      verbose_name="Авто-перевірка повідомлень"),
        ),
        migrations.AlterField(
            model_name="digikeyconfig",
            name="msg_notify_telegram",
            field=models.BooleanField(default=True,
                                      help_text="Нове повідомлення покупця чи оператора DigiKey — у Telegram (бот і чат — «Налаштування → Сповіщення»)",
                                      verbose_name="Telegram — при новому повідомленні"),
        ),
        migrations.AlterField(
            model_name="digikeyconfig",
            name="msg_notify_email",
            field=models.BooleanField(default=True,
                                      help_text="Нове повідомлення покупця чи оператора DigiKey пересилається листом: повний текст, попередні повідомлення розмови й посилання на відповідь у Minerva",
                                      verbose_name="Email — при новому повідомленні"),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
