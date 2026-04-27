"""Email + Telegram alert service — critical stock + overdue shipping deadlines."""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from datetime import timedelta

from django.utils import timezone


def _get_company_name() -> str:
    try:
        from config.models import SystemSettings
        return SystemSettings.get().company_name or 'Minerva'
    except Exception:
        return 'Minerva'


# ── Data collection ────────────────────────────────────────────────────────────

def _get_critical_stock():
    """Products with < 1.5 months supply based on 90-day average consumption."""
    try:
        from django.db.models import Sum
        from inventory.models import Product, InventoryTransaction
        from sales.models import SalesOrderLine

        since_3m = timezone.now() - timedelta(days=90)
        items = []

        for p in Product.objects.filter(is_active=True).only(
            'pk', 'sku', 'name', 'reorder_point',
        ):
            stock = float(
                InventoryTransaction.objects.filter(product=p)
                .aggregate(t=Sum('qty'))['t'] or 0
            )
            sold = float(
                SalesOrderLine.objects.filter(
                    product=p,
                    order__order_date__gte=since_3m.date(),
                    order__affects_stock=True,
                ).aggregate(t=Sum('qty'))['t'] or 0
            )
            monthly = sold / 3
            if monthly > 0:
                months_left = max(0.0, stock / monthly)
                if months_left < 1.5:
                    items.append({
                        'pk':          p.pk,
                        'sku':         p.sku,
                        'name':        p.name,
                        'stock':       round(stock, 1),
                        'monthly':     round(monthly, 1),
                        'months_left': round(months_left, 2),
                        'is_critical': months_left < 0.5,
                    })
        return sorted(items, key=lambda x: x['months_left'])
    except Exception:
        return []


def _get_overdue_orders(overdue_days: int = 0):
    """SalesOrders that are past their shipping_deadline and not yet shipped."""
    try:
        from sales.models import SalesOrder

        today  = timezone.now().date()
        cutoff = today - timedelta(days=overdue_days)

        qs = (
            SalesOrder.objects.filter(
                shipping_deadline__lte=cutoff,
                shipped_at__isnull=True,
                affects_stock=True,
            )
            .exclude(status__in=['shipped', 'cancelled'])
            .order_by('shipping_deadline')
        )

        orders = []
        for o in qs:
            days_late = (today - o.shipping_deadline).days
            orders.append({
                'pk':           o.pk,
                'order_number': o.order_number,
                'client':       o.client or '—',
                'deadline':     o.shipping_deadline,
                'days_late':    days_late,
                'status':       o.get_status_display(),
            })
        return orders
    except Exception:
        return []


# ── Telegram builder ───────────────────────────────────────────────────────────

def _build_telegram_text(critical_items, overdue_orders, company_name='Minerva'):
    """Build Telegram HTML message (parse_mode='HTML'). Truncates at 3500 chars."""
    now_str = timezone.now().strftime('%d.%m.%Y %H:%M')
    total   = len(critical_items) + len(overdue_orders)

    lines = [
        f'🏛️ <b>Minerva — Системні сповіщення</b>',
        f'<i>{company_name} · {now_str}</i>',
        '',
    ]

    if total == 0:
        lines.append('✅ <b>Немає активних алертів</b>')
        lines.append('<i>Тестове повідомлення — канал налаштований правильно.</i>')
    else:
        parts = []
        if critical_items:
            parts.append(f'🔥 {len(critical_items)} критичних залишків')
        if overdue_orders:
            parts.append(f'⏰ {len(overdue_orders)} прострочених дедлайнів')
        lines.append('⚠️ ' + ' · '.join(parts))

    if critical_items:
        lines += ['', '🔥 <b>Критичний залишок:</b>']
        for item in critical_items:
            icon = '🔴' if item['is_critical'] else '⚠️'
            lines.append(
                f'<code>{item["sku"]}</code> — {item["name"]} | '
                f'{item["stock"]} шт | <b>{item["months_left"]} міс {icon}</b>'
            )

    if overdue_orders:
        lines += ['', '⏰ <b>Прострочені дедлайни:</b>']
        for o in overdue_orders:
            icon = '🔴' if o['days_late'] > 7 else '⚠️'
            lines.append(
                f'#{o["order_number"]} | {o["client"]} | '
                f'{o["deadline"].strftime("%d.%m.%Y")} | '
                f'<b>+{o["days_late"]} дн {icon}</b>'
            )

    text = '\n'.join(lines)
    if len(text) > 3500:
        text = text[:3480] + '\n\n<i>... (обрізано)</i>'
    return text


