"""
dashboard/digest.py — Periodic digest report

Sections (each toggleable in NotificationSettings):
  - 📦 Pending shipments    — orders with deadline, not yet shipped
  - ⏰ Overdue              — past deadline, not shipped
  - 🆕 New orders           — arrived since last digest
  - ✅ Delivered            — reached 'delivered' status in the period
  - 🔥 Critical stock       — < 1.5 months supply

Triggered by: `python manage.py send_digest [--force]`
"""
from __future__ import annotations
from datetime import timedelta
from django.utils import timezone


# ── Data collectors ────────────────────────────────────────────────────────────

def _get_pending_shipments() -> list:
    """Orders with a shipping deadline that haven't been shipped yet."""
    try:
        from sales.models import SalesOrder
        today = timezone.now().date()
        qs = (
            SalesOrder.objects
            .filter(affects_stock=True, shipped_at__isnull=True,
                    shipping_deadline__isnull=False)
            .exclude(status__in=["shipped", "delivered", "cancelled"])
            .order_by("shipping_deadline")
        )
        result = []
        for o in qs:
            days_left = (o.shipping_deadline - today).days
            result.append({
                "pk":           o.pk,
                "order_number": o.order_number,
                "client":       o.client or "—",
                "deadline":     o.shipping_deadline,
                "days_left":    days_left,
                "status":       o.get_status_display(),
                "source":       o.source or "",
            })
        return result
    except Exception:
        return []


def _get_overdue_orders() -> list:
    """Orders past deadline, not shipped."""
    try:
        from sales.models import SalesOrder
        today = timezone.now().date()
        qs = (
            SalesOrder.objects
            .filter(affects_stock=True, shipping_deadline__lt=today,
                    shipped_at__isnull=True)
            .exclude(status__in=["shipped", "delivered", "cancelled"])
            .order_by("shipping_deadline")
        )
        result = []
        for o in qs:
            days_late = (today - o.shipping_deadline).days
            result.append({
                "pk":           o.pk,
                "order_number": o.order_number,
                "client":       o.client or "—",
                "deadline":     o.shipping_deadline,
                "days_late":    days_late,
            })
        return result
    except Exception:
        return []


def _get_new_orders_since(since_dt) -> list:
    """Orders created since the given datetime."""
    try:
        from sales.models import SalesOrder
        qs = (
            SalesOrder.objects
            .filter(affects_stock=True, order_date__gte=since_dt.date())
            .order_by("-order_date", "-pk")[:25]
        )
        result = []
        for o in qs:
            result.append({
                "pk":           o.pk,
                "order_number": o.order_number,
                "client":       o.client or "—",
                "total":        o.total_price,
                "currency":     o.currency or "",
                "source":       o.source or "",
            })
        return result
    except Exception:
        return []


def _get_delivered_since(since_dt) -> list:
    """Orders with status 'delivered' updated in the period (approximate via order_date window)."""
    try:
        from sales.models import SalesOrder
        # We don't have a delivered_at index, so use a generous window
        window_start = since_dt.date() - timedelta(days=30)
        qs = (
            SalesOrder.objects
            .filter(affects_stock=True, status="delivered",
                    order_date__gte=window_start)
            .order_by("-pk")[:25]
        )
        result = []
        for o in qs:
            result.append({
                "pk":           o.pk,
                "order_number": o.order_number,
                "client":       o.client or "—",
                "source":       o.source or "",
                "tracking":     o.tracking_number or "",
            })
        return result
    except Exception:
        return []


def _get_critical_stock() -> list:
    from dashboard.notifications import _get_critical_stock as _cs
    return _cs()


# ── Main data builder ──────────────────────────────────────────────────────────

