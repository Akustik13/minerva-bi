from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('calendar_app', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='calendarevent',
            name='push_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='CalendarSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('notify_telegram', models.BooleanField(default=False,
                                                        verbose_name='Telegram')),
                ('notify_email', models.BooleanField(default=True,
                                                     verbose_name='Email')),
                ('notify_push', models.BooleanField(default=True,
                                                    verbose_name='Push у браузері')),
                ('default_remind_minutes', models.PositiveIntegerField(
                    default=60,
                    verbose_name='Нагадувати за N хвилин (за замовчуванням)')),
                ('email_to', models.EmailField(
                    blank=True, verbose_name='Email для сповіщень',
                    help_text='Порожньо → використовується системний email')),
                ('telegram_chat_id', models.CharField(
                    blank=True, max_length=50, verbose_name='Telegram Chat ID',
                    help_text='Порожньо → UserProfile.telegram_id або системний')),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='calendar_settings',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Налаштування сповіщень календаря',
                'verbose_name_plural': 'Налаштування сповіщень календаря',
            },
        ),
    ]
