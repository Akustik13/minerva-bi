from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator


class ProductCategory(models.Model):
    """Словник категорій товарів — редагується з адмін-інтерфейсу."""
    slug  = models.CharField("Код (slug)", max_length=64, unique=True,
                             help_text="Латиниця, без пробілів. Зберігається в товарах.")
    name  = models.CharField("Назва", max_length=128)
    color = models.CharField("Колір бейджу", max_length=16, default="#607d8b",
                             help_text="HEX, напр. #e91e63")
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    # ── Митне оформлення (CN23) ────────────────────────────────────────────────
    customs_hs_code = models.CharField(
        "HS-Code (категорія)", max_length=20, blank=True, default="",
        help_text="Загальний HS-код для категорії — підставляється якщо у товару не заданий власний",
    )
    customs_description_de = models.CharField(
        "Опис товару (DE/EN)", max_length=255, blank=True, default="",
        help_text="Наприклад: Antennen, Kabel, Elektronische Bauteile. "
                  "Друкується в CN23 → Description of Contents",
    )
    customs_country_of_origin = models.CharField(
        "Країна походження (ISO 2)", max_length=2, blank=True, default="DE",
        help_text="Дефолт для товарів категорії без заданої країни. ISO-2: DE, UA, CN, US…",
    )

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Категорія товару"
        verbose_name_plural = "Категорії товарів"

    def __str__(self):
        return self.name


