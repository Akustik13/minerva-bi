"""
bots/services/api_health.py

Helpers for DigiKey API health monitoring:
- notify_connection_failure()  — timeout / network error after all retries
- notify_reauth_needed()       — refresh_token invalid, manual OAuth required
"""
import logging

log = logging.getLogger(__name__)


def send_api_notification(config, subject: str, tg_text: str, html_body: str):
    """Send Telegram and/or Email using DigiKeyConfig api_notify_* settings."""
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.objects.filter(pk=1).first()
        if not ns:
            return

        if config.api_notify_telegram and ns.telegram_enabled:
            if config.api_notify_telegram_mode == 'private':
                _send_tg_private(ns, tg_text)
            else:
                from dashboard.notifications import _send_telegram
                _send_telegram(ns, tg_text)

        if config.api_notify_email and ns.email_enabled:
            import copy
            from dashboard.notifications import _send_event_email
            email_to = (config.api_notify_email_to or '').strip()
            if email_to:
                ns_copy = copy.copy(ns)
                ns_copy.email_to = email_to
                _send_event_email(ns_copy, subject, html_body)
            else:
                _send_event_email(ns, subject, html_body)
    except Exception:
        log.exception("API health notification failed")


def _send_tg_private(ns, text: str):
    """Send to superadmin's private Telegram chat; falls back to group if no telegram_id found."""
    try:
        from core.models import UserProfile
        profile = (
            UserProfile.objects
            .filter(user__is_superuser=True)
            .exclude(telegram_id=None)
            .first()
        )
        if not profile or not profile.telegram_id:
            from dashboard.notifications import _send_telegram
            _send_telegram(ns, text)
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{ns.telegram_bot_token}/sendMessage",
            json={"chat_id": profile.telegram_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        log.exception("Private Telegram send failed")


def notify_connection_failure(config, error: str, attempts: int,
                               source: str = "DigiKey синхронізація"):
    """Notify that API is unreachable after N retries."""
    if not config.api_notify_on_error:
        return
    subject = f"⚠️ DigiKey API — збій з'єднання"
    tg_text = (
        f"⚠️ <b>DigiKey API недоступний</b>\n"
        f"Джерело: {source}\n"
        f"Спроб: {attempts}\n"
        f"Помилка: {error[:250]}\n\n"
        f"Синхронізація не виконана. Перевірте підключення до інтернету та "
        f"статус DigiKey API."
    )
    html_body = (
        f"<p><b>DigiKey API не відповів після {attempts} спроб.</b></p>"
        f"<p><b>Джерело:</b> {source}</p>"
        f"<p><b>Помилка:</b><br><code>{error}</code></p>"
        f"<p>Перевірте підключення до інтернету та "
        f"<a href='https://status.digikey.com/'>статус DigiKey API</a>.</p>"
    )
    send_api_notification(config, subject, tg_text, html_body)


def notify_reauth_needed(config, error: str):
    """Notify that manual OAuth reauthorization is required (refresh_token invalid)."""
    if not config.api_notify_on_reauth:
        return
    base = (config.public_base_url or '').rstrip('/')
    admin_link = f"{base}/admin/bots/digikeyconfig/1/change/" if base else '/admin/bots/digikeyconfig/1/change/'

    subject = "🔐 DigiKey Marketplace — потрібна повторна авторизація"
    tg_text = (
        f"🔐 <b>DigiKey Marketplace: токен недійсний</b>\n"
        f"Помилка: {error[:250]}\n\n"
        f"Потрібна повторна OAuth авторизація.\n"
        f"🔗 Відкрийте DigiKey Config і натисніть\n"
        f"<b>🔑 Авторизувати Marketplace</b>"
        + (f"\n{admin_link}" if base else "")
    )
    html_body = (
        f"<p><b>DigiKey Marketplace refresh token недійсний або протух.</b></p>"
        f"<p><b>Помилка:</b><br><code>{error}</code></p>"
        f"<p>Необхідно пройти повторну авторизацію:</p>"
        f"<ol>"
        f"<li>Відкрийте <a href='{admin_link}'>DigiKey Config в адмінці</a></li>"
        f"<li>Натисніть кнопку <b>🔑 Авторизувати Marketplace</b></li>"
        f"<li>Пройдіть OAuth авторизацію в браузері</li>"
        f"</ol>"
    )
    send_api_notification(config, subject, tg_text, html_body)
