from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0004_companysettings_invoice_images"),
    ]

    operations = [
        migrations.AddField(
            model_name="companysettings",
            name="fax",
            field=models.CharField(blank=True, default="", max_length=50, verbose_name="Факс (Fax)"),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="mobile",
            field=models.CharField(blank=True, default="", max_length=50, verbose_name="Мобільний (Mob)"),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="website",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="Сайт"),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="eori",
            field=models.CharField(blank=True, default="", max_length=50, verbose_name="EORI номер"),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="tax_id",
            field=models.CharField(blank=True, default="", max_length=50, verbose_name="TAX ID (Steuernummer)"),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="registration_court",
            field=models.CharField(
                blank=True, default="", max_length=100,
                verbose_name="Реєстраційний суд (напр. Munich, HRB 208657)",
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="ceo_name",
            field=models.CharField(
                blank=True, default="", max_length=200,
                verbose_name="Підписант (для рахунків)",
                help_text="Ім'я та посада, напр.: Dr. Max Mustermann, CEO",
            ),
        ),
    ]
