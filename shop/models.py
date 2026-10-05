"""
Інтернет-магазини: кілька магазинів (сайт, маркетплейс …), у кожного свій асортимент і ціни.

  Shop         — магазин; slug = джерело замовлень (SalesOrder.source); ключ API прив'язується до магазину
  ShopListing  — товар у конкретному магазині: видимість + ціна (порожньо = «Ціна продажу» товару)
  ShopPriceTier— ступінь ціни позиції за кількістю (як на DigiKey: від 10 / 100 … шт.)
  ShopSettings — шаблон ступенів, націнка, округлення (спільні для всіх магазинів)
"""
from decimal import Decimal

from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from inventory.models import Product

DEFAULT_PRICE_BREAKS = [
    {"min_qty": 10, "discount": 5},
    {"min_qty": 25, "discount": 8},
    {"min_qty": 100, "discount": 12},
    {"min_qty": 250, "discount": 15},
    {"min_qty": 500, "discount": 18},
    {"min_qty": 1000, "discount": 22},
]


class ShopSettings(models.Model):
    """Налаштування цін — синглтон (pk=1)."""

    class Rounding(models.TextChoices):
        NONE   = "none", "Без округлення"
        CENT   = "0.01", "До 0,01"
        FIVE   = "0.05", "До 0,05"
        TEN    = "0.10", "До 0,10"
        HALF   = "0.50", "До 0,50"
        WHOLE  = "1.00", "До 1,00"
        NINETY = "x.90", "Закінчення ,90"
        NINE9  = "x.99", "Закінчення ,99"

    price_breaks = models.JSONField(
        "Шаблон ступенів цін", default=list, blank=True,
        help_text='Знижка від базової ціни (1 шт.) за кількістю, напр. '
                  '[{"min_qty": 10, "discount": 5}, {"min_qty": 100, "discount": 12}]',
    )
    default_markup = models.DecimalField(
        "Націнка за замовчуванням, %", max_digits=7, decimal_places=2, default=Decimal("100"),
        help_text="Для дії «Ціна = закупівля + націнка». 100 % = закупівельна ціна × 2.",
    )
    rounding = models.CharField("Округлення цін", max_length=8, choices=Rounding.choices,
                                default=Rounding.CENT)

    class Meta:
        verbose_name = "Налаштування цін магазину"
        verbose_name_plural = "⚙️ Налаштування цін"

    def __str__(self):
        return "Налаштування цін магазину"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        if created and not obj.price_breaks:
            obj.price_breaks = DEFAULT_PRICE_BREAKS
            obj.save(update_fields=["price_breaks"])
        return obj


