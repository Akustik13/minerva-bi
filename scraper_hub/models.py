from django.db import models
from django.utils import timezone

SITE_CHOICES = [
    ('jlcpcb', 'JLCPCB'),
    ('ups',    'UPS Billing'),
]

STATUS_CHOICES = [
    ('idle',    'Очікує'),
    ('running', 'Виконується'),
    ('ok',      'Успішно'),
    ('partial', 'Частково'),
    ('error',   'Помилка'),
]


class ScraperSiteConfig(models.Model):
    site_name  = models.CharField('Сайт', max_length=50, choices=SITE_CHOICES, unique=True)
    username   = models.CharField('Логін / Email', max_length=200)
    password   = models.CharField('Пароль', max_length=200)
    enabled    = models.BooleanField('Увімкнено', default=True)

    cron_schedule = models.CharField(
        'Cron-розклад', max_length=100, blank=True, default='0 9 * * 1',
        help_text='Cron-вираз для автозапуску (напр. "0 9 * * 1" = пн 9:00). '
                  'Порожньо — лише вручну.',
    )
    lookback_days = models.PositiveIntegerField(
        'Глибина пошуку (днів)', default=30,
        help_text='Скільки днів назад перевіряти рахунки при кожному запуску.',
    )

    # ── Сповіщення ────────────────────────────────────────────────────────────
    notify_email    = models.BooleanField('Email-сповіщення', default=True)
    notify_telegram = models.BooleanField('Telegram-сповіщення', default=False)

    # ── Бухгалтерія ───────────────────────────────────────────────────────────
    auto_create_expense = models.BooleanField(
        'Авто-витрата в бухгалтерії', default=True,
        help_text='Автоматично створювати запис Expense після завантаження PDF.',
    )
    expense_category = models.ForeignKey(
        'accounting.ExpenseCategory', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='Категорія витрат',
        related_name='scraper_configs',
    )
    supplier = models.ForeignKey(
        'inventory.Supplier', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='Постачальник',
        related_name='scraper_configs',
    )

    # ── Статус (денормалізовано для швидкого відображення) ────────────────────
    last_run_at     = models.DateTimeField('Останній запуск', null=True, blank=True)
    last_run_status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='idle')
    last_run_files  = models.PositiveIntegerField('Завантажено файлів', default=0)

    class Meta:
        verbose_name        = 'Scraper — сайт'
        verbose_name_plural = 'Scraper — сайти'

    def __str__(self):
        return f'{self.get_site_name_display()} ({self.username})'


class ScraperRun(models.Model):
    config           = models.ForeignKey(ScraperSiteConfig, on_delete=models.CASCADE, related_name='runs')
    started_at       = models.DateTimeField('Початок', default=timezone.now)
    finished_at      = models.DateTimeField('Кінець', null=True, blank=True)
    status           = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='running')
    files_downloaded = models.PositiveIntegerField('Файлів', default=0)
    error_message    = models.TextField('Помилка', blank=True)
    log_output       = models.TextField('Лог', blank=True)
    triggered_by     = models.CharField('Запущено', max_length=100, default='manual')

    class Meta:
        verbose_name        = 'Запуск'
        verbose_name_plural = 'Запуски'
        ordering            = ['-started_at']

    def __str__(self):
        return f'{self.config.site_name} {self.started_at:%Y-%m-%d %H:%M}'

    @property
    def duration_s(self):
        if self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds())
        return None


class ScraperDocument(models.Model):
    run          = models.ForeignKey(ScraperRun, on_delete=models.CASCADE, related_name='documents')
    batch_num    = models.CharField('Batch #', max_length=100, db_index=True)
    invoice_date = models.DateField('Дата рахунку', null=True, blank=True)
    amount       = models.DecimalField('Сума', max_digits=12, decimal_places=2, null=True, blank=True)
    currency     = models.CharField('Валюта', max_length=3, default='USD')
    file         = models.FileField('PDF', upload_to='scraper/%Y/%m/', blank=True)
    jlc_order = models.ForeignKey(
        'jlcpcb.JLCOrder', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='JLCPCB замовлення',
        related_name='scraper_documents',
    )
    expense = models.OneToOneField(
        'accounting.Expense', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='Витрата (бухгалтерія)',
        related_name='scraper_document',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Рахунок'
        verbose_name_plural = 'Рахунки'
        ordering            = ['-created_at']

    def __str__(self):
        return self.batch_num
