from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0060_packaging_stock'),
    ]

    operations = [
        migrations.AlterField(
            model_name='carrier',
            name='connection_uuid',
            field=models.CharField(blank=True, default='', help_text='Jumingo: UUID інтеграції | DHL: Account Number експорту й Innerdeutsch (9 цифр; відправник — Німеччина) | UPS: Account Number (6 символів)', max_length=100, verbose_name='Connection UUID / Account №'),
        ),
        migrations.AddField(
            model_name='carrier',
            name='import_account',
            field=models.CharField(blank=True, default='', help_text='DHL Express Import + Drittland (9 цифр): посилки, де відправник за кордоном — до нас або між двома іншими країнами. Порожньо — для всіх посилок «Account №».', max_length=20, verbose_name='DHL: акаунт імпорту / третьої країни'),
        ),
    ]
