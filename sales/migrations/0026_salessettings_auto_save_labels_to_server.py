from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0025_salessettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='salessettings',
            name='auto_save_labels_to_server',
            field=models.BooleanField(
                default=True,
                help_text='При створенні UPS/DHL мітки — автоматично копіювати PDF етикетки '
                          'і митної декларації у папку документів замовлення '
                          '(media/orders/{source}/{order_number}/).',
                verbose_name='Зберігати мітки перевізника в документи замовлення',
            ),
        ),
    ]
