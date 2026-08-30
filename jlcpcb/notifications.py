"""JLCPCB — email + Telegram notifications on order status changes."""
from __future__ import annotations
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_STATUS_ICONS = {
    'ordered':       '📋',
    'reviewed':      '🔍',
    'in_production': '🏭',
    'manufactured':  '✅',
    'shipped':       '📦',
    'delivered':     '🎉',
    'cancelled':     '❌',
}

_STATUS_LABELS_UK = {
    'ordered':       'Замовлено',
    'reviewed':      'Перевірено JLC',
    'in_production': 'У виробництві',
    'manufactured':  'Виготовлено',
    'shipped':       'Відправлено',
    'delivered':     'Доставлено',
    'cancelled':     'Скасовано',
}

_STATUS_COLORS = {
    'ordered':       '#607d8b',
    'reviewed':      '#1565c0',
    'in_production': '#e65100',
    'manufactured':  '#2e7d32',
    'shipped':       '#6a1b9a',
    'delivered':     '#1b5e20',
    'cancelled':     '#757575',
}


def _get_telegram_token_and_chat() -> tuple[str, str]:
    try:
        from config.models import NotificationSettings
        from jlcpcb.models import JLCConfig
        ns      = NotificationSettings.get()
        token   = ns.telegram_bot_token or ''
        jlc_cfg = JLCConfig.get()
        # Personal chat ID takes priority over the global channel
        chat_id = jlc_cfg.telegram_personal_chat_id or ns.telegram_chat_id or ''
        return token, chat_id
    except Exception:
        return '', ''


def _get_company_name() -> str:
    try:
        from config.models import SystemSettings
        return SystemSettings.get().company_name or 'Minerva'
    except Exception:
        return 'Minerva'


def _send_telegram(text: str) -> bool:
    token, chat_id = _get_telegram_token_and_chat()
    if not token or not chat_id:
        return False
    try:
        import json
        payload = json.dumps({
            'chat_id': chat_id,
            'text':    text,
            'parse_mode': 'HTML',
        }).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.warning('Telegram send failed: %s', e)
        return False


def _send_html_email(subject: str, html: str, plain: str,
                     to_addresses: list[str]) -> bool:
    if not to_addresses:
        return False
    try:
        from django.core.mail import EmailMultiAlternatives
        msg = EmailMultiAlternatives(subject, plain, None, to_addresses)
        msg.attach_alternative(html, 'text/html')
        msg.send(fail_silently=True)
        return True
    except Exception as e:
        logger.warning('Email send failed: %s', e)
        return False


def _resolve_email_recipients(cfg) -> list[str]:
    if cfg.notify_email_to:
        return [e.strip() for e in cfg.notify_email_to.split(',') if e.strip()]
    try:
        from config.models import NotificationSettings
        s = NotificationSettings.get()
        if s.alert_email_to:
            return [e.strip() for e in s.alert_email_to.split(',') if e.strip()]
    except Exception:
        pass
    return []


def _format_eta(expected_date) -> str:
    if not expected_date:
        return ''
    try:
        months_uk = ['', 'січня', 'лютого', 'березня', 'квітня', 'травня', 'червня',
                     'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня']
        return f'{expected_date.day} {months_uk[expected_date.month]} {expected_date.year}'
    except Exception:
        return str(expected_date)


def _extract_expected_date(order):
    """Return expected_date from model field or raw_data fallback."""
    if order.expected_date:
        return order.expected_date
    raw = getattr(order, 'raw_data', None)
    if isinstance(raw, dict):
        for item in raw.get('orderItem', []):
            dt_str = (item.get('pcbItem') or {}).get('deliveryTime')
            if dt_str:
                try:
                    from datetime import datetime as _dt
                    return _dt.strptime(dt_str[:10], '%Y-%m-%d').date()
                except Exception:
                    pass
    return None