def build_digest_data(ns) -> dict:
    since_dt = ns.digest_last_sent or (timezone.now() - timedelta(hours=25))
    return {
        "pending":    _get_pending_shipments()      if ns.digest_include_pending    else [],
        "overdue":    _get_overdue_orders()         if ns.digest_include_overdue    else [],
        "new_orders": _get_new_orders_since(since_dt) if ns.digest_include_new_orders else [],
        "delivered":  _get_delivered_since(since_dt)  if ns.digest_include_delivered  else [],
        "stock":      _get_critical_stock()         if ns.digest_include_stock      else [],
        "since_dt":   since_dt,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _days_label(days_left: int) -> str:
    if days_left > 1:
        return f"{days_left} дн."
    if days_left == 1:
        return "завтра ⚠️"
    if days_left == 0:
        return "СЬОГОДНІ 🔴"
    return f"прострочено {-days_left} дн. 🔴"


# ── Telegram builder ───────────────────────────────────────────────────────────

def build_digest_telegram(data: dict, company_name: str = "Minerva") -> str:
    now_str = timezone.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        "📊 <b>Minerva — Щоденний звіт</b>",
        f"<i>{company_name} · {now_str}</i>",
        "",
    ]

    if not any([data["pending"], data["overdue"],
                data["new_orders"], data["delivered"], data["stock"]]):
        lines.append("✅ <b>Все в порядку — нема алертів</b>")
        return "\n".join(lines)

    if data["pending"]:
        lines.append(f'📦 <b>Очікують відправки ({len(data["pending"])})</b>:')
        for o in data["pending"][:10]:
            lines.append(
                f'  • <code>{o["order_number"]}</code> | {o["client"]} | '
                f'⏰ {o["deadline"].strftime("%d.%m")} ({_days_label(o["days_left"])})'
            )
        if len(data["pending"]) > 10:
            lines.append(f'  <i>... і ще {len(data["pending"]) - 10}</i>')
        lines.append("")

    if data["overdue"]:
        lines.append(f'⏰ <b>Прострочено ({len(data["overdue"])})</b>:')
        for o in data["overdue"][:5]:
            lines.append(
                f'  • <code>{o["order_number"]}</code> | {o["client"]} | '
                f'🔴 +{o["days_late"]} дн.'
            )
        lines.append("")

    if data["new_orders"]:
        lines.append(f'🆕 <b>Нові замовлення ({len(data["new_orders"])})</b>:')
        for o in data["new_orders"][:8]:
            total_part = f' — {o["total"]} {o["currency"]}'.strip() if o["total"] else ""
            lines.append(f'  • <code>{o["order_number"]}</code> | {o["client"]}{total_part}')
        lines.append("")

    if data["delivered"]:
        lines.append(f'✅ <b>Доставлено ({len(data["delivered"])})</b>:')
        for o in data["delivered"][:8]:
            lines.append(f'  • <code>{o["order_number"]}</code> | {o["client"]}')
        lines.append("")

    if data["stock"]:
        lines.append(f'🔥 <b>Критичний залишок ({len(data["stock"])})</b>:')
        for item in data["stock"][:5]:
            icon = "🔴" if item["is_critical"] else "⚠️"
            lines.append(
                f'  • <code>{item["sku"]}</code> — {item["name"]} | '
                f'{item["stock"]} шт | {item["months_left"]} міс {icon}'
            )

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3480] + "\n<i>... (обрізано)</i>"
    return text


# ── Email HTML builder ─────────────────────────────────────────────────────────

