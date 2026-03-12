from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0022_weight_decimal_delivered_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='salesorder',
            name='addr_state',
            field=models.CharField(
                blank=True,
                default='',
                help_text='США/Канада: дволітерний код (CA, NY, TX). Інші країни: повна назва регіону.',
                max_length=100,
                verbose_name='Штат / провінція',
            ),
        ),
    ]
