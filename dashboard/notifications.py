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
                .exclude(tx_type=InventoryTransaction.TxType.RESERVED)
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


def _send_telegram_photo(ns, photo_url, caption):
    """Send a photo via Telegram Bot API (caption max 1024 chars)."""
    url  = f'https://api.telegram.org/bot{ns.telegram_bot_token}/sendPhoto'
    if len(caption) > 1024:
        caption = caption[:1020] + '\n...'
    data = json.dumps({
        'chat_id':    ns.telegram_chat_id,
        'photo':      photo_url,
        'caption':    caption,
        'parse_mode': 'HTML',
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
                    extra    = ch.get("extra","")
                    line = (
                        f'<code>{ch.get("order","—")}</code> {ch.get("client","")}\n'
                        f'  {old_lbl} → <b>{new_lbl}</b>'
                    )
                    if tracking:
                        line += f'\n  🔍 <code>{tracking}</code>'
                    if extra:
                        line += f'\n  🔌 <i>{extra}</i>'
                    lines.append(line)
                if len(changes) > 20:
                    lines.append(f'<i>... і ще {len(changes)-20}</i>')
            _send_telegram(ns, '\n'.join(lines))
        except Exception:
            pass


def notify_digikey_auto_confirmed(sale, mode: str, raw_order: dict = None):
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
    now_str = timezone.now().strftime('%d.%m.%Y %H:%M')

    # Country label
    _COUNTRY_NAMES = {
        'AT':'Австрія','BE':'Бельгія','BG':'Болгарія','CH':'Швейцарія','CY':'Кіпр',
        'CZ':'Чехія','DE':'Німеччина','DK':'Данія','EE':'Естонія','ES':'Іспанія',
        'FI':'Фінляндія','FR':'Франція','GB':'Велика Британія','GR':'Греція',
        'HR':'Хорватія','HU':'Угорщина','IE':'Ірландія','IT':'Італія','LT':'Литва',
        'LU':'Люксембург','LV':'Латвія','MT':'Мальта','NL':'Нідерланди','NO':'Норвегія',
        'PL':'Польща','PT':'Португалія','RO':'Румунія','SE':'Швеція','SI':'Словенія',
        'SK':'Словаччина','UA':'Україна','US':'США','CA':'Канада','AU':'Австралія',
        'JP':'Японія','CN':'Китай','TR':'Туреччина',
    }
    _cc = (sale.addr_country or '').strip().upper()
    destination = _COUNTRY_NAMES.get(_cc, _cc)

    # Deadline
    deadline_str, days_left_str, days_left = '', '', None
    if sale.shipping_deadline:
        deadline_str = sale.shipping_deadline.strftime('%d.%m.%Y')
        days_left = (sale.shipping_deadline - timezone.now().date()).days
        days_left_str = (
            f'{days_left} дн.' if days_left > 1 else
            'Завтра ⚠️' if days_left == 1 else
            'Сьогодні! ⚠️' if days_left == 0 else
            f'Прострочено ({-days_left} дн.) 🔴'
        )

    # CRM order count
    crm_orders = None
    try:
        cust = sale.crm_customer
        if not cust:
            from crm.models import Customer as _Cust
            _cn = (sale.client or '').strip()
            if _cn:
                cust = (
                    _Cust.objects.filter(company__iexact=_cn).first()
                    or _Cust.objects.filter(name__iexact=_cn).first()
                )
        if cust:
            crm_orders = cust.total_orders()
    except Exception:
        pass

    # Collect product lines — prefer DB lines, fall back to raw DigiKey data
    lines_data = []
    try:
        for line in sale.lines.select_related('product').all():
            p = line.product
            img = ''
            try:
                img = _abs_url(p.image_display_url() or '') if p else ''
            except Exception:
                pass
            lines_data.append({
                'sku':       (p.sku if p else line.sku_raw) or '—',
                'name':      (p.name if p else '') or '—',
                'qty':       line.qty,
                'datasheet': (p.datasheet_url if p else '') or '',
                'image':     img,
                'in_stock':  None,
                'unit_price': float(line.unit_price or 0),
                'currency':  line.currency or '',
            })
    except Exception:
        pass

    # Fallback: build lines_data from raw DigiKey API response
    if not lines_data and raw_order:
        try:
            for detail in (raw_order.get('orderDetails') or []):
                sku  = (detail.get('supplierSku') or detail.get('manufacturerPartNumber') or '').strip()
                name = (detail.get('productDescription') or '').strip()
                qty  = detail.get('quantity') or detail.get('adjustedQuantity') or 0
                up   = detail.get('unitPrice')
                lines_data.append({
                    'sku':       sku or '—',
                    'name':      name or '—',
                    'qty':       qty,
                    'datasheet': '',
                    'image':     '',
                    'in_stock':  None,
                    'unit_price': float(up) if up else 0,
                    'currency':  sale.currency or '',
                })
        except Exception:
            pass

    # Total
    total_str = ''
    try:
        from django.db.models import Sum as _Sum
        t = sale.lines.aggregate(s=_Sum('total_price'))['s']
        if t:
            total_str = f'{t} {sale.currency or ""}'.strip()
        elif raw_order:
            t = raw_order.get('totalPrice')
            if t:
                total_str = f'{t} {sale.currency or ""}'.strip()
    except Exception:
        pass

    if send_tg:
        try:
            _cname = _get_company_name()
            tg = [f'🏛️ <b>{_cname}</b>']
            tg.append(f'✅ <b>DigiKey: авто-підтверджено</b> · <i>{now_str}</i>')
            tg.append('')
            tg.append(f'📋 <code>{sale.order_number}</code> · digikey')
            tg.append(f'👤 <b>{client}</b>')
            if crm_orders is not None:
                tg.append(f'   📊 Замовлень всього: <b>{crm_orders}</b>')
            if destination:
                tg.append(f'📍 {destination}')
            if deadline_str:
                dl_warn = ' ⚠️' if days_left is not None and days_left <= 2 else ''
                tg.append(f'📦 Дедлайн: <b>{deadline_str}</b> ({days_left_str}){dl_warn}')
            if total_str:
                tg.append(f'💰 <b>{total_str}</b>')
            tg.append(f'🤖 <i>Підтверджено автоматично ({mode_label})</i>')

            if lines_data:
                tg.append('')
                tg.append('📦 <b>Товари:</b>')
                for ld in lines_data:
                    name_part = f' — {ld["name"]}' if ld['name'] not in ('—', '', None) else ''
                    qty_val = ld["qty"] or 0
                    qty_str = str(int(qty_val)) if qty_val and float(qty_val) == int(float(qty_val)) else str(qty_val)
                    curr = ld.get('currency', '')
                    line_lines = [f'• <code>{ld["sku"]}</code>{name_part}']
                    line_lines.append(f'   📦 × <b>{qty_str} шт</b>')
                    if ld.get('unit_price'):
                        line_lines.append(f'   💵 {ld["unit_price"]:.2f} {curr}/шт'.strip())
                    if ld.get('datasheet'):
                        line_lines.append(f'   📄 <a href="{ld["datasheet"]}">Datasheet</a>')
                    tg.append('\n'.join(line_lines))

            tg_text = '\n'.join(tg)
            first_image = next(
                (ld['image'] for ld in lines_data if ld.get('image', '').startswith('http')),
                ''
            )
            if first_image:
                try:
                    _send_telegram_photo(ns, first_image, tg_text)
                except Exception:
                    _send_telegram(ns, tg_text)
            else:
                _send_telegram(ns, tg_text)
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
                    img_url = ld.get('image', '')
                    img_cell = (
                        f'<td style="padding:5px 8px;text-align:center;width:44px">'
                        f'<img src="{img_url}" width="38" height="38" style="object-fit:cover;'
                        f'border-radius:4px;display:block;margin:auto" onerror="this.style.display=\'none\'">'
                        f'</td>'
                        if img_url and img_url.startswith('http') else
                        '<td style="width:44px"></td>'
                    )
                    rows += (
                        f'<tr style="border-bottom:1px solid #eee">'
                        f'{img_cell}'
                        f'<td style="padding:5px 8px">{sku_cell}</td>'
                        f'<td style="padding:5px 8px;font-size:13px;color:#333">{ld["name"]}</td>'
                        f'<td style="padding:5px 8px;text-align:center;color:#555">{ld["qty"]}</td>'
                        f'</tr>'
                    )
                lines_html = (
                    '<div style="margin-top:12px"><b style="font-size:13px">📋 Товари:</b>'
                    '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">'
                    '<tr style="background:#f0f4f8;color:#555;font-size:11px">'
                    '<th style="padding:5px 8px;width:44px"></th>'
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


def _abs_url(url: str) -> str:
    """Make a relative /media/... URL absolute using the first HTTPS trusted origin."""
    if not url or url.startswith('http'):
        return url
    try:
        from django.conf import settings as _ds
        origins = getattr(_ds, 'CSRF_TRUSTED_ORIGINS', [])
        base = next((o for o in origins if o.startswith('https://')), '')
        if not base:
            base = next((o for o in origins if o.startswith('http://')), '')
        return base.rstrip('/') + url if base else url
    except Exception:
        return url


def _order_email_html(order, title, color, body_extra='', show_total: bool = True):
    """Compact HTML email block for a single order event."""
    now_str = timezone.now().strftime('%d.%m.%Y %H:%M')
    client  = order.client or order.email or '—'
    total   = ''
    if show_total:
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


def notify_new_order(order, is_test: bool = False):
    """Send new-order notification via email and/or Telegram."""
    ns = _get_ns()
    if not ns:
        return

    send_email = ns.email_enabled and ns.new_order_email
    send_tg    = ns.telegram_enabled and ns.new_order_telegram
    if not send_email and not send_tg:
        return

    client  = order.client or order.email or '—'
    subject = (
        f'🧪 ТЕСТ — Нове замовлення {order.order_number}'
        if is_test else
        f'🆕 Minerva: Нове замовлення {order.order_number}'
    )

    # ── Enriched data ───────────────────────────────────────────────────────
    lines_data = []
    _IT = _LP = None
    try:
        from inventory.models import InventoryTransaction as _IT, Product as _LP
    except Exception:
        pass
    try:
        order_lines = list(order.lines.select_related('product').all())
    except Exception:
        order_lines = []
    from django.db.models import Sum as _LSum
    for line in order_lines:
        try:
            product = getattr(line, 'product', None)
            if not product and _LP and getattr(line, 'sku_raw', None):
                product = _LP.objects.filter(sku=line.sku_raw).first()
            stock    = 0
            in_stock = None
            if product and _IT:
                stock    = int(_IT.objects.filter(product=product).aggregate(t=_LSum('qty'))['t'] or 0)
                in_stock = stock >= (line.qty or 0)
            img = ''
            if product:
                # Prefer external image_url (reachable by email clients + Telegram)
                ext_img = getattr(product, 'image_url', '') or ''
                if ext_img and ext_img.startswith('http'):
                    img = ext_img
                elif hasattr(product, 'image_display_url'):
                    try:
                        img = _abs_url(product.image_display_url() or '')
                    except Exception:
                        pass
            name_val = getattr(product, 'name', None) or ''
            if not name_val and product:
                name_val = getattr(product, 'description', None) or ''
            lines_data.append({
                'sku':        getattr(product, 'sku', None) or getattr(line, 'sku_raw', None) or '—',
                'name':       name_val or '—',
                'qty':        line.qty or 0,
                'stock':      stock,
                'in_stock':   in_stock,
                'datasheet':  getattr(product, 'datasheet_url', '') or '',
                'image':      img,
                'unit_price': float(line.unit_price or 0),
                'line_total': float(line.total_price or (
                    (line.unit_price or 0) * (line.qty or 0)
                )),
                'currency':   line.currency or getattr(order, 'currency', '') or '',
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

    # Destination — full country name
    _COUNTRY_NAMES = {
        'AT':'Австрія','BE':'Бельгія','BG':'Болгарія','CH':'Швейцарія','CY':'Кіпр',
        'CZ':'Чехія','DE':'Німеччина','DK':'Данія','EE':'Естонія','ES':'Іспанія',
        'FI':'Фінляндія','FR':'Франція','GB':'Велика Британія','GR':'Греція',
        'HR':'Хорватія','HU':'Угорщина','IE':'Ірландія','IT':'Італія','LT':'Литва',
        'LU':'Люксембург','LV':'Латвія','MT':'Мальта','NL':'Нідерланди','NO':'Норвегія',
        'PL':'Польща','PT':'Португалія','RO':'Румунія','SE':'Швеція','SI':'Словенія',
        'SK':'Словаччина','UA':'Україна','US':'США','CA':'Канада','AU':'Австралія',
        'JP':'Японія','CN':'Китай','TR':'Туреччина',
    }
    _cc = (order.addr_country or '').strip().upper()
    destination = _COUNTRY_NAMES.get(_cc, _cc)

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

    # CRM: total orders from this customer
    crm_orders = None
    try:
        cust = order.crm_customer  # @property
        if not cust:
            from crm.models import Customer as _Cust
            _cn = (order.client or '').strip()
            if _cn:
                cust = (
                    _Cust.objects.filter(company__iexact=_cn).first()
                    or _Cust.objects.filter(name__iexact=_cn).first()
                    or _Cust.objects.filter(company__icontains=_cn).first()
                )
        if cust:
            crm_orders = cust.total_orders()
    except Exception:
        pass

    if send_email:
        try:
            # ── Products table ───────────────────────────────────────────────
            lines_html = ''
            if lines_data:
                rows = ''
                for ld in lines_data:
                    if ld['in_stock'] is True:
                        stock_cell = f'<span style="color:#2e7d32;white-space:nowrap">✅ {ld["stock"]} шт</span>'
                    elif ld['in_stock'] is False:
                        stock_cell = (
                            f'<span style="color:#c62828;white-space:nowrap">❌ {ld["stock"]} / {ld["qty"]} шт</span>'
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
                    curr = ld.get('currency', '')
                    unit_str     = f'{ld["unit_price"]:.2f} {curr}'.strip() if ld.get('unit_price') else '—'
                    total_str_ld = f'{ld["line_total"]:.2f} {curr}'.strip() if ld.get('line_total') else '—'
                    qty_val_e = ld["qty"] or 0
                    qty_disp  = str(int(qty_val_e)) if float(qty_val_e) == int(float(qty_val_e)) else str(qty_val_e)
                    img_url = _abs_url(ld.get('image', ''))
                    if img_url:
                        img_cell = (
                            f'<td style="padding:4px 8px;width:72px;vertical-align:middle">'
                            f'<span class="mv-img-wrap" style="position:relative;display:inline-block">'
                            f'<a href="{img_url}" target="_blank">'
                            f'<img src="{img_url}" width="60" height="60" class="mv-thumb"'
                            f' style="object-fit:cover;border-radius:6px;display:block;'
                            f'border:1px solid #e0e0e0;cursor:zoom-in"'
                            f' onerror="this.closest(\'td\').style.display=\'none\'">'
                            f'</a>'
                            f'<img src="{img_url}" class="mv-big" style="display:none">'
                            f'</span></td>'
                        )
                    else:
                        img_cell = '<td style="width:72px"></td>'
                    rows += (
                        f'<tr style="border-bottom:1px solid #eee">'
                        f'{img_cell}'
                        f'<td style="padding:6px 8px">{sku_cell}</td>'
                        f'<td style="padding:6px 8px;text-align:center;font-weight:600;color:#1565c0">{qty_disp} шт</td>'
                        f'<td style="padding:6px 8px;text-align:right;color:#555;white-space:nowrap">{unit_str}</td>'
                        f'<td style="padding:6px 8px;text-align:right;font-weight:600;white-space:nowrap">{total_str_ld}</td>'
                        f'<td style="padding:6px 8px;text-align:right;font-size:12px">{stock_cell}</td>'
                        f'</tr>'
                    )
                lines_html = (
                    '<style>'
                    '.mv-img-wrap:hover .mv-big{'
                    'display:block !important;'
                    'position:fixed !important;'
                    'top:50% !important;left:50% !important;'
                    'transform:translate(-50%,-50%) !important;'
                    'width:300px !important;height:300px !important;'
                    'object-fit:contain !important;background:#fff !important;'
                    'border-radius:12px !important;border:2px solid #ddd !important;'
                    'box-shadow:0 8px 40px rgba(0,0,0,.55) !important;'
                    'z-index:99999 !important;pointer-events:none !important}'
                    '.mv-thumb{transition:opacity .15s}'
                    '.mv-thumb:hover{opacity:.7}'
                    '</style>'
                    '<div style="margin-top:16px">'
                    '<b style="font-size:13px;color:#333">📋 Товари:</b>'
                    '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px;'
                    'border:1px solid #e8ecf0;border-radius:6px;overflow:hidden">'
                    '<tr style="background:#e8f0fe;color:#1565c0;font-size:11px;font-weight:600">'
                    '<th style="padding:6px 8px;width:72px"></th>'
                    '<th style="padding:6px 8px;text-align:left">SKU</th>'
                    '<th style="padding:6px 8px;text-align:center">К-сть</th>'
                    '<th style="padding:6px 8px;text-align:right">Ціна/шт</th>'
                    '<th style="padding:6px 8px;text-align:right">Сума</th>'
                    '<th style="padding:6px 8px;text-align:right">Склад</th>'
                    '</tr>'
                    + rows +
                    '</table></div>'
                )

            # ── Extra block ───────────────────────────────────────────────────
            meta = ''
            if total_str:
                meta += f'<br><b>💰 Сума:</b> <b style="color:#1565c0">{total_str}</b>'
            if crm_orders is not None:
                meta += f'<br><b>📊 Замовлень від клієнта:</b> <b style="color:#1565c0">{crm_orders}</b>'
            if destination:
                meta += f'<br><b>📍 Куди:</b> {destination}'
            if deadline_str:
                dl_color = '#c62828' if days_left is not None and days_left <= 2 else '#2e7d32'
                meta += (
                    f'<br><b>📦 Дедлайн:</b> {deadline_str}'
                    + (f' <span style="color:{dl_color};font-weight:600">({days_left_str})</span>'
                       if days_left_str else '')
                )

            extra = meta + (f'<br>{lines_html}' if lines_html else '')

            title = '🆕 Нове замовлення'
            html = _order_email_html(order, title, '#1565c0', extra, show_total=False)
            _send_event_email(ns, subject, html)
        except Exception:
            pass

    if send_tg:
        try:
            _cname = _get_company_name()
            now_str = timezone.now().strftime('%d.%m.%Y %H:%M')
            tg = [f'🏛️ <b>{_cname}</b>']
            tg.append(f'🆕 <b>Нове замовлення</b> · <i>{now_str}</i>')
            tg.append('')

            # ── Order info ─────────────────────────────────────────────────
            tg.append(f'📋 <code>{order.order_number}</code> · {order.source}')
            tg.append(f'👤 <b>{client}</b>')
            if crm_orders is not None:
                tg.append(f'   📊 Замовлень всього: <b>{crm_orders}</b>')
            if destination:
                tg.append(f'📍 {destination}')
            if deadline_str:
                dl_warn = ' ⚠️' if days_left is not None and days_left <= 2 else ''
                tg.append(f'📦 Дедлайн: <b>{deadline_str}</b> ({days_left_str}){dl_warn}')
            if total_str:
                tg.append(f'💰 <b>{total_str}</b>')

            # ── Products ───────────────────────────────────────────────────
            if lines_data:
                tg.append('')
                tg.append('📦 <b>Товари:</b>')
                for ld in lines_data:
                    icon = '✅' if ld['in_stock'] is True else ('❌' if ld['in_stock'] is False else '•')
                    curr = ld.get('currency', '')
                    name_part = f' — {ld["name"]}' if ld['name'] not in ('—', '', None) else ''
                    qty_val = ld["qty"] or 0
                    qty_str = str(int(qty_val)) if float(qty_val) == int(float(qty_val)) else str(qty_val)
                    lines = [f'{icon} <code>{ld["sku"]}</code>{name_part}']
                    lines.append(f'   📦 × <b>{qty_str} шт</b>')
                    if ld.get('unit_price'):
                        lines.append(f'   💵 {ld["unit_price"]:.2f} {curr}/шт'.strip())
                    if ld['in_stock'] is True:
                        lines.append(f'   🏪 склад: <b>{ld["stock"]} шт</b> ✅')
                    elif ld['in_stock'] is False:
                        lines.append(f'   🏪 склад: <b>{ld["stock"]} шт</b> ❌')
                    if ld.get('datasheet'):
                        lines.append(f'   📄 <a href="{ld["datasheet"]}">Datasheet</a>')
                    tg.append('\n'.join(lines))

            tg_text = '\n'.join(tg)
            first_image = next(
                (_abs_url(ld['image']) for ld in lines_data if ld.get('image')),
                ''
            )
            if first_image and first_image.startswith('http'):
                try:
                    _send_telegram_photo(ns, first_image, tg_text)
                except Exception:
                    _send_telegram(ns, tg_text)
            else:
                _send_telegram(ns, tg_text)
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
            src = getattr(order, 'status_source', '') or ''
            if src:
                lines.append(f'🔌 Джерело: <i>{src}</i>')
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


# ── Shipment status notifications ─────────────────────────────────────────────

_SHIPMENT_STATUS_LABELS = {
    'submitted':   'Передано перевізнику',
    'label_ready': 'Етикетка готова',
    'in_transit':  'В дорозі 🚚',
    'delivered':   'Доставлено ✅',
    'error':       'Помилка ❌',
    'cancelled':   'Скасовано',
}
_SHIPMENT_STATUS_COLORS = {
    'submitted':   '#1976d2',
    'label_ready': '#0288d1',
    'in_transit':  '#f57c00',
    'delivered':   '#2e7d32',
    'error':       '#c62828',
    'cancelled':   '#757575',
}


def notify_shipment_status(shipment, old_status, new_status):
    """Send notification when Shipment status changes."""
    ns = _get_ns()
    if not ns:
        return

    flag_map = {
        'submitted':   getattr(ns, 'shipment_on_submitted',   False),
        'label_ready': getattr(ns, 'shipment_on_label_ready', False),
        'in_transit':  getattr(ns, 'shipment_on_in_transit',  True),
        'delivered':   getattr(ns, 'shipment_on_delivered',   True),
        'error':       getattr(ns, 'shipment_on_error',       True),
        'cancelled':   getattr(ns, 'shipment_on_cancelled',   False),
    }
    if not flag_map.get(new_status, False):
        return

    send_email = ns.email_enabled and getattr(ns, 'shipment_email', False)
    send_tg    = ns.telegram_enabled and getattr(ns, 'shipment_telegram', True)
    if not send_email and not send_tg:
        return

    new_label = _SHIPMENT_STATUS_LABELS.get(new_status, new_status)
    old_label = _SHIPMENT_STATUS_LABELS.get(old_status, old_status)
    color     = _SHIPMENT_STATUS_COLORS.get(new_status, '#333')

    recipient = (getattr(shipment, 'recipient_name', '') or
                 getattr(shipment, 'recipient_company', '') or '—')
    carrier   = str(getattr(shipment, 'carrier', '') or '—')
    tracking  = getattr(shipment, 'tracking_number', '') or ''
    country   = getattr(shipment, 'recipient_country', '') or ''
    order_num = ''
    try:
        if shipment.order:
            order_num = shipment.order.order_number or ''
    except Exception:
        pass

    if send_email:
        try:
            _cname  = _get_company_name()
            from django.utils import timezone as _tz
            now_str = _tz.now().strftime('%d.%m.%Y %H:%M')
            subject = f'📦 Відправлення #{shipment.pk}: {new_label}'
            rows = f'<br><b>Статус:</b> {old_label} → <b style="color:{color}">{new_label}</b>'
            if order_num:
                rows += f'<br><b>Замовлення:</b> {order_num}'
            rows += f'<br><b>Отримувач:</b> {recipient}'
            if country:
                rows += f'<br><b>Країна:</b> {country}'
            rows += f'<br><b>Перевізник:</b> {carrier}'
            if tracking:
                rows += (f'<br><b>Трекінг:</b> '
                         f'<code style="font-family:monospace">{tracking}</code>')
            clabel = getattr(shipment, 'carrier_status_label', '') or ''
            if clabel:
                rows += f'<br><b>Статус перевізника:</b> {clabel}'
            eta = getattr(shipment, 'carrier_eta', None)
            if eta:
                rows += f'<br><b>Очікувана доставка:</b> {eta.strftime("%d.%m.%Y")}'
            html = (
                f'<div style="font-family:sans-serif;max-width:560px;margin:0 auto;'
                f'border:1px solid #e0e0e0;border-radius:8px;overflow:hidden">'
                f'<div style="background:{color};padding:16px 20px">'
                f'<span style="color:#fff;font-size:18px;font-weight:700">📦 {new_label}</span>'
                f'</div>'
                f'<div style="padding:20px;background:#fafafa">'
                f'<p style="margin:0 0 4px;font-size:13px;color:#555">{_cname} · {now_str}</p>'
                f'<hr style="border:none;border-top:1px solid #e0e0e0;margin:10px 0">'
                f'<p style="font-size:14px;line-height:1.8;margin:0">{rows}</p>'
                f'</div></div>'
            )
            _send_event_email(ns, subject, html)
        except Exception:
            pass

    if send_tg:
        try:
            _cname  = _get_company_name()
            from django.utils import timezone as _tz
            now_str = _tz.now().strftime('%d.%m.%Y %H:%M')
            lines = [
                f'🏛️ <b>{_cname}</b>',
                f'📦 <b>Відправлення #{shipment.pk}</b> · <i>{now_str}</i>',
                '',
                f'Статус: {old_label} → <b>{new_label}</b>',
            ]
            if order_num:
                lines.append(f'🛒 Замовлення: <code>{order_num}</code>')
            lines.append(f'👤 {recipient}')
            if country:
                lines.append(f'📍 {country}')
            lines.append(f'🚚 {carrier}')
            if tracking:
                lines.append(f'🔎 Трекінг: <code>{tracking}</code>')
            clabel = getattr(shipment, 'carrier_status_label', '') or ''
            if clabel:
                lines.append(f'ℹ️ {clabel}')
            eta = getattr(shipment, 'carrier_eta', None)
            if eta:
                lines.append(f'📅 ETA: <b>{eta.strftime("%d.%m.%Y")}</b>')
            _send_telegram(ns, '\n'.join(lines))
        except Exception:
            pass


def send_order_confirm_notification(order):
    """Send order-received confirmation email to customer.
    Called on auto-send (import/create) or manually via notify_order_confirm_view.
    Respects order_confirm_notify_sources filter."""
    ns = _get_ns()
    if not ns:
        return False
    if not getattr(ns, 'order_confirm_notify_enabled', False):
        return False
    if not ns.email_enabled:
        return False

    allowed_srcs = [s.strip() for s in (getattr(ns, 'order_confirm_notify_sources', '') or '').split(',') if s.strip()]
    if allowed_srcs and (order.source not in allowed_srcs):
        return False

    to_email = (getattr(order, 'ship_email', '') or getattr(order, 'email', '') or '').strip()
    if not to_email:
        try:
            to_email = (order.customer.email or '').strip()
        except Exception:
            to_email = ''
    if not to_email:
        return False

    customer_name = (getattr(order, 'ship_name', '') or getattr(order, 'client', '') or getattr(order, 'contact_name', '') or '').strip()
    order_date    = order.order_date.strftime('%d.%m.%Y') if order.order_date else ''

    try:
        lines_qs = order.lines.select_related('product').all()
    except Exception:
        lines_qs = []
    items_lines = []
    for line in lines_qs:
        sku = getattr(line, 'sku_raw', '') or (line.product.sku if line.product else '')
        qty = line.qty
        try:
            qty = int(qty) if qty == int(qty) else qty
        except Exception:
            pass
        items_lines.append(f'• {sku} — {qty} Stk.')
    items_str = '\n'.join(items_lines) if items_lines else '—'

    def _render(tmpl):
        return (tmpl
                .replace('{order_number}', order.order_number or '')
                .replace('{customer_name}', customer_name)
                .replace('{order_date}', order_date)
                .replace('{items}', items_str))

    subject   = _render(getattr(ns, 'order_confirm_notify_subject', '') or '')
    body_text = _render(getattr(ns, 'order_confirm_notify_body', '') or '')
    cc_str    = (getattr(ns, 'order_confirm_notify_cc', '') or '').strip()
    from_email = (ns.email_from or ns.email_host_user or '').strip()

    if not subject or not body_text:
        return False

    try:
        from django.core.mail import EmailMultiAlternatives
        html_body = ('<html><body style="font-family:sans-serif;font-size:14px;line-height:1.7">'
                     + body_text.replace('\n', '<br>') + '</body></html>')
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_text,
            from_email=from_email,
            to=[to_email],
            cc=[a.strip() for a in cc_str.split(',') if a.strip()] if cc_str else [],
            connection=_smtp_connection(ns),
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send()
        from django.utils import timezone as _tz
        from sales.models import SalesOrder
        SalesOrder.objects.filter(pk=order.pk).update(order_confirm_sent_at=_tz.now())
        return True
    except Exception:
        return False


# ── Shipment notification to customer ─────────────────────────────────────────

_EU_COUNTRIES = {
    'AT','BE','BG','CY','CZ','DE','DK','EE','ES','FI',
    'FR','GR','HR','HU','IE','IT','LT','LU','LV','MT',
    'NL','PL','PT','RO','SE','SI','SK',
}


def send_ship_notification(order) -> bool:
    """Send shipment notification email to customer.

    Uses EU or non-EU template based on order.addr_country.
    Sets order.ship_notify_sent_at on success.
    Returns True on success, False on failure.
    """
    try:
        from config.models import NotificationSettings as _NS
        ns = _NS.get()
    except Exception:
        return False

    if not ns.email_enabled:
        return False

    to_email = (getattr(order, 'ship_email', '') or order.email or '').strip()
    if not to_email:
        return False

    cust_name   = (getattr(order, 'ship_name', '') or order.client or
                   getattr(order, 'contact_name', '') or '').strip()
    tracking    = (order.tracking_number or '').strip()
    carrier     = (order.shipping_courier or '').strip()
    shipped     = order.shipped_at.strftime('%d.%m.%Y') if order.shipped_at else ''
    country     = (order.addr_country or '').strip().upper()
    is_eu       = country in _EU_COUNTRIES

    lines = order.lines.select_related('product').all() if hasattr(order, 'lines') else []
    items_text = '\n'.join(
        '• {} — {} Stk.'.format(
            l.sku_raw or (l.product.sku if l.product else ''), int(l.qty)
        )
        for l in lines
    ) or '—'

    addr_parts = filter(None, [
        getattr(order, 'ship_name', '') or order.client,
        getattr(order, 'addr_street', '') or getattr(order, 'shipping_address', ''),
        ('{} {}'.format(
            getattr(order, 'addr_zip', '') or '',
            getattr(order, 'addr_city', '') or ''
        )).strip(),
        country,
    ])
    ship_address = '\n'.join(addr_parts)

    ctx = {
        'order_number':    order.order_number or str(order.pk),
        'customer_name':   cust_name,
        'tracking_number': tracking,
        'carrier':         carrier,
        'shipped_date':    shipped,
        'items':           items_text,
        'ship_address':    ship_address,
    }

    def _render(tpl):
        try:
            return (tpl or '').format(**ctx)
        except KeyError:
            return tpl or ''

    body_tpl = ns.customer_notify_body if is_eu else (
        getattr(ns, 'customer_notify_body_noneu', '') or ns.customer_notify_body
    )
    subject   = _render(ns.customer_notify_subject)
    body_text = _render(body_tpl)
    cc_str    = (getattr(ns, 'customer_notify_cc', '') or '').strip()
    from_email = (ns.email_from or ns.email_host_user or '').strip()

    if not subject or not body_text:
        return False

    try:
        from django.core.mail import EmailMultiAlternatives
        from django.utils import timezone as _tz
        html_body = ('<html><body style="font-family:sans-serif;font-size:14px;line-height:1.7">'
                     + body_text.replace('\n', '<br>') + '</body></html>')
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_text,
            from_email=from_email,
            to=[to_email],
            cc=[a.strip() for a in cc_str.split(',') if a.strip()] if cc_str else [],
            connection=_smtp_connection(ns),
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send()

        from sales.models import SalesOrder
        SalesOrder.objects.filter(pk=order.pk).update(ship_notify_sent_at=_tz.now())
        return True
    except Exception:
        return False
