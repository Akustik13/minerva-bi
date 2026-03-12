# bots/migrations/0001_initial.py
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Bot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Назва')),
                ('bot_type', models.CharField(choices=[('digikey', 'DigiKey Parser'), ('mouser', 'Mouser Parser'), ('custom', 'Custom Script')], default='digikey', max_length=20, verbose_name='Тип')),
                ('description', models.TextField(blank=True, default='', verbose_name='Опис')),
                ('is_active', models.BooleanField(default=True, verbose_name='Увімкнено')),
                ('status', models.CharField(choices=[('active', 'Активний'), ('paused', 'Призупинений'), ('error', 'Помилка'), ('running', 'Виконується')], default='paused', max_length=20, verbose_name='Статус')),
                ('login', models.CharField(blank=True, default='', max_length=200, verbose_name='Логін')),
                ('password', models.CharField(blank=True, default='', help_text='⚠️ Буде зашифровано при збереженні', max_length=200, verbose_name='Пароль')),
                ('api_key', models.CharField(blank=True, default='', max_length=500, verbose_name='API ключ')),
                ('schedule_enabled', models.BooleanField(default=False, verbose_name='Авто-запуск')),
                ('schedule_cron', models.CharField(blank=True, default='0 */6 * * *', help_text='Формат: хвилина година день місяць день_тижня. Приклад: 0 */6 * * * (кожні 6 год)', max_length=100, verbose_name='Розклад (cron)')),
                ('schedule_interval_minutes', models.IntegerField(blank=True, help_text='Альтернатива cron: запуск кожні N хвилин', null=True, verbose_name='Інтервал (хвилин)')),
                ('last_run_at', models.DateTimeField(blank=True, null=True, verbose_name='Останній запуск')),
                ('last_run_status', models.CharField(blank=True, default='', max_length=20, verbose_name='Статус останнього запуску')),
                ('last_run_duration', models.IntegerField(blank=True, null=True, verbose_name='Тривалість (сек)')),
                ('next_run_at', models.DateTimeField(blank=True, null=True, verbose_name='Наступний запуск')),
                ('total_runs', models.IntegerField(default=0, verbose_name='Всього запусків')),
                ('success_runs', models.IntegerField(default=0, verbose_name='Успішних запусків')),
                ('error_runs', models.IntegerField(default=0, verbose_name='Помилок')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Оновлено')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='Автор')),
            ],
            options={
                'verbose_name': 'Бот',
                'verbose_name_plural': 'Боти',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='BotLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField(auto_now_add=True, verbose_name='Початок')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='Кінець')),
                ('duration', models.IntegerField(blank=True, null=True, verbose_name='Тривалість (сек)')),
                ('level', models.CharField(choices=[('info', 'Інформація'), ('success', 'Успіх'), ('warning', 'Попередження'), ('error', 'Помилка')], default='info', max_length=20, verbose_name='Рівень')),
                ('message', models.TextField(blank=True, default='', verbose_name='Повідомлення')),
                ('details', models.JSONField(blank=True, null=True, verbose_name='Деталі')),
                ('items_processed', models.IntegerField(default=0, verbose_name='Оброблено записів')),
                ('items_created', models.IntegerField(default=0, verbose_name='Створено')),
                ('items_updated', models.IntegerField(default=0, verbose_name='Оновлено')),
                ('items_failed', models.IntegerField(default=0, verbose_name='Помилок')),
                ('bot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='logs', to='bots.bot', verbose_name='Бот')),
            ],
            options={
                'verbose_name': 'Лог бота',
                'verbose_name_plural': 'Логи ботів',
                'ordering': ['-started_at'],
            },
        ),
    ]
