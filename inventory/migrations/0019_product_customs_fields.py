from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0018_alter_productcategory_id_alter_productcategory_slug"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="hs_code",
            field=models.CharField(
                blank=True, default="", max_length=20, verbose_name="HS-код (митний)"
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="country_of_origin",
            field=models.CharField(
                blank=True, default="", max_length=2, verbose_name="Країна виробника"
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="net_weight_g",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="Вага нетто (г/шт)"
            ),
        ),
    ]
