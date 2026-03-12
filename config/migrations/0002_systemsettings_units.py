from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='weight_unit',
            field=models.CharField(
                verbose_name='Одиниця ваги',
                max_length=5,
                choices=[('kg', 'кг (кілограм)'), ('g', 'г (грам)'), ('lb', 'lb (фунт)')],
                default='kg',
                help_text='Використовується у відправленнях та пакувальних матеріалах',
            ),
        ),
        migrations.AddField(
            model_name='systemsettings',
            name='dimension_unit',
            field=models.CharField(
                verbose_name='Одиниця розмірів',
                max_length=5,
                choices=[('cm', 'см (сантиметр)'), ('mm', 'мм (міліметр)'), ('in', 'in (дюйм)')],
                default='cm',
                help_text='Використовується для габаритів коробок та посилок',
            ),
        ),
    ]
