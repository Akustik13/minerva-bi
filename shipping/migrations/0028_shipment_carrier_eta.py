from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0027_add_customs_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="shipment",
            name="carrier_eta",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="Орієнтовна дата доставки",
                help_text="Заповнюється автоматично при підтвердженні тарифу",
            ),
        ),
    ]
