from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0019_rename_sales_order_date_idx_sales_sales_order_d_55e46c_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesorder',
            name='addr_state',
            field=models.CharField(
                blank=True, default='',
                help_text='Тільки для США/Канади: CA, NY, TX, FL...',
                max_length=2, verbose_name='Штат / провінція (ISO 2)',
            ),
        ),
    ]
