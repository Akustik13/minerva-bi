from django.db import models
from django.core.exceptions import ValidationError
from inventory.models import Product


class SalesSource(models.Model):
    """Словник джерел замовлень — редагується з адмін-інтерфейсу."""
    slug  = models.CharField("Код (slug)", max_length=32, unique=True,
                             help_text="Латиниця, без пробілів. Зберігається в замовленнях.")
    name  = models.CharField("Назва", max_length=128)
    color = models.CharField("Колір бейджу", max_length=16, default="#607d8b",
                             help_text="HEX, напр. #e91e63")
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Джерело замовлення"
        verbose_name_plural = "Джерела замовлень"

    def __str__(self):
        return self.name


class SalesOrder(models.Model):
    DOCUMENT_TYPE_CHOICES = [
        ("SALE", "Sale"), ("SAMPLE", "Sample"), ("TRANSFER", "Transfer"),
        ("WARRANTY", "Warranty"), ("OTHER", "Other"),
    ]
    source          = models.CharField(max_length=32, default="digikey")
    document_type   = models.CharField(max_length=16, choices=DOCUMENT_TYPE_CHOICES, default="SALE")
    affects_stock   = models.BooleanField(default=True)
    order_number    = models.CharField(max_length=64)
    order_date      = models.DateField(null=True, blank=True)
    shipped_at      = models.DateField(null=True, blank=True)
    delivered_at    = models.DateTimeField("Фактична доставка", null=True, blank=True)
    shipping_courier   = models.CharField(max_length=64,  blank=True, default="")
    tracking_number    = models.CharField(max_length=128, blank=True, default="")
    lieferschein_nr    = models.CharField(max_length=64,  blank=True, default="")
    shipping_region    = models.CharField(max_length=32,  blank=True, default="")
    shipping_address   = models.TextField(blank=True, default="")
    client          = models.CharField(max_length=255, blank=True, default="")
    contact_name    = models.CharField(max_length=255, blank=True, default="", verbose_name="Контактна особа")
    phone           = models.CharField(max_length=64,  blank=True, default="")
    email           = models.EmailField(blank=True, default="")

    # --- CRM зв'язок ---
    customer_key = models.CharField(
        "Ключ клієнта CRM", 
        max_length=64, 
        blank=True, 
        default="",
        db_index=True,
        help_text="Посилання на Customer.external_key"
    )
    # ── Структурована адреса доставки ────────────────────────────────────────
    addr_street  = models.CharField("Вулиця, будинок", max_length=300, blank=True, default="")
    addr_city    = models.CharField("Місто",           max_length=100, blank=True, default="")
    addr_zip     = models.CharField("Поштовий індекс", max_length=20,  blank=True, default="")
    addr_state   = models.CharField("Штат / провінція", max_length=100, blank=True, default="",
                                    help_text="США/Канада: дволітерний код (CA, NY, TX). Інші країни: повна назва регіону.")
    addr_country = models.CharField("Країна (ISO 2)",  max_length=2,   blank=True, default="",
                                    help_text="Двобуквений код: DE, UA, PL, US...")

    shipping_deadline  = models.DateField(null=True, blank=True)
    # ── Загальна сума замовлення ──
    STATUS_CHOICES = [
        ("received",   "Отримано"),
        ("processing", "В обробці"),
        ("shipped",    "Відправлено"),
        ("delivered",  "Доставлено"),
        ("cancelled",  "Скасовано"),
    ]
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default="received")
    total_price     = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency        = models.CharField(max_length=8, blank=True, default="USD", verbose_name="Валюта продажу")
    shipping_cost   = models.DecimalField("Вартість доставки", max_digits=10, decimal_places=2, default=0)
    shipping_currency = models.CharField(max_length=8, blank=True, default="EUR", verbose_name="Валюта доставки")

    class Meta:
        unique_together = [("source", "order_number")]
        indexes = [
            models.Index(fields=["order_date"]),
            models.Index(fields=["status"]),
            models.Index(fields=["source"]),
            models.Index(fields=["addr_country"]),
        ]

    def save(self, *args, **kwargs):
        if self.shipping_courier:
            from sales.utils import normalize_courier
            self.shipping_courier = normalize_courier(self.shipping_courier)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.source}:{self.order_number}"

    @property
    def deadline_status(self):
        """Returns dict with deadline info for template rendering, or None."""
        if not self.shipping_deadline:
            return None
        from datetime import date
        today = date.today()
        days = (self.shipping_deadline - today).days
        if days < 0:
            return {'days': abs(days), 'icon': '🔴', 'label': f'Прострочено {abs(days)}д', 'color': '#c62828'}
        if days == 0:
            return {'days': 0, 'icon': '🔴', 'label': 'Дедлайн сьогодні!', 'color': '#c62828'}
        if days <= 3:
            return {'days': days, 'icon': '⚠️', 'label': f'Залишилось {days}д', 'color': '#e65100'}
        if days <= 7:
            return {'days': days, 'icon': '⏰', 'label': f'Залишилось {days}д', 'color': '#e65100'}
        return {'days': days, 'icon': '✅', 'label': f'Залишилось {days}д', 'color': '#2e7d32'}

    def order_total(self):
        """Сума з рядків якщо є unit_price, інакше total_price замовлення."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        line_total = self.lines.aggregate(
            t=Sum(ExpressionWrapper(
                F('qty') * F('unit_price'),
                output_field=DecimalField(max_digits=18, decimal_places=2)
            ))
        )['t']
        return line_total or self.total_price or 0

    @property
    def crm_customer(self):
        """Returns linked CRM Customer (cached per instance)."""
        if not hasattr(self, '_crm_cust'):
            from crm.models import Customer
            self._crm_cust = None
            if self.customer_key:
                self._crm_cust = Customer.objects.filter(external_key=self.customer_key).first()
            if not self._crm_cust and self.email:
                self._crm_cust = Customer.objects.filter(email=self.email).first()
        return self._crm_cust


class SalesOrderLine(models.Model):
    order       = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    product     = models.ForeignKey(Product, on_delete=models.PROTECT)
    sku_raw     = models.CharField(max_length=128, blank=True, default="")
    qty         = models.DecimalField(max_digits=12, decimal_places=3)
    # ── Ціни ──
    unit_price  = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    total_price = models.DecimalField(max_digits=18, decimal_places=2,  null=True, blank=True)
    currency    = models.CharField(max_length=8, blank=True, default="USD", verbose_name="Валюта")

    def __str__(self):
        return f"{self.order} {self.qty} {self.product.sku}"

    def line_total(self):
        if self.total_price:
            return self.total_price
        if self.unit_price and self.qty:
            return float(self.unit_price) * float(self.qty)
        return 0

    def clean(self):
        if self.product and self.qty:
            if not self.product.is_fractional_unit():
                if self.qty != int(self.qty):
                    raise ValidationError({
                        'qty': f'Товар "{self.product.sku}" вимірюється в штуках. '
                               f'Кількість має бути цілим числом.'
                    })


class SalesSettings(models.Model):
    """Налаштування модуля продажів — синглтон (завжди pk=1)."""
    local_docs_path = models.CharField(
        max_length=500, blank=True, default='',
        verbose_name='Шлях для локального збереження',
        help_text='Основна директорія на ПК куди копіювати документи. '
                  r'Приклад: C:\Users\name\Documents\Orders',
    )
    local_save_enabled = models.BooleanField(
        default=False,
        verbose_name='Зберігати копії локально',
        help_text='Якщо ввімкнено і вказано шлях — документи автоматично '
                  'копіюються на ПК при завантаженні та генерації.',
    )
    auto_save_to_server = models.BooleanField(
        default=True,
        verbose_name='Автоматично зберігати PDF на сервер',
        help_text='При натисканні 💾 на Пакувальному листі / Proforma / Митній декларації '
                  'зберігати PDF на сервер (без зайвого підтвердження).',
    )
    auto_save_labels_to_server = models.BooleanField(
        default=True,
        verbose_name='Зберігати мітки перевізника в документи замовлення',
        help_text='При створенні UPS/DHL мітки — автоматично копіювати PDF етикетки '
                  'і митної декларації у папку документів замовлення '
                  '(media/orders/{source}/{order_number}/).',
    )

    class Meta:
        verbose_name = 'Налаштування'
        verbose_name_plural = 'Налаштування'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
