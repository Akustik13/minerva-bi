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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Магазин"
        verbose_name_plural = "🏬 Магазини"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            Shop.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


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


class ShopProduct(Product):
    """Проксі товару: каталог для додавання товарів у магазини."""

    class Meta:
        proxy = True
        verbose_name = "Товар каталогу"
        verbose_name_plural = "📦 Каталог → додати в магазин"
