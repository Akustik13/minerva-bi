"""
shipping/models.py — Модуль управління відправленнями
Підтримка: Jumingo, DHL, UPS, FedEx (розширюється через сервіси)
"""
from decimal import Decimal
from django.db import models
from django.utils import timezone


class Carrier(models.Model):
    """Перевізник / платформа доставки з API-налаштуваннями."""

    class CarrierType(models.TextChoices):
        JUMINGO = "jumingo", "Jumingo (агрегатор)"
        DHL     = "dhl",     "DHL"
        UPS     = "ups",     "UPS"
        FEDEX   = "fedex",   "FedEx"
        OTHER   = "other",   "Інше"

    name         = models.CharField("Назва", max_length=100)
    carrier_type = models.CharField("Тип", max_length=20,
                                    choices=CarrierType.choices,
                                    default=CarrierType.JUMINGO)
    is_active    = models.BooleanField("Активний", default=True)
    is_default   = models.BooleanField("За замовчуванням", default=False)

    # ── API credentials ──────────────────────────────────────────────────────
    api_key    = models.CharField("API ключ", max_length=500,
                                  blank=True, default="",
                                  help_text="Jumingo: X-AUTH-TOKEN | DHL: API Key | UPS/FedEx: Client ID")
    api_secret = models.CharField("API Secret / пароль", max_length=500,
                                  blank=True, default="",
                                  help_text="DHL: API Secret | UPS/FedEx: Client Secret | Jumingo: не використовується")
    api_url    = models.CharField("API URL / режим", max_length=300,
                                  blank=True, default="",
                                  help_text="DHL: введи «test» для sandbox. Для інших залишити порожнім.")
    track_api_key = models.CharField(
        "Tracking API Key", max_length=200, blank=True, default="",
        help_text="DHL Shipment Tracking – Unified API ключ (developer.dhl.com → My Apps). "
                  "Окремий від API ключа для тарифів.",
    )
    connection_uuid = models.CharField(
        "Connection UUID / Account №", max_length=100, blank=True, default="",
        help_text="Jumingo: UUID інтеграції | DHL: Account Number (9 цифр) | інші: не використовується",
    )

    # ── Дані відправника (звідси беруться при кожному відправленні) ──────────
    sender_name    = models.CharField("Ім'я відправника", max_length=200, blank=True, default="")
    sender_company = models.CharField("Компанія відправника", max_length=200, blank=True, default="")
    sender_street  = models.CharField("Вулиця, будинок", max_length=300, blank=True, default="")
    sender_city    = models.CharField("Місто", max_length=100, blank=True, default="")
    sender_zip     = models.CharField("Поштовий індекс", max_length=20, blank=True, default="")
    sender_country = models.CharField("Країна (ISO 2)", max_length=2, blank=True, default="DE",
                                      help_text="Двобуквений код: DE, UA, PL, US...")
    sender_phone   = models.CharField("Телефон відправника", max_length=50, blank=True, default="")
    sender_email   = models.EmailField("Email відправника", blank=True, default="")

    notes = models.TextField("Нотатки", blank=True, default="")

    class Meta:
        verbose_name        = "Перевізник"
        verbose_name_plural = "Перевізники"
        ordering            = ["-is_default", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_carrier_type_display()})"

    def save(self, *args, **kwargs):
        # Тільки один перевізник може бути за замовчуванням
        if self.is_default:
            Carrier.objects.exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class Shipment(models.Model):
    """Відправлення — пов'язане із замовленням SalesOrder."""

    class Status(models.TextChoices):
        DRAFT       = "draft",       "Чернетка"
        SUBMITTED   = "submitted",   "Передано перевізнику"
        LABEL_READY = "label_ready", "Етикетка готова"
        IN_TRANSIT  = "in_transit",  "В дорозі"
        DELIVERED   = "delivered",   "Доставлено"
        ERROR       = "error",       "Помилка"
        CANCELLED   = "cancelled",   "Скасовано"

    # ── Зв'язки ───────────────────────────────────────────────────────────────
    order   = models.ForeignKey(
        "sales.SalesOrder", on_delete=models.PROTECT,
        related_name="shipments", verbose_name="Замовлення"
    )
    carrier = models.ForeignKey(
        Carrier, on_delete=models.PROTECT,
        verbose_name="Перевізник"
    )
    status  = models.CharField("Статус", max_length=20,
                               choices=Status.choices, default=Status.DRAFT)

    # ── Дані отримувача (копіюються з замовлення при створенні) ───────────────
    recipient_name    = models.CharField("Контактна особа", max_length=255, blank=True, default="")
    recipient_company = models.CharField("Компанія", max_length=255, blank=True, default="")
    recipient_street  = models.CharField("Вулиця, будинок", max_length=300, blank=True, default="")
    recipient_city    = models.CharField("Місто", max_length=100, blank=True, default="")
    recipient_zip     = models.CharField("Поштовий індекс", max_length=20, blank=True, default="")
    recipient_state   = models.CharField("Штат / провінція", max_length=100, blank=True, default="",
                                         help_text="США/Канада: дволітерний код (CA, NY, TX). Інші країни: повна назва регіону.")
    recipient_country = models.CharField("Країна (ISO 2)", max_length=2, blank=True, default="",
                                         help_text="Двобуквений код: UA, PL, DE, US...")
    recipient_phone   = models.CharField("Телефон", max_length=50, blank=True, default="")
    recipient_email   = models.EmailField("Email", blank=True, default="")

    # ── Параметри посилки ─────────────────────────────────────────────────────
    weight_kg   = models.DecimalField("Вага (кг)", max_digits=8, decimal_places=3, default=1)
    length_cm   = models.DecimalField("Довжина (см)", max_digits=6, decimal_places=1,
                                      null=True, blank=True)
    width_cm    = models.DecimalField("Ширина (см)", max_digits=6, decimal_places=1,
                                      null=True, blank=True)
    height_cm   = models.DecimalField("Висота (см)", max_digits=6, decimal_places=1,
                                      null=True, blank=True)
    description      = models.CharField("Опис вмісту (макс. 35 символів)", max_length=300,
                                        blank=True, default="",
                                        help_text="Для митниці: напр. 'Electronic components'")
    EXPORT_REASON_CHOICES = [
        ("Commercial", "Commercial — продаж"),
        ("Gift",       "Gift — подарунок"),
        ("Personal",   "Personal — особисте"),
        ("Return",     "Return — повернення"),
        ("Claim",      "Claim — рекламація"),
    ]
    export_reason    = models.CharField(
        "Причина експорту", max_length=20,
        choices=EXPORT_REASON_CHOICES, default="Commercial",
        help_text="Для митної декларації CN23",
    )
    declared_value   = models.DecimalField("Задекларована вартість", max_digits=10,
                                           decimal_places=2, null=True, blank=True)
    declared_currency = models.CharField("Валюта", max_length=3, default="EUR")
    reference        = models.CharField("Референс/номер замовлення", max_length=100,
                                        blank=True, default="",
                                        help_text="Буде надруковано на етикетці")

    # ── Результат від перевізника ─────────────────────────────────────────────
    carrier_shipment_id = models.CharField("ID відправлення (перевізник)",
                                           max_length=200, blank=True, default="")
    tracking_number     = models.CharField("Трекінг номер", max_length=200,
                                           blank=True, default="")
    label_url           = models.URLField("URL етикетки (PDF)", max_length=500,
                                          blank=True, default="")
    carrier_price       = models.DecimalField("Вартість доставки", max_digits=8,
                                              decimal_places=2, null=True, blank=True)
    carrier_currency    = models.CharField("Валюта вартості", max_length=3,
                                           blank=True, default="EUR")
    carrier_service     = models.CharField("Послуга перевізника", max_length=200,
                                           blank=True, default="",
                                           help_text="Наприклад: DHL Express, UPS Standard")
    selected_tariff_id  = models.CharField("ID тарифу Jumingo", max_length=50,
                                           blank=True, default="")
    jumingo_order_number = models.CharField("Номер замовлення Jumingo", max_length=50,
                                            blank=True, default="")
    customs_articles    = models.JSONField("Митна декларація (артикули)", null=True, blank=True,
                                           help_text="Заповнюється автоматично при створенні відправлення")

    # ── Розширені поля статусу від перевізника ────────────────────────────────
    carrier_status_label = models.CharField(
        "Статус перевізника", max_length=200, blank=True, default="",
        help_text="Текстовий статус від API перевізника (напр. «Unterwegs»)"
    )
    carrier_delayed = models.BooleanField(
        "Затримка доставки", default=False,
        help_text="Перевізник підтвердив затримку посилки"
    )
    eta_from = models.DateField(
        "Очікувана доставка від", null=True, blank=True,
        help_text="Початок вікна очікуваної доставки"
    )
    eta_to = models.DateField(
        "Очікувана доставка до", null=True, blank=True,
        help_text="Кінець вікна очікуваної доставки"
    )

    # ── Технічні поля ─────────────────────────────────────────────────────────
    raw_request  = models.JSONField("Запит (JSON)", null=True, blank=True)
    raw_response = models.JSONField("Відповідь (JSON)", null=True, blank=True)
    error_message = models.TextField("Повідомлення про помилку", blank=True, default="")

    created_at   = models.DateTimeField("Створено", auto_now_add=True)
    submitted_at = models.DateTimeField("Відправлено", null=True, blank=True)
    created_by   = models.ForeignKey("auth.User", on_delete=models.SET_NULL,
                                     null=True, blank=True, verbose_name="Автор")

    class Meta:
        verbose_name        = "Відправлення"
        verbose_name_plural = "Відправлення"
        ordering            = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} {self.carrier} → {self.recipient_name} [{self.get_status_display()}]"

    def copy_from_order(self):
        """Заповнює дані отримувача з пов'язаного замовлення."""
        o = self.order
        contact = getattr(o, 'contact_name', '') or ''
        client  = o.client or ''
        if contact:
            # B2B: company = client, name = contact person
            self.recipient_company = client
            self.recipient_name    = contact
        else:
            # B2C: name = client (person or company)
            self.recipient_name    = client
            self.recipient_company = ""
        self.recipient_phone = o.phone or ""
        self.recipient_email = o.email or ""
        self.reference       = o.order_number or ""

        # Пріоритет: структуровані поля addr_*
        if o.addr_street or o.addr_city:
            self.recipient_street  = o.addr_street  or ""
            self.recipient_city    = o.addr_city    or ""
            self.recipient_zip     = o.addr_zip     or ""
            self.recipient_state   = o.addr_state   or ""
            self.recipient_country = o.addr_country or ""
        else:
            # Fallback: парсимо legacy TextField
            addr = (o.shipping_address or "").strip()
            lines = [l.strip() for l in addr.splitlines() if l.strip()]
            if lines:
                self.recipient_street = lines[0]
                if len(lines) >= 2:
                    last = lines[-1]
                    parts = last.split(" ", 1)
                    token = parts[0].replace("-", "").replace(" ", "")
                    if token.isalnum() and len(token) <= 7:
                        self.recipient_zip  = parts[0]
                        self.recipient_city = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        self.recipient_city = last
            # Країна з регіону
            from config.country_utils import normalize_to_iso2
            self.recipient_country = normalize_to_iso2(o.shipping_region or "")