def _send_telegram(ns, text):
    """Send a message via Telegram Bot API using stdlib urllib."""
    url  = f'https://api.telegram.org/bot{ns.telegram_bot_token}/sendMessage'
    data = json.dumps({
        'chat_id':                  ns.telegram_chat_id,
        'text':                     text,
        'parse_mode':               'HTML',
        'disable_web_page_preview': True,
    }).encode('utf-8')
    req = urllib.request.Request(url, data, {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ── Email HTML builder ─────────────────────────────────────────────────────────

def _build_html(critical_items, overdue_orders, company_name='Minerva'):
    now_str = timezone.now().strftime('%d.%m.%Y %H:%M')
    total   = len(critical_items) + len(overdue_orders)

    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,sans-serif;background:#f0f2f5;margin:0;padding:20px">'
        '<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;'
        'overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.12)">'

        # ── header ──────────────────────────────────────────────────────────
        '<div style="background:#1a237e;color:#fff;padding:20px 24px">'
        '<div style="font-size:19px;font-weight:700;margin-bottom:4px">'
        f'&#9888;&#65039; Minerva &mdash; Системні сповіщення</div>'
        f'<div style="font-size:12px;opacity:.75">{company_name} &middot; {now_str}</div>'
        '</div>'
    )

    # ── summary banner ───────────────────────────────────────────────────────
    if total == 0:
        html += (
            '<div style="padding:16px 24px;background:#e8f5e9;border-left:4px solid #4caf50">'
            '<b style="color:#2e7d32">&#9989; Немає активних алертів</b><br>'
            '<span style="font-size:13px;color:#555">Тестове повідомлення — SMTP налаштований правильно.</span>'
            '</div>'
        )
    else:
        parts = []
        if critical_items:
            parts.append(f'&#128293; {len(critical_items)} критичних залишків')
        if overdue_orders:
            parts.append(f'&#128680; {len(overdue_orders)} прострочених дедлайнів')
        html += (
            '<div style="padding:12px 24px;background:#fff3e0;border-left:4px solid #ff9800">'
            f'<b style="color:#e65100">{"&nbsp;&nbsp;&middot;&nbsp;&nbsp;".join(parts)}</b>'
            '</div>'
        )

    # ── critical stock table ─────────────────────────────────────────────────
    if critical_items:
        html += (
            '<div style="padding:16px 24px 0">'
            '<h3 style="margin:0 0 10px;font-size:15px;color:#b71c1c">&#128293; Критичний залишок товарів</h3>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<tr style="background:#fce4e4;color:#b71c1c;font-weight:600">'
            '<td style="padding:7px 10px;border-bottom:2px solid #ef9a9a">SKU</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ef9a9a">Назва товару</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ef9a9a;text-align:right">Залишок</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ef9a9a;text-align:right">Місяців</td>'
            '</tr>'
        )
        for i, item in enumerate(critical_items):
            bg    = '#fff8f8' if i % 2 == 0 else '#fff'
            color = '#b71c1c' if item['is_critical'] else '#e65100'
            html += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:7px 10px;font-family:monospace;font-weight:600">{item["sku"]}</td>'
                f'<td style="padding:7px 10px;color:#333">{item["name"]}</td>'
                f'<td style="padding:7px 10px;text-align:right">{item["stock"]}</td>'
                f'<td style="padding:7px 10px;text-align:right;font-weight:700;color:{color}">'
                f'{item["months_left"]}</td>'
                '</tr>'
            )
        html += '</table></div>'

    # ── overdue orders table ─────────────────────────────────────────────────
    if overdue_orders:
        html += (
            '<div style="padding:16px 24px 0">'
            '<h3 style="margin:0 0 10px;font-size:15px;color:#6a1b9a">&#128680; Прострочені дедлайни доставки</h3>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<tr style="background:#f3e5f5;color:#6a1b9a;font-weight:600">'
            '<td style="padding:7px 10px;border-bottom:2px solid #ce93d8">&#8470; замовлення</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ce93d8">Клієнт</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ce93d8">Дедлайн</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ce93d8;text-align:right">Прострочено</td>'
            '<td style="padding:7px 10px;border-bottom:2px solid #ce93d8">Статус</td>'
            '</tr>'
        )
        for i, o in enumerate(overdue_orders):
            bg    = '#fdf4ff' if i % 2 == 0 else '#fff'
            color = '#b71c1c' if o['days_late'] > 7 else '#e65100'
            html += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:7px 10px;font-family:monospace;font-weight:600">'
                f'<a href="/admin/sales/salesorder/{o["pk"]}/change/" style="color:#1976d2">'
                f'{o["order_number"]}</a></td>'
                f'<td style="padding:7px 10px">{o["client"]}</td>'
                f'<td style="padding:7px 10px">{o["deadline"].strftime("%d.%m.%Y")}</td>'
                f'<td style="padding:7px 10px;text-align:right;font-weight:700;color:{color}">'
                f'{o["days_late"]} дн.</td>'
                f'<td style="padding:7px 10px;color:#555">{o["status"]}</td>'
                '</tr>'
            )
        html += '</table></div>'

    # ── footer ───────────────────────────────────────────────────────────────
    html += (
        '<div style="padding:16px 24px;margin-top:20px;border-top:1px solid #eee;'
        'font-size:12px;color:#999">'
        'Minerva Business Intelligence &mdash; Автоматичне сповіщення'
        '</div>'
        '</div></body></html>'
    )
    return html


# ── Email sending ──────────────────────────────────────────────────────────────

