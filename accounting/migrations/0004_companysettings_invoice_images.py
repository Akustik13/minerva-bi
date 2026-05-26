from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0003_rename_accounting_invoice_status_idx_accounting__status_3055e0_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="companysettings",
            name="invoice_signature",
            field=models.ImageField(
                blank=True, null=True,
                upload_to="accounting/",
                verbose_name="Підпис (для рахунків-фактур)",
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="invoice_stamp",
            field=models.ImageField(
                blank=True, null=True,
                upload_to="accounting/",
                verbose_name="Печатка (для рахунків-фактур)",
            ),
        ),
    ]
