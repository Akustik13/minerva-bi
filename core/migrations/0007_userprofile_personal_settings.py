import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_userprofile_denied_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='can_manage_users',
            field=models.BooleanField(
                blank=True, default=None, null=True,
                verbose_name='Може керувати юзерами',
                help_text='Порожньо = за роллю',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_email',
            field=models.BooleanField(default=True, verbose_name='Email сповіщення'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notify_telegram',
            field=models.BooleanField(default=False, verbose_name='Telegram сповіщення'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='interface_language',
            field=models.CharField(
                choices=[('uk', 'Українська'), ('de', 'Deutsch'), ('en', 'English')],
                default='uk', max_length=5, verbose_name='Мова інтерфейсу',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='items_per_page',
            field=models.PositiveIntegerField(default=25, verbose_name='Записів на сторінку'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='theme',
            field=models.CharField(
                choices=[
                    ('dark', 'Темна'), ('light', 'Світла'),
                    ('minerva', 'Minerva'), ('auto', 'Авто'),
                ],
                default='dark', max_length=20, verbose_name='Тема',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='created_at',
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                verbose_name='Дата створення',
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(
                choices=[
                    ('superadmin', '👑 Суперадмін'),
                    ('admin', '🔑 Адміністратор'),
                    ('manager', '💼 Менеджер'),
                    ('warehouse', '📦 Складник'),
                    ('accountant', '💰 Бухгалтер'),
                    ('ai', '🤖 AI-асистент'),
                    ('readonly', '👁 Тільки перегляд'),
                ],
                default='manager',
                max_length=20,
                verbose_name='Роль',
            ),
        ),
    ]
