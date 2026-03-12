from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0019_product_customs_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='name_export',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Англійська або німецька назва — друкується у Packing List, Proforma та CN23. '
                          'Якщо порожньо — використовується основна назва.',
                max_length=255,
                verbose_name='Назва (EN/DE) для документів',
            ),
        ),
    ]
