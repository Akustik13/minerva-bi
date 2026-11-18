"""Оплата й рахунок замовлення людською мовою — один розрахунок для списку замовлень (картки й таблиця), картки
замовлення, панелі праворуч і листа «🆕 Нове замовлення» (dashboard/notifications.py).

- `payment(order)` → {text, tone, method, status, reference} або None: «Передоплата (Vorkasse) · Не оплачено»;
  tone — ok (оплачено) / warn (не оплачено, очікує) / bad (помилка) / muted (повернено, без статусу).
- `invoice(order)` → куди виставляти рахунок: платіжна адреса з сайту (shop.ShopOrder.billing — компанія, ім'я,
  адреса, USt-IdNr, email, телефон), інакше «Клієнт (білінг)» + адреса замовлення; `separate` — доставка на іншу
  адресу; `vat_rate` — ставка ПДВ, яку порахував сайт.
- `site_note(order)` — підсумок, який сайт пише в «Внутрішню нотатку» («Webshop | Zahlung: … | USt 19% | Brutto: …»).
"""
from __future__ import annotations

TONES = {"paid": "ok", "unpaid": "warn", "pending": "warn", "failed": "bad", "refunded": "muted"}
COLORS = {"ok": "#2e7d32", "warn": "#e65100", "bad": "#c62828", "muted": "#757575"}
SITE_NOTE_PREFIX = "Webshop"


def payment(order) -> dict | None:
    method, status = order.payment_method or "", order.payment_status or ""
    if not method and not status:
        return None
    parts = [order.get_payment_method_display() if method else "",
             order.get_payment_status_display() if status else ""]
    return {"text": " · ".join(p for p in parts if p and p != "—"), "tone": TONES.get(status, "muted"),
            "method": method, "status": status, "reference": order.payment_reference or ""}


def _shop_info(order):
    try:
        return order.shop_info
    except Exception:  # noqa: BLE001 — немає запису з сайту (ObjectDoesNotExist) або застосунку shop
        return None


def _country(code: str) -> str:
    try:
        from shop.countries import name
        return name(code) or code
    except Exception:  # noqa: BLE001
        return code


def invoice(order) -> dict:
    """{lines, vat_id, email, phone, vat_rate, separate, from_site}: адреса рахунку рядками."""
    info = _shop_info(order)
    billing = (info.billing if info is not None and isinstance(info.billing, dict) else {}) or {}
    if billing:
        lines = [billing.get("company", ""), billing.get("name", ""), billing.get("street", ""),
                 " ".join(x for x in (billing.get("zip", ""), billing.get("city", "")) if x),
                 _country(billing.get("country", ""))]
        separate = (billing.get("street", ""), billing.get("zip", ""), billing.get("city", "")) != \
            (order.addr_street or "", order.addr_zip or "", order.addr_city or "")
        vat_id = billing.get("vat_id") or order.buyer_vat_id or ""
        email, phone = billing.get("email") or order.email or "", billing.get("phone") or order.phone or ""
    else:
        lines = [order.client or "", order.contact_name or "", order.addr_street or "",
                 " ".join(x for x in (order.addr_zip or "", order.addr_city or "") if x),
                 _country(order.addr_country or "")]
        separate, vat_id, email, phone = False, order.buyer_vat_id or "", order.email or "", order.phone or ""
    seen, out = set(), []
    for line in lines:
        line = (line or "").strip()
        if line and line.lower() not in seen:
            seen.add(line.lower())
            out.append(line)
    return {"lines": out, "vat_id": vat_id, "email": email, "phone": phone,
            "vat_rate": info.vat_rate if info is not None else None, "separate": separate,
            "from_site": bool(billing)}


def delivery_lines(order) -> list[str]:
    """Адреса доставки рядками (для «доставка на іншу адресу»)."""
    lines = [order.ship_company or "", order.ship_name or "", order.addr_street or "",
             " ".join(x for x in (order.addr_zip or "", order.addr_city or "") if x), _country(order.addr_country or "")]
    return [x.strip() for x in lines if x and x.strip()]


def site_note(order) -> str:
    note = (order.internal_note or "").strip()
    return note if note.startswith(SITE_NOTE_PREFIX) else ""