def _send_email(ns, critical_items, overdue_orders, is_test=False):
    from django.core.mail import get_connection, EmailMultiAlternatives

    recipients = [e.strip() for e in (ns.email_to or '').split(',') if e.strip()]
    if not recipients:
        raise ValueError("Немає отримувачів (поле email_to порожнє)")

    # Subject
    if is_test:
        subject = '✅ Minerva — Тестове повідомлення (SMTP OK)'
    elif not critical_items and not overdue_orders:
        subject = '✅ Minerva — Немає алертів'
    else:
        parts = []
        if critical_items:
            parts.append(f'{len(critical_items)} critical stock')
        if overdue_orders:
            parts.append(f'{len(overdue_orders)} overdue orders')
        subject = '⚠️ Minerva Alert: ' + ', '.join(parts)

    # Company name
    try:
        from accounting.models import CompanySettings
        company_name = CompanySettings.get().name or 'Minerva'
    except Exception:
        company_name = 'Minerva'

    html_body = _build_html(critical_items, overdue_orders, company_name)
    from_email = (ns.email_from or ns.email_host_user or 'noreply@minerva.local').strip()

    connection = get_connection(
        backend='django.core.mail.backends.smtp.EmailBackend',
        host=ns.email_host,
        port=ns.email_port,
        username=ns.email_host_user,
        password=ns.email_host_password,
        use_tls=ns.email_use_tls,
        use_ssl=ns.email_use_ssl,
        fail_silently=False,
    )

    plain = (
        f'Minerva Alert\n'
        f'Critical stock: {len(critical_items)}\n'
        f'Overdue orders: {len(overdue_orders)}\n'
    )
    msg = EmailMultiAlternatives(
        subject=subject,
        body=plain,
        from_email=from_email,
        to=recipients,
        connection=connection,
    )
    msg.attach_alternative(html_body, 'text/html')
    msg.send()


# ── Public API ─────────────────────────────────────────────────────────────────

def run_alerts(force: bool = False, is_test: bool = False, test_channel: str | None = None) -> dict:
    """
    Main entry point.
    Returns dict with keys: sent (bool), reason/error (str), critical/overdue (int).
    """
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception as e:
        return {'sent': False, 'error': str(e)}

    email_active = ns.email_enabled and bool(
        [e.strip() for e in (ns.email_to or '').split(',') if e.strip()]
    )
    tg_active = (
        ns.telegram_enabled
        and bool(ns.telegram_bot_token)
        and bool(ns.telegram_chat_id)
    )
    if not email_active and not tg_active:
        return {'sent': False, 'reason': 'Жоден канал сповіщень не налаштований (email / Telegram)'}

    # Test mode — verify channels
    if is_test:
        email_result = {}
        tg_result    = {}
        if email_active and test_channel in (None, 'email'):
            try:
                _send_email(ns, [], [], is_test=True)
                email_result = {'sent': True}
            except Exception as e:
                email_result = {'error': str(e)}
        if tg_active and test_channel in (None, 'telegram'):
            try:
                tg_text = (
                    '✅ <b>Minerva — Тест Telegram (OK)</b>\n'
                    '<i>Канал налаштований правильно.</i>'
                )
                _send_telegram(ns, tg_text)
                tg_result = {'sent': True}
            except Exception as e:
                tg_result = {'error': str(e)}
        overall_sent = email_result.get('sent', False) or tg_result.get('sent', False)
        return {
            'sent':     overall_sent,
            'is_test':  True,
            'email':    email_result,
            'telegram': tg_result,
        }

    # Anti-spam interval check
    if not force and ns.last_alert_sent:
        hours_since = (timezone.now() - ns.last_alert_sent).total_seconds() / 3600
        if hours_since < ns.alert_min_interval_hours:
            remaining = ns.alert_min_interval_hours - hours_since
            return {
                'sent': False,
                'reason': f'Занадто рано: наступне сповіщення через {remaining:.1f} год.'
                          f' (інтервал: {ns.alert_min_interval_hours} год.)',
            }

    critical_items = _get_critical_stock()   if ns.stock_alerts_enabled    else []
    overdue_orders = _get_overdue_orders(ns.deadline_overdue_days) if ns.deadline_alerts_enabled else []

    if not critical_items and not overdue_orders:
        return {'sent': False, 'reason': 'Немає алертів для надсилання', 'ok': True}

    # Company name (for both channels)
    try:
        from accounting.models import CompanySettings
        company_name = CompanySettings.get().name or 'Minerva'
    except Exception:
        company_name = 'Minerva'

    email_result = {}
    tg_result    = {}

    # ── Email ──────────────────────────────────────────────────────────────────
    if email_active:
        try:
            _send_email(ns, critical_items, overdue_orders)
            email_result = {'sent': True}
        except Exception as e:
            email_result = {'error': str(e)}

    # ── Telegram ───────────────────────────────────────────────────────────────
    if tg_active:
        try:
            tg_text = _build_telegram_text(critical_items, overdue_orders, company_name)
            _send_telegram(ns, tg_text)
            tg_result = {'sent': True}
        except Exception as e:
            tg_result = {'error': str(e)}

    overall_sent = email_result.get('sent', False) or tg_result.get('sent', False)
    if overall_sent:
        ns.last_alert_sent = timezone.now()
        ns.save(update_fields=['last_alert_sent'])

    return {
        'sent':     overall_sent,
        'critical': len(critical_items),
        'overdue':  len(overdue_orders),
        'email':    email_result,
        'telegram': tg_result,
    }


