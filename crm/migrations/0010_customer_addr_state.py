from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0009_alter_customer_name_company_verbose'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='addr_state',
            field=models.CharField(
                blank=True, default='',
                help_text='Тільки для США/Канади: CA, NY, TX, FL...',
                max_length=2, verbose_name='Штат / провінція (ISO 2)',
            ),
        ),
    ]
