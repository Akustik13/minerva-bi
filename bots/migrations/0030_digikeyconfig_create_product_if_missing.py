from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0029_digikeyconfig_api_retry_notify'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='create_product_if_missing',
            field=models.BooleanField(
                default=False,
                verbose_name='Створювати товар якщо нема на складі',
                help_text=(
                    "При імпорті лістингів («🆕 Створити лістинги»): якщо товар з таким SKU "
                    "відсутній на складі — автоматично створити картку товару і прив'язати лістинг. "
                    "Якщо вимкнено — SKU без товару пропускаються (виводяться у звіті)."
                ),
            ),
        ),
    ]