class Shop(models.Model):
    name = models.CharField("Назва", max_length=100)
    slug = models.CharField(
        "Код (джерело замовлень)", max_length=32, unique=True,
        validators=[RegexValidator(r"^[a-z0-9][a-z0-9_-]*$", "Лише латиниця в нижньому регістрі, цифри, - і _")],
        help_text="Записується в замовлення як «Джерело» (напр. webshop) — за ним фільтруються продажі.",
    )
    currency = models.CharField("Валюта", max_length=3, default="EUR")
    is_active = models.BooleanField("Активний", default=True)
    is_default = models.BooleanField(
        "За замовчуванням", default=False,
        help_text="Для ключів API, не прив'язаних до магазину.",
    )
    url = models.URLField("Адреса сайту", blank=True, default="")
    notes = models.TextField("Нотатки", blank=True, default="")
    free_shipping_enabled = models.BooleanField(
        "Безкоштовна доставка", default=False,
        help_text="Доставка безкоштовна, коли сума товарів (нетто) досягає порогу. Діє в регіонах, де це дозволено.",
    )
    free_shipping_threshold = models.DecimalField(
        "Безкоштовно від (нетто)", max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Магазин"
        verbose_name_plural = "🏬 Магазини"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.free_shipping_enabled and self.free_shipping_threshold is None:
            raise ValidationError({"free_shipping_threshold": "Вкажіть поріг безкоштовної доставки."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            Shop.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)
        # Код магазину — джерело замовлень: має бути в довіднику джерел (фільтри й назви в «Продажах»)
        from sales.models import SalesSource
        SalesSource.objects.get_or_create(slug=self.slug, defaults={"name": self.name})


class ShopListing(models.Model):
    """Товар у конкретному магазині."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="listings", verbose_name="Магазин")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="shop_listings", verbose_name="Товар")
    is_visible = models.BooleanField("Показувати", default=True)
    price = models.DecimalField(
        "Ціна (нетто, 1 шт.)", max_digits=18, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Порожньо — «Ціна продажу» товару; без ціни на сайті «Ціна за запитом».",
    )
    PRICE_MANUAL, PRICE_DIGIKEY = "manual", "digikey"
    price_source = models.CharField(
        "Джерело ціни", max_length=10, default=PRICE_MANUAL,
        choices=[(PRICE_MANUAL, "Вручну"), (PRICE_DIGIKEY, "DigiKey (автоматично)")],
        help_text="DigiKey — ціна і ступені беруться з цін офера на DigiKey і оновлюються разом з ними.",
    )
    price_factor = models.DecimalField(
        "% від ціни DigiKey", max_digits=7, decimal_places=2, default=Decimal("100"),
        validators=[MinValueValidator(Decimal("1"))],
        help_text="100 = як на DigiKey, 95 = на 5 % дешевше.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиція магазину"
        verbose_name_plural = "💶 Асортимент і ціни"
        ordering = ["shop", "product__sku"]
        constraints = [models.UniqueConstraint(fields=["shop", "product"], name="uniq_shop_listing")]

    def __str__(self):
        return f"{self.shop.slug}: {self.product.sku}"

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.product.sale_price


class ShopPriceTier(models.Model):
    """Ціна за штуку від певної кількості (1 шт. = ціна позиції)."""
    listing    = models.ForeignKey(ShopListing, on_delete=models.CASCADE, related_name="tiers",
                                   verbose_name="Позиція")
    min_qty    = models.PositiveIntegerField("Від кількості, шт.", validators=[MinValueValidator(2)])
    unit_price = models.DecimalField("Ціна за шт. (нетто)", max_digits=18, decimal_places=4,
                                     validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        verbose_name = "Ступінь ціни"
        verbose_name_plural = "Ступені цін"
        ordering = ["listing", "min_qty"]
        constraints = [models.UniqueConstraint(fields=["listing", "min_qty"], name="uniq_listing_tier_qty")]

    def __str__(self):
        return f"{self.listing}: від {self.min_qty} шт. — {self.unit_price}"


# Країни ЄС (ISO 3166-1 alpha-2) — розгортання токена «EU» у регіонах доставки
EU_COUNTRIES = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
                "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"]
REST_OF_WORLD = "*"


class ShippingZone(models.Model):
    """Регіон доставки магазину: країни, ціна доставки (нетто), участь у безкоштовній доставці."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="shipping_zones", verbose_name="Магазин")
    name = models.CharField("Регіон", max_length=80)
    countries = models.JSONField(
        "Країни", default=list,
        help_text="Коди ISO через кому: DE, AT, CH. «EU» — усі країни ЄС, «*» — решта світу.",
    )
    price = models.DecimalField("Доставка (нетто)", max_digits=10, decimal_places=2, default=Decimal("0"),
                                validators=[MinValueValidator(Decimal("0"))])
    free_shipping = models.BooleanField("Діє безкоштовна доставка", default=True,
                                        help_text="Якщо в магазині увімкнено безкоштовну доставку від порогу.")
    is_active = models.BooleanField("Активний", default=True)
    sort_order = models.PositiveSmallIntegerField("Порядок", default=0)
    dk_code = models.CharField("Код DigiKey", max_length=40, blank=True, default="",
                               help_text="Заповнюється при імпорті регіонів з DigiKey.")

    class Meta:
        verbose_name = "Регіон доставки"
        verbose_name_plural = "🚚 Регіони доставки"
        ordering = ["shop", "sort_order", "name"]

    def __str__(self):
        return f"{self.shop.slug}: {self.name}"

    @property
    def is_rest_of_world(self) -> bool:
        return REST_OF_WORLD in (self.countries or [])

    def clean(self):
        # Дублікати країн між регіонами перевіряє формсет в адмінці (ShippingZoneFormSet) —
        # так можна перенести країну з одного регіону в інший одним збереженням.
        from django.core.exceptions import ValidationError
        if not self.countries:
            raise ValidationError({"countries": "Вкажіть хоча б одну країну."})


class ShopProduct(Product):
    """Проксі товару: каталог для додавання товарів у магазини."""

    class Meta:
        proxy = True
        verbose_name = "Товар каталогу"
        verbose_name_plural = "📦 Каталог → додати в магазин"