# ─────────────────────────────────────────────────────────────────────────────
# PACKAGING MATERIALS
# ─────────────────────────────────────────────────────────────────────────────

class PackagingMaterial(models.Model):
    """Пакувальний матеріал — коробка, конверт, тощо."""

    class BoxType(models.TextChoices):
        BOX      = 'box',      '📦 Коробка'
        ENVELOPE = 'envelope', '✉️ Конверт'
        TUBE     = 'tube',     '🗄️ Тубус'
        BAG      = 'bag',      '🛍️ Пакет'
        CUSTOM   = 'custom',   '⚙️ Інше'

    name      = models.CharField('Назва', max_length=255, blank=True, default='',
                                 help_text='Залиш порожнім — заповниться автоматично з розмірів')
    box_type  = models.CharField('Тип', max_length=20,
                                 choices=BoxType.choices, default=BoxType.BOX)
    length_cm = models.DecimalField('Довжина (см)', max_digits=6, decimal_places=1)
    width_cm  = models.DecimalField('Ширина (см)',  max_digits=6, decimal_places=1)
    height_cm = models.DecimalField('Висота (см)',  max_digits=6, decimal_places=1)

    tare_weight_kg = models.DecimalField('Вага порожньої (кг)', max_digits=6, decimal_places=3,
                                         default=0, help_text='Вага самої коробки без вмісту')
    max_weight_kg  = models.DecimalField('Макс. вага вмісту (кг)', max_digits=6, decimal_places=3,
                                         null=True, blank=True,
                                         help_text='Максимально допустима вага товарів')
    cost   = models.DecimalField('Вартість за шт', max_digits=8, decimal_places=2,
                                 null=True, blank=True)
    notes  = models.TextField('Нотатки', blank=True, default='')
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name        = 'Пакувальний матеріал'
        verbose_name_plural = 'Пакувальні матеріали'
        ordering            = ['box_type', 'length_cm', 'width_cm']

    def _dim_str(self):
        return f'{self.length_cm}×{self.width_cm}×{self.height_cm} см'

    def save(self, *args, **kwargs):
        if not self.name and self.length_cm and self.width_cm and self.height_cm:
            self.name = self._dim_str()
        super().save(*args, **kwargs)

    @property
    def volume_cm3(self):
        return round(float(self.length_cm) * float(self.width_cm) * float(self.height_cm), 1)

    def __str__(self):
        return self.name or self._dim_str()


