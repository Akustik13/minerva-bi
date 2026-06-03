from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0012_digikeylisting_dk_attributes'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeylisting',
            name='dk_category_name',
            field=models.CharField(
                blank=True, default='', max_length=200,
                verbose_name='Назва категорії DK',
                help_text='Заповнюється автоматично з DigiKey',
            ),
        ),
    ]
