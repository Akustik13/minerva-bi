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
from datetime import timedelta, date as _date
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def _check_holiday(today: _date, country_code: str) -> str | None:
    """
    Check via Nager.Date API if `today` is a public holiday in `country_code`.
    Returns a human-readable reason string if it IS a holiday, else None.
    On network error / timeout — returns None (fail-open: do not skip sending).
    """
    try:
        import urllib.request
        url = f"https://date.nager.at/api/v3/IsTodayPublicHoliday/{country_code}"
        req = urllib.request.Request(url, headers={"User-Agent": "MinervaBI/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                # 200 = today is a public holiday; 204 = not a holiday
                country_names = {"UA": "Україна", "DE": "Німеччина", "PL": "Польща", "US": "США"}
                name = country_names.get(country_code, country_code)
                return f"Державне свято ({name}) — надсилання пропущено"
    except Exception as exc:
        logger.warning("digest holiday check failed (%s): %s", country_code, exc)
    return None


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


def _get_top_products(since, until, limit: int = 10) -> list:
    """Top products by quantity sold in [since, until] date range."""
    try:
        from django.db.models import Sum
        from sales.models import SalesOrderLine
        rows = (
            SalesOrderLine.objects
            .filter(
                order__order_date__range=(since, until),
                order__affects_stock=True,
            )
            .exclude(order__status='cancelled')
            .values('product__sku', 'product__name')
            .annotate(total_qty=Sum('qty'), total_rev=Sum('total_price'))
            .order_by('-total_qty')[:limit]
        )
        return [
            {
                'sku':       r['product__sku'] or '—',
                'name':      r['product__name'] or '—',
                'total_qty': int(r['total_qty'] or 0),
                'total_rev': float(r['total_rev'] or 0),
            }
            for r in rows
        ]
    except Exception:
        return []


def _get_shipments_count(since, until) -> int:
    """Count of shipped orders in [since, until] date range."""
    try:
        from sales.models import SalesOrder
        return SalesOrder.objects.filter(
            shipped_at__date__range=(since, until),
            affects_stock=True,
        ).count()
    except Exception:
        return 0


def _get_monthly_revenue_comparison() -> dict:
    """Revenue + order count: current month vs previous month."""
    try:
        from django.db.models import Sum
        from sales.models import SalesOrder, SalesOrderLine
        today = timezone.now().date()
        # Current month
        cur_start = today.replace(day=1)
        # Previous month
        prev_end = cur_start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)

        def _month_stats(start, end):
            orders = SalesOrder.objects.filter(
                order_date__range=(start, end), affects_stock=True,
            ).exclude(status='cancelled')
            count = orders.count()
            rev = (
                SalesOrderLine.objects
                .filter(order__in=orders)
                .aggregate(s=Sum('total_price'))['s'] or 0
            )
            return {'count': count, 'revenue': float(rev)}

        cur  = _month_stats(cur_start, today)
        prev = _month_stats(prev_start, prev_end)
        diff_rev = cur['revenue'] - prev['revenue']
        diff_pct = (diff_rev / prev['revenue'] * 100) if prev['revenue'] else None
        return {
            'cur_start':  cur_start,
            'prev_start': prev_start,
            'cur':        cur,
            'prev':       prev,
            'diff_rev':   diff_rev,
            'diff_pct':   diff_pct,
        }
    except Exception:
        return {}


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

def build_digest_telegram(data: dict, company_name: str = "Minerva", period: str = "daily") -> str:
    now_str = timezone.localtime().strftime("%d.%m.%Y %H:%M")
    period_labels = {"daily": "Щоденний звіт", "weekly": "Тижневий звіт", "monthly": "Місячний звіт"}
    period_label = period_labels.get(period, "Звіт")
    lines = [
        f"📊 <b>Minerva — {period_label}</b>",
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
        lines.append("")

    # ── Weekly/Monthly extras ─────────────────────────────────────────────────
    if period in ("weekly", "monthly") and data.get("shipments") is not None:
        lines.append(f'🚚 <b>Відправлень за період:</b> {data["shipments"]}')

    if period in ("weekly", "monthly") and data.get("top_products"):
        limit = 5 if period == "weekly" else 10
        lines.append(f'\n📦 <b>Топ товарів ({period_label}):</b>')
        for i, p in enumerate(data["top_products"][:limit], 1):
            lines.append(
                f'  {i}. <code>{p["sku"]}</code> {p["name"]} | '
                f'{p["total_qty"]} шт'
            )

    if period == "monthly" and data.get("revenue_cmp"):
        rc = data["revenue_cmp"]
        cur  = rc.get("cur",  {})
        prev = rc.get("prev", {})
        diff_pct = rc.get("diff_pct")
        arrow = ""
        if diff_pct is not None:
            arrow = f" {'▲' if diff_pct >= 0 else '▼'}{abs(diff_pct):.1f}%"
        lines.append(
            f'\n💰 <b>Виручка місяця:</b> {cur.get("revenue", 0):.0f}{arrow}'
        )
        lines.append(
            f'   Поп. місяць: {prev.get("revenue", 0):.0f} | '
            f'Замовлень: {cur.get("count", 0)}'
        )

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3480] + "\n<i>... (обрізано)</i>"
    return text


# ── Email HTML builder ─────────────────────────────────────────────────────────

def build_digest_email_html(data: dict, company_name: str = "Minerva", period: str = "daily") -> str:
    now_str = timezone.localtime().strftime("%d.%m.%Y %H:%M")
    period_labels = {"daily": "Щоденний звіт", "weekly": "Тижневий звіт", "monthly": "Місячний звіт"}
    period_label = period_labels.get(period, "Звіт")
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

    # ── Weekly/Monthly extras ──────────────────────────────────────────────────
    if period in ("weekly", "monthly") and data.get("top_products"):
        limit = 5 if period == "weekly" else 10
        top = data["top_products"][:limit]
        rows = ""
        for i, p in enumerate(top):
            bg = "#f8f9fa" if i % 2 == 0 else "#fff"
            rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:5px 10px;font-weight:700;color:#888;text-align:center">{i+1}</td>'
                f'<td style="padding:5px 10px;font-family:monospace;color:#1565c0">{p["sku"]}</td>'
                f'<td style="padding:5px 10px">{p["name"]}</td>'
                f'<td style="padding:5px 10px;text-align:right;font-weight:600">{p["total_qty"]}</td>'
                f'</tr>'
            )
        sections += _table_section(
            f"📦 Топ товарів", len(top), "#4527a0", "#ede7f6",
            ("#", "SKU", "Назва", "Продано (шт)"), rows,
        )

    if period in ("weekly", "monthly") and data.get("shipments") is not None:
        sections += (
            f'<div style="padding:12px 24px 0">'
            f'<p style="margin:0;font-size:13px;color:#333">'
            f'🚚 <b>Відправлено за період:</b> {data["shipments"]} замовлень'
            f'</p></div>'
        )

    if period == "monthly" and data.get("revenue_cmp"):
        rc = data["revenue_cmp"]
        cur  = rc.get("cur",  {})
        prev = rc.get("prev", {})
        diff_rev = rc.get("diff_rev", 0)
        diff_pct = rc.get("diff_pct")
        arrow_color = "#2e7d32" if diff_rev >= 0 else "#c62828"
        arrow_str = ""
        if diff_pct is not None:
            arrow = "▲" if diff_pct >= 0 else "▼"
            arrow_str = f' <span style="color:{arrow_color};font-weight:700">{arrow}{abs(diff_pct):.1f}%</span>'
        sections += (
            f'<div style="padding:16px 24px 0">'
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#1565c0">💰 Виручка: поточний vs попередній місяць</h3>'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
            f'<tr style="background:#e3f2fd;font-weight:600;color:#1565c0">'
            f'<td style="padding:6px 10px">Місяць</td>'
            f'<td style="padding:6px 10px;text-align:right">Виручка</td>'
            f'<td style="padding:6px 10px;text-align:right">Замовлень</td>'
            f'</tr>'
            f'<tr style="background:#f5f5f5">'
            f'<td style="padding:6px 10px">Поточний</td>'
            f'<td style="padding:6px 10px;text-align:right;font-weight:700">'
            f'{cur.get("revenue", 0):.0f}{arrow_str}</td>'
            f'<td style="padding:6px 10px;text-align:right">{cur.get("count", 0)}</td>'
            f'</tr>'
            f'<tr>'
            f'<td style="padding:6px 10px;color:#888">Попередній</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#888">{prev.get("revenue", 0):.0f}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#888">{prev.get("count", 0)}</td>'
            f'</tr>'
            f'</table></div>'
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
        f'<div style="font-size:19px;font-weight:700">📊 Minerva — {period_label}</div>'
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

def send_digest(force: bool = False, period: str = "daily") -> dict:
    """
    Send digest report for the given period ('daily', 'weekly', 'monthly').
    Call with force=True to bypass schedule check.
    Returns dict: {'sent': bool, 'reason': str | None, 'email': {}, 'telegram': {}}
    """
    if period == "weekly":
        return _send_weekly(force)
    if period == "monthly":
        return _send_monthly(force)

    # ── Daily (existing logic) ────────────────────────────────────────────────
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
    if not force:
        try:
            import zoneinfo
            from django.conf import settings as _settings
            now       = timezone.now()
            tz        = zoneinfo.ZoneInfo(getattr(_settings, "TIME_ZONE", "UTC"))
            local_now = now.astimezone(tz)
            send_time = ns.digest_send_time  # TimeField value

            # Has today's send time been reached?
            today_send = local_now.replace(
                hour=send_time.hour, minute=send_time.minute, second=0, microsecond=0,
            )
            if local_now < today_send:
                return {
                    "sent": False,
                    "reason": f"Ще не час (налаштовано {send_time.strftime('%H:%M')})",
                }

            # Skip weekends?
            skip_wknd = getattr(ns, "digest_skip_weekends", False)
            if skip_wknd and local_now.weekday() >= 5:  # 5=Sat, 6=Sun
                day_name = "суботу" if local_now.weekday() == 5 else "неділю"
                return {"sent": False, "reason": f"Вихідний день ({day_name}) — надсилання пропущено"}

            # Skip public holidays?
            skip_hol = getattr(ns, "digest_skip_holidays", False)
            if skip_hol:
                country = getattr(ns, "digest_holiday_country", "UA")
                holiday_reason = _check_holiday(local_now.date(), country)
                if holiday_reason:
                    return {"sent": False, "reason": holiday_reason}

            # Already sent recently?
            if ns.digest_last_sent:
                last_local = ns.digest_last_sent.astimezone(tz)
                if ns.digest_frequency == "daily":
                    if last_local.date() >= local_now.date():
                        return {"sent": False, "reason": "Вже надіслано сьогодні"}
                else:
                    days_since = (local_now.date() - last_local.date()).days
                    if days_since < 7:
                        next_dt = (last_local + timedelta(days=7)).strftime("%d.%m.%Y")
                        return {"sent": False, "reason": f"Вже надіслано — наступний: {next_dt}"}
        except Exception as exc:
            logger.error("send_digest schedule check failed: %s", exc, exc_info=True)
            return {"sent": False, "reason": f"Помилка перевірки розкладу: {exc}"}

    try:
        from config.models import SystemSettings
        company_name = SystemSettings.get().company_name or "Minerva"
    except Exception:
        company_name = "Minerva"

    data    = build_digest_data(ns)
    results = {"email": {}, "telegram": {}}

    if send_email:
        try:
            from dashboard.notifications import _send_event_email
            subject = f"📊 Minerva — Щоденний звіт · {timezone.localtime().strftime('%d.%m.%Y')}"
            html    = build_digest_email_html(data, company_name, period="daily")
            _send_event_email(ns, subject, html)
            results["email"] = {"sent": True}
        except Exception as e:
            results["email"] = {"error": str(e)}

    if send_tg:
        try:
            from dashboard.notifications import _send_telegram
            text = build_digest_telegram(data, company_name, period="daily")
            _send_telegram(ns, text)
            results["telegram"] = {"sent": True}
        except Exception as e:
            results["telegram"] = {"error": str(e)}

    overall = results["email"].get("sent") or results["telegram"].get("sent")
    if overall:
        from config.models import NotificationSettings as NS
        NS.objects.filter(pk=1).update(digest_last_sent=timezone.now())

    return {"sent": overall, **results}


def _send_weekly(force: bool = False) -> dict:
    """Send weekly digest report."""
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception as e:
        return {"sent": False, "error": str(e)}

    if not ns.weekly_digest_enabled and not force:
        return {"sent": False, "reason": "Тижневий звіт вимкнено"}

    send_email = ns.email_enabled and ns.digest_email
    send_tg    = ns.telegram_enabled and ns.digest_telegram
    if not send_email and not send_tg:
        return {"sent": False, "reason": "Жоден канал не налаштований (email / Telegram)"}

    if not force:
        try:
            import zoneinfo
            from django.conf import settings as _settings
            tz        = zoneinfo.ZoneInfo(getattr(_settings, "TIME_ZONE", "UTC"))
            local_now = timezone.now().astimezone(tz)
            if local_now.weekday() != ns.weekly_digest_day:
                return {"sent": False, "reason": "Не той день тижня"}
            send_time = ns.weekly_digest_time
            today_send = local_now.replace(
                hour=send_time.hour, minute=send_time.minute, second=0, microsecond=0,
            )
            if local_now < today_send:
                return {"sent": False, "reason": f"Ще не час ({send_time.strftime('%H:%M')})"}
            if ns.weekly_digest_last_sent:
                last_local = ns.weekly_digest_last_sent.astimezone(tz)
                if last_local.date() >= local_now.date():
                    return {"sent": False, "reason": "Вже надіслано сьогодні"}
        except Exception as exc:
            logger.error("weekly digest schedule check failed: %s", exc, exc_info=True)

    try:
        from config.models import SystemSettings
        company_name = SystemSettings.get().company_name or "Minerva"
    except Exception:
        company_name = "Minerva"

    today = timezone.now().date()
    since = today - timedelta(days=7)
    top_products    = _get_top_products(since, today, limit=5)
    shipments_count = _get_shipments_count(since, today)
    data = {
        "pending":       _get_pending_shipments(),
        "overdue":       _get_overdue_orders(),
        "new_orders":    [],
        "delivered":     [],
        "stock":         _get_critical_stock(),
        "since_dt":      timezone.now() - timedelta(days=7),
        "top_products":  top_products,
        "shipments":     shipments_count,
        "period":        "weekly",
        "period_since":  since,
    }

    results = {"email": {}, "telegram": {}}
    date_str = timezone.localtime().strftime("%d.%m.%Y")

    if send_email:
        try:
            from dashboard.notifications import _send_event_email
            html = build_digest_email_html(data, company_name, period="weekly")
            _send_event_email(ns, f"📊 Minerva — Тижневий звіт · {date_str}", html)
            results["email"] = {"sent": True}
        except Exception as e:
            results["email"] = {"error": str(e)}

    if send_tg:
        try:
            from dashboard.notifications import _send_telegram
            text = build_digest_telegram(data, company_name, period="weekly")
            _send_telegram(ns, text)
            results["telegram"] = {"sent": True}
        except Exception as e:
            results["telegram"] = {"error": str(e)}

    overall = results["email"].get("sent") or results["telegram"].get("sent")
    if overall:
        from config.models import NotificationSettings as NS
        NS.objects.filter(pk=1).update(weekly_digest_last_sent=timezone.now())

    return {"sent": overall, **results}


def _send_monthly(force: bool = False) -> dict:
    """Send monthly digest report."""
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception as e:
        return {"sent": False, "error": str(e)}

    if not ns.monthly_digest_enabled and not force:
        return {"sent": False, "reason": "Місячний звіт вимкнено"}

    send_email = ns.email_enabled and ns.digest_email
    send_tg    = ns.telegram_enabled and ns.digest_telegram
    if not send_email and not send_tg:
        return {"sent": False, "reason": "Жоден канал не налаштований (email / Telegram)"}

    if not force:
        try:
            import zoneinfo
            from django.conf import settings as _settings
            tz        = zoneinfo.ZoneInfo(getattr(_settings, "TIME_ZONE", "UTC"))
            local_now = timezone.now().astimezone(tz)
            if local_now.day != ns.monthly_digest_day:
                return {"sent": False, "reason": "Не той день місяця"}
            send_time = ns.monthly_digest_time
            today_send = local_now.replace(
                hour=send_time.hour, minute=send_time.minute, second=0, microsecond=0,
            )
            if local_now < today_send:
                return {"sent": False, "reason": f"Ще не час ({send_time.strftime('%H:%M')})"}
            if ns.monthly_digest_last_sent:
                last_local = ns.monthly_digest_last_sent.astimezone(tz)
                if last_local.date() >= local_now.date():
                    return {"sent": False, "reason": "Вже надіслано сьогодні"}
        except Exception as exc:
            logger.error("monthly digest schedule check failed: %s", exc, exc_info=True)

    try:
        from config.models import SystemSettings
        company_name = SystemSettings.get().company_name or "Minerva"
    except Exception:
        company_name = "Minerva"

    today = timezone.now().date()
    cur_start = today.replace(day=1)
    top_products    = _get_top_products(cur_start, today, limit=10)
    shipments_count = _get_shipments_count(cur_start, today)
    revenue_cmp     = _get_monthly_revenue_comparison()
    data = {
        "pending":       _get_pending_shipments(),
        "overdue":       _get_overdue_orders(),
        "new_orders":    [],
        "delivered":     [],
        "stock":         _get_critical_stock(),
        "since_dt":      timezone.now() - timedelta(days=31),
        "top_products":  top_products,
        "shipments":     shipments_count,
        "revenue_cmp":   revenue_cmp,
        "period":        "monthly",
        "period_since":  cur_start,
    }

    results = {"email": {}, "telegram": {}}
    date_str = timezone.localtime().strftime("%d.%m.%Y")

    if send_email:
        try:
            from dashboard.notifications import _send_event_email
            html = build_digest_email_html(data, company_name, period="monthly")
            _send_event_email(ns, f"📊 Minerva — Місячний звіт · {date_str}", html)
            results["email"] = {"sent": True}
        except Exception as e:
            results["email"] = {"error": str(e)}

    if send_tg:
        try:
            from dashboard.notifications import _send_telegram
            text = build_digest_telegram(data, company_name, period="monthly")
            _send_telegram(ns, text)
            results["telegram"] = {"sent": True}
        except Exception as e:
            results["telegram"] = {"error": str(e)}

    overall = results["email"].get("sent") or results["telegram"].get("sent")
    if overall:
        from config.models import NotificationSettings as NS
        NS.objects.filter(pk=1).update(monthly_digest_last_sent=timezone.now())

    return {"sent": overall, **results}
