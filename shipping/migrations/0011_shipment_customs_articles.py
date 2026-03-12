from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0010_shipment_recipient_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='customs_articles',
            field=models.JSONField(
                blank=True, null=True,
                verbose_name='Митна декларація (артикули)',
                help_text='Заповнюється автоматично при створенні відправлення',
            ),
        ),
    ]
