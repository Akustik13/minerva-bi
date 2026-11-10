from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0039_briefing_last_sent'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='routes',
            field=models.JSONField(blank=True, default=dict, help_text='Свої адреси й Telegram-чати для окремих сповіщень; порожньо — «Кому (email)» і Chat ID вище.', verbose_name='Кому які сповіщення'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_block',
            field=models.TextField(blank=True, default='', help_text='Адреси або @домени через кому чи з нового рядка: їм не йдуть ні автоматичні, ні ручні листи «📧 про відправку» і «📥 підтвердження замовлення».', verbose_name='🚫 Не надсилати листи цим клієнтам'),
        ),
    ]
