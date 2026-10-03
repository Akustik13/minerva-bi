"""
Інтернет-магазин: ціноутворення і керування асортиментом.

Галочка «у магазині» і базова ціна живуть у Product (shop_visible, shop_price);
тут — ступені цін за кількістю (як на DigiKey: 1 / 10 / 100 …) і налаштування.
"""
from decimal import Decimal

from django.core.validators import MinValueValidator
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
    """Налаштування цін магазину — синглтон (pk=1)."""

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


class ShopPriceTier(models.Model):
    """Ціна за штуку від певної кількості (1 шт. = базова ціна товару)."""
    product    = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="shop_tiers",
                                   verbose_name="Товар")
    min_qty    = models.PositiveIntegerField("Від кількості, шт.", validators=[MinValueValidator(2)])
    unit_price = models.DecimalField("Ціна за шт. (нетто)", max_digits=18, decimal_places=4,
                                     validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        verbose_name = "Ступінь ціни"
        verbose_name_plural = "Ступені цін"
        ordering = ["product", "min_qty"]
        constraints = [models.UniqueConstraint(fields=["product", "min_qty"], name="uniq_shop_tier_qty")]

    def __str__(self):
        return f"{self.product.sku}: від {self.min_qty} шт. — {self.unit_price}"


class ShopProduct(Product):
    """Проксі товару для розділу «Інтернет-магазин» (той самий запис, що й у складі)."""

    class Meta:
        proxy = True
        verbose_name = "Товар магазину"
        verbose_name_plural = "🏪 Товари магазину"
