from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0039_shippingsettings_status_source_rules'),
    ]

    operations = [
        migrations.AddField(
            model_name='shippingsettings',
            name='status_conflict_priority',
            field=models.CharField(
                choices=[
                    ('highest',     'Вищий статус — найбільш просунутий (поточна поведінка)'),
                    ('carrier',     'Перевізник (UPS/DHL) — якщо відправлення активне, не ставити «Доставлено» з маркетплейсу'),
                    ('marketplace', 'Маркетплейс (DigiKey) — завжди вірити статусу замовлення'),
                ],
                default='highest',
                help_text=(
                    'Що робити якщо різні джерела показують різний статус. '
                    'Наприклад: DigiKey каже «Доставлено», а UPS — «В дорозі».'
                ),
                max_length=20,
                verbose_name='Пріоритет при конфлікті статусів',
            ),
        ),
    ]