def build_digest_email_html(data: dict, company_name: str = "Minerva") -> str:
    now_str = timezone.now().strftime("%d.%m.%Y %H:%M")
    sections = ""

    # ── Pending shipments ────────────────────────────────────────────────────
    if data["pending"]:
        rows = ""
        for i, o in enumerate(data["pending"]):
            bg = "#fff8f0" if i % 2 == 0 else "#fff"
            dl = o["days_left"]
            dl_color = "#c62828" if dl <= 0 else ("#e65100" if dl <= 3 else "#2e7d32")
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:6px 10px;font-family:monospace;color:#1565c0">{o["order_number"]}</td>'
                f'<td style="padding:6px 10px">{o["client"]}</td>'
                f'<td style="padding:6px 10px;color:#555">{o["deadline"].strftime("%d.%m.%Y")}</td>'
                f'<td style="padding:6px 10px;font-weight:700;color:{dl_color}">{_days_label(dl)}</td>'
                f'<td style="padding:6px 10px;color:#888;font-size:11px">{o["status"]}</td>'
                f'</tr>'
            )
        sections += _table_section(
            "📦 Очікують відправки", len(data["pending"]), "#e65100", "#fff3e0",
            ("Замовлення", "Клієнт", "Дедлайн", "Залишилось", "Статус"), rows,
        )

    # ── Overdue ───────────────────────────────────────────────────────────────
    if data["overdue"]:
        rows = ""
        for i, o in enumerate(data["overdue"]):
            bg = "#fff8f8" if i % 2 == 0 else "#fff"
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:6px 10px;font-family:monospace;color:#c62828">{o["order_number"]}</td>'
                f'<td style="padding:6px 10px">{o["client"]}</td>'
                f'<td style="padding:6px 10px">{o["deadline"].strftime("%d.%m.%Y")}</td>'
                f'<td style="padding:6px 10px;font-weight:700;color:#c62828">+{o["days_late"]} дн.</td>'
                f'</tr>'
            )
        sections += _table_section(
            "⏰ Прострочено", len(data["overdue"]), "#c62828", "#fce4e4",
            ("Замовлення", "Клієнт", "Дедлайн", "Прострочено"), rows,
        )

    # ── New orders ────────────────────────────────────────────────────────────
    if data["new_orders"]:
        rows = ""
        for i, o in enumerate(data["new_orders"]):
            bg = "#f1f8e9" if i % 2 == 0 else "#fff"
            total_str = f'{o["total"]} {o["currency"]}'.strip() if o["total"] else "—"
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:6px 10px;font-family:monospace;color:#1565c0">{o["order_number"]}</td>'
                f'<td style="padding:6px 10px">{o["client"]}</td>'
                f'<td style="padding:6px 10px;font-weight:600;color:#2e7d32">{total_str}</td>'
                f'<td style="padding:6px 10px;color:#888;font-size:11px">{o["source"]}</td>'
                f'</tr>'
            )
        sections += _table_section(
            "🆕 Нові замовлення", len(data["new_orders"]), "#2e7d32", "#dcedc8",
            ("Замовлення", "Клієнт", "Сума", "Джерело"), rows,
        )

    # ── Delivered ─────────────────────────────────────────────────────────────
    if data["delivered"]:
        rows = ""
        for i, o in enumerate(data["delivered"]):
            bg = "#e8f5e9" if i % 2 == 0 else "#fff"
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:6px 10px;font-family:monospace;color:#1565c0">{o["order_number"]}</td>'
                f'<td style="padding:6px 10px">{o["client"]}</td>'
                f'<td style="padding:6px 10px;font-size:11px;color:#888">{o["tracking"] or "—"}</td>'
                f'</tr>'
            )
        sections += _table_section(
            "✅ Доставлено", len(data["delivered"]), "#1b5e20", "#c8e6c9",
            ("Замовлення", "Клієнт", "Трекінг"), rows,
        )

    # ── Critical stock ────────────────────────────────────────────────────────
    if data["stock"]:
        rows = ""
        for i, item in enumerate(data["stock"]):
            bg = "#fff8f8" if i % 2 == 0 else "#fff"
            color = "#c62828" if item["is_critical"] else "#e65100"
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:6px 10px;font-family:monospace">{item["sku"]}</td>'
                f'<td style="padding:6px 10px">{item["name"]}</td>'
                f'<td style="padding:6px 10px;text-align:right">{item["stock"]}</td>'
                f'<td style="padding:6px 10px;text-align:right;font-weight:700;color:{color}">'
                f'{item["months_left"]} міс</td>'
                f'</tr>'
            )
        sections += _table_section(
            "🔥 Критичний залишок", len(data["stock"]), "#b71c1c", "#fce4e4",
            ("SKU", "Назва", "Залишок", "Місяців"), rows,
        )

    if not sections:
        sections = (
            '<div style="padding:16px 24px;background:#e8f5e9;border-left:4px solid #4caf50">'
            '<b style="color:#2e7d32">✅ Все в порядку — нема алертів</b></div>'
        )

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,sans-serif;background:#f0f2f5;margin:0;padding:20px">'
        '<div style="max-width:700px;margin:0 auto;background:#fff;border-radius:8px;'
        'overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.12)">'
        '<div style="background:#37474f;color:#fff;padding:20px 24px">'
        '<div style="font-size:19px;font-weight:700">📊 Minerva — Щоденний звіт</div>'
        f'<div style="font-size:12px;opacity:.75">{company_name} &middot; {now_str}</div>'
        '</div>'
        + sections +
        '<div style="padding:14px 24px;border-top:1px solid #eee;font-size:12px;color:#999;margin-top:16px">'
        'Minerva Business Intelligence — автоматичне сповіщення'
        '</div></div></body></html>'
    )