def _calc_estimated_ship_date(order):
    """
    Calculate estimated ship date = orderDate + buildTime hours.
    Returns (date, label_str) or (None, '').
    Used when no deliveryTime/tracking data is available yet.
    """
    raw = getattr(order, 'raw_data', None)
    if not isinstance(raw, dict):
        return None, ''
    for item in raw.get('orderItem', []):
        pcb = item.get('pcbItem') or {}
        order_date_str = pcb.get('orderDate')
        build_time     = pcb.get('buildTime')
        if not order_date_str or not build_time:
            continue
        try:
            from datetime import datetime as _dt, timedelta
            order_dt   = _dt.strptime(order_date_str[:19], '%Y-%m-%d %H:%M:%S')
            ship_dt    = order_dt + timedelta(hours=int(build_time))
            months_uk  = ['', 'січня', 'лютого', 'березня', 'квітня', 'травня', 'червня',
                          'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня']
            label = (
                f'{ship_dt.day} {months_uk[ship_dt.month]} {ship_dt.year}'
                f' (~{build_time}г виготовлення)'
            )
            return ship_dt.date(), label
        except Exception:
            pass
    return None, ''


def _extract_shipping_method(order) -> str:
    if order.tracking_carrier:
        return order.tracking_carrier
    raw = getattr(order, 'raw_data', None)
    if isinstance(raw, dict):
        return raw.get('shippingMethod', '')
    return ''


# ── HTML email builder ────────────────────────────────────────────────────────

