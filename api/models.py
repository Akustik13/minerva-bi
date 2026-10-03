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
    ('webhooks:read',   'Вебхуки — перегляд'),
    ('webhooks:write',  'Вебхуки — створення / зміна / видалення'),
]

WEBHOOK_EVENTS = [
    ('stock.changed',        'Склад — змінився залишок товару'),
    ('order.created',        'Замовлення — створено'),
    ('order.status_changed', 'Замовлення — змінився статус'),
    ('shipment.updated',     'Доставка — статус або трекінг-номер'),
]


class APIKey(models.Model):
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
