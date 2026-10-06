"""Одноразово: ціни офера DigiKey → базові ціни товару (якщо базових ще немає) + запис в історію."""
from decimal import Decimal, InvalidOperation

from django.db import migrations
from django.utils import timezone


def _normalize(rows):
    out = {}
    for r in rows or []:
        try:
            qty = int(r.get("min_qty", r.get("qty")))
            price = Decimal(str(r.get("unit_price", r.get("price")))).quantize(Decimal("0.0001"))
        except (TypeError, ValueError, InvalidOperation, AttributeError):
            continue
        if qty >= 1 and price > 0:
            out[qty] = price
    return [{"min_qty": q, "unit_price": format(p.normalize(), "f")} for q, p in sorted(out.items())]


def forwards(apps, schema_editor):
    Listing = apps.get_model("bots", "DigiKeyListing")
    History = apps.get_model("inventory", "ProductPriceHistory")
    Config = apps.get_model("bots", "DigiKeyConfig")
    cfg = Config.objects.filter(pk=1).first()
    currency = (getattr(cfg, "locale_currency", "") or "USD").upper()[:3]
    now = timezone.now()
    for listing in Listing.objects.select_related("product").exclude(product__isnull=True):
        product = listing.product
        rows = _normalize(listing.dk_prices)
        if not rows or product.base_prices:
            continue
        parts = ["DigiKey"]
        if listing.dk_offer_id:
            parts.append(f"офер {listing.dk_offer_id}")
        if listing.last_synced_at:
            parts.append("синхр. " + timezone.localtime(listing.last_synced_at).strftime("%d.%m.%Y %H:%M"))
        label = " · ".join(parts)
        product.base_prices = rows
        product.base_price_currency = currency
        product.base_prices_source = label[:200]
        product.base_prices_updated_at = now
        product.base_prices_updated_by = "система"
        product.save(update_fields=["base_prices", "base_price_currency", "base_prices_source",
                                    "base_prices_updated_at", "base_prices_updated_by"])
        History.objects.create(product=product, user_label="система", source="migration", currency=currency,
                               old_prices=[], new_prices=rows,
                               note=f"Перенесено з цін DigiKey при оновленні ({label})"[:255])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0037_base_prices"),
        ("bots", "0035_digikeyconfig_auto_invoice_eu"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
