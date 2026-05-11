import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('email_assistant', '0008_emailsettings_show_admin_sidebar'),
    ]
    operations = [
        migrations.CreateModel(
            name='ScheduledEmail',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='scheduled_emails',
                    to='email_assistant.emailaccount',
                    verbose_name='Акаунт',
                )),
                ('subject',      models.CharField(max_length=500, verbose_name='Тема')),
                ('to_emails',    models.JSONField(default=list, verbose_name='Отримувачі')),
                ('cc_emails',    models.JSONField(default=list, verbose_name='Копія')),
                ('body',         models.TextField(blank=True, verbose_name='Текст')),
                ('body_html',    models.TextField(blank=True, verbose_name='HTML')),
                ('scheduled_at', models.DateTimeField(db_index=True, verbose_name='Час відправки')),
                ('status', models.CharField(
                    choices=[
                        ('pending',   '⏳ Очікує'),
                        ('sent',      '✓ Надіслано'),
                        ('failed',    '✗ Помилка'),
                        ('cancelled', '✗ Скасовано'),
                    ],
                    db_index=True, default='pending', max_length=20, verbose_name='Статус',
                )),
                ('sent_at',   models.DateTimeField(blank=True, null=True, verbose_name='Надіслано о')),
                ('error_msg', models.TextField(blank=True, verbose_name='Помилка')),
                ('trigger',   models.CharField(default='manual', max_length=50, verbose_name='Тригер')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Запланований лист',
                'verbose_name_plural': 'Заплановані листи',
                'ordering': ['scheduled_at'],
            },
        ),
    ]
