from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0026_customer_shipment_notify'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notificationsettings',
            name='customer_notify_body',
            field=models.TextField(
                blank=True,
                default=(
                    "Dear {customer_name},\n\n"
                    "Your order #{order_number} has been shipped on {shipped_date}.\n\n"
                    "Carrier: {carrier}\n"
                    "Tracking number: {tracking_number}\n\n"
                    "Items shipped:\n{items}\n\n"
                    "Delivery address:\n{ship_address}\n\n"
                    "Best regards"
                ),
                verbose_name="Текст листа клієнту — ЄС (шаблон)",
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_notify_body_noneu',
            field=models.TextField(
                blank=True,
                default=(
                    "Dear {customer_name},\n\n"
                    "Your order #{order_number} has been shipped on {shipped_date}.\n\n"
                    "Carrier: {carrier}\n"
                    "Tracking number: {tracking_number}\n\n"
                    "Items shipped:\n{items}\n\n"
                    "Delivery address:\n{ship_address}\n\n"
                    "Please note: Your shipment originates from the EU. "
                    "Depending on your country's import regulations, customs duties and/or import taxes "
                    "may be charged upon delivery. These fees are the responsibility of the recipient "
                    "and are not included in the order price. "
                    "We recommend checking with your local customs authority for more information.\n\n"
                    "Best regards"
                ),
                verbose_name="Текст листа клієнту — не-ЄС (шаблон)",
            ),
        ),
    ]
