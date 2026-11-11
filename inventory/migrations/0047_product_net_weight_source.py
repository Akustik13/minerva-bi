from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0046_supplier_categories_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='net_weight_source',
            field=models.CharField(blank=True, choices=[('', '—'), ('datasheet', '📄 З даташита'), ('manual', '✍️ Внесено вручну'), ('customs', '🛃 З митної декларації'), ('agent', '🤖 Агент')], default='', max_length=12, verbose_name='Звідки вага'),
        ),
        migrations.AddField(
            model_name='product',
            name='net_weight_note',
            field=models.CharField(blank=True, default='', help_text='Цитата з даташита або результат перевірки («у даташиті ваги немає»).', max_length=300, verbose_name='Вага: примітка'),
        ),
        migrations.AddField(
            model_name='product',
            name='net_weight_checked_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Даташит на вагу перевірено'),
        ),
    ]