def _build_jlc_html(order, old_status: str, new_status: str,
                    company: str, eta_str: str,
                    shipping_method: str,
                    est_ship_label: str = '') -> str:
    from django.utils import timezone as tz

    status_color = _STATUS_COLORS.get(new_status, '#546e7a')
    status_label = _STATUS_LABELS_UK.get(new_status, new_status)
    status_icon_text = {
        'ordered': '📋', 'reviewed': '🔍', 'in_production': '🏭',
        'manufactured': '✅', 'shipped': '📦', 'delivered': '🎉', 'cancelled': '❌',
    }.get(new_status, '⚙️')

    old_label = _STATUS_LABELS_UK.get(old_status, old_status)
    is_same   = (old_status == new_status)

    now_str = tz.now().strftime('%d.%m.%Y %H:%M')

    # Product/description
    product_name = ''
    if order.product_id:
        product_name = order.product.sku
    elif order.description:
        product_name = order.description[:80]

    # PCB specs from raw_data
    specs_rows = ''
    raw = getattr(order, 'raw_data', None)
    if isinstance(raw, dict):
        for item in raw.get('orderItem', []):
            pcb = item.get('pcbItem') or {}
            if not pcb:
                continue
            st_int = pcb.get('orderStatus')
            st_key = new_status  # use current
            spec_items = []
            if pcb.get('width') and pcb.get('length'):
                spec_items.append(f"{pcb['width']}×{pcb['length']} мм")
            if pcb.get('layer'):
                spec_items.append(f"{pcb['layer']} шари")
            if pcb.get('thickness'):
                spec_items.append(f"{pcb['thickness']} мм")
            if pcb.get('pcbColor'):
                spec_items.append(pcb['pcbColor'])
            if pcb.get('surfaceFinish'):
                spec_items.append(pcb['surfaceFinish'])
            if pcb.get('materialDetails'):
                spec_items.append(pcb['materialDetails'])
            specs_str = ' · '.join(spec_items)
            fn = pcb.get('fileName', '')
            count = pcb.get('count', 0)
            price = pcb.get('price', '')
            price_str = f'{price} USD' if price else ''
            specs_rows += (
                f'<tr>'
                f'<td style="padding:8px 10px;font-family:monospace;font-size:12px;'
                f'color:#1a237e;font-weight:600">{fn}</td>'
                f'<td style="padding:8px 10px;text-align:center;font-weight:700">{count}</td>'
                f'<td style="padding:8px 10px;font-size:11px;color:#555">{specs_str}</td>'
                f'<td style="padding:8px 10px;text-align:right;white-space:nowrap">{price_str}</td>'
                f'</tr>'
            )
            break  # show only first pcbItem for brevity

    specs_section = ''
    if specs_rows:
        specs_section = (
            '<div style="padding:0 24px 16px">'
            '<p style="margin:0 0 8px;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:.05em;color:#888">Специфікація PCB</p>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;'
            'border:1px solid #e0e0e0;border-radius:4px">'
            '<tr style="background:#f5f5f5;font-size:11px;color:#666;font-weight:600">'
            '<td style="padding:6px 10px">Файл</td>'
            '<td style="padding:6px 10px;text-align:center">К-сть</td>'
            '<td style="padding:6px 10px">Специфікація</td>'
            '<td style="padding:6px 10px;text-align:right">Ціна</td>'
            '</tr>'
            f'{specs_rows}'
            '</table></div>'
        )

    # Tracking block
    tracking_section = ''
    if order.tracking_number:
        carrier_str = f' ({shipping_method})' if shipping_method else ''
        tracking_section = (
            '<div style="margin:0 24px 16px;padding:12px 14px;'
            'background:#e8f5e9;border-left:4px solid #4caf50;border-radius:0 4px 4px 0">'
            f'<div style="font-weight:700;color:#2e7d32;margin-bottom:4px">🔍 Трекінг{carrier_str}</div>'
            f'<div style="font-family:monospace;font-size:15px;color:#1b5e20;font-weight:700">'
            f'{order.tracking_number}</div>'
        )
        if order.tracking_url:
            tracking_section += (
                f'<div style="margin-top:6px">'
                f'<a href="{order.tracking_url}" style="color:#1565c0;font-size:12px">'
                f'🔗 Відстежити посилку →</a></div>'
            )
        tracking_section += '</div>'
    else:
        carrier_line = f'<b>{shipping_method}</b> · ' if shipping_method else ''
        hint = ''
        if new_status == 'shipped':
            hint = ('<div style="font-size:11px;color:#e65100;margin-top:4px">'
                    '⚠️ Трекінг-номер буде у листі від JLCPCB. '
                    'Введи його в картку замовлення для авто-відстеження.</div>')
        if shipping_method or new_status == 'shipped':
            tracking_section = (
                '<div style="margin:0 24px 16px;padding:12px 14px;'
                'background:#fff8e1;border-left:4px solid #ffc107;border-radius:0 4px 4px 0">'
                f'<div style="color:#795548">{carrier_line}Трекінг-номер не вказано</div>'
                f'{hint}</div>'
            )

    # Status change or same
    if is_same:
        status_change_html = (
            f'<span style="background:{status_color};color:#fff;padding:4px 14px;'
            f'border-radius:12px;font-weight:700;font-size:13px">'
            f'{status_icon_text} {status_label}</span>'
            f'<span style="font-size:11px;color:#999;margin-left:8px">(поточний статус)</span>'
        )
    else:
        old_color = _STATUS_COLORS.get(old_status, '#aaa')
        status_change_html = (
            f'<span style="background:{old_color}22;color:{old_color};padding:4px 12px;'
            f'border-radius:12px;font-weight:600;font-size:12px">{old_label}</span>'
            f'<span style="color:#aaa;margin:0 8px;font-size:16px">→</span>'
            f'<span style="background:{status_color};color:#fff;padding:4px 14px;'
            f'border-radius:12px;font-weight:700;font-size:13px">'
            f'{status_icon_text} {status_label}</span>'
        )

    html = f'''<!DOCTYPE html>
<html lang="uk">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;
            overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.12)">

  <!-- HEADER -->
  <div style="background:linear-gradient(135deg,#1a237e 0%,#283593 100%);
              color:#fff;padding:22px 24px;position:relative">
    <div style="font-size:22px;font-weight:700;letter-spacing:.5px">
      &#9654; Minerva <span style="font-weight:300;opacity:.8">BI</span>
    </div>
    <div style="font-size:13px;opacity:.7;margin-top:2px">{company}</div>
    <div style="position:absolute;right:24px;top:50%;transform:translateY(-50%);
                font-size:42px;opacity:.15">⬛</div>
  </div>

  <!-- PCB STATUS BANNER -->
  <div style="background:{status_color};color:#fff;
              padding:14px 24px;display:flex;align-items:center;gap:12px">
    <div style="font-size:28px">{status_icon_text}</div>
    <div>
      <div style="font-size:16px;font-weight:700">JLCPCB — {status_label}</div>
      <div style="font-size:12px;opacity:.85">Замовлення {order.jlc_order_id}</div>
    </div>
  </div>

  <!-- ORDER SUMMARY -->
  <div style="padding:20px 24px 8px">
    <table style="width:100%;border-collapse:collapse">
      <tr>
        <td style="padding:6px 0;color:#888;font-size:12px;width:40%">Замовлення</td>
        <td style="padding:6px 0;font-family:monospace;font-weight:700;color:#1a237e">
          {order.jlc_order_id}</td>
      </tr>
      {"" if not product_name else f"""
      <tr>
        <td style="padding:6px 0;color:#888;font-size:12px">Товар / Файл</td>
        <td style="padding:6px 0;font-weight:600">{product_name}</td>
      </tr>"""}
      <tr>
        <td style="padding:6px 0;color:#888;font-size:12px">Кількість</td>
        <td style="padding:6px 0;font-weight:700;font-size:16px">{order.quantity} шт.</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#888;font-size:12px">Статус</td>
        <td style="padding:10px 0">{status_change_html}</td>
      </tr>
      {"" if not eta_str else f"""
      <tr>
        <td style="padding:6px 0;color:#888;font-size:12px">Очікувана доставка</td>
        <td style="padding:6px 0;font-weight:700;color:#e65100">📅 {eta_str}</td>
      </tr>"""}
      {"" if eta_str or not est_ship_label else f"""
      <tr>
        <td style="padding:6px 0;color:#888;font-size:12px">Розрахункова відправка</td>
        <td style="padding:6px 0;font-weight:700;color:#1565c0">🏭 {est_ship_label}</td>
      </tr>"""}
    </table>
  </div>

  <!-- PCB SPECS -->
  {specs_section}

  <!-- TRACKING -->
  {tracking_section}

  <!-- FOOTER -->
  <div style="background:#f5f5f5;padding:16px 24px;border-top:1px solid #e0e0e0">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <div>
        <div style="font-weight:700;color:#1a237e;font-size:13px">
          &#9654; Minerva BI System
        </div>
        <div style="font-size:11px;color:#999;margin-top:2px">
          {company} · Автоматичне сповіщення · {now_str}
        </div>
      </div>
      <div style="font-size:11px;color:#bbb;text-align:right">
        Це автоматичний лист.<br>Не відповідайте на нього.
      </div>
    </div>
  </div>

</div>
</body>
</html>'''
    return html


