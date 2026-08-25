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


def _get_telegram_token_and_chat() -> tuple[str, str]:
    try:
        from config.models import NotificationSettings
        s = NotificationSettings.get()
        return s.telegram_bot_token or '', s.telegram_chat_id or ''
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


def _send_email(subject: str, body: str, to_addresses: list[str]) -> bool:
    if not to_addresses:
        return False
    try:
        from django.core.mail import send_mail
        send_mail(subject, body, None, to_addresses, fail_silently=True)
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


def notify_jlc_status_change(order, old_status: str, new_status: str,
                              force: bool = False) -> None:
    """Send email + Telegram when JLC order status changes.

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

    # Product / description line
    product_info = ''
    if order.product_id:
        product_info = f'\n🏷 Товар: <b>{order.product.sku}</b>'
    elif order.description:
        product_info = f'\n📄 {order.description[:80]}'

    # Shipping method (from model field or raw_data fallback)
    shipping_method = order.tracking_carrier or ''
    if not shipping_method and isinstance(getattr(order, 'raw_data', None), dict):
        shipping_method = order.raw_data.get('shippingMethod', '')

    # Expected delivery date — from model field or raw_data fallback
    eta_line = ''
    eta_plain = ''
    expected_date = order.expected_date
    if not expected_date and isinstance(getattr(order, 'raw_data', None), dict):
        for _item in order.raw_data.get('orderItem', []):
            dt_str = (_item.get('pcbItem') or {}).get('deliveryTime')
            if dt_str:
                try:
                    from datetime import date as _date, datetime as _datetime
                    expected_date = _datetime.strptime(dt_str[:10], '%Y-%m-%d').date()
                except Exception:
                    pass
                break
    if expected_date:
        try:
            months_uk = ['', 'січня', 'лютого', 'березня', 'квітня', 'травня', 'червня',
                         'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня']
            d = expected_date
            eta_str = f'{d.day} {months_uk[d.month]} {d.year}'
        except Exception:
            eta_str = str(expected_date)
        eta_line  = f'\n📅 Очікувана доставка: <b>{eta_str}</b>'
        eta_plain = f'Очікувана доставка: {eta_str}\n'

    # Tracking / carrier block
    tracking_info  = ''
    tracking_plain = ''
    if order.tracking_number:
        carrier_str = f' ({shipping_method})' if shipping_method else ''
        tracking_info  = f'\n🔍 Трекінг: <code>{order.tracking_number}</code>{carrier_str}'
        tracking_plain = f'Трекінг: {order.tracking_number}{carrier_str}\n'
        if order.tracking_url:
            tracking_info  += f'\n<a href="{order.tracking_url}">🔗 Відстежити посилку</a>'
            tracking_plain += f'Відстежити: {order.tracking_url}\n'
    else:
        # No tracking yet — show shipping method and hint
        if shipping_method:
            tracking_info  = f'\n🚚 Перевізник: <b>{shipping_method}</b>'
            tracking_plain = f'Перевізник: {shipping_method}\n'
        if new_status == 'shipped':
            tracking_info  += '\n⚠️ Трекінг-номер не вказано — перевір email від JLCPCB'
            tracking_plain += 'Трекінг-номер буде у листі від JLCPCB. Введи його в картку замовлення.\n'

    # Status line
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

    email_subject = f'[{company}] JLCPCB {new_label}: {order.jlc_order_id}'
    email_body = (
        f'Замовлення JLCPCB {order.jlc_order_id}\n'
        f'Статус: {new_label}\n'
        f'Кількість: {order.quantity} шт.\n'
    )
    if order.description:
        email_body += f'Опис: {order.description}\n'
    email_body += eta_plain + tracking_plain

    if cfg.notify_telegram:
        _send_telegram(tg_text)

    if cfg.notify_email:
        recipients = _resolve_email_recipients(cfg)
        if recipients:
            _send_email(email_subject, email_body, recipients)

    if not force:
        order.last_notified_status = new_status
        order.save(update_fields=['last_notified_status', 'updated_at'])


def notify_jlc_active_orders_summary() -> dict:
    """Send a Telegram+email summary of all active JLCPCB orders.

    Returns dict with keys: telegram (bool), email (bool), count (int).
    """
    from jlcpcb.models import JLCConfig, JLCOrder
    cfg = JLCConfig.get()
    company = _get_company_name()

    active = list(
        JLCOrder.objects.exclude(
            local_status__in=['delivered', 'cancelled']
        ).select_related('product').order_by('order_date')
    )

    if not active:
        tg_text    = f'📋 <b>JLCPCB</b> — активних замовлень немає.\n<i>{company}</i>'
        email_body = 'JLCPCB — активних замовлень немає.'
    else:
        lines = [f'📋 <b>JLCPCB — Активні замовлення: {len(active)}</b>']
        lines.append('─' * 30)
        for o in active:
            icon  = _STATUS_ICONS.get(o.local_status, '📋')
            label = _STATUS_LABELS_UK.get(o.local_status, o.local_status)
            name  = (
                o.product.sku if o.product_id
                else (o.description[:35] if o.description else o.jlc_order_id)
            )
            tracking = f' | 📍 <code>{o.tracking_number}</code>' if o.tracking_number else ''
            lines.append(f'{icon} <code>{o.jlc_order_id}</code>\n   {name} — <b>{label}</b>{tracking}')
        lines.append(f'─' * 30)
        lines.append(f'<i>{company}</i>')
        tg_text = '\n'.join(lines)

        plain_lines = [f'JLCPCB — Активні замовлення: {len(active)}', '']
        for o in active:
            label = _STATUS_LABELS_UK.get(o.local_status, o.local_status)
            name  = (
                o.product.sku if o.product_id
                else (o.description[:35] if o.description else o.jlc_order_id)
            )
            tracking = f' | Трекінг: {o.tracking_number}' if o.tracking_number else ''
            plain_lines.append(f'{o.jlc_order_id} — {name} — {label}{tracking}')
        email_body = '\n'.join(plain_lines)

    tg_sent    = False
    email_sent = False

    if cfg.notify_telegram:
        tg_sent = _send_telegram(tg_text)

    if cfg.notify_email:
        recipients = _resolve_email_recipients(cfg)
        if recipients:
            email_sent = _send_email(
                f'[{company}] JLCPCB — Статус замовлень',
                email_body,
                recipients,
            )

    return {'telegram': tg_sent, 'email': email_sent, 'count': len(active)}
