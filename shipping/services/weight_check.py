"""
shipping/services/weight_check.py — «⚖️ Вага посилки»: порожня коробка (тара) + товар (вага нетто × к-сть).

Лише читання бази для сторінки нового відправлення. Товар без ваги — «🔎 Знайти вагу в даташиті»
(inventory/admin_weight.py → inventory/services/datasheet_weight.py).
"""
from decimal import Decimal

ZERO = Decimal(0)


def _tare_g(order):
    """(тара, г; звідки) — Фактична упаковка замовлення, інакше рекомендація (📜 історія / упаковки товарів)."""
    from shipping.models import OrderPackaging
    ops = [o for o in OrderPackaging.objects.filter(order=order).select_related("packaging") if o.packaging]
    if ops:
        return sum((Decimal(o.packaging.tare_weight_kg or 0) * 1000 * max(1, o.qty_boxes or 1) for o in ops),
                   ZERO), "order_packaging"
    from shipping.services.packaging import recommend
    rec = recommend(order)
    boxes = [b for b in (rec.boxes if rec else []) if b.packaging]
    if boxes:
        return sum((Decimal(b.packaging.tare_weight_kg or 0) * 1000 * b.qty for b in boxes), ZERO), "recommend"
    return None, ""


def breakdown(order, quantities=None) -> dict | None:
    """{rows, product_g, tare_g, estimate_g, missing} для позицій посилки.
    quantities — {pk позиції: к-сть} (посилка частини замовлення), інакше уся кількість."""
    from inventory.services import datasheet_weight
    if order is None or not order.pk:
        return None
    lines = list(order.lines.select_related("product").order_by("pk"))
    if not lines:
        return None
    rows, product_g, missing = [], ZERO, 0
    for line in lines:
        qty = Decimal(str(quantities.get(line.pk, 0) if quantities is not None else (line.qty or 0)))
        if qty <= 0:
            continue
        p = line.product
        sku = (line.sku_raw or (p.sku if p else "") or "?").strip()
        grams = p.net_weight_g if p and p.net_weight_g else None
        sub = grams * qty if grams else None
        if sub is not None:
            product_g += sub
        else:
            missing += 1
        st = datasheet_weight.status(p) if p else {}
        rows.append({"sku": sku, "qty": qty, "grams": grams, "sub": sub, "product": p,
                     "source_text": st.get("source_text", ""), "note": st.get("note", ""),
                     "checked": st.get("checked")})
    if not rows:
        return None
    tare, tare_from = _tare_g(order)
    estimate = (tare or ZERO) + product_g
    return {"rows": rows, "product_g": product_g, "tare_g": tare, "tare_from": tare_from,
            "estimate_g": estimate, "missing": missing}