class Product(models.Model):
    """Single SKU entry.

    - Stock is NOT stored here (derived from InventoryTransaction)
    - For assemblies we keep a simple BOM (ProductComponent)
    """

    class Kind(models.TextChoices):
        FINISHED = "finished", "Finished (sellable)"
        COMPONENT = "component", "Component (used for assembly)"

    class BomType(models.TextChoices):
        NONE = "none", "No BOM"
        KEY = "key", "Key components (simple BOM)"

    class UnitType(models.TextChoices):
        """Визначає тип одиниці виміру товару"""
        PIECE = "piece", "Штуки (цілі числа)"
        METER = "meter", "Метри (дробні числа)"
        KILOGRAM = "kilogram", "Кілограми (дробні числа)"
        LITER = "liter", "Літри (дробні числа)"
        SET = "set", "Комплекти (цілі числа)"

    sku = models.CharField(max_length=255, unique=True)
    sku_short = models.CharField(max_length=255, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")
    name_export = models.CharField(
        "Назва (EN/DE) для документів", max_length=255, blank=True, default="",
        help_text="Англійська або німецька назва — друкується у Packing List, Proforma та CN23. "
                  "Якщо порожньо — використовується основна назва.",
    )
    category = models.CharField("Категорія", max_length=64, default="other",
                               help_text="Оберіть з довідника або введіть slug вручну")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.FINISHED)
    bom_type = models.CharField(max_length=20, choices=BomType.choices, default=BomType.NONE)
    
    # НОВЕ ПОЛЕ: тип одиниці виміру
    unit_type = models.CharField(
        max_length=20, 
        choices=UnitType.choices, 
        default=UnitType.PIECE,
        help_text="Визначає чи товар вимірюється в цілих числах (штуки) чи дробних (метри, кг)"
    )
    
    manufacturer   = models.CharField("Виробник", max_length=255, blank=True, default="")
    purchase_price = models.DecimalField("Ціна закупки", max_digits=18, decimal_places=4,
                                         null=True, blank=True)
    sale_price     = models.DecimalField("Ціна продажу", max_digits=18, decimal_places=4,
                                         null=True, blank=True)
    reorder_point  = models.PositiveIntegerField("Точка дозамовлення", default=0,
                                                 help_text="При залишку нижче цього значення — дозамовити")
    lead_time_days = models.PositiveSmallIntegerField("Термін поставки (дні)", null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    # ── Митне оформлення ─────────────────────────────────────────────────────
    hs_code           = models.CharField("HS-код (митний)", max_length=20, blank=True, default="")
    country_of_origin = models.CharField("Країна виробника", max_length=2, blank=True, default="")
    net_weight_g      = models.DecimalField("Вага нетто (г/шт)", max_digits=12, decimal_places=4,
                                           null=True, blank=True)

    # ── Медіа та документи ────────────────────────────────────────────────────
    datasheet_url  = models.URLField("Посилання на Datasheet", blank=True, default="")
    datasheet_file = models.FileField(
        "Datasheet (PDF файл)", upload_to="products/datasheets/",
        null=True, blank=True,
        help_text="Завантажте PDF з ПК. Якщо заповнено — має пріоритет над посиланням.",
    )
    image_url     = models.URLField("Зображення (URL)", blank=True, default="")
    image         = models.ImageField("Зображення (файл)", upload_to="products/images/",
                                      null=True, blank=True)

    def __str__(self) -> str:
        return self.sku

    @property
    def image_display_url(self) -> str:
        """Returns best available image URL without raising ValueError on empty ImageField."""
        if self.image and self.image.name:
            try:
                return self.image.url
            except Exception:
                pass
        return self.image_url or ""

    @property
    def datasheet_display_url(self) -> str:
        """Returns uploaded datasheet URL if available, else datasheet_url."""
        if self.datasheet_file and self.datasheet_file.name:
            try:
                return self.datasheet_file.url
            except Exception:
                pass
        return self.datasheet_url or ""

    def is_fractional_unit(self) -> bool:
        """Повертає True якщо товар може мати дробну кількість"""
        return self.unit_type in [
            self.UnitType.METER,
            self.UnitType.KILOGRAM,
            self.UnitType.LITER,
        ]

    def get_decimal_places(self) -> int:
        """Повертає кількість десяткових знаків для цього типу товару"""
        if self.is_fractional_unit():
            return 3
        return 0


class ProductAlias(models.Model):
    alias = models.CharField(max_length=255, unique=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)

    def __str__(self) -> str:
        return self.alias


class Location(models.Model):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        return self.code


class InventoryTransaction(models.Model):
    class TxType(models.TextChoices):
        INCOMING   = "Incoming",   "Incoming"
        OUTGOING   = "Outgoing",   "Outgoing"
        ADJUSTMENT = "Adjustment", "Adjustment"
        RESERVED   = "Reserved",   "Резерв"

    tx_type = models.CharField(max_length=20, choices=TxType.choices)
    qty = models.DecimalField(max_digits=18, decimal_places=3)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    ref_doc = models.CharField(max_length=255, blank=True, default="")
    external_key = models.CharField(max_length=255, unique=True)
    tx_date = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    performed_by = models.ForeignKey(
        "auth.User",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name="Виконавець",
        related_name="inventory_transactions",
    )

    class Meta:
        indexes = [
            models.Index(fields=["product", "location"]),
            models.Index(fields=["tx_type"]),
            models.Index(fields=["tx_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.tx_type} {self.qty} {self.product} ({self.location})"

    def clean(self):
        from django.core.exceptions import ValidationError
        
        if self.product and self.qty:
            if not self.product.is_fractional_unit():
                if self.qty != int(self.qty):
                    raise ValidationError({
                        'qty': f'Товар "{self.product.sku}" вимірюється в штуках. '
                               f'Кількість має бути цілим числом.'
                    })


class ProductComponent(models.Model):
    parent = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="components")
    component = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="used_in")
    qty_per = models.DecimalField(
        max_digits=18, 
        decimal_places=3, 
        default=Decimal("1.000"),
        validators=[MinValueValidator(Decimal('0.001'))],
        help_text="Кількість компонента на одиницю готового виробу"
    )
    optional = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = (("parent", "component"),)

    def __str__(self) -> str:
        return f"{self.parent.sku} needs {self.qty_per}x {self.component.sku}"


class Supplier(models.Model):
    name           = models.CharField("Назва", max_length=255, unique=True)
    contact_person = models.CharField("Контактна особа", max_length=255, blank=True, default="")
    email          = models.EmailField("Email", blank=True, default="")
    phone          = models.CharField("Телефон", max_length=50, blank=True, default="")
    website        = models.URLField("Веб-сайт", blank=True, default="")
    payment_terms  = models.CharField("Умови оплати", max_length=100, blank=True, default="",
                                      help_text="напр. Net 30, Prepayment, 50/50")
    currency       = models.CharField("Валюта", max_length=3, blank=True, default="EUR")
    notes          = models.TextField("Нотатки", blank=True, default="")
    addr_street    = models.CharField("Вулиця, будинок", max_length=300, blank=True, default="")
    addr_city      = models.CharField("Місто", max_length=100, blank=True, default="")
    addr_zip       = models.CharField("Поштовий індекс", max_length=20, blank=True, default="")
    addr_country   = models.CharField("Країна (ISO 2)", max_length=2, blank=True, default="")

    def __str__(self) -> str:
        return self.name


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ORDERED = "ordered", "Ordered"
        PARTIAL = "partial", "Partial"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    code = models.CharField(max_length=32, unique=True, null=True, blank=True)
    order_date = models.DateField(default=timezone.now)
    expected_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)
    shipping_service = models.CharField(max_length=255, blank=True, default="")
    tracking_number = models.CharField(max_length=255, blank=True, default="")
    total_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True, default="EUR")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if (self.code is None or self.code == "") and self.pk:
            self.code = f"PO-{self.pk}"
            super().save(update_fields=["code"])

    def __str__(self) -> str:
        return self.code or f"PO-{self.pk or '?'}"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True, default="")
    qty_ordered = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0"))
    qty_received = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0"))
    unit_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True, default="EUR")
    notes = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        p = self.product.sku if self.product_id else (self.description or "line")
        return f"{self.purchase_order}: {p}"

    def clean(self):
        from django.core.exceptions import ValidationError
        
        if self.product:
            if self.qty_ordered and not self.product.is_fractional_unit():
                if self.qty_ordered != int(self.qty_ordered):
                    raise ValidationError({
                        'qty_ordered': f'Товар "{self.product.sku}" вимірюється в штуках. '
                                      f'Замовлена кількість має бути цілим числом.'
                    })
            
            if self.qty_received and not self.product.is_fractional_unit():
                if self.qty_received != int(self.qty_received):
                    raise ValidationError({
                        'qty_received': f'Товар "{self.product.sku}" вимірюється в штуках. '
                                       f'Отримана кількість має бути цілим числом.'
                    })