class ProductPackaging(models.Model):
    """Рекомендована упаковка для конкретного товару."""

    product   = models.ForeignKey(
        'inventory.Product', on_delete=models.CASCADE,
        related_name='packaging_recommendations', verbose_name='Товар',
    )
    packaging = models.ForeignKey(
        PackagingMaterial, on_delete=models.CASCADE,
        verbose_name='Упаковка',
    )
    qty_per_box        = models.PositiveSmallIntegerField(
        'Товарів в коробку', default=1,
        help_text='Скільки одиниць товару вміщується в одну коробку',
    )
    estimated_weight_g = models.PositiveIntegerField(
        'Орієнт. вага посилки (г)', null=True, blank=True,
        help_text='Якщо порожньо — розраховується автоматично з ваги товару + коробки',
    )
    is_default = models.BooleanField('Рекомендована', default=True,
                                     help_text='Основна рекомендація для цього товару')
    notes      = models.TextField('Нотатки', blank=True, default='')

    class Meta:
        verbose_name        = 'Рекомендована упаковка'
        verbose_name_plural = 'Рекомендовані упаковки'
        ordering            = ['-is_default']

    def save(self, *args, **kwargs):
        if not self.estimated_weight_g:
            nw = getattr(self.product, 'net_weight_g', None)
            if nw:
                self.estimated_weight_g = (
                    nw * self.qty_per_box + int(self.packaging.tare_weight_kg * 1000)
                )
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.product.sku} → {self.packaging}'


