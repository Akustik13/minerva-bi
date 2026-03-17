from django.db import models
from django.utils import timezone


class AutoImportProfile(models.Model):
    TYPE_SALES    = 'sales'
    TYPE_PRODUCTS = 'products'
    TYPE_RECEIPT  = 'receipt'
    TYPE_ADJUST   = 'adjust'
    IMPORT_TYPES = [
        (TYPE_SALES,    '🛒 Замовлення (Sales)'),
        (TYPE_PRODUCTS, '📦 Товари (Products)'),
        (TYPE_RECEIPT,  '📥 Прихід на склад (Receipt)'),
        (TYPE_ADJUST,   '🔧 Коригування залишків (Adjust)'),
    ]

    SOURCE_FOLDER = 'folder'
    SOURCE_URL    = 'url'
    SOURCE_TYPES = [
        (SOURCE_FOLDER, '📁 Папка (локальний шлях)'),
        (SOURCE_URL,    '🌐 URL (HTTP / Google Sheets)'),
    ]

    CONFLICT_SKIP   = 'skip'
    CONFLICT_UPDATE = 'update'
    CONFLICT_CHOICES = [
        (CONFLICT_SKIP,   'Пропустити (не перезаписувати)'),
        (CONFLICT_UPDATE, 'Оновити існуючі записи'),
    ]

    name            = models.CharField('Назва профілю', max_length=100)
    import_type     = models.CharField('Тип імпорту', max_length=20, choices=IMPORT_TYPES, default=TYPE_SALES)
    source_type     = models.CharField('Джерело', max_length=10, choices=SOURCE_TYPES, default=SOURCE_FOLDER)
    source_path     = models.CharField(
        'Шлях / URL', max_length=500,
        help_text='Локальна папка: /data/imports/ або C:\\imports\\ | URL: https://... | Google Sheets: https://docs.google.com/spreadsheets/d/{ID}/export?format=csv'
    )
    file_mask       = models.CharField(
        'Маска файлів', max_length=100, default='*.xlsx;*.csv',
        help_text='Маски через крапку з комою, напр. *.xlsx;*.csv (тільки для типу Папка)'
    )
    interval_minutes = models.PositiveSmallIntegerField(
        'Інтервал (хв)', default=60,
        help_text='Мінімальний інтервал між запусками. Cron запускає команду частіше — профіль пропустить якщо ще не час.'
    )
    enabled         = models.BooleanField('Активний', default=True)
    archive_processed = models.BooleanField(
        'Архівувати оброблені', default=True,
        help_text='Переміщує оброблені файли у підпапку _done/ (тільки для типу Папка)'
    )
    notify          = models.BooleanField(
        'Сповіщення після запуску', default=True,
        help_text='Надсилати звіт через Email/Telegram після кожного запуску'
    )
    conflict_strategy = models.CharField(
        'Конфлікти', max_length=10, choices=CONFLICT_CHOICES, default=CONFLICT_SKIP,
        help_text='Що робити якщо запис вже існує (тільки для Замовлень)'
    )
    sheet_name      = models.CharField(
        'Вкладка (лист)', max_length=100, blank=True,
        help_text='Назва вкладки Excel. Порожньо = перший лист. Для CSV не застосовується.'
    )
    column_map      = models.JSONField(
        'Маппінг колонок', default=dict, blank=True,
        help_text='Словник {поле_системи: назва_колонки_у_файлі}. Заповнюється автоматично через кнопку «Виявити колонки».'
    )
    dry_run_mode    = models.BooleanField(
        'Режим dry-run', default=False,
        help_text='Завжди запускати без запису в БД (безпечне тестування маппінгу)'
    )
    last_run_at     = models.DateTimeField('Останній запуск', null=True, blank=True, editable=False)
    next_run_at     = models.DateTimeField('Наступний запуск', null=True, blank=True, editable=False)
    notes           = models.TextField('Нотатки', blank=True)

    class Meta:
        verbose_name = '📥 Профіль авто-імпорту'
        verbose_name_plural = '📥 Профілі авто-імпорту'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_import_type_display()})'

    def update_schedule(self):
        """Update last_run_at and compute next_run_at."""
        self.last_run_at = timezone.now()
        from datetime import timedelta
        self.next_run_at = self.last_run_at + timedelta(minutes=self.interval_minutes)
        self.save(update_fields=['last_run_at', 'next_run_at'])

    def is_due(self):
        """Return True if this profile should run now."""
        if not self.enabled:
            return False
        if self.next_run_at is None:
            return True
        return timezone.now() >= self.next_run_at


class AutoImportLog(models.Model):
    STATUS_OK      = 'ok'
    STATUS_SKIPPED = 'skipped'
    STATUS_ERROR   = 'error'
    STATUS_DRY_RUN = 'dry_run'
    STATUS_CHOICES = [
        (STATUS_OK,      '✅ Успішно'),
        (STATUS_SKIPPED, '⏭️ Пропущено (дублікат)'),
        (STATUS_ERROR,   '❌ Помилка'),
        (STATUS_DRY_RUN, '🧪 Dry-run'),
    ]

    profile         = models.ForeignKey(
        AutoImportProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='logs', verbose_name='Профіль'
    )
    ran_at          = models.DateTimeField('Час запуску', auto_now_add=True, db_index=True)
    source_name     = models.CharField('Файл / URL', max_length=500)
    file_hash       = models.CharField('SHA256', max_length=64, blank=True)
    status          = models.CharField('Статус', max_length=10, choices=STATUS_CHOICES, default=STATUS_OK)
    records_created = models.IntegerField('Створено', default=0)
    records_updated = models.IntegerField('Оновлено', default=0)
    records_skipped = models.IntegerField('Пропущено', default=0)
    errors_count    = models.IntegerField('Помилок', default=0)
    error_detail    = models.TextField('Деталі помилок', blank=True)
    duration_ms     = models.IntegerField('Час (мс)', default=0)

    class Meta:
        verbose_name = '📋 Лог авто-імпорту'
        verbose_name_plural = '📋 Логи авто-імпорту'
        ordering = ['-ran_at']

    def __str__(self):
        return f'[{self.ran_at:%Y-%m-%d %H:%M}] {self.source_name} → {self.get_status_display()}'