class InventorySettings(models.Model):
    """Налаштування модуля складу — синглтон (завжди pk=1)."""

    class DeductOn(models.TextChoices):
        CREATION  = "creation",  "При створенні замовлення"
        SHIPPED   = "shipped",   "При зміні статусу на «Відправлено»"
        DELIVERED = "delivered", "При зміні статусу на «Доставлено»"

    deduct_on = models.CharField(
        "Списувати товар зі складу",
        max_length=16,
        choices=DeductOn.choices,
        default=DeductOn.CREATION,
        help_text="Коли автоматично зменшувати залишок при продажах.",
    )
    add_on_po_receive = models.BooleanField(
        "Додавати при надходженні закупівлі",
        default=True,
        help_text="Автоматично збільшувати залишок при зміні qty_received у замовленні на закупівлю.",
    )
    default_location = models.CharField(
        "Локація за замовчуванням",
        max_length=50,
        default="MAIN",
        help_text="Код складської локації для нових транзакцій (наприклад: MAIN, WAREHOUSE-A).",
    )
    allow_negative_stock = models.BooleanField(
        "Дозволити від'ємний залишок",
        default=True,
        help_text="Дозволити відвантаження навіть якщо залишок менший за 0.",
    )
    low_stock_alert_enabled = models.BooleanField(
        "Попередження про низький залишок",
        default=True,
        help_text="Показувати попередження в інтерфейсі для товарів нижче точки дозамовлення.",
    )
    use_reservation = models.BooleanField(
        "Бронювати замовлення (не списувати одразу)",
        default=False,
        help_text=(
            "При надходженні замовлення — створювати резерв (🔒 Резерв) замість негайного списання. "
            "Товар залишається на складі, але позначається як зарезервований. "
            "При переведенні замовлення у «Відправлено» — резерв автоматично конвертується у фактичне списання."
        ),
    )

    class Meta:
        verbose_name = "Налаштування складу"
        verbose_name_plural = "⚙️ Налаштування складу"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
