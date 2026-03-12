from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0012_alter_carrier_api_key_alter_carrier_api_secret_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="carrier",
            name="track_api_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "DHL Shipment Tracking – Unified API ключ (developer.dhl.com → My Apps). "
                    "Окремий від API ключа для тарифів."
                ),
                max_length=200,
                verbose_name="Tracking API Key",
            ),
        ),
    ]
