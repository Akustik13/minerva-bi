"""Листи покупцям магазину (особистий кабінет): підтвердження email, скидання пароля, сповіщення.

SMTP — акаунт «Відправник листів покупцям» магазину (email_assistant.EmailAccount) або, якщо його немає,
SMTP сповіщень Minerva (config.NotificationSettings). Копія в «Надісланих» НЕ зберігається
(на відміну від email_assistant.SMTPClient): у листах одноразові посилання, їх не повинно бути в скриньці CRM.
"""
from __future__ import annotations

import html
import logging
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

logger = logging.getLogger("shop")


class MailNotConfigured(Exception):
    pass


def _backend() -> str:
    # У тестах — locmem (tabele/settings_test.py)
    return getattr(settings, "SHOP_MAIL_BACKEND", "") or "django.core.mail.backends.smtp.EmailBackend"


def sender(shop):
    """(з'єднання SMTP, адреса From) для листів магазину."""
    acc = shop.mail_sender
    if acc is not None and acc.is_active and acc.smtp_host and acc.email_address:
        conn = get_connection(
            backend=_backend(), host=acc.smtp_host, port=int(acc.smtp_port or 587),
            username=acc.smtp_username, password=acc.smtp_password,
            use_ssl=bool(acc.smtp_use_ssl), use_tls=bool(acc.smtp_use_tls) and not acc.smtp_use_ssl,
            timeout=20, fail_silently=False,
        )
        return conn, formataddr((shop.mail_from_name or acc.display_name or shop.name, acc.email_address))

    from config.models import NotificationSettings
    ns = NotificationSettings.get()
    name, addr = parseaddr((ns.email_from or ns.email_host_user or "").strip())
    if not (ns.email_host and addr):
        raise MailNotConfigured("Не задано SMTP: оберіть «Відправник листів покупцям» у магазині "
                                "або заповніть SMTP у Налаштування → Сповіщення.")
    conn = get_connection(
        backend=_backend(), host=ns.email_host, port=ns.email_port,
        username=ns.email_host_user, password=ns.email_host_password,
        use_ssl=bool(ns.email_use_ssl), use_tls=bool(ns.email_use_tls) and not ns.email_use_ssl,
        timeout=20, fail_silently=False,
    )
    return conn, formataddr((shop.mail_from_name or name or shop.name, addr))


def describe_sender(shop) -> str:
    """Для адмінки: через що підуть листи."""
    try:
        conn, from_addr = sender(shop)
    except MailNotConfigured as e:
        return f"❌ {e}"
    src = "акаунт пошти" if shop.mail_sender_id else "SMTP сповіщень Minerva"
    server = f" · {conn.host}:{conn.port}" if getattr(conn, "host", None) else ""
    return f"{from_addr}{server} ({src})"


# ── Тексти (de / en) ─────────────────────────────────────────────────────────

