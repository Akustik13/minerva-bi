from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0040_notify_routes'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_senders',
            field=models.JSONField(blank=True, default=dict, help_text='Листи «📧 про відправку» і «📥 підтвердження замовлення»: SMTP сповіщень або скринька пошти Minerva.', verbose_name='З якої адреси листи клієнтам'),
        ),
    ]