# ── Event-based notifications (real-time, triggered by signals) ────────────────

def notify_sync_result(source: str, stats: dict, force_notify: bool = False):
    """
    Надсилає підсумок після синхронізації з деталями по кожному запису.

    source — назва джерела: 'DigiKey', 'Авто-трекінг відправлень', тощо
    stats  — dict:
        created  : int
        updated  : int
        errors   : list[str]
        changes  : list[dict]  — деталі змін:
            { order: str, client: str, old_status: str, new_status: str,
              tracking: str (опц.), extra: str (опц.) }
    """
    ns = _get_ns()
    if not ns:
        return

    send_email = ns.email_enabled and ns.sync_result_email
    send_tg    = ns.telegram_enabled and ns.sync_result_telegram
    if not send_email and not send_tg:
        return

    created  = stats.get("created", 0)
    updated  = stats.get("updated", 0)
    errors   = stats.get("errors", [])
    changes  = stats.get("changes", [])
    n_errors = len(errors) if isinstance(errors, list) else int(errors)

    if created == 0 and updated == 0 and n_errors == 0:
        if not force_notify and getattr(ns, 'sync_skip_if_no_changes', True):
            return

    now_str = timezone.now().strftime("%d.%m.%Y %H:%M")
    subject = f"⚙️ Minerva: {source} — {created} нових, {updated} оновлено"

    STATUS_COLORS_HEX = {
        'received':   '#2196f3',
        'processing': '#ff9800',
        'shipped':    '#1976d2',
        'delivered':  '#4caf50',
        'cancelled':  '#f44336',
        'in_transit': '#2196f3',
        'label_ready':'#ff9800',
        'submitted':  '#607d8b',
    }
    STATUS_UA = {
        'received':   'Отримано',
        'processing': 'В обробці',
        'shipped':    'Відправлено',
        'delivered':  'Доставлено',
        'cancelled':  'Скасовано',
        'in_transit': 'В дорозі',
        'label_ready':'Мітка готова',
        'submitted':  'Підтверджено',
    }

    if send_email:
        try:
            # ── summary row ──────────────────────────────────────────────────
            summary = ""
            for label, val, color in [
                ("Нових",    created,  "#4caf50"),
                ("Оновлено", updated,  "#2196f3"),
                ("Помилок",  n_errors, "#f44336"),
            ]:
                if val:
                    summary += (
                        f'<span style="display:inline-block;margin:0 8px 4px 0;'
                        f'background:#162030;padding:4px 12px;border-radius:12px;'
                        f'font-size:13px;color:{color};font-weight:700">'
                        f'{label}: {val}</span>'
                    )

            # ── changes table ─────────────────────────────────────────────────
            detail_html = ""
            if changes:
                rows = ""
                for i, ch in enumerate(changes):
                    bg       = "#1a2a38" if i % 2 == 0 else "#1e2a35"
                    new_col  = STATUS_COLORS_HEX.get(ch.get("new_status",""), "#c9d8e4")
                    old_lbl  = STATUS_UA.get(ch.get("old_status",""), ch.get("old_status","—"))
                    new_lbl  = STATUS_UA.get(ch.get("new_status",""), ch.get("new_status","—"))
                    tracking = ch.get("tracking","")
                    extra    = ch.get("extra","")
                    rows += (
                        f'<tr style="background:{bg}">'
                        f'<td style="padding:7px 12px;font-family:monospace;color:#64b5f6;white-space:nowrap">'
                        f'{ch.get("order","—")}</td>'
                        f'<td style="padding:7px 12px;color:#c9d8e4">{ch.get("client","—")}</td>'
                        f'<td style="padding:7px 12px;color:#607d8b;white-space:nowrap">'
                        f'{old_lbl} →</td>'
                        f'<td style="padding:7px 12px;font-weight:700;color:{new_col};white-space:nowrap">'
                        f'{new_lbl}</td>'
                        f'<td style="padding:7px 12px;font-family:monospace;color:#9aafbe;font-size:11px">'
                        f'{tracking or extra or ""}</td>'
                        f'</tr>'
                    )
                detail_html = (
                    '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px">'
                    '<tr style="background:#0d1117;color:#607d8b;font-size:11px">'
                    '<th style="padding:6px 12px;text-align:left">Замовлення</th>'
                    '<th style="padding:6px 12px;text-align:left">Клієнт</th>'
                    '<th style="padding:6px 12px;text-align:left">Старий</th>'
                    '<th style="padding:6px 12px;text-align:left">Новий</th>'
                    '<th style="padding:6px 12px;text-align:left">Трекінг</th>'
                    '</tr>'
                    + rows +
                    '</table>'
                )

            html = (
                '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
                '<body style="font-family:Arial,sans-serif;background:#0d1117;margin:0;padding:16px">'
                '<div style="max-width:640px;margin:0 auto;background:#1e2a35;'
                'border-radius:8px;overflow:hidden;border:1px solid #2a3f52">'
                '<div style="background:#162030;padding:14px 20px;border-bottom:1px solid #2a3f52">'
                f'<div style="font-size:15px;font-weight:700;color:#c9d8e4">⚙️ {source}</div>'
                f'<div style="font-size:11px;color:#607d8b">Minerva · {now_str}</div>'
                '</div>'
                f'<div style="padding:14px 20px">{summary}{detail_html}</div>'
                '<div style="padding:10px 20px;border-top:1px solid #162030;'
                'font-size:11px;color:#455a64">Minerva Business Intelligence</div>'
                '</div></body></html>'
            )
            _send_event_email(ns, subject, html)
        except Exception:
            pass

    if send_tg:
        try:
            _cname = _get_company_name()
            lines = [f'🏛️ <b>{_cname}</b>', f'⚙️ <b>{source}</b>', f'<i>{now_str}</i>']
            if created:
                lines.append(f'➕ Нових: <b>{created}</b>')
            if updated:
                lines.append(f'🔄 Оновлено: <b>{updated}</b>')
            if n_errors:
                lines.append(f'❌ Помилок: <b>{n_errors}</b>')
            if changes:
                lines.append('')
                for ch in changes[:20]:   # обмеження Telegram
                    old_lbl = STATUS_UA.get(ch.get("old_status",""), ch.get("old_status","?"))
                    new_lbl = STATUS_UA.get(ch.get("new_status",""), ch.get("new_status","?"))
                    tracking = ch.get("tracking","")
                    line = (
                        f'<code>{ch.get("order","—")}</code> {ch.get("client","")}\n'
                        f'  {old_lbl} → <b>{new_lbl}</b>'
                    )
                    if tracking:
                        line += f'\n  🔍 <code>{tracking}</code>'
                    lines.append(line)
                if len(changes) > 20:
                    lines.append(f'<i>... і ще {len(changes)-20}</i>')
            _send_telegram(ns, '\n'.join(lines))
        except Exception:
            pass


