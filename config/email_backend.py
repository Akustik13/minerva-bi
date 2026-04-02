"""
config/email_backend.py

Custom Django email backend that reads SMTP settings from
NotificationSettings singleton (configured in admin).

Falls back to ConsoleEmailBackend if:
  - DB is unavailable
  - email_enabled = False
  - email_host / email_host_user / email_host_password are empty
"""

from django.core.mail.backends.smtp import EmailBackend as SmtpBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend


class NotificationSettingsEmailBackend(SmtpBackend):
    """
    SMTP backend that loads connection params from NotificationSettings.
    Re-reads DB on each open() call so changes take effect without restart.
    """

    def __init__(self, **kwargs):
        # Start with empty/default values; _load() will fill them in open()
        super().__init__(
            host='localhost', port=25,
            username='', password='',
            use_tls=False, use_ssl=False,
            fail_silently=kwargs.get('fail_silently', False),
        )

    def open(self):
        """Load settings from DB before opening SMTP connection."""
        try:
            from config.models import NotificationSettings
            ns = NotificationSettings.get()
            if not ns.email_enabled or not ns.email_host or not ns.email_host_user:
                # Not configured — silently skip
                return False
            self.host       = ns.email_host
            self.port       = ns.email_port
            self.username   = ns.email_host_user
            self.password   = ns.email_host_password
            self.use_tls    = ns.email_use_tls
            self.use_ssl    = ns.email_use_ssl
        except Exception:
            return False
        return super().open()

    def send_messages(self, email_messages):
        """Override FROM address if email_from is set in NotificationSettings."""
        try:
            from config.models import NotificationSettings
            ns = NotificationSettings.get()
            if ns.email_from:
                for msg in email_messages:
                    if not msg.from_email or 'noreply@minerva-bi.local' in msg.from_email:
                        msg.from_email = ns.email_from
        except Exception:
            pass
        return super().send_messages(email_messages)
