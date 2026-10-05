from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.shortcuts import render

from .models import Shop, ShopListing, ShopPriceTier, ShopSettings
from .services import KEY_DEFAULT, keys_for_shop


@staff_member_required
def shop_help(request):
    """Довідка розділу «Інтернет-магазин» з живою статистикою."""
    from api.models import Webhook
    from sales.models import SalesOrder

    shops = list(Shop.objects.annotate(
        n_listings=Count("listings", distinct=True),
        n_visible=Count("listings", filter=Q(listings__is_visible=True), distinct=True),
    ).order_by("-is_default", "name"))
    by_source = {
        row["source"]: row for row in SalesOrder.objects
        .filter(source__in=[s.slug for s in shops])
        .values("source")
        .annotate(orders=Count("id", filter=~Q(document_type="QUOTE")),
                  quotes=Count("id", filter=Q(document_type="QUOTE")))
    }
    # Ключі, прив'язані до магазину (поле «Магазин» або «Джерело замовлень» = код магазину)
    for s in shops:
        s.n_keys = sum(1 for _, m in keys_for_shop(s) if m != KEY_DEFAULT)
        row = by_source.get(s.slug, {})
        s.n_orders = row.get("orders", 0)
        s.n_quotes = row.get("quotes", 0)

    context = admin.site.each_context(request)
    context.update({
        "title": "Довідка: інтернет-магазин",
        "shops": shops,
        "stats": {
            "listings": ShopListing.objects.count(),
            "tiers": ShopPriceTier.objects.count(),
            "webhooks": Webhook.objects.filter(is_active=True).count(),
        },
        "settings_obj": ShopSettings.objects.filter(pk=1).first(),
    })
    return render(request, "shop/help.html", context)
