import secrets
import uuid

from django.db import models
from django.utils import timezone

SCOPE_CHOICES = [
    ('orders:read',     'Замовлення — читання'),
    ('orders:write',    'Замовлення — запис'),
    ('products:read',   'Товари — читання'),
    ('products:write',  'Товари — запис'),
    ('customers:read',  'Клієнти — читання'),
    ('customers:write', 'Клієнти — запис'),
    ('stock:read',      'Склад — залишки, рухи, локації (читання)'),
    ('stock:write',     'Склад — прихід, списання, інвентаризація'),
    ('shipments:read',  'Доставка — відправлення і трекінг (читання)'),
    ('purchases:read',  'Закупівлі — замовлення постачальникам, трекінг, доставки (читання)'),
    ('purchases:write', 'Закупівлі — передати замовлення зовнішньої CRM, трекінг, «доставлено»'),
    ('accounts:read',   'Акаунти покупців магазину — кабінет, замовлення, кошик (читання)'),
    ('accounts:write',  'Акаунти покупців магазину — реєстрація, вхід, пароль, профіль, кошик'),
    ('webhooks:read',   'Вебхуки — перегляд'),
    ('webhooks:write',  'Вебхуки — створення / зміна / видалення'),
    ('knowledge:read',  'База знань — стрічка листів для RAG (лише службовий ключ RAG, не магазин)'),
    ('knowledge:write', 'База знань — статуси індексації від RAG'),
]

WEBHOOK_EVENTS = [
    ('stock.changed',        'Склад — змінився залишок товару'),
    ('order.created',        'Замовлення — створено'),
    ('order.status_changed', 'Замовлення — змінився статус'),
    ('shipment.updated',     'Доставка — статус або трекінг-номер'),
    ('purchase_order.updated', 'Закупівля — стан, трекінг, очікувана дата, доставка (лише якщо вибрано)'),
]

# Події лише для вебхуків, які вибрали саме їх: «усі події» (порожній список) їх не отримують —
# дані закупівель (постачальники, ціни) не йдуть на сайт магазину.
EXPLICIT_EVENTS = {'purchase_order.updated'}


class APIKey(models.Model):
    LOG_ALL, LOG_CHANGES, LOG_ERRORS, LOG_OFF = 'all', 'changes', 'errors', 'off'
    LOG_CHOICES = [
        (LOG_ALL, 'Усі запити'),
        (LOG_CHANGES, 'Лише зміни (POST / PATCH / DELETE) і помилки'),
        (LOG_ERRORS, 'Лише помилки й відмови'),
        (LOG_OFF, 'Не записувати'),
    ]

    name       = models.CharField('Назва', max_length=100)
    key        = models.CharField('Ключ', max_length=64, unique=True, editable=False)
    scopes     = models.JSONField('Права доступу', default=list)
    is_active  = models.BooleanField('Активний', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used  = models.DateTimeField('Останнє використання', null=True, blank=True)
    expires_at = models.DateField('Дійсний до', null=True, blank=True)
    shop = models.ForeignKey(
        'shop.Shop', on_delete=models.SET_NULL, null=True, blank=True, related_name='api_keys',
        verbose_name='Магазин',
        help_text='Асортимент і ціни якого магазину бачить цей ключ (/shop/products/) і куди йдуть його замовлення. '
                  'Порожньо — магазин з кодом = «Джерело замовлень», інакше магазин за замовчуванням.',
    )
    default_source = models.CharField(
        'Джерело замовлень', max_length=32, blank=True, default='',
        help_text="Slug джерела (SalesSource) для замовлень, створених цим ключем, "
                  "якщо в запиті не вказано 'source'. Напр.: webshop",
    )

    # ── Обмеження й журнал (api/key_limits.py) ──
    allowed_ips = models.TextField(
        'Дозволені IP-адреси', blank=True, default='',
        help_text='Адреси або мережі через кому чи з нового рядка (203.0.113.5, 10.0.0.0/24). '
                  'Порожньо — з будь-якої адреси. З іншої адреси ключ не працює (401).')
    limit_minute = models.PositiveIntegerField(
        'Запитів за хвилину', null=True, blank=True,
        help_text='Порожньо — загальний ліміт сервера (API_RATE_LIMIT, зазвичай 120).')
    limit_hour = models.PositiveIntegerField('Запитів за годину', null=True, blank=True,
                                             help_text='Календарна година. Порожньо — без ліміту.')
    limit_day = models.PositiveIntegerField('Запитів за добу', null=True, blank=True,
                                            help_text='Календарна доба (Europe/Berlin). Порожньо — без ліміту.')
    items_per_day = models.PositiveIntegerField(
        'Різних записів за добу', null=True, blank=True,
        help_text='Скільки різних товарів / замовлень (за артикулом або номером) ключ може побачити за добу. '
                  'Уже переглянуті сьогодні — без обмежень, решта у списках приховується. Порожньо — без ліміту.')
    fields = models.TextField(
        'Лише ці поля у відповідях', blank=True, default='',
        help_text='Напр. «sku, available» — у списках і картках записів лишаться тільки ці поля. '
                  'Порожньо — усі поля.')
    log_mode = models.CharField('Журнал запитів', max_length=8, choices=LOG_CHOICES, default=LOG_ALL)

    class Meta:
        verbose_name = 'API Ключ'
        verbose_name_plural = 'API Ключі'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = secrets.token_hex(32)  # 64 hex chars
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def has_scope(self, scope):
        return scope in self.scopes

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False


def _webhook_secret():
    return "whsec_" + secrets.token_hex(24)


class Webhook(models.Model):
    """Підписка зовнішньої системи (магазину) на події Minerva."""
    name       = models.CharField('Назва', max_length=100)
    url        = models.URLField('URL отримувача', max_length=500,
                                 help_text='HTTPS-адреса, куди Minerva надсилатиме POST з подією')
    secret     = models.CharField('Секрет підпису', max_length=80, default=_webhook_secret,
                                  help_text='Використовується для HMAC-SHA256 підпису (X-Minerva-Signature)')
    events     = models.JSONField('Події', default=list, blank=True,
                                  help_text='Порожньо = всі події')
    order_sources = models.CharField(
        'Тільки джерела замовлень', max_length=255, blank=True, default='',
        help_text='Через кому, напр.: webshop. Порожньо = замовлення з усіх джерел. '
                  'Стосується order.* і shipment.*',
    )
    is_active  = models.BooleanField('Активний', default=True)
    api_key    = models.ForeignKey(APIKey, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name='webhooks', verbose_name='Створено ключем',
                                   help_text='Заповнюється, якщо вебхук зареєстровано через API')
    created_at = models.DateTimeField(auto_now_add=True)
    last_delivery_at     = models.DateTimeField('Остання доставка', null=True, blank=True)
    last_status_code     = models.PositiveSmallIntegerField('Останній HTTP-код', null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField('Невдач поспіль', default=0)

    class Meta:
        verbose_name = 'Вебхук'
        verbose_name_plural = 'Вебхуки'
        ordering = ['name']

    def __str__(self):
        return self.name

    def wants(self, event, source=None):
        if not self.is_active:
            return False
        if self.events and event not in self.events and event != 'ping':
            return False
        if event in EXPLICIT_EVENTS and event not in (self.events or []):
            return False
        if source is not None and self.order_sources.strip():
            allowed = {s.strip() for s in self.order_sources.split(',') if s.strip()}
            return source in allowed
        return True


class WebhookDelivery(models.Model):
    """Одна спроба доставки події (з повторами)."""
    PENDING, SUCCESS, FAILED = 'pending', 'success', 'failed'
    STATUS_CHOICES = [(PENDING, 'Очікує'), (SUCCESS, 'Доставлено'), (FAILED, 'Помилка')]

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook    = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name='deliveries')
    event      = models.CharField('Подія', max_length=50)
    payload    = models.JSONField('Дані')
    status     = models.CharField('Статус', max_length=10, choices=STATUS_CHOICES, default=PENDING)
    attempts   = models.PositiveSmallIntegerField('Спроб', default=0)
    response_code = models.PositiveSmallIntegerField('HTTP-код', null=True, blank=True)
    response_body = models.TextField('Відповідь / помилка', blank=True, default='')
    next_attempt_at = models.DateTimeField('Наступна спроба', default=timezone.now)
    created_at   = models.DateTimeField('Створено', auto_now_add=True)
    delivered_at = models.DateTimeField('Доставлено', null=True, blank=True)

    class Meta:
        verbose_name = 'Доставка вебхука'
        verbose_name_plural = 'Журнал вебхуків'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'next_attempt_at'])]

    def __str__(self):
        return f"{self.event} → {self.webhook}"


