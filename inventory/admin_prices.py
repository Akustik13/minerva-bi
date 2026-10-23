"""Базові ціни в адмінці товарів: масовий редактор для вибраних товарів і застосування цін у магазинах
(блок «🏪 Інтернет-магазини» картки та низ редактора). Логіка цін — inventory/services/base_prices.py,
магазинів — shop/services.py (apply_base_to_shop)."""
import json
import re
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import Product
from .services.base_prices import normalize, set_base_prices


def editor_config() -> dict:
    """Курси, округлення, шаблон знижок, націнка — для редактора в картці й масового редактора."""
    try:
        from shop.models import Shop, ShopSettings
        st = ShopSettings.get()
        return {
            "fx_rates": {k.upper(): float(v) for k, v in (st.fx_rates or {}).items() if v},
            "home": getattr(Shop.objects.filter(is_default=True).first(), "currency", None) or "EUR",
            "rounding": st.rounding,
            "roundings": [[k, str(v)] for k, v in st.Rounding.choices],
            "template": [{"min_qty": int(r["min_qty"]), "discount": float(r["discount"])} for r in (st.price_breaks or [])],
            "markup": float(st.default_markup),
        }
    except Exception:
        return {}


def _shops():
    from shop.models import Shop
    return list(Shop.objects.filter(is_active=True).order_by("-is_default", "name"))


def _pct(value) -> str:
    """Decimal("95.50") → «95.5», Decimal("100.00") → «100»."""
    return format(Decimal(value).normalize(), "f")


def _price_text(listing) -> str:
    price = listing.effective_price
    if price is None:
        return "ціна за запитом"
    tiers = len(listing.tiers.all())
    return f"{price:.2f} {listing.shop.currency}" + (f" · ступенів {tiers}" if tiers else "")


# ── Картка товару: «🏪 Інтернет-магазини» ─────────────────────────────────────

def shop_prices_panel(product) -> str:
    """Таблиця магазинів: що зараз, звідки ціна, галочка «базові ціни → магазин» (спрацьовує при «Зберегти»)."""
    from shop.models import ShopListing
    if not product or not product.pk:
        return "Спершу збережіть товар."
    shops = _shops()
    if not shops:
        return "Магазинів ще немає — розділ «🏪 Інтернет-магазин → Магазини»."
    listings = {l.shop_id: l for l in product.shop_listings.select_related("shop").prefetch_related("tiers")}
    rows = []
    for shop in shops:
        l = listings.get(shop.pk)
        if l is None:
            rows.append(format_html(
                '<tr><td><b>{}</b></td><td style="opacity:.7">немає</td><td>—</td><td>—</td>'
                '<td style="white-space:nowrap"><label><input type="checkbox" name="shop_apply" value="{}"> додати й застосувати</label> '
                '× <input type="text" name="shop_factor_{}" value="100" size="4" style="width:52px"> % '
                '<label style="margin-left:6px"><input type="checkbox" name="shop_visible_{}" checked> показувати</label></td></tr>',
                shop.name, shop.pk, shop.pk, shop.pk))
            continue
        base = l.price_source == ShopListing.PRICE_BASE
        source = (f"базові × {_pct(l.price_factor)} % · оновлюється автоматично" if base else "вручну")
        rows.append(format_html(
            '<tr><td><a href="{}"><b>{}</b></a></td><td>{}</td><td>{}</td><td>{}</td>'
            '<td style="white-space:nowrap"><label><input type="checkbox" name="shop_apply" value="{}"> {}</label> '
            '× <input type="text" name="shop_factor_{}" value="{}" size="4" style="width:52px"> %</td></tr>',
            reverse("admin:shop_shoplisting_change", args=[l.pk]), shop.name,
            "показується" if l.is_visible else "сховано", _price_text(l), source,
            shop.pk, "змінити %" if base else "застосувати базові", shop.pk, _pct(l.price_factor)))
    return format_html(
        '<table class="inv-shop-prices" style="border-collapse:collapse;font-size:12px;min-width:640px">'
        '<thead><tr><th style="text-align:left">Магазин</th><th style="text-align:left">На сайті</th>'
        '<th style="text-align:left">Ціна зараз (нетто)</th><th style="text-align:left">Звідки ціна</th>'
        '<th style="text-align:left">Базові ціни → магазин</th></tr></thead><tbody>{}</tbody></table>'
        '<div class="help" style="margin-top:6px">Галочка спрацює при «Зберегти» — разом зі зміненими вище базовими '
        'цінами. Позиція з базовими цінами далі оновлюється сама; % — від базової ціни (100 = як базова, 95 = −5 %). '
        'Валюта магазину — за курсом із «Налаштувань цін». Усе інше по магазину — <a href="{}">«Асортимент і ціни»</a>.</div>',
        format_html_join("", "{}", ((r,) for r in rows)),
        reverse("admin:shop_shoplisting_changelist") + f"?q={product.sku}")


