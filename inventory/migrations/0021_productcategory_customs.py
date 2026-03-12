from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0020_product_name_export'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcategory',
            name='customs_hs_code',
            field=models.CharField(
                blank=True, default='', max_length=20,
                help_text='Загальний HS-код для категорії — підставляється якщо у товару не заданий власний',
                verbose_name='HS-Code (категорія)',
            ),
        ),
        migrations.AddField(
            model_name='productcategory',
            name='customs_description_de',
            field=models.CharField(
                blank=True, default='', max_length=255,
                help_text='Наприклад: Antennen, Kabel, Elektronische Bauteile. Друкується в CN23 → Description of Contents',
                verbose_name='Опис товару (DE/EN)',
            ),
        ),
        migrations.AddField(
            model_name='productcategory',
            name='customs_country_of_origin',
            field=models.CharField(
                blank=True, default='DE', max_length=2,
                help_text='Дефолт для товарів категорії без заданої країни. ISO-2: DE, UA, CN, US…',
                verbose_name='Країна походження (ISO 2)',
            ),
        ),
    ]
