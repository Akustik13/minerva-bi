from django.db import migrations, models

COUNTRY_MAP = {
    "UA": "UA", "UKR": "UA", "UKRAINE": "UA", "УКРАЇНА": "UA",
    "DE": "DE", "DEU": "DE", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "PL": "PL", "POL": "PL", "POLAND": "PL",
    "US": "US", "USA": "US",
    "GB": "GB", "GBR": "GB", "UK": "GB",
    "AT": "AT", "AUT": "AT", "AUSTRIA": "AT",
    "CH": "CH", "CHE": "CH", "SWITZERLAND": "CH",
    "CZ": "CZ", "CZE": "CZ",
    "NL": "NL", "NLD": "NL",
    "FR": "FR", "FRA": "FR",
    "IT": "IT", "ITA": "IT",
    "ES": "ES", "ESP": "ES",
}


def normalize_country_values(apps, schema_editor):
    """Нормалізувати country до ISO 2 перед AlterField(max_length=2)."""
    Customer = apps.get_model("crm", "Customer")
    to_update = []
    for customer in Customer.objects.exclude(country="").only("id", "country"):
        raw = (customer.country or "").strip().upper()
        iso2 = COUNTRY_MAP.get(raw, raw[:2] if raw else "")
        if iso2 != customer.country:
            customer.country = iso2
            to_update.append(customer)
    if to_update:
        Customer.objects.bulk_update(to_update, ["country"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0005_alter_customer_email_alter_customer_external_key"),
    ]

    operations = [
        # Спочатку нормалізуємо дані — потім змінюємо max_length
        migrations.RunPython(normalize_country_values, migrations.RunPython.noop),

        migrations.AddField(
            model_name="customer",
            name="addr_street",
            field=models.CharField(blank=True, default="", max_length=300, verbose_name="Вулиця, будинок"),
        ),
        migrations.AddField(
            model_name="customer",
            name="addr_city",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="Місто"),
        ),
        migrations.AddField(
            model_name="customer",
            name="addr_zip",
            field=models.CharField(blank=True, default="", max_length=20, verbose_name="Поштовий індекс"),
        ),
        # Тепер безпечно міняємо max_length=3 → 2
        migrations.AlterField(
            model_name="customer",
            name="country",
            field=models.CharField(
                blank=True, db_index=True, default="",
                help_text="Двобуквений код: DE, UA, PL, US...",
                max_length=2, verbose_name="Країна (ISO 2)",
            ),
        ),
        migrations.AlterField(
            model_name="customer",
            name="shipping_address",
            field=models.TextField(blank=True, default="", verbose_name="Адреса (raw, legacy)"),
        ),
    ]
