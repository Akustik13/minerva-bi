from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0034_stripe_embedded'),
    ]

    operations = [
        migrations.AddField(
            model_name='shop',
            name='team_notify_telegram',
            field=models.BooleanField(default=True, help_text='Оплата отримана, новий акаунт покупця — ще й повідомленням у Telegram (бот — «Налаштування → Сповіщення»).', verbose_name='Команді — і в Telegram'),
        ),
        migrations.AddField(
            model_name='shop',
            name='team_telegram',
            field=models.CharField(blank=True, default='', help_text='Chat ID через кому (група, канал @назва або особистий чат з ботом). Порожньо — основний чат «Налаштування → Сповіщення».', max_length=300, verbose_name='Telegram-чати команди'),
        ),
        migrations.AlterField(
            model_name='shop',
            name='team_emails',
            field=models.CharField(blank=True, default='', help_text='Через кому. Оплати, нові акаунти покупців, а з «Сайт надсилає листи з цієї ж скриньки» — і нові замовлення з сайту. Порожньо — email з «Налаштування → Сповіщення» (замовлення — як на сайті).', max_length=1000, verbose_name='Сповіщення команді — на адреси'),
        ),
    ]
