"""
python manage.py send_calendar_reminders

Sends calendar event reminders via:
  - Telegram  (if CalendarSettings.notify_telegram)
  - Email     (if CalendarSettings.notify_email)

Push notifications are handled client-side via /calendar/pending-push/ polling.
Run every ~120 s from cron_runner.sh.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Send pending calendar event reminders (Telegram + Email)'

    def handle(self, *args, **options):
        from calendar_app.models import CalendarEvent, CalendarSettings

        now = timezone.now()
        events = (CalendarEvent.objects
                  .filter(is_done=False, remind_sent=False)
                  .select_related('user', 'crm_customer'))

        sent = 0
        for event in events:
            remind_at = event.start_at - timedelta(minutes=event.remind_minutes_before)
            if remind_at > now:
                continue

            cfg = CalendarSettings.for_user(event.user)
            self._notify(event, cfg)
            event.remind_sent = True
            event.save(update_fields=['remind_sent'])
            sent += 1

        if sent:
            self.stdout.write(self.style.SUCCESS(f'Sent {sent} calendar reminder(s)'))

    # ── internal helpers ────────────────────────────────────────────────────

    def _notify(self, event, cfg):
        from config.models import NotificationSettings
        ns = NotificationSettings.current()

        time_str = event.start_at.strftime('%d.%m.%Y %H:%M')
        tg_text = (
            f'🔔 <b>Нагадування</b>: {event.title}\n'
            f'📅 {time_str}\n'
            f'📌 {event.get_event_type_display()}'
        )
        if event.crm_customer:
            tg_text += f'\n👤 {event.crm_customer}'
        if event.description:
            tg_text += f'\n{event.description[:200]}'

        if cfg.notify_telegram:
            self._send_telegram(ns, event, cfg, tg_text)

        if cfg.notify_email:
            self._send_email(ns, event, cfg, time_str, tg_text)

    def _send_telegram(self, ns, event, cfg, text):
        import urllib.request
        import urllib.parse

        # Priority: CalendarSettings override → UserProfile.telegram_id → system
        chat_id = cfg.telegram_chat_id
        if not chat_id:
            try:
                chat_id = str(event.user.profile.telegram_id or '')
            except Exception:
                chat_id = ''
        if not chat_id:
            chat_id = ns.telegram_chat_id or ''

        token = ns.telegram_bot_token or ''
        if not chat_id or not token:
            return

        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
        }).encode()
        try:
            urllib.request.urlopen(url, data, timeout=10)
        except Exception as exc:
            self.stderr.write(f'[calendar] Telegram error: {exc}')

    def _send_email(self, ns, event, cfg, time_str, plain_text):
        if not (ns.email_host and ns.email_from):
            return

        to_email = cfg.email_to or ns.email_to or ''
        if not to_email:
            return

        from django.core.mail import get_connection, EmailMultiAlternatives

        subject = f'🔔 Нагадування: {event.title}'
        html = (
            f'<p>🔔 <b>Нагадування про подію</b></p>'
            f'<table cellpadding="4">'
            f'<tr><td><b>Назва:</b></td><td>{event.title}</td></tr>'
            f'<tr><td><b>Час:</b></td><td>{time_str}</td></tr>'
            f'<tr><td><b>Тип:</b></td><td>{event.get_event_type_display()}</td></tr>'
        )
        if event.crm_customer:
            html += f'<tr><td><b>Клієнт:</b></td><td>{event.crm_customer}</td></tr>'
        if event.description:
            html += f'<tr><td><b>Опис:</b></td><td>{event.description}</td></tr>'
        html += '</table>'

        try:
            conn = get_connection(
                backend='django.core.mail.backends.smtp.EmailBackend',
                host=ns.email_host,
                port=ns.email_port,
                username=ns.email_host_user,
                password=ns.email_host_password,
                use_tls=ns.email_use_tls,
                use_ssl=ns.email_use_ssl,
            )
            msg = EmailMultiAlternatives(
                subject, plain_text, ns.email_from, [to_email], connection=conn)
            msg.attach_alternative(html, 'text/html')
            msg.send()
        except Exception as exc:
            self.stderr.write(f'[calendar] Email error: {exc}')