# ── Main notification function ────────────────────────────────────────────────

def notify_jlc_status_change(order, old_status: str, new_status: str,
                              force: bool = False) -> None:
    """Send HTML email + Telegram when JLC order status changes.

    force=True bypasses per-event flag checks (used for manual test sends).
    """
    from jlcpcb.models import JLCConfig
    cfg = JLCConfig.get()

    if not force:
        if new_status == 'shipped' and not cfg.notify_on_shipped:
            return
        if new_status == 'delivered' and not cfg.notify_on_delivered:
            return
        if new_status not in ('shipped', 'delivered') and not cfg.notify_on_status_change:
            return

    old_label = _STATUS_LABELS_UK.get(old_status, old_status)
    new_label = _STATUS_LABELS_UK.get(new_status, new_status)
    icon      = _STATUS_ICONS.get(new_status, '📋')
    company   = _get_company_name()

    shipping_method = _extract_shipping_method(order)
    expected_date   = _extract_expected_date(order)
    eta_str         = _format_eta(expected_date)

    # Estimated ship date (orderDate + buildTime) — fallback when no deliveryTime/tracking
    est_ship_date, est_ship_label = (None, '')
    if not eta_str and not order.tracking_number:
        est_ship_date, est_ship_label = _calc_estimated_ship_date(order)

    # Product / description
    product_info = ''
    if order.product_id:
        product_info = f'\n🏷 Товар: <b>{order.product.sku}</b>'
    elif order.description:
        product_info = f'\n📄 {order.description[:80]}'

    # ETA / estimated ship
    if eta_str:
        eta_line = f'\n📅 Очікувана доставка: <b>{eta_str}</b>'
    elif est_ship_label:
        eta_line = f'\n🏭 Розрахункова відправка: <b>{est_ship_label}</b>'
    else:
        eta_line = ''

    # Tracking
    tracking_info  = ''
    tracking_plain = ''
    if order.tracking_number:
        carrier_str    = f' ({shipping_method})' if shipping_method else ''
        tracking_info  = f'\n🔍 Трекінг: <code>{order.tracking_number}</code>{carrier_str}'
        tracking_plain = f'Трекінг: {order.tracking_number}{carrier_str}\n'
        if order.tracking_url:
            tracking_info  += f'\n<a href="{order.tracking_url}">🔗 Відстежити посилку</a>'
            tracking_plain += f'Відстежити: {order.tracking_url}\n'
    else:
        if shipping_method:
            tracking_info  = f'\n🚚 Перевізник: <b>{shipping_method}</b>'
            tracking_plain = f'Перевізник: {shipping_method}\n'
        if new_status == 'shipped':
            tracking_info  += '\n⚠️ Трекінг-номер не вказано — перевір email від JLCPCB'
            tracking_plain += 'Трекінг-номер буде у листі від JLCPCB.\n'
        elif est_ship_label and not tracking_info:
            tracking_plain += f'Розрахункова відправка: {est_ship_label}\n'

    # Status line for Telegram
    status_line = (
        f'\nСтатус: <b>{new_label}</b> (поточний)'
        if old_status == new_status
        else f'\nСтатус: {old_label} → <b>{new_label}</b>'
    )

    tg_text = (
        f'{icon} <b>JLCPCB — {new_label}</b>\n'
        f'Замовлення: <code>{order.jlc_order_id}</code>\n'
        f'Кількість: <b>{order.quantity}</b> шт.'
        f'{product_info}'
        f'{status_line}'
        f'{eta_line}'
        f'{tracking_info}\n'
        f'<i>{company}</i>'
    )

    # Plain text fallback
    plain_body = (
        f'Замовлення JLCPCB {order.jlc_order_id}\n'
        f'Статус: {new_label}\n'
        f'Кількість: {order.quantity} шт.\n'
    )
    if order.description:
        plain_body += f'Опис: {order.description}\n'
    if eta_str:
        plain_body += f'Очікувана доставка: {eta_str}\n'
    plain_body += tracking_plain

    if cfg.notify_telegram:
        _send_telegram(tg_text)

    if cfg.notify_email:
        recipients = _resolve_email_recipients(cfg)
        if recipients:
            subject   = f'[{company}] JLCPCB {new_label}: {order.jlc_order_id}'
            html_body = _build_jlc_html(
                order, old_status, new_status, company, eta_str, shipping_method, est_ship_label
            )
            _send_html_email(subject, html_body, plain_body, recipients)

    if not force:
        order.last_notified_status = new_status
        order.save(update_fields=['last_notified_status', 'updated_at'])