def _table_section(title, count, hdr_color, hdr_bg, columns, rows_html) -> str:
    cols_html = "".join(
        f'<td style="padding:5px 10px">{c}</td>' for c in columns
    )
    return (
        f'<div style="padding:16px 24px 0">'
        f'<h3 style="margin:0 0 10px;font-size:14px;color:{hdr_color}">{title} ({count})</h3>'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<tr style="background:{hdr_bg};color:{hdr_color};font-size:11px;font-weight:700">'
        f'{cols_html}</tr>'
        f'{rows_html}</table></div>'
    )


# ── Send digest ────────────────────────────────────────────────────────────────

def send_digest(force: bool = False) -> dict:
    """
    Send digest report. Call with force=True to bypass schedule check.
    Returns dict: {'sent': bool, 'reason': str | None, 'email': {}, 'telegram': {}}
    """
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception as e:
        return {"sent": False, "error": str(e)}

    if not ns.digest_enabled and not force:
        return {"sent": False, "reason": "Digest вимкнено"}

    send_email = ns.email_enabled and ns.digest_email
    send_tg    = ns.telegram_enabled and ns.digest_telegram
    if not send_email and not send_tg:
        return {"sent": False, "reason": "Жоден канал не налаштований (email / Telegram)"}

    # Schedule check (unless forced)
    if not force and ns.digest_last_sent:
        now = timezone.now()
        if ns.digest_frequency == "daily":
            next_send = ns.digest_last_sent + timedelta(days=1)
        else:
            next_send = ns.digest_last_sent + timedelta(weeks=1)
        if now < next_send:
            return {
                "sent": False,
                "reason": f"Ще не час — наступний звіт: {next_send.strftime('%d.%m.%Y %H:%M')}",
            }

    try:
        from accounting.models import CompanySettings
        company_name = CompanySettings.get().name or "Minerva"
    except Exception:
        company_name = "Minerva"

    data    = build_digest_data(ns)
    results = {"email": {}, "telegram": {}}

    if send_email:
        try:
            from dashboard.notifications import _send_event_email
            freq_label = "Щоденний" if ns.digest_frequency == "daily" else "Щотижневий"
            subject    = f"📊 Minerva — {freq_label} звіт · {timezone.now().strftime('%d.%m.%Y')}"
            html       = build_digest_email_html(data, company_name)
            _send_event_email(ns, subject, html)
            results["email"] = {"sent": True}
        except Exception as e:
            results["email"] = {"error": str(e)}

    if send_tg:
        try:
            from dashboard.notifications import _send_telegram
            text = build_digest_telegram(data, company_name)
            _send_telegram(ns, text)
            results["telegram"] = {"sent": True}
        except Exception as e:
            results["telegram"] = {"error": str(e)}

    overall = results["email"].get("sent") or results["telegram"].get("sent")
    if overall:
        from config.models import NotificationSettings as NS
        NS.objects.filter(pk=1).update(digest_last_sent=timezone.now())

    return {"sent": overall, **results}