class OrderPackaging(models.Model):
    """Фактична упаковка конкретного замовлення — для статистики."""

    order     = models.ForeignKey(
        'sales.SalesOrder', on_delete=models.CASCADE,
        related_name='packaging_used', verbose_name='Замовлення',
    )
    packaging = models.ForeignKey(
        PackagingMaterial, on_delete=models.PROTECT,
        verbose_name='Упаковка',
    )
    qty_boxes       = models.PositiveSmallIntegerField('Кількість коробок', default=1)
    actual_weight_g = models.PositiveIntegerField(
        'Фактична вага (г)', null=True, blank=True,
        help_text='Фактична вага готової посилки',
    )
    notes      = models.TextField('Нотатки', blank=True, default='')
    created_at = models.DateTimeField('Зафіксовано', auto_now_add=True)

    class Meta:
        verbose_name        = 'Упаковка замовлення'
        verbose_name_plural = 'Упаковки замовлень'
        ordering            = ['-created_at']

    def __str__(self):
        return f'{self.order} → {self.packaging} ×{self.qty_boxes}'


class ShippingSettings(models.Model):
    """Глобальні налаштування доставки — singleton (pk=1)."""

    auto_tracking_enabled = models.BooleanField(
        "Автоматичне оновлення трекінгу", default=False,
        help_text="Вмикає автоматичне опитування API перевізників для всіх активних відправлень.",
    )
    tracking_interval_minutes = models.PositiveSmallIntegerField(
        "Інтервал оновлення (хвилини)", default=30,
        help_text="Як часто оновлювати трекінг. Cron повинен запускати команду частіше цього інтервалу.",
    )
    last_tracking_run = models.DateTimeField(
        "Останній запуск", null=True, blank=True,
        help_text="Заповнюється автоматично після кожного успішного запуску.",
    )

    class Meta:
        verbose_name        = "Налаштування доставки"
        verbose_name_plural = "Налаштування доставки"

    def __str__(self):
        return "Налаштування доставки"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