def notify_digikey_auto_confirmed(sale, mode: str):
    """Надсилає сповіщення після успішного авто-підтвердження DigiKey замовлення."""
    ns = _get_ns()
    if not ns:
        return

    send_tg    = ns.telegram_enabled and ns.new_order_telegram
    send_email = ns.email_enabled    and ns.new_order_email
    if not send_tg and not send_email:
        return

    MODE_LABELS = {
        'always':   'всі замовлення (always)',
        'in_stock': 'є на складі (in_stock)',
    }
    mode_label = MODE_LABELS.get(mode, mode)
    client = sale.client or sale.email or '—'

    # Collect product lines
    lines_data = []
    try:
        for line in sale.lines.select_related('product').all():
            p = line.product
            lines_data.append({
                'sku':       (p.sku if p else line.sku_raw) or '—',
                'name':      (p.name if p else '') or '—',
                'qty':       line.qty,
                'datasheet': (p.datasheet_url if p else '') or '',
            })
    except Exception:
        pass

    if send_tg:
        try:
            _cname = _get_company_name()
            tg = [
                f'🏛️ <b>{_cname}</b>',
                f'✅ <b>DigiKey: авто-підтверджено</b>',
                '',
                f'Замовлення: <code>{sale.order_number}</code>',
                f'Клієнт: <b>{client}</b>',
                f'🤖 Система підтвердила автоматично <i>({mode_label})</i>',
                f'Статус → <b>В обробці</b>',
            ]
            if lines_data:
                tg.append('')
                tg.append('📋 <b>Товари:</b>')
                for ld in lines_data:
                    name_part = f' {ld["name"]}' if ld['name'] not in ('—', '') else ''
                    line_txt  = f'• <code>{ld["sku"]}</code>{name_part} × {ld["qty"]}'
                    if ld.get('datasheet'):
                        line_txt += f'\n  <a href="{ld["datasheet"]}">📄 Datasheet</a>'
                    tg.append(line_txt)
            _send_telegram(ns, '\n'.join(tg))
        except Exception:
            pass

    if send_email:
        try:
            lines_html = ''
            if lines_data:
                rows = ''
                for ld in lines_data:
                    ds = ld.get('datasheet', '')
                    sku_cell = (
                        f'<a href="{ds}" style="color:#1565c0;font-family:monospace;font-size:12px;'
                        f'text-decoration:none">{ld["sku"]} 📄</a>'
                        if ds else
                        f'<span style="font-family:monospace;font-size:12px;color:#1565c0">{ld["sku"]}</span>'
                    )
                    rows += (
                        f'<tr style="border-bottom:1px solid #eee">'
                        f'<td style="padding:5px 8px">{sku_cell}</td>'
                        f'<td style="padding:5px 8px;font-size:13px;color:#333">{ld["name"]}</td>'
                        f'<td style="padding:5px 8px;text-align:center;color:#555">{ld["qty"]}</td>'
                        f'</tr>'
                    )
                lines_html = (
                    '<div style="margin-top:12px"><b style="font-size:13px">📋 Товари:</b>'
                    '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">'
                    '<tr style="background:#f0f4f8;color:#555;font-size:11px">'
                    '<th style="padding:5px 8px;text-align:left">SKU</th>'
                    '<th style="padding:5px 8px;text-align:left">Назва</th>'
                    '<th style="padding:5px 8px;text-align:center">К-сть</th>'
                    '</tr>' + rows + '</table></div>'
                )
            extra = (
                f'<br><b>🤖 Підтверджено:</b> автоматично системою'
                f' <span style="color:#555;font-size:12px">({mode_label})</span>'
                f'<br><b>Статус:</b> → <b style="color:#f57c00">В обробці</b>'
            )
            if lines_html:
                extra += f'<br>{lines_html}'
            html = _order_email_html(
                sale, '✅ DigiKey: авто-підтверджено', '#2e7d32', extra
            )
            _send_event_email(ns, f'✅ DigiKey авто-підтверджено: {sale.order_number}', html)
        except Exception:
            pass


