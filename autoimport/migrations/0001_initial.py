from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='AutoImportProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Назва профілю')),
                ('import_type', models.CharField(
                    choices=[
                        ('sales', '🛒 Замовлення (Sales)'),
                        ('products', '📦 Товари (Products)'),
                        ('receipt', '📥 Прихід на склад (Receipt)'),
                        ('adjust', '🔧 Коригування залишків (Adjust)'),
                    ],
                    default='sales', max_length=20, verbose_name='Тип імпорту'
                )),
                ('source_type', models.CharField(
                    choices=[
                        ('folder', '📁 Папка (локальний шлях)'),
                        ('url', '🌐 URL (HTTP / Google Sheets)'),
                    ],
                    default='folder', max_length=10, verbose_name='Джерело'
                )),
                ('source_path', models.CharField(
                    help_text='Локальна папка: /data/imports/ або C:\\imports\\ | URL: https://... | Google Sheets: https://docs.google.com/spreadsheets/d/{ID}/export?format=csv',
                    max_length=500, verbose_name='Шлях / URL'
                )),
                ('file_mask', models.CharField(
                    default='*.xlsx;*.csv',
                    help_text='Маски через крапку з комою, напр. *.xlsx;*.csv (тільки для типу Папка)',
                    max_length=100, verbose_name='Маска файлів'
                )),
                ('interval_minutes', models.PositiveSmallIntegerField(
                    default=60,
                    help_text='Мінімальний інтервал між запусками. Cron запускає команду частіше — профіль пропустить якщо ще не час.',
                    verbose_name='Інтервал (хв)'
                )),
                ('enabled', models.BooleanField(default=True, verbose_name='Активний')),
                ('archive_processed', models.BooleanField(
                    default=True,
                    help_text='Переміщує оброблені файли у підпапку _done/ (тільки для типу Папка)',
                    verbose_name='Архівувати оброблені'
                )),
                ('notify', models.BooleanField(
                    default=True,
                    help_text='Надсилати звіт через Email/Telegram після кожного запуску',
                    verbose_name='Сповіщення після запуску'
                )),
                ('conflict_strategy', models.CharField(
                    choices=[
                        ('skip', 'Пропустити (не перезаписувати)'),
                        ('update', 'Оновити існуючі записи'),
                    ],
                    default='skip',
                    help_text='Що робити якщо запис вже існує (тільки для Замовлень)',
                    max_length=10, verbose_name='Конфлікти'
                )),
                ('dry_run_mode', models.BooleanField(
                    default=False,
                    help_text='Завжди запускати без запису в БД (безпечне тестування маппінгу)',
                    verbose_name='Режим dry-run'
                )),
                ('last_run_at', models.DateTimeField(blank=True, editable=False, null=True, verbose_name='Останній запуск')),
                ('next_run_at', models.DateTimeField(blank=True, editable=False, null=True, verbose_name='Наступний запуск')),
                ('notes', models.TextField(blank=True, verbose_name='Нотатки')),
            ],
            options={
                'verbose_name': '📥 Профіль авто-імпорту',
                'verbose_name_plural': '📥 Профілі авто-імпорту',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='AutoImportLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('profile', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='logs',
                    to='autoimport.autoimportprofile',
                    verbose_name='Профіль'
                )),
                ('ran_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Час запуску')),
                ('source_name', models.CharField(max_length=500, verbose_name='Файл / URL')),
                ('file_hash', models.CharField(blank=True, max_length=64, verbose_name='SHA256')),
                ('status', models.CharField(
                    choices=[
                        ('ok', '✅ Успішно'),
                        ('skipped', '⏭️ Пропущено (дублікат)'),
                        ('error', '❌ Помилка'),
                        ('dry_run', '🧪 Dry-run'),
                    ],
                    default='ok', max_length=10, verbose_name='Статус'
                )),
                ('records_created', models.IntegerField(default=0, verbose_name='Створено')),
                ('records_updated', models.IntegerField(default=0, verbose_name='Оновлено')),
                ('records_skipped', models.IntegerField(default=0, verbose_name='Пропущено')),
                ('errors_count', models.IntegerField(default=0, verbose_name='Помилок')),
                ('error_detail', models.TextField(blank=True, verbose_name='Деталі помилок')),
                ('duration_ms', models.IntegerField(default=0, verbose_name='Час (мс)')),
            ],
            options={
                'verbose_name': '📋 Лог авто-імпорту',
                'verbose_name_plural': '📋 Логи авто-імпорту',
                'ordering': ['-ran_at'],
            },
        ),
    ]
