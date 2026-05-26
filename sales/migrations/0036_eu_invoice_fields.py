from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0035_salesorder_status_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesorder",
            name="eu_invoice_number",
            field=models.IntegerField(
                blank=True, null=True, verbose_name="EU Invoice №",
                help_text="Номер рахунку-фактури для ЄС (автозаповнюється при генерації)",
            ),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="eu_invoice_date",
            field=models.DateField(
                blank=True, null=True, verbose_name="Дата рахунку-фактури",
            ),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="buyer_vat_id",
            field=models.CharField(
                blank=True, default="", max_length=50,
                verbose_name="VAT ID покупця",
                help_text="ПДВ маркетплейсу (напр. DigiKey DE815236628)",
            ),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="ship_vat_id",
            field=models.CharField(
                blank=True, default="", max_length=50,
                verbose_name="VAT ID отримувача",
                help_text="ПДВ отримувача (якщо є)",
            ),
        ),
    ]