STATUS_LABELS = {
    'received':   'Отримано (нове)',
    'processing': 'В обробці',
    'shipped':    'Відправлено',
    'delivered':  'Доставлено',
    'cancelled':  'Скасовано',
}


def _get_ns():
    """Return NotificationSettings singleton or None."""
    try:
        from config.models import NotificationSettings
        return NotificationSettings.get()
    except Exception:
        return None


def _smtp_connection(ns):
    from django.core.mail import get_connection
    return get_connection(
        backend='django.core.mail.backends.smtp.EmailBackend',
        host=ns.email_host,
        port=ns.email_port,
        username=ns.email_host_user,
        password=ns.email_host_password,
        use_tls=ns.email_use_tls,
        use_ssl=ns.email_use_ssl,
        fail_silently=False,
    )


def _send_event_email(ns, subject, html_body):
    """Send a simple event email via SMTP."""
    from django.core.mail import EmailMultiAlternatives
    recipients = [e.strip() for e in (ns.email_to or '').split(',') if e.strip()]
    if not recipients:
        return
    from_email = (ns.email_from or ns.email_host_user or 'noreply@minerva.local').strip()
    msg = EmailMultiAlternatives(
        subject=subject,
        body='',
        from_email=from_email,
        to=recipients,
        connection=_smtp_connection(ns),
    )
    msg.attach_alternative(html_body, 'text/html')
    msg.send()


def _order_email_html(order, title, color, body_extra=''):
    """Compact HTML email block for a single order event."""
    now_str = timezone.now().strftime('%d.%m.%Y %H:%M')
    client  = order.client or order.email or '—'
    total   = ''
    try:
        from django.db.models import Sum
        t = order.lines.aggregate(s=Sum('total_price'))['s']
        if t:
            total = f'<br><span style="color:#aaa">Сума: <b>{t}</b></span>'
    except Exception:
        pass
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,sans-serif;background:#f0f2f5;margin:0;padding:20px">'
        '<div style="max-width:580px;margin:0 auto;background:#fff;border-radius:8px;'
        'overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.12)">'
        f'<div style="background:{color};color:#fff;padding:16px 24px">'
        f'<div style="font-size:17px;font-weight:700">{title}</div>'
        f'<div style="font-size:12px;opacity:.75">Minerva · {now_str}</div>'
        '</div>'
        '<div style="padding:16px 24px;font-size:14px;color:#333">'
        f'<b>Замовлення:</b> <code>{order.order_number}</code><br>'
        f'<b>Клієнт:</b> {client}<br>'
        f'<b>Джерело:</b> {order.source}'
        f'{total}'
        f'{body_extra}'
        '</div>'
        f'<div style="padding:10px 24px;background:#f5f5f5;font-size:12px;color:#999">'
        f'Minerva Business Intelligence — автоматичне сповіщення</div>'
        '</div></body></html>'
    )


