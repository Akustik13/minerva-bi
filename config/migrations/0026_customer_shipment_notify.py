from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0025_shipment_notifications'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_notify_enabled',
            field=models.BooleanField(
                default=False,
                verbose_name="Кнопка «Надіслати клієнту»",
                help_text="На сторінці замовлення з'являється кнопка для надсилання клієнту повідомлення про відправку.",
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_notify_auto',
            field=models.BooleanField(
                default=False,
                verbose_name="Авто-відправка (без підтвердження)",
                help_text="Якщо увімкнено — лист надсилається одразу при натисканні кнопки без попереднього перегляду.",
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_notify_subject',
            field=models.CharField(
                blank=True,
                default="Ihre Bestellung #{order_number} wurde versendet",
                max_length=500,
                verbose_name="Тема листа клієнту (шаблон)",
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_notify_body',
            field=models.TextField(
                blank=True,
                default=(
                    "Sehr geehrte/r {customer_name},\n\n"
                    "Ihre Bestellung #{order_number} wurde am {shipped_date} versendet.\n\n"
                    "Versanddienstleister: {carrier}\n"
                    "Tracking-Nummer: {tracking_number}\n\n"
                    "Bestellte Artikel:\n{items}\n\n"
                    "Lieferadresse:\n{ship_address}\n\n"
                    "Mit freundlichen Grüßen"
                ),
                verbose_name="Текст листа клієнту (шаблон)",
            ),
        ),
    ]
