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


def _minerva_sender(shop: Shop, purpose: str = "account") -> str:
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


def _box_label(acc) -> str:
    owner = (acc.user.get_full_name() or acc.user.get_username()) if acc.user_id else ""
    return f"{acc.email_address}" + (f" — {owner}" if owner else "")


def _boxes():
    from email_assistant.models import EmailAccount
    return [a for a in EmailAccount.objects.filter(is_active=True).select_related("user").order_by("email_address")
            if mail.usable(a)]


def _choice(name, value, options, note=""):
    return {"name": name, "value": value, "options": options, "note": note}


def rows(shop: Shop) -> list[dict]:
    """[{mail, who, sender, to, change, route}] — route {name, value, options [(значення, підпис)], note}: меню
    «З якої адреси» (зберігає ShopAdmin.save_model → save_routes). У порядку важливості."""
    missing = "❌ не задано: оберіть скриньку або SMTP у «Налаштування → Сповіщення»"
    sender = _minerva_sender(shop) or missing
    acc = shop.mail_sender
    smtp = ([_url(reverse("admin:email_assistant_emailaccount_change", args=[acc.pk]), f"SMTP скриньки {acc.email_address}")]
            if acc is not None else [])
    templates = _url(reverse("admin:shop_shopmailtemplate_changelist") + f"?shop__id__exact={shop.pk}", "📝 Шаблони листів")
    notifications = _url(reverse("admin:config_notificationsettings_changelist"), "Налаштування → Сповіщення")
    add_box = _url(reverse("admin:email_assistant_emailaccount_add"), "➕ Додати скриньку в Minerva")
    team = mail.addresses(shop.team_emails)
    ns = _notifications()
    reply = f" · відповіді — на {shop.mail_reply_to}" if shop.mail_reply_to else ""
    bcc = mail.addresses(shop.mail_bcc)
    boxes = [(f"acc:{a.pk}", _box_label(a)) for a in _boxes()]
    ns_label = (f"{ns['sender']} — SMTP сповіщень Minerva" if ns["sender"] else "SMTP сповіщень Minerva")
    main_value = f"acc:{acc.pk}" if mail.usable(acc) else ""
    if acc is not None and not mail.usable(acc):   # вибрана, але вимкнена / без SMTP — показати, щоб було видно
        boxes.append((f"acc:{acc.pk}", f"{acc.email_address} — ⚠ вимкнена або без SMTP"))
    same = f"як «Підтвердження email…» ({sender if main_value else ns_label})"
    notices_sender = _minerva_sender(shop, "notices") or missing

    def routed(purpose):
        r = mail.route_of(shop, purpose)
        return r if r == "ns" or r.startswith("acc:") else ""

    out = [
        {"mail": "Підтвердження email, новий пароль, запрошення від менеджера", "who": "Minerva", "sender": sender,
         "to": "покупцю (без прихованої копії — в листі одноразове посилання)",
         "route": _choice("route_account", main_value, [("", ns_label)] + boxes),
         "change": [_go("mail_from_name", "Ім'я відправника"), *smtp, add_box, templates]},
        {"mail": "«Willkommen», «Zahlung erhalten», пароль змінено, акаунт видалено", "who": "Minerva",
         "sender": notices_sender + reply,
         "to": "покупцю" + (f" · прихована копія: {', '.join(bcc)}" if bcc else ""),
         "route": _choice("route_notices", routed("notices"), [("", same), ("ns", ns_label)] + boxes),
         "change": [_go("mail_reply_to", "Відповіді покупців"), _go("mail_bcc", "Прихована копія"),
                    _go("mail_welcome", "Лист «Ласкаво просимо»"), templates]},
    ]
    if team:
        team_sender, team_to, team_note = _minerva_sender(shop, "team") or missing, ", ".join(team), ""
    else:
        team_sender = ns["sender"] or "SMTP сповіщень Minerva"
        team_to = ", ".join(ns["to"]) or "нікому — email у «Налаштування → Сповіщення» вимкнено або без адрес"
        team_note = "вибір діє, коли задано «Сповіщення команді — на адреси»; зараз — SMTP сповіщень"
    out.append({"mail": "Команді: оплата отримана, новий акаунт покупця", "who": "Minerva", "sender": team_sender,
                "to": team_to + (" · Telegram" if ns["telegram"] else ""),
                "route": _choice("route_team", routed("team"), [("", same), ("ns", ns_label)] + boxes, team_note),
                "change": [_go("team_emails", "Сповіщення команді — на адреси"),
                           _go("team_notify_registration", "Команді: новий акаунт покупця"), notifications]})

    host, report = _site_report(shop)
    site_admin = _site_admin(shop)
    on_site = [_url(site_admin, SITE_MAIL, external=True)] if site_admin else []
    who = f"сайт {host}" if host else "сайт"
    site_acc = site_mail.sender_account(shop)
    managed = shop.site_mail_managed and site_acc is not None
    files = (f"налаштування сайту: {report.get('from') or '—'} ({report.get('host') or 'SMTP не задано'})"
             if report else "налаштування сайту (/admin/ → «✉️ Пошта»)")
    site_value = "files" if not shop.site_mail_managed else routed("site").replace("ns", "")
    site_same = f"як «Підтвердження email…» ({sender})" if main_value else "як «Підтвердження email…» — ⚠ там не скринька"
    site_route = _choice("route_site", site_value, [("files", files), ("", site_same)] + boxes,
                         "одна скринька для всіх листів сайту")
    if managed:
        site_sender = f"{site_acc.email_address} (з Minerva)"
        confirm = shop.site_order_confirmation
        notify = ", ".join(team) or ((report.get("notify") or "—") + " (як на сайті)")
        change = [*on_site]
    elif report:
        site_sender = f"{report.get('from') or '—'} ({report.get('host') or 'SMTP не задано'}) — файли сайту"
        confirm = report.get("confirmation")
        notify = report.get("notify") or "—"
        change = [*on_site]
    else:
        site_sender = "невідомо — сайт ще не звітував про пошту"
        confirm, notify = None, "—"
        change = [*on_site]
    out += [
        {"mail": "Підтвердження замовлення покупцю", "who": who, "sender": site_sender,
         "to": "невідомо" if confirm is None else ("покупцю" if confirm else "не надсилається"), "route": site_route,
         "change": change + ([_go("site_order_confirmation", "Сайт надсилає покупцю підтвердження")] if managed else [])},
        {"mail": "Команді: нове замовлення з сайту", "who": who, "sender": site_sender, "to": notify,
         "route": None, "same_as": "Підтвердження замовлення покупцю",
         "change": ([_go("team_emails", "Сповіщення команді — на адреси")] if managed else []) + change},
        {"mail": "Контактна форма сайту", "who": who, "sender": site_sender,
         "to": report.get("recipient") or "—", "route": None, "same_as": "Підтвердження замовлення покупцю",
         "change": on_site and [_url(site_admin, SITE_MAIL + " → «Отримувач запитів з форми»", external=True)]},
    ]
    return out


