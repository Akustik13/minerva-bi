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


def notify_jlc_status_change(order, old_status: str, new_status: str) -> None:
    """Send email + Telegram when JLC order status changes."""
    from jlcpcb.models import JLCConfig
    cfg = JLCConfig.get()

    # Check if this event type is enabled
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

    product_info = ''
    if order.product_id:
        product_info = f'\n🏷 Товар: <b>{order.product.sku}</b>'
    elif order.description:
        product_info = f'\n📄 {order.description[:80]}'

    tracking_info = ''
    if new_status == 'shipped' and order.tracking_number:
        carrier = f' ({order.tracking_carrier})' if order.tracking_carrier else ''
        tracking_info = f'\n🔍 Трекінг: <code>{order.tracking_number}</code>{carrier}'
        if order.tracking_url:
            tracking_info += f'\n<a href="{order.tracking_url}">Відстежити посилку</a>'

    tg_text = (
        f'{icon} <b>JLCPCB — {new_label}</b>\n'
        f'Замовлення: <code>{order.jlc_order_id}</code>\n'
        f'Кількість: {order.quantity} шт.'
        f'{product_info}'
        f'\nСтатус: {old_label} → <b>{new_label}</b>'
        f'{tracking_info}\n'
        f'<i>{company}</i>'
    )

    email_subject = f'[{company}] JLCPCB {new_label}: {order.jlc_order_id}'
    email_body = (
        f'Замовлення JLCPCB {order.jlc_order_id}\n'
        f'Статус: {old_label} → {new_label}\n'
        f'Кількість: {order.quantity} шт.\n'
    )
    if order.description:
        email_body += f'Опис: {order.description}\n'
    if new_status == 'shipped' and order.tracking_number:
        carrier = f' ({order.tracking_carrier})' if order.tracking_carrier else ''
        email_body += f'Трекінг: {order.tracking_number}{carrier}\n'
        if order.tracking_url:
            email_body += f'Відстежити: {order.tracking_url}\n'

    if cfg.notify_telegram:
        _send_telegram(tg_text)

    if cfg.notify_email:
        recipients = _resolve_email_recipients(cfg)
        if recipients:
            _send_email(email_subject, email_body, recipients)

    order.last_notified_status = new_status
    order.save(update_fields=['last_notified_status', 'updated_at'])