def notify_new_order(order):
    """Send new-order notification via email and/or Telegram."""
    ns = _get_ns()
    if not ns:
        return

    send_email = ns.email_enabled and ns.new_order_email
    send_tg    = ns.telegram_enabled and ns.new_order_telegram
    if not send_email and not send_tg:
        return

    client  = order.client or order.email or '—'
    subject = f'🆕 Minerva: Нове замовлення {order.order_number}'

    # ── Enriched data ───────────────────────────────────────────────────────
    lines_data = []
    try:
        from django.db.models import Sum
        from inventory.models import InventoryTransaction
        for line in order.lines.select_related('product').all():
            if not line.product:
                lines_data.append({
                    'sku': line.sku_raw or '—', 'name': '—',
                    'qty': line.qty, 'in_stock': None, 'stock': 0,
                    'datasheet': '',
                })
                continue
            stock = (
                InventoryTransaction.objects.filter(product=line.product)
                .aggregate(t=Sum('qty'))['t'] or 0
            )
            lines_data.append({
                'sku':       line.product.sku,
                'name':      line.product.name,
                'qty':       line.qty,
                'stock':     int(stock),
                'in_stock':  stock >= line.qty,
                'datasheet': line.product.datasheet_url or '',
            })
    except Exception:
        pass

    # Deadline countdown
    deadline_str  = ''
    days_left_str = ''
    days_left     = None
    if order.shipping_deadline:
        deadline_str = order.shipping_deadline.strftime('%d.%m.%Y')
        days_left    = (order.shipping_deadline - timezone.now().date()).days
        if days_left > 1:
            days_left_str = f'{days_left} дн.'
        elif days_left == 1:
            days_left_str = 'Завтра ⚠️'
        elif days_left == 0:
            days_left_str = 'Сьогодні! ⚠️'
        else:
            days_left_str = f'Прострочено ({-days_left} дн.) 🔴'

    # Destination
    dest_parts  = [p for p in [order.addr_city, order.addr_country] if p]
    destination = ', '.join(dest_parts) if dest_parts else ''

    # Total
    total_str = ''
    try:
        from django.db.models import Sum as _Sum
        t = order.lines.aggregate(s=_Sum('total_price'))['s']
        if t:
            currency  = getattr(order, 'currency', '') or ''
            total_str = f'{t} {currency}'.strip()
    except Exception:
        pass

    if send_email:
        try:
            # Products table
            lines_html = ''
            if lines_data:
                rows = ''
                for ld in lines_data:
                    if ld['in_stock'] is True:
                        stock_cell = f'<span style="color:#2e7d32">✅ {ld["stock"]} шт</span>'
                    elif ld['in_stock'] is False:
                        stock_cell = (
                            f'<span style="color:#c62828">❌ є: {ld["stock"]} шт, '
                            f'потрібно: {ld["qty"]}</span>'
                        )
                    else:
                        stock_cell = '—'
                    ds = ld.get('datasheet', '')
                    sku_cell = (
                        f'<a href="{ds}" style="color:#1565c0;font-family:monospace;font-size:12px;'
                        f'text-decoration:none" title="📄 Datasheet">{ld["sku"]} 📄</a>'
                        if ds else
                        f'<span style="font-family:monospace;font-size:12px;color:#1565c0">{ld["sku"]}</span>'
                    )
                    rows += (
                        f'<tr style="border-bottom:1px solid #eee">'
                        f'<td style="padding:5px 8px">{sku_cell}</td>'
                        f'<td style="padding:5px 8px;font-size:13px;color:#333">{ld["name"]}</td>'
                        f'<td style="padding:5px 8px;text-align:center;color:#555">{ld["qty"]}</td>'
                        f'<td style="padding:5px 8px;text-align:right;font-size:12px">{stock_cell}</td>'
                        f'</tr>'
                    )
                lines_html = (
                    '<div style="margin-top:12px">'
                    '<b style="font-size:13px">📋 Товари:</b>'
                    '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">'
                    '<tr style="background:#f0f4f8;color:#555;font-size:11px">'
                    '<th style="padding:5px 8px;text-align:left">SKU</th>'
                    '<th style="padding:5px 8px;text-align:left">Назва</th>'
                    '<th style="padding:5px 8px;text-align:center">Кіл-ть</th>'
                    '<th style="padding:5px 8px;text-align:right">Склад</th>'
                    '</tr>'
                    + rows +
                    '</table></div>'
                )

            extra = ''
            if total_str:
                extra += f'<br><b>Сума:</b> {total_str}'
            if destination:
                extra += f'<br><b>📍 Куди:</b> {destination}'
            if order.contact_name and order.contact_name != client:
                extra += f'<br><b>Контакт:</b> {order.contact_name}'
            if deadline_str:
                dl_color = '#c62828' if days_left is not None and days_left <= 2 else '#333'
                extra += (
                    f'<br><b>📦 Дедлайн відправки:</b> {deadline_str}'
                    + (f' <span style="color:{dl_color}">({days_left_str})</span>'
                       if days_left_str else '')
                )
            if lines_html:
                extra += f'<br>{lines_html}'

            html = _order_email_html(order, '🆕 Нове замовлення', '#1565c0', extra)
            _send_event_email(ns, subject, html)
        except Exception:
            pass

    if send_tg:
        try:
            _cname = _get_company_name()
            tg = [
                f'🏛️ <b>{_cname}</b>',
                f'🆕 <b>Нове замовлення</b>',
                f'<i>{_cname} · {timezone.now().strftime("%d.%m.%Y %H:%M")}</i>',
                '',
                f'Замовлення: <code>{order.order_number}</code>',
                f'Клієнт: <b>{client}</b>',
                f'Джерело: {order.source}',
            ]
            if destination:
                tg.append(f'📍 Куди: {destination}')
            if order.contact_name and order.contact_name != client:
                tg.append(f'Контакт: {order.contact_name}')
            if deadline_str:
                tg.append(f'📦 Відправити до: <b>{deadline_str}</b> ({days_left_str})')
            if total_str:
                tg.append(f'💰 Сума: <b>{total_str}</b>')
            if lines_data:
                tg.append('')
                tg.append('📋 <b>Товари:</b>')
                for ld in lines_data:
                    icon = '✅' if ld['in_stock'] is True else ('❌' if ld['in_stock'] is False else '•')
                    name_part  = f' {ld["name"]}' if ld['name'] != '—' else ''
                    stock_part = f' | склад: {ld["stock"]} шт' if ld['in_stock'] is not None else ''
                    line_txt = f'{icon} <code>{ld["sku"]}</code>{name_part} × {ld["qty"]}{stock_part}'
                    if ld.get('datasheet'):
                        line_txt += f'\n  <a href="{ld["datasheet"]}">📄 Datasheet</a>'
                    tg.append(line_txt)
            _send_telegram(ns, '\n'.join(tg))
        except Exception:
            pass


