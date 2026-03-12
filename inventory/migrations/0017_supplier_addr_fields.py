from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0016_product_supplier_extra_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplier",
            name="addr_street",
            field=models.CharField(blank=True, default="", max_length=300, verbose_name="Вулиця, будинок"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="addr_city",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="Місто"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="addr_zip",
            field=models.CharField(blank=True, default="", max_length=20, verbose_name="Поштовий індекс"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="addr_country",
            field=models.CharField(blank=True, default="", max_length=2, verbose_name="Країна (ISO 2)"),
        ),
    ]
