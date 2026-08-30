from decimal import Decimal
from django.db import models
from django.utils import timezone


class JLCConfig(models.Model):
    """Singleton — JLCPCB API credentials + sync + notification settings."""

    # ── Credentials ──────────────────────────────────────────────────────────
    app_id      = models.CharField('App ID', max_length=200, blank=True, default='',
                                   help_text='JLCPCB Developer Portal → App ID (числовий, наприклад 614741474579288066)')
    access_key  = models.CharField('Access Key', max_length=500, blank=True, default='',
                                   help_text='JLCPCB Developer Portal → Access Key')
    secret_key  = models.CharField('Secret Key / Tokenization Key', max_length=500, blank=True, default='',
                                   help_text='JLCPCB Developer Portal → Secret Key або Tokenization Key')
    use_sandbox = models.BooleanField('Sandbox режим', default=False,
                                      help_text='Тестовий режим (якщо JLCPCB надав sandbox endpoint)')

    # ── Connection log ────────────────────────────────────────────────────────
    connection_log = models.TextField('Лог підключення', blank=True, default='',
                                      help_text='Результат останнього тесту / синхронізації')

    # ── Sync ─────────────────────────────────────────────────────────────────
    sync_enabled         = models.BooleanField('Авто-синхронізація', default=False,
                                               help_text='Автоматично оновлювати статуси замовлень з JLCPCB API')
    sync_interval_hours  = models.PositiveSmallIntegerField('Інтервал (годин)', default=4,
                                                            help_text='Рекомендовано: 2–8 год')
    sync_days_back       = models.PositiveSmallIntegerField(
        'Період пошуку нових замовлень (днів)', default=30,
        help_text='Скільки днів назад шукати нові замовлення при синхронізації. '
                  'Рекомендовано: 30–90 днів. Більше = повільніше.',
    )
    last_synced_at       = models.DateTimeField('Остання синхронізація', null=True, blank=True)

    # ── Inventory integration ─────────────────────────────────────────────────
    auto_receive_on_delivered = models.BooleanField(
        'Авто-прийом на склад при доставці', default=True,
        help_text='Автоматично створювати транзакцію Incoming при статусі "Доставлено". '
                  'Вимкніть для ручного підтвердження через кнопку в адміні.',
    )
    default_location = models.CharField(
        'Локація складу за замовчуванням', max_length=50, default='MAIN',
        help_text='Код локації для нових транзакцій прийому (напр. MAIN, WAREHOUSE-A)',
    )

    # ── Notifications ─────────────────────────────────────────────────────────
    notify_on_shipped       = models.BooleanField('Сповіщення: відправлено', default=True)
    notify_on_status_change = models.BooleanField('Сповіщення: будь-яка зміна статусу', default=True)
    notify_on_delivered     = models.BooleanField('Сповіщення: доставлено', default=True)
    notify_telegram         = models.BooleanField('Telegram', default=True)
    telegram_personal_chat_id = models.CharField(
        'Telegram Personal Chat ID', max_length=50, blank=True, default='',
        help_text=(
            'Особистий Chat ID для JLCPCB сповіщень (наприклад: 123456789). '
            'Отримайте у @userinfobot в Telegram. '
            'Якщо порожньо — використовується Chat ID з загальних налаштувань (канал).'
        ),
    )
    notify_email            = models.BooleanField('Email', default=False)
    notify_email_to         = models.CharField(
        'Email одержувачів (через кому)', max_length=500, blank=True, default='',
        help_text='Порожньо = адреса з загальних налаштувань сповіщень',
    )

    class Meta:
        verbose_name        = 'Налаштування JLCPCB'
        verbose_name_plural = '⚙️ Налаштування JLCPCB'

    def __str__(self):
        return 'Налаштування JLCPCB'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class JLCOrder(models.Model):
    """Замовлення JLCPCB — виробництво PCB/PCBA/Stencil."""

    class OrderType(models.TextChoices):
        PCB     = 'pcb',     'PCB'
        PCBA    = 'pcba',    'PCBA (складання)'
        STENCIL = 'stencil', 'Stencil'
        THREE_D = '3d',      '3D Printing'
        OTHER   = 'other',   'Інше'

    class LocalStatus(models.TextChoices):
        ORDERED       = 'ordered',       'Замовлено'
        REVIEWED      = 'reviewed',      'Перевірено JLC'
        IN_PRODUCTION = 'in_production', 'У виробництві'
        MANUFACTURED  = 'manufactured',  'Виготовлено'
        SHIPPED       = 'shipped',       'Відправлено'
        DELIVERED     = 'delivered',     'Доставлено'
        CANCELLED     = 'cancelled',     'Скасовано'

    class MappingStatus(models.TextChoices):
        UNMATCHED  = 'unmatched',  'Не прив\'язано'
        MATCHED    = 'matched',    'Прив\'язано'
        TRACK_ONLY = 'track_only', 'Тільки трекінг'
        IGNORED    = 'ignored',    'Ігнорувати'

    # ── JLC дані ──────────────────────────────────────────────────────────────
    jlc_order_id     = models.CharField('ID замовлення JLC', max_length=100, unique=True,
                                        help_text='Унікальний ID з системи JLCPCB')
    jlc_order_number = models.CharField('Номер замовлення', max_length=100, blank=True, default='')
    order_type       = models.CharField('Тип', max_length=20,
                                        choices=OrderType.choices, default=OrderType.PCB)
    description      = models.CharField(
        'Опис/назва JLC', max_length=500, blank=True, default='',
        help_text='Назва з JLC, наприклад: AN120202-01H_2L_FR4_0.8x160x77.5mm_'
    )
    quantity = models.PositiveIntegerField('Кількість (шт)', default=1)

    # ── Статус ────────────────────────────────────────────────────────────────
    jlc_status   = models.CharField('Статус JLC (raw)', max_length=100, blank=True, default='',
                                    help_text='Оригінальний статус від JLCPCB API')
    local_status = models.CharField('Статус', max_length=20,
                                    choices=LocalStatus.choices, default=LocalStatus.ORDERED,
                                    db_index=True)

    # ── Доставка ──────────────────────────────────────────────────────────────
    tracking_number  = models.CharField('Трекінг номер', max_length=255, blank=True, default='')
    tracking_carrier = models.CharField('Перевізник', max_length=100, blank=True, default='',
                                        help_text='DHL, UPS, FedEx тощо')
    tracking_url     = models.URLField('URL трекінгу', blank=True, default='')

    # ── Дати ─────────────────────────────────────────────────────────────────
    order_date     = models.DateField('Дата замовлення', default=timezone.now)
    shipped_date   = models.DateField('Дата відправлення', null=True, blank=True)
    delivered_date = models.DateField('Дата доставки', null=True, blank=True)
    expected_date  = models.DateField('Очікувана дата', null=True, blank=True)

    # ── Фінанси ───────────────────────────────────────────────────────────────
    unit_price  = models.DecimalField('Ціна за шт', max_digits=12, decimal_places=2,
                                      null=True, blank=True)
    total_price = models.DecimalField('Загальна вартість', max_digits=12, decimal_places=2,
                                      null=True, blank=True)
    currency    = models.CharField('Валюта', max_length=3, default='USD')

    # ── Прив'язка до товару ───────────────────────────────────────────────────
    product = models.ForeignKey(
        'inventory.Product', on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name='Товар на складі',
        related_name='jlc_orders',
        help_text='Продукт у каталозі складу, що відповідає цьому замовленню',
    )
    mapping_status   = models.CharField('Прив\'язка', max_length=20,
                                        choices=MappingStatus.choices,
                                        default=MappingStatus.UNMATCHED, db_index=True)
    auto_matched_sku = models.CharField('Авто-знайдений SKU', max_length=255, blank=True, default='',
                                        help_text='SKU знайдений автоматично при матчингу')

    # ── Склад ─────────────────────────────────────────────────────────────────
    purchase_order = models.ForeignKey(
        'inventory.PurchaseOrder', on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name='Замовлення на закупівлю (PO)',
        related_name='jlc_orders',
    )
    received_qty = models.DecimalField('Отримано на склад (шт)', max_digits=12, decimal_places=3,
                                       default=Decimal('0'))

    # ── Сповіщення ────────────────────────────────────────────────────────────
    last_notified_status = models.CharField('Останній сповіщений статус', max_length=20,
                                            blank=True, default='')

    # ── Технічні поля ─────────────────────────────────────────────────────────
    raw_data   = models.JSONField('Дані API (JSON)', default=dict, blank=True)
    notes      = models.TextField('Нотатки', blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = 'Замовлення JLCPCB'
        verbose_name_plural = 'Замовлення JLCPCB'
        ordering            = ['-order_date', '-created_at']

    def __str__(self):
        desc = self.description[:40] if self.description else self.get_order_type_display()
        return f'{self.jlc_order_id} — {desc}'

    def is_active(self):
        return self.local_status not in (self.LocalStatus.DELIVERED, self.LocalStatus.CANCELLED)


class JLCProductMapping(models.Model):
    """Таблиця відповідностей: JLC reference/назва → Product на складі."""

    jlc_reference = models.CharField(
        'Назва/reference JLC', max_length=500, unique=True,
        help_text='Повна або часткова назва замовлення з JLCPCB '
                  '(наприклад: AN120202-01H або AN120202-01H_2L_FR4_...)',
    )
    product = models.ForeignKey(
        'inventory.Product', on_delete=models.CASCADE,
        verbose_name='Товар на складі',
        related_name='jlc_mappings',
    )
    match_type = models.CharField(
        'Тип матчу', max_length=20,
        choices=[('auto', 'Авто'), ('manual', 'Вручну')],
        default='manual',
    )
    notes      = models.CharField('Нотатки', max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Прив\'язка JLC → Товар'
        verbose_name_plural = 'Прив\'язки JLC → Товар'
        ordering            = ['jlc_reference']

    def __str__(self):
        return f'{self.jlc_reference} → {self.product.sku}'