def _factor(raw) -> Decimal | None:
    raw = str(raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("% має бути числом") from None
    if not (Decimal("1") <= value <= Decimal("1000")):
        raise ValueError("% — від 1 до 1000")
    return value


def apply_shop_choices(request, product) -> None:
    """save_model картки: виконати позначені «Базові ціни → магазин»."""
    from shop.models import Shop
    from shop.services import APPLY_TEXT, apply_base_to_shop
    ids = [int(x) for x in request.POST.getlist("shop_apply") if str(x).isdigit()]
    for shop in Shop.objects.filter(pk__in=ids, is_active=True):
        try:
            factor = _factor(request.POST.get(f"shop_factor_{shop.pk}"))
        except ValueError as e:
            messages.error(request, f"«{shop.name}»: {e} — не застосовано.")
            continue
        result = apply_base_to_shop(product, shop, factor, add=True,
                                    visible=bool(request.POST.get(f"shop_visible_{shop.pk}")))
        level = messages.SUCCESS if result in ("ok", "added") else messages.WARNING
        messages.add_message(request, level, f"«{shop.name}»: {APPLY_TEXT[result]}.")


# ── Масовий редактор базових цін ──────────────────────────────────────────────

def _ids(raw) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", str(raw or ""))][:1000]


def editor_items(products) -> list[dict]:
    from shop.models import ShopListing
    shops = _shops()
    listings = {}
    for l in (ShopListing.objects.filter(product__in=products, shop__in=shops).select_related("shop")
              .prefetch_related("tiers")):
        listings[(l.product_id, l.shop_id)] = l
    items = []
    for p in products:
        state = {}
        for shop in shops:
            l = listings.get((p.pk, shop.pk))
            state[shop.pk] = None if l is None else {
                "visible": l.is_visible, "base": l.price_source == ShopListing.PRICE_BASE,
                "factor": _pct(l.price_factor), "price": _price_text(l)}
        items.append({"id": p.pk, "sku": p.sku, "name": (p.name or "")[:90],
                      "url": reverse("admin:inventory_product_change", args=[p.pk]),
                      "currency": p.base_price_currency or "EUR",
                      "purchase": float(p.purchase_price) if p.purchase_price else None,
                      "rows": [[r["min_qty"], r["unit_price"]] for r in normalize(p.base_prices)],
                      "shops": state})
    return items


def save_editor(products, data: dict, user) -> tuple[int, list[str], list[str]]:
    """Зберегти ціни з редактора і застосувати в позначених магазинах. → (змінено, помилки, підсумки магазинів)."""
    from shop.models import Shop
    from shop.services import APPLY_TEXT, apply_base_to_shop
    by_id = {p.pk: p for p in products}
    note = str(data.get("note") or "").strip()[:150]
    changed, errors = 0, []
    for item in data.get("products") or []:
        p = by_id.get(int(item.get("id") or 0)) if str(item.get("id") or "").isdigit() else None
        if p is None:
            continue
        currency = str(item.get("currency") or p.base_price_currency or "EUR").strip().upper()
        rows = normalize(item.get("rows") or [])
        if not re.fullmatch(r"[A-Z]{3}", currency):
            errors.append(f"{p.sku}: валюта «{currency}» — код з 3 літер")
        elif not rows:
            errors.append(f"{p.sku}: немає жодної ціни — не змінено")
        elif rows[0]["min_qty"] != 1:
            errors.append(f"{p.sku}: перший ступінь має бути від 1 шт. — не змінено")
        elif set_base_prices(p, rows, source="manual", user=user, currency=currency,
                             note="Масовий редактор" + (f" · {note}" if note else "")):
            changed += 1
    summaries = []
    for s in data.get("shops") or []:
        shop = Shop.objects.filter(pk=s.get("id"), is_active=True).first() if str(s.get("id", "")).isdigit() else None
        if shop is None:
            continue
        try:
            factor = _factor(s.get("factor"))
        except ValueError as e:
            errors.append(f"«{shop.name}»: {e}")
            continue
        counts = {}
        for p in products:   # p уже з новими базовими цінами (set_base_prices змінює об'єкт)
            r = apply_base_to_shop(p, shop, factor, add=bool(s.get("add")), visible=bool(s.get("visible", True)))
            counts[r] = counts.get(r, 0) + 1
        summaries.append(f"«{shop.name}»: " + ", ".join(f"{APPLY_TEXT[k]} — {v}" for k, v in counts.items()))
    return changed, errors, summaries


def editor_view(model_admin, request):
    """GET ?ids=1,2,3 — сторінка редактора; POST — зберегти (payload JSON з таблиці)."""
    if not model_admin.has_change_permission(request):
        raise PermissionDenied
    ids = _ids(request.GET.get("ids") or request.POST.get("ids"))
    products = list(Product.objects.filter(pk__in=ids).order_by("sku"))
    url = reverse("admin:inventory_product_base_prices") + "?ids=" + ",".join(str(p.pk) for p in products)
    if request.method == "POST":
        try:
            data = json.loads(request.POST.get("payload") or "{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            messages.error(request, "Не вдалося прочитати зміни — оновіть сторінку й спробуйте ще раз.")
            return redirect(url)
        changed, errors, summaries = save_editor(products, data, request.user)
        messages.success(request, f"Базові ціни змінено: {changed} товарів (записано в історію)." if changed
                         else "Базові ціни не змінювались.")
        for text in summaries:
            messages.info(request, text)
        for text in errors[:30]:
            messages.warning(request, text)
        return redirect(url)
    cfg = editor_config()
    cfg["shops"] = [{"id": s.pk, "name": s.name, "currency": s.currency} for s in _shops()]
    cfg["template_url"] = reverse("admin:inventory_product_base_prices_template")
    cfg["can_save_template"] = request.user.has_perm("shop.change_shopsettings")
    return render(request, "admin/inventory/product/base_prices_editor.html", {
        **model_admin.admin_site.each_context(request),
        "title": f"Базові ціни: {len(products)} товарів", "opts": Product._meta,
        "items": editor_items(products), "config": cfg, "ids": ",".join(str(p.pk) for p in products),
        "back_url": reverse("admin:inventory_product_changelist"),
    })


def template_view(model_admin, request):
    """POST {price_breaks: [{min_qty, discount}]} — шаблон ступенів з редактора → «Налаштування цін» (стандартний)."""
    from shop.models import ShopSettings
    from shop.services import parse_price_breaks
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Лише POST."}, status=405)
    if not request.user.has_perm("shop.change_shopsettings"):
        return JsonResponse({"ok": False, "error": "Немає права змінювати «Налаштування цін»."}, status=403)
    try:
        rows = parse_price_breaks(json.loads(request.body or b"{}").get("price_breaks"))
    except (ValueError, AttributeError) as e:
        return JsonResponse({"ok": False, "error": str(e) or "Неправильний шаблон."}, status=400)
    st = ShopSettings.get()
    st.price_breaks = rows
    st.save(update_fields=["price_breaks"])
    return JsonResponse({"ok": True, "price_breaks": rows})