def save_routes(shop: Shop, post) -> list[str]:
    """Меню «З якої адреси» таблиці → mail_sender, mail_routes, site_mail_managed (shop ще не збережено).
    Лише ключі route_*, що прийшли у формі; невідома скринька — без змін. Повертає змінені призначення."""
    def box(value):
        acc = mail.route_account(value)
        return acc if acc is not None and acc.is_active else None

    changed = []
    if "route_account" in post:
        v = str(post.get("route_account") or "")
        acc = box(v) if v else None
        if (not v or acc is not None) and shop.mail_sender_id != (acc.pk if acc else None):
            shop.mail_sender, changed = acc, changed + ["account"]
    routes = dict(shop.mail_routes or {})
    for purpose in ("notices", "team"):
        key = f"route_{purpose}"
        if key in post:
            v = str(post.get(key) or "")
            if v in ("", "ns") or box(v) is not None:
                if routes.get(purpose, "") != v:
                    changed.append(purpose)
                routes[purpose] = v
    if "route_site" in post:
        v = str(post.get("route_site") or "")
        managed = v != "files"
        if v == "files" or v == "" or box(v) is not None:
            new = "" if v in ("files", "") else v
            if routes.get("site", "") != new or shop.site_mail_managed != managed:
                changed.append("site")
            routes["site"], shop.site_mail_managed = new, managed
    shop.mail_routes = {k: v for k, v in routes.items() if v}
    return changed
