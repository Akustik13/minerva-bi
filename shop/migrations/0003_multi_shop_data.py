"""Кілька магазинів (2/3): перенесення даних (окрема транзакція для PostgreSQL).

Створюється магазин за замовчуванням «Сайт sevskiy.de» (код webshop); товари з галочкою
«Показувати в інтернет-магазині», власною ціною магазину або ступенями стають його позиціями.
"""
from django.db import migrations, models


def forward(apps, schema_editor):
    Shop = apps.get_model("shop", "Shop")
    ShopListing = apps.get_model("shop", "ShopListing")
    ShopPriceTier = apps.get_model("shop", "ShopPriceTier")
    Product = apps.get_model("inventory", "Product")

    shop, _ = Shop.objects.get_or_create(
        slug="webshop", defaults={"name": "Сайт sevskiy.de", "is_default": True, "url": "https://www.sevskiy.de"})
    tier_products = set(ShopPriceTier.objects.values_list("product_id", flat=True))
    products = Product.objects.filter(
        models.Q(shop_visible=True) | models.Q(shop_price__isnull=False) | models.Q(pk__in=tier_products))
    for p in products:
        listing, _ = ShopListing.objects.get_or_create(
            shop=shop, product=p, defaults={"is_visible": p.shop_visible, "price": p.shop_price})
        ShopPriceTier.objects.filter(product_id=p.pk).update(listing=listing)
    ShopPriceTier.objects.filter(listing__isnull=True).delete()


def backward(apps, schema_editor):
    ShopPriceTier = apps.get_model("shop", "ShopPriceTier")
    for t in ShopPriceTier.objects.select_related("listing"):
        t.product_id = t.listing.product_id
        t.save(update_fields=["product"])


class Migration(migrations.Migration):

    dependencies = [("shop", "0002_multi_shop")]

    operations = [migrations.RunPython(forward, backward)]
