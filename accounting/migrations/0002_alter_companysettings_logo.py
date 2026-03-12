from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companysettings",
            name="logo",
            field=models.FileField(
                blank=True, null=True,
                upload_to="accounting/logos/",
                verbose_name="Логотип (PNG/JPG)",
            ),
        ),
    ]
