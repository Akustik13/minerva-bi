"""«Що звідки йде» — карта листів магазину для картки магазину (блок «✉️ Пошта магазину»).

Кожен рядок: який лист, хто надсилає (Minerva чи сайт), з якої адреси, кому і де це змінити (поле картки —
перехід data-go, або сторінка: скринька, сповіщення, шаблони, адмінка сайту). Лише читає налаштування й
останній звіт сайту про пошту (Shop.site_mail_reports); нічого не надсилає.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from django.urls import reverse

from . import mail, site_mail
from .models import Shop

SITE_MAIL = "сайт /admin/ → «✉️ Пошта»"


def _go(field: str, label: str) -> dict:
    return {"go": field, "label": label}


def _url(url: str, label: str, external: bool = False) -> dict:
    return {"url": url, "label": label, "external": external}


def _site_admin(shop: Shop) -> str:
    parts = urlsplit(shop.url or "")
    return f"{parts.scheme}://{parts.netloc}/admin/" if parts.scheme in ("http", "https") and parts.netloc else ""


def _minerva_sender(shop: Shop, purpose: str = "customer") -> str:
    try:
        return mail.smtp_settings(shop, purpose)["from"]
    except mail.MailNotConfigured:
        return ""


def _notifications() -> dict:
    """SMTP і Telegram з «Налаштування → Сповіщення» — туди йдуть сповіщення команді без адрес у магазині."""
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception:
        return {"to": [], "sender": "", "telegram": False}
    on = bool(getattr(ns, "email_enabled", False))
    return {"to": mail.addresses(ns.email_to) if on else [], "sender": (ns.email_from or ns.email_host_user or "") if on else "",
            "telegram": bool(getattr(ns, "telegram_enabled", False) and getattr(ns, "telegram_chat_id", ""))}


def _site_report(shop: Shop) -> tuple[str, dict]:
    """Найновіший звіт сайту про пошту: (сайт, звіт) або ('', {})."""
    reports = shop.site_mail_reports or {}
    if not reports:
        return "", {}
    host, report = max(reports.items(), key=lambda kv: (kv[1] or {}).get("at") or "")
    return host, report or {}


def rows(shop: Shop) -> list[dict]:
    """[{mail, who, sender, to, change: [{go, label} | {url, label, external}]}] — у порядку важливості."""
    missing = "❌ не задано: оберіть «Відправник листів покупцям» або SMTP у «Налаштування → Сповіщення»"
    sender = _minerva_sender(shop) or missing
    paid_sender = _minerva_sender(shop, "payment") or missing
    acc = shop.mail_sender
    smtp = ([_url(reverse("admin:email_assistant_emailaccount_change", args=[acc.pk]), f"SMTP скриньки {acc.email_address}")]
            if acc is not None else [])
    templates = _url(reverse("admin:shop_shopmailtemplate_changelist") + f"?shop__id__exact={shop.pk}", "📝 Шаблони листів")
    notifications = _url(reverse("admin:config_notificationsettings_changelist"), "Налаштування → Сповіщення")
    team = mail.addresses(shop.team_emails)
    ns = _notifications()
    reply = f" · відповіді — на {shop.mail_reply_to}" if shop.mail_reply_to else ""
    bcc = mail.addresses(shop.mail_bcc)

    out = [
        {"mail": "Підтвердження email, новий пароль, запрошення від менеджера", "who": "Minerva", "sender": sender,
         "to": "покупцю (без прихованої копії — в листі одноразове посилання)",
         "change": [_go("mail_sender", "Відправник листів покупцям"), _go("mail_from_name", "Ім'я відправника"),
                    *smtp, templates]},
        {"mail": "«Willkommen», пароль змінено, акаунт видалено", "who": "Minerva",
         "sender": sender + reply,
         "to": "покупцю" + (f" · прихована копія: {', '.join(bcc)}" if bcc else ""),
         "change": [_go("mail_reply_to", "Відповіді покупців"), _go("mail_bcc", "Прихована копія"),
                    _go("mail_welcome", "Лист «Ласкаво просимо»"), templates]},
        {"mail": "«Zahlung erhalten» — оплату отримано", "who": "Minerva", "sender": paid_sender + reply,
         "to": "покупцю" + (f" · прихована копія: {', '.join(bcc)}" if bcc else ""),
         "change": [_go("mail_sender_payments", "«Zahlung erhalten» — з якої скриньки"), templates]},
    ]
    if team:
        team_sender, team_to = _minerva_sender(shop, "team") or missing, ", ".join(team)
    else:
        team_sender = ns["sender"] or "SMTP сповіщень Minerva"
        team_to = ", ".join(ns["to"]) or "нікому — email у «Налаштування → Сповіщення» вимкнено або без адрес"
    out.append({"mail": "Команді: оплата отримана, новий акаунт покупця", "who": "Minerva", "sender": team_sender,
                "to": team_to + (" · Telegram" if ns["telegram"] else ""),
                "change": [_go("team_emails", "Сповіщення команді — на адреси"),
                           _go("mail_sender_team", "Сповіщення команді — з якої скриньки"),
                           _go("team_notify_registration", "Команді: новий акаунт покупця"), notifications]})

    host, report = _site_report(shop)
    site_admin = _site_admin(shop)
    on_site = [_url(site_admin, SITE_MAIL, external=True)] if site_admin else []
    who = f"сайт {host}" if host else "сайт"
    managed = shop.site_mail_managed and site_mail.sender_account(shop) is not None
    if managed:
        site_sender = (_minerva_sender(shop, "site") or missing) + " (з Minerva)"
        confirm = shop.site_order_confirmation
        notify = ", ".join(team) or ((report.get("notify") or "—") + " (як на сайті)")
        change = [_go("mail_sender_site", "Листи сайту — з якої скриньки"),
                  _go("site_mail_managed", "Сайт надсилає листи з цієї ж скриньки")]
    elif report:
        site_sender = f"{report.get('from') or '—'} ({report.get('host') or 'SMTP не задано'}) — файли сайту"
        confirm = report.get("confirmation")
        notify = report.get("notify") or "—"
        change = [*on_site, _go("site_mail_managed", "або брати пошту з Minerva")]
    else:
        site_sender = "невідомо — сайт ще не звітував про пошту"
        confirm, notify = None, "—"
        change = [*on_site, _go("site_mail_managed", "або брати пошту з Minerva")]
    out += [
        {"mail": "Підтвердження замовлення покупцю", "who": who, "sender": site_sender,
         "to": "невідомо" if confirm is None else ("покупцю" if confirm else "не надсилається"),
         "change": change + ([_go("site_order_confirmation", "Сайт надсилає покупцю підтвердження")] if managed else [])},
        {"mail": "Команді: нове замовлення з сайту", "who": who, "sender": site_sender, "to": notify,
         "change": ([_go("team_emails", "Сповіщення команді — на адреси")] if managed else []) + change},
        {"mail": "Контактна форма сайту", "who": who, "sender": site_sender,
         "to": report.get("recipient") or "—",
         "change": on_site and [_url(site_admin, SITE_MAIL + " → «Отримувач запитів з форми»", external=True)]},
    ]
    return out