# ── Active orders summary ─────────────────────────────────────────────────────

def notify_jlc_active_orders_summary() -> dict:
    """Send Telegram + HTML email summary of all active JLCPCB orders."""
    from jlcpcb.models import JLCConfig, JLCOrder
    from django.utils import timezone as tz
    cfg     = JLCConfig.get()
    company = _get_company_name()
    now_str = tz.now().strftime('%d.%m.%Y %H:%M')

    active = list(
        JLCOrder.objects.exclude(
            local_status__in=['delivered', 'cancelled']
        ).select_related('product').order_by('order_date')
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    if not active:
        tg_text = f'📋 <b>JLCPCB</b> — активних замовлень немає.\n<i>{company}</i>'
    else:
        lines = [f'📋 <b>JLCPCB — Активні замовлення: {len(active)}</b>']
        lines.append('─' * 30)
        for o in active:
            icon  = _STATUS_ICONS.get(o.local_status, '📋')
            label = _STATUS_LABELS_UK.get(o.local_status, o.local_status)
            name  = o.product.sku if o.product_id else (o.description[:35] if o.description else o.jlc_order_id)
            ed    = _extract_expected_date(o)
            eta   = f' | 📅 {_format_eta(ed)}' if ed else ''
            trk   = f' | 📍 <code>{o.tracking_number}</code>' if o.tracking_number else ''
            lines.append(f'{icon} <code>{o.jlc_order_id}</code>\n   {name} — <b>{label}</b>{eta}{trk}')
        lines.append('─' * 30)
        lines.append(f'<i>{company}</i>')
        tg_text = '\n'.join(lines)

    # ── HTML email ────────────────────────────────────────────────────────────
    if not active:
        rows_html  = '<tr><td colspan="5" style="padding:16px;text-align:center;color:#888">Активних замовлень немає</td></tr>'
        plain_body = 'JLCPCB — активних замовлень немає.'
    else:
        rows_html  = ''
        plain_lines = [f'JLCPCB — Активні замовлення: {len(active)}', '']
        for i, o in enumerate(active):
            bg    = '#fafafa' if i % 2 else '#fff'
            icon  = _STATUS_ICONS.get(o.local_status, '📋')
            label = _STATUS_LABELS_UK.get(o.local_status, o.local_status)
            color = _STATUS_COLORS.get(o.local_status, '#607d8b')
            name  = o.product.sku if o.product_id else (o.description[:40] if o.description else '—')
            ed    = _extract_expected_date(o)
            eta   = _format_eta(ed) if ed else '—'
            trk   = o.tracking_number or '—'
            rows_html += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:9px 10px;font-family:monospace;font-size:12px;color:#1a237e">{o.jlc_order_id}</td>'
                f'<td style="padding:9px 10px">{name}</td>'
                f'<td style="padding:9px 10px">'
                f'<span style="background:{color};color:#fff;padding:2px 10px;border-radius:10px;font-size:11px;font-weight:600">'
                f'{icon} {label}</span></td>'
                f'<td style="padding:9px 10px;font-size:12px;color:#e65100">{eta}</td>'
                f'<td style="padding:9px 10px;font-family:monospace;font-size:11px">{trk}</td>'
                f'</tr>'
            )
            plain_lines.append(f'{o.jlc_order_id} — {name} — {label} | ETA: {eta}')
        plain_body = '\n'.join(plain_lines)

    html_body = f'''<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f0f2f5;font-family:Arial,sans-serif">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:10px;
            overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.12)">
  <div style="background:linear-gradient(135deg,#1a237e 0%,#283593 100%);color:#fff;padding:20px 24px">
    <div style="font-size:20px;font-weight:700">&#9654; Minerva BI</div>
    <div style="font-size:13px;opacity:.75">{company} · JLCPCB Статус замовлень · {now_str}</div>
  </div>
  <div style="padding:16px 24px 8px">
    <p style="margin:0 0 12px;font-size:14px;font-weight:700;color:#1a237e">
      📋 Активні замовлення: {len(active)}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr style="background:#e8eaf6;font-size:11px;font-weight:700;color:#3949ab;text-transform:uppercase">
        <td style="padding:8px 10px">Замовлення</td>
        <td style="padding:8px 10px">Файл / SKU</td>
        <td style="padding:8px 10px">Статус</td>
        <td style="padding:8px 10px">Доставка ETA</td>
        <td style="padding:8px 10px">Трекінг</td>
      </tr>
      {rows_html}
    </table>
  </div>
  <div style="background:#f5f5f5;padding:14px 24px;border-top:1px solid #e0e0e0;margin-top:8px">
    <div style="font-weight:700;color:#1a237e;font-size:13px">&#9654; Minerva BI System</div>
    <div style="font-size:11px;color:#999;margin-top:2px">{company} · Автоматичне сповіщення · {now_str}</div>
  </div>
</div>
</body></html>'''

    tg_sent = email_sent = False

    if cfg.notify_telegram:
        tg_sent = _send_telegram(tg_text)

    if cfg.notify_email:
        recipients = _resolve_email_recipients(cfg)
        if recipients:
            email_sent = _send_html_email(
                f'[{company}] JLCPCB — Статус замовлень ({len(active)} активних)',
                html_body, plain_body, recipients,
            )

    return {'telegram': tg_sent, 'email': email_sent, 'count': len(active)}
