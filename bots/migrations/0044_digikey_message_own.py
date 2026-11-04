from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0043_digikey_message_watch"),
    ]

    operations = [
        migrations.AddField(
            model_name="digikeyconfig",
            name="msg_notify_own",
            field=models.BooleanField(
                default=True,
                help_text="Сповіщати й про відповіді колеги чи ваші (з Minerva або supplier.digikey.com); вимкніть — "
                          "надходитимуть лише повідомлення покупця й оператора DigiKey",
                verbose_name="І відповіді з нашого боку"),
        ),
    ]