def notify_status_change(order, old_status, new_status):
    """Send status-change notification via email and/or Telegram."""
    ns = _get_ns()
    if not ns:
        return

    # Check if this status transition is enabled
    status_notify_map = {
        'processing': ns.notify_on_processing,
        'shipped':    ns.notify_on_shipped,
        'delivered':  ns.notify_on_delivered,
        'cancelled':  ns.notify_on_cancelled,
    }
    if not status_notify_map.get(new_status, False):
        return

    send_email = ns.email_enabled and ns.status_change_email
    send_tg    = ns.telegram_enabled and ns.status_change_telegram
    if not send_email and not send_tg:
        return

    old_label = STATUS_LABELS.get(old_status, old_status)
    new_label = STATUS_LABELS.get(new_status, new_status)
    client    = order.client or order.email or '—'

    status_colors = {
        'processing': '#f57c00',
        'shipped':    '#1976d2',
        'delivered':  '#2e7d32',
        'cancelled':  '#c62828',
    }
    color   = status_colors.get(new_status, '#455a64')
    subject = f'🔄 Minerva: {order.order_number} → {new_label}'

    # Extra context: manual DigiKey confirm + product lines
    is_dk_manual = (
        new_status == 'processing'
        and 'digikey' in (getattr(order, 'source', '') or '').lower()
    )
    prod_lines_data = []
    if is_dk_manual:
        try:
            for line in order.lines.select_related('product').all():
                p = line.product
                prod_lines_data.append({
                    'sku':       (p.sku if p else line.sku_raw) or '—',
                    'name':      (p.name if p else '') or '—',
                    'qty':       line.qty,
                    'datasheet': (p.datasheet_url if p else '') or '',
                })
        except Exception:
            pass

    if send_email:
        try:
            extra = f'<br><b>Статус:</b> {old_label} → <b>{new_label}</b>'
            if is_dk_manual:
                extra += '<br><b>✋ Підтверджено:</b> вручну на DigiKey'
            if new_status == 'shipped' and order.tracking_number:
                extra += f'<br><b>Трекінг:</b> <code>{order.tracking_number}</code>'
            if prod_lines_data:
                rows = ''
                for ld in prod_lines_data:
                    ds = ld.get('datasheet', '')
                    sku_cell = (
                        f'<a href="{ds}" style="color:#1565c0;font-family:monospace;font-size:12px;'
                        f'text-decoration:none">{ld["sku"]} 📄</a>'
                        if ds else
                        f'<span style="font-family:monospace;font-size:12px;color:#1565c0">{ld["sku"]}</span>'
                    )
                    rows += (
                        f'<tr style="border-bottom:1px solid #eee">'
                        f'<td style="padding:5px 8px">{sku_cell}</td>'
                        f'<td style="padding:5px 8px;font-size:13px;color:#333">{ld["name"]}</td>'
                        f'<td style="padding:5px 8px;text-align:center;color:#555">{ld["qty"]}</td>'
                        f'</tr>'
                    )
                extra += (
                    '<div style="margin-top:12px"><b style="font-size:13px">📋 Товари:</b>'
                    '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">'
                    '<tr style="background:#f0f4f8;color:#555;font-size:11px">'
                    '<th style="padding:5px 8px;text-align:left">SKU</th>'
                    '<th style="padding:5px 8px;text-align:left">Назва</th>'
                    '<th style="padding:5px 8px;text-align:center">К-сть</th>'
                    '</tr>' + rows + '</table></div>'
                )
            html = _order_email_html(order, f'🔄 {new_label}', color, extra)
            _send_event_email(ns, subject, html)
        except Exception:
            pass

    if send_tg:
        try:
            _cname = _get_company_name()
            lines = [
                f'🏛️ <b>{_cname}</b>',
                f'🔄 <b>Зміна статусу</b>',
                f'<code>{order.order_number}</code> · {order.source}',
                f'Клієнт: {client}',
                f'Статус: {old_label} → <b>{new_label}</b>',
            ]
            if is_dk_manual:
                lines.append('✋ Підтверджено вручну на DigiKey')
            if new_status == 'shipped' and order.tracking_number:
                lines.append(f'Трекінг: <code>{order.tracking_number}</code>')
            if prod_lines_data:
                lines.append('')
                lines.append('📋 <b>Товари:</b>')
                for ld in prod_lines_data:
                    line_txt = f'• <code>{ld["sku"]}</code> {ld["name"]} × {ld["qty"]}'
                    if ld.get('datasheet'):
                        line_txt += f'\n  <a href="{ld["datasheet"]}">📄 Datasheet</a>'
                    lines.append(line_txt)
            _send_telegram(ns, '\n'.join(lines))
        except Exception:
            pass
