from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0016_salessource"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesorder",
            name="addr_street",
            field=models.CharField(blank=True, default="", max_length=300, verbose_name="Вулиця, будинок"),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="addr_city",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="Місто"),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="addr_zip",
            field=models.CharField(blank=True, default="", max_length=20, verbose_name="Поштовий індекс"),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="addr_country",
            field=models.CharField(blank=True, default="", help_text="Двобуквений код: DE, UA, PL, US...",
                                   max_length=2, verbose_name="Країна (ISO 2)"),
        ),
        migrations.AddIndex(
            model_name="salesorder",
            index=models.Index(fields=["order_date"], name="sales_order_date_idx"),
        ),
        migrations.AddIndex(
            model_name="salesorder",
            index=models.Index(fields=["status"], name="sales_status_idx"),
        ),
        migrations.AddIndex(
            model_name="salesorder",
            index=models.Index(fields=["source"], name="sales_source_idx"),
        ),
        migrations.AddIndex(
            model_name="salesorder",
            index=models.Index(fields=["addr_country"], name="sales_addr_country_idx"),
        ),
    ]