TEXTS = {
    "verify": {
        "de": ("E-Mail-Adresse bestätigen – {shop}",
               "vielen Dank für Ihre Registrierung im Online-Shop von {shop}. Bitte bestätigen Sie Ihre "
               "E-Mail-Adresse. Zur Bestätigung geben Sie auf der Seite Ihr Passwort ein.",
               "Konto bestätigen",
               "Der Link ist {hours} Stunden gültig. Falls Sie kein Kundenkonto angelegt haben, ignorieren Sie "
               "diese E-Mail – ohne Bestätigung wird kein Konto aktiviert."),
        "en": ("Confirm your e-mail address – {shop}",
               "thank you for registering with the {shop} online shop. Please confirm your e-mail address. "
               "You will be asked for your password on the confirmation page.",
               "Confirm account",
               "The link is valid for {hours} hours. If you did not create an account, please ignore this "
               "e-mail – no account will be activated without confirmation."),
    },
    "exists": {
        "de": ("Ihr Kundenkonto – {shop}",
               "mit dieser E-Mail-Adresse wurde gerade versucht, ein Kundenkonto im Online-Shop von {shop} "
               "anzulegen. Für diese Adresse besteht bereits ein Konto – Sie können sich direkt anmelden. "
               "Falls Sie Ihr Passwort nicht mehr wissen, nutzen Sie „Passwort vergessen“ auf der Anmeldeseite.",
               "Zur Anmeldung",
               "Wenn Sie das nicht waren, können Sie diese E-Mail ignorieren. Ihr Konto bleibt unverändert."),
        "en": ("Your customer account – {shop}",
               "someone just tried to create an account in the {shop} online shop with this e-mail address. "
               "An account already exists for it – you can sign in directly. If you have forgotten your "
               "password, use “Forgot password” on the sign-in page.",
               "Sign in",
               "If this was not you, you can ignore this e-mail. Your account has not been changed."),
    },
    "reset": {
        "de": ("Passwort zurücksetzen – {shop}",
               "Sie haben ein neues Passwort für Ihr Kundenkonto im Online-Shop von {shop} angefordert.",
               "Neues Passwort festlegen",
               "Der Link ist {minutes} Minuten gültig und kann nur einmal verwendet werden. Falls Sie kein neues "
               "Passwort angefordert haben, ignorieren Sie diese E-Mail – Ihr Passwort bleibt unverändert."),
        "en": ("Reset your password – {shop}",
               "you have requested a new password for your customer account in the {shop} online shop.",
               "Set a new password",
               "The link is valid for {minutes} minutes and can only be used once. If you did not request a new "
               "password, please ignore this e-mail – your password remains unchanged."),
    },
    "changed": {
        "de": ("Ihr Passwort wurde geändert – {shop}",
               "das Passwort Ihres Kundenkontos im Online-Shop von {shop} wurde soeben geändert. "
               "Alle anderen Anmeldungen wurden beendet.",
               "Zum Kundenkonto",
               "Waren Sie das nicht? Setzen Sie Ihr Passwort sofort über „Passwort vergessen“ zurück und "
               "informieren Sie uns."),
        "en": ("Your password has been changed – {shop}",
               "the password of your customer account in the {shop} online shop has just been changed. "
               "All other sessions have been signed out.",
               "Go to my account",
               "Was this not you? Reset your password immediately via “Forgot password” and let us know."),
    },
    "deleted": {
        "de": ("Ihr Kundenkonto wurde gelöscht – {shop}",
               "Ihr Kundenkonto im Online-Shop von {shop} wurde gelöscht. Ihre Anmeldedaten und gespeicherten "
               "Adressen sind entfernt. Bestellungen bewahren wir aufgrund gesetzlicher Aufbewahrungsfristen auf.",
               "", "Sie können jederzeit ein neues Konto anlegen."),
        "en": ("Your customer account has been deleted – {shop}",
               "your customer account in the {shop} online shop has been deleted. Your sign-in details and saved "
               "addresses have been removed. We keep orders as required by statutory retention periods.",
               "", "You can create a new account at any time."),
    },
    "test": {
        "de": ("Testnachricht – {shop}", "dies ist eine Testnachricht aus Minerva: Die E-Mails des "
               "Kundenkontos (Bestätigung, Passwort) werden über diesen Absender versendet.", "", ""),
        "en": ("Test message – {shop}", "this is a test message from Minerva: customer account e-mails "
               "(confirmation, password) are sent via this sender.", "", ""),
    },
}
GREETING = {"de": "Guten Tag {name},", "en": "Dear {name},"}
GREETING_ANON = {"de": "Guten Tag,", "en": "Hello,"}


def compose(shop, kind: str, lang: str, *, name: str = "", link: str = "", **vars) -> tuple[str, str, str]:
    """(тема, текст, html) листа."""
    lang = lang if lang in ("de", "en") else "de"
    subject, intro, button, outro = TEXTS[kind][lang]
    fmt = {"shop": shop.name, **{k: str(v) for k, v in vars.items()}}
    subject, intro, outro = subject.format(**fmt), intro.format(**fmt), outro.format(**fmt)
    greet = GREETING[lang].format(name=name) if name else GREETING_ANON[lang]
    footer = (shop.mail_footer or shop.name).strip()

    text = "\n\n".join(p for p in [greet, intro, (f"{button}:\n{link}" if link else ""), outro, "—\n" + footer] if p)
    esc = html.escape
    btn = (f'<p style="margin:24px 0"><a href="{esc(link)}" style="background:#0b5cad;color:#fff;padding:12px 22px;'
           f'border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">{esc(button)}</a></p>'
           f'<p style="font-size:12px;color:#666;word-break:break-all">{esc(link)}</p>') if link else ""
    body = (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#222;max-width:560px">'
            f'<p>{esc(greet)}</p><p>{esc(intro)}</p>{btn}'
            + (f'<p style="color:#555">{esc(outro)}</p>' if outro else "")
            + f'<hr style="border:0;border-top:1px solid #ddd;margin:24px 0">'
              f'<p style="font-size:12px;color:#777;white-space:pre-line">{esc(footer)}</p></div>')
    return subject, text, body


def send(shop, to: str, kind: str, lang: str, **kwargs) -> None:
    """Надсилає лист; помилки SMTP — винятком (обробляє виклик)."""
    subject, text, body = compose(shop, kind, lang, **kwargs)
    conn, from_addr = sender(shop)
    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=from_addr, to=[to], connection=conn)
    msg.attach_alternative(body, "text/html")
    msg.send()
    logger.info("Shop mail %s → %s (%s)", kind, to, shop.slug)
