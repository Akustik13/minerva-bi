from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0028_product_tech_attributes'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventorysettings',
            name='rfq_show_sku',
            field=models.BooleanField(default=True, verbose_name='Показувати SKU в листі'),
        ),
        migrations.AddField(
            model_name='inventorysettings',
            name='rfq_show_item_no',
            field=models.BooleanField(default=True, verbose_name='Показувати №пп'),
        ),
        migrations.AddField(
            model_name='inventorysettings',
            name='rfq_show_cable_params',
            field=models.BooleanField(
                default=False,
                help_text='Додає стовпець з tech_attributes до таблиці в листі.',
                verbose_name='Показувати технічні параметри (cable params)',
            ),
        ),
        migrations.AddField(
            model_name='inventorysettings',
            name='rfq_email_subject',
            field=models.CharField(
                default='Bestellung / Order Request',
                max_length=255,
                verbose_name='Тема листа',
            ),
        ),
        migrations.AddField(
            model_name='inventorysettings',
            name='rfq_email_greeting',
            field=models.TextField(
                default='Sehr geehrte Damen und Herren,\n\nhiermit möchten wir folgende Artikel bestellen:',
                verbose_name='Вступний текст',
            ),
        ),
        migrations.AddField(
            model_name='inventorysettings',
            name='rfq_email_signature',
            field=models.TextField(
                default='Mit freundlichen Grüßen',
                verbose_name='Підпис',
            ),
        ),
    ]