class APIRequestLog(models.Model):
    """Журнал запитів до /api/v1/: який ключ, з якої адреси, що робив, чим відповіли (api/key_limits.record)."""
    created_at = models.DateTimeField('Коли', auto_now_add=True, db_index=True)
    key = models.ForeignKey(APIKey, on_delete=models.SET_NULL, null=True, blank=True,
                            related_name='request_logs', verbose_name='Ключ')
    key_name = models.CharField('Назва ключа', max_length=100, blank=True, default='')
    key_hint = models.CharField('Невідомий ключ', max_length=20, blank=True, default='',
                                help_text='Початок і кінець ключа, з яким не пустили')
    ip = models.GenericIPAddressField('IP-адреса', null=True, blank=True, db_index=True)
    method = models.CharField('Метод', max_length=8)
    path = models.CharField('Адреса', max_length=300)
    query = models.CharField('Параметри', max_length=500, blank=True, default='')
    view = models.CharField('Дія', max_length=100, blank=True, default='')
    scope = models.CharField('Право', max_length=40, blank=True, default='')
    status = models.PositiveSmallIntegerField('Відповідь', db_index=True)
    code = models.CharField('Код помилки', max_length=50, blank=True, default='')
    items = models.PositiveIntegerField('Записів', null=True, blank=True)
    objects_text = models.CharField('Артикули / номери', max_length=300, blank=True, default='')
    object_ids = models.JSONField(
        'Усі записи відповіді', default=list, blank=True,
        help_text='«sku:AMP-100», «order_number:WS-1001», «code:PO-12», «id:5» — кожен запис, який ключ отримав '
                  '(до 2000; більше — лише кількість у «Записів»)')
    result = models.JSONField(
        'Що змінилось', null=True, blank=True,
        help_text='Для POST / PATCH / PUT / DELETE: назви полів тіла запиту (без значень) і що відповіла Minerva — '
                  'номер, статус, рух складу')
    note = models.CharField('Примітка', max_length=300, blank=True, default='')
    duration_ms = models.PositiveIntegerField('мс', default=0)
    user_agent = models.CharField('Програма', max_length=200, blank=True, default='')

    class Meta:
        verbose_name = 'Запит API'
        verbose_name_plural = '📜 Журнал API'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['key', '-created_at'], name='api_log_key_time')]

    def __str__(self):
        return f'{self.method} {self.path} → {self.status}'
