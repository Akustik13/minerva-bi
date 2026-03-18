# Generated manually 2026-03-18

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0005_marketplace_oauth_tokens'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='auto_confirm_mode',
            field=models.CharField(
                choices=[
                    ('never',    'Мануально — не підтверджувати автоматично'),
                    ('always',   'Завжди — підтверджувати одразу при надходженні'),
                    ('in_stock', 'Якщо є на складі — підтверджувати тільки якщо всі товари є'),
                ],
                default='never',
                help_text=(
                    'Що робити коли нове Marketplace замовлення надходить: '
                    'підтвердити одразу на DigiKey, перевірити залишки на складі, '
                    'або залишити для ручного підтвердження.'
                ),
                max_length=20,
                verbose_name='Авто-підтвердження на DigiKey',
            ),
        ),
    ]
