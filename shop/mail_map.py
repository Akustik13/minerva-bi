"""«Хто що кому шле» — карта пошти магазину (картка магазину → блок «✉️ Пошта магазину»).

Три частини:
① скриньки — з якої адреси йде кожна група листів (меню «З якої скриньки» → save_routes: mail_sender,
  mail_routes, site_mail_managed) і куди відповідають покупці;
② листи покупцю і ③ листи команді — кожен лист окремим рядком: хто надсилає (Minerva чи сайт) і від якої адреси,
  коли, чи надсилається, кому, копія (CC) і прихована копія (BCC; лише покупцю). Копії — поля mlc__<лист> /
  mlb__<лист> → ShopForm._clean_letter_copies → Shop.mail_letters (shop/mail.py copies; сайт отримує їх у
  GET /shop/site-mail/ → letters).
Лише читає налаштування й останній звіт сайту про пошту (Shop.site_mail_reports); нічого не надсилає.
"""
from __future__ import annotations

from email.utils import formataddr
from urllib.parse import urlsplit

from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from . import mail, site_mail
from .models import Shop


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
        return {"to": [], "sender": "", "telegram": False, "bot": False, "chat": ""}
    on = bool(getattr(ns, "email_enabled", False))
    return {"to": mail.addresses(ns.email_to) if on else [], "sender": (ns.email_from or ns.email_host_user or "") if on else "",
            "telegram": bool(getattr(ns, "telegram_enabled", False) and getattr(ns, "telegram_chat_id", "")),
            "bot": bool(getattr(ns, "telegram_enabled", False) and getattr(ns, "telegram_bot_token", "")),
            "chat": str(getattr(ns, "telegram_chat_id", "") or "").strip()}


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


def _templates(shop: Shop, kind: str = "") -> dict:
    url = reverse("admin:shop_shopmailtemplate_changelist") + f"?shop__id__exact={shop.pk}"
    return _url(url + (f"&kind__exact={kind}" if kind else ""), "📝 Текст")


def _invoice_auto() -> bool | None:
    try:
        from sales.models_invoice import InvoiceSettings
        return bool(InvoiceSettings.get().auto_send)
    except Exception:  # noqa: BLE001 — без рахунків
        return None


def site_state(shop: Shop) -> dict:
    """Як зараз пише сайт: зі скриньки Minerva (managed) чи зі своїх налаштувань (звіт сайту)."""
    host, report = _site_report(shop)
    acc = site_mail.sender_account(shop)
    managed = shop.site_mail_managed and acc is not None
    team = mail.addresses(shop.team_emails)
    if managed:
        sender = formataddr((shop.mail_from_name or acc.display_name or shop.name, acc.email_address))
        confirm, notify = shop.site_order_confirmation, ", ".join(team) or (
            f"{report.get('notify') or '—'} (як на сайті: адрес команди в Minerva немає)")
    elif report:
        sender = f"{report.get('from') or '—'} ({report.get('host') or 'SMTP не задано'}) — налаштування сайту"
        confirm, notify = report.get("confirmation"), report.get("notify") or "—"
    else:
        sender, confirm, notify = "невідомо — сайт ще не звітував про пошту", None, "—"
    return {"host": host, "report": report, "managed": managed, "sender": sender, "confirm": confirm,
            "notify": notify, "recipient": report.get("recipient") or "—", "letters_ok": site_mail.supports_letters(report),
            "admin": _site_admin(shop), "who": f"🌐 сайт {host}" if host else "🌐 сайт"}


def senders(shop: Shop, st: dict | None = None) -> list[dict]:
    """① З якої скриньки: [{key, title, letters, route, sender, note}] — меню «З якої скриньки» на кожне
    призначення (shop/mail.py ROUTED) і адреса, з якої лист піде насправді."""
    missing = "❌ не задано: оберіть скриньку або SMTP у «Налаштування → Сповіщення»"
    acc = shop.mail_sender
    ns = _notifications()
    boxes = [(f"acc:{a.pk}", _box_label(a)) for a in _boxes()]
    ns_label = f"{ns['sender']} — SMTP сповіщень Minerva" if ns["sender"] else "SMTP сповіщень Minerva"
    main_value = f"acc:{acc.pk}" if mail.usable(acc) else ""
    if acc is not None and not mail.usable(acc):   # вибрана, але вимкнена / без SMTP — показати, щоб було видно
        boxes.append((f"acc:{acc.pk}", f"{acc.email_address} — ⚠ вимкнена або без SMTP"))
    sender = _minerva_sender(shop) or missing
    same = f"як «🔑 Кабінет покупця» ({sender if main_value else ns_label})"

    def routed(purpose):
        r = mail.route_of(shop, purpose)
        return r if r == "ns" or r.startswith("acc:") else ""

    team = mail.addresses(shop.team_emails)
    st = st or site_state(shop)
    report = st["report"]
    files = (f"налаштування сайту: {report.get('from') or '—'} ({report.get('host') or 'SMTP не задано'})"
             if report else "налаштування сайту (/admin/ → «✉️ Пошта»)")
    site_value = "files" if not shop.site_mail_managed else routed("site").replace("ns", "")
    site_same = f"як «🔑 Кабінет покупця» ({sender})" if main_value else "як «🔑 Кабінет покупця» — ⚠ там не скринька"
    return [
        {"key": "account", "title": "🔑 Кабінет покупця",
         "letters": "підтвердження email, новий пароль, запрошення, «Konto existiert bereits»",
         "route": _choice("route_account", main_value, [("", ns_label)] + boxes), "sender": sender, "note": ""},
        {"key": "notices", "title": "✉️ Інші листи покупцю від Minerva",
         "letters": "«Willkommen», «Zahlung erhalten», пароль змінено, акаунт видалено, рахунок",
         "route": _choice("route_notices", routed("notices"), [("", same), ("ns", ns_label)] + boxes),
         "sender": _minerva_sender(shop, "notices") or missing, "note": ""},
        {"key": "team", "title": "👥 Команді від Minerva", "letters": "оплату отримано, новий акаунт покупця",
         "route": _choice("route_team", routed("team"), [("", same), ("ns", ns_label)] + boxes),
         "sender": (_minerva_sender(shop, "team") or missing) if team else (ns["sender"] or "SMTP сповіщень Minerva"),
         "note": "" if team else "адрес команди ще немає (③) — зараз ідуть з SMTP сповіщень Minerva"},
        {"key": "site", "title": "🌐 Листи сайту",
         "letters": "підтвердження замовлення й запиту пропозиції; команді — замовлення, запит, контактна форма",
         "route": _choice("route_site", site_value, [("files", files), ("", site_same)] + boxes),
         "sender": st["sender"], "note": "одна скринька на всі листи сайту; сайт перевіряє раз на хвилину"},
    ]


def _check(sync, on, label="так"):
    return {"type": "check", "sync": sync, "on": bool(on), "label": label}


def _text(text):
    return {"type": "text", "text": text}


def customer_rows(shop: Shop, st: dict | None = None) -> list[dict]:
    """② Листи покупцю: [{key, title, when, who, sender, send, copy, note, text, site}]. copy False — без копій."""
    st = st or site_state(shop)
    notices = _minerva_sender(shop, "notices") or "❌ не задано"
    account = _minerva_sender(shop) or "❌ не задано"
    minerva = "🏛 Minerva"
    auto = _invoice_auto()
    site_text = [_url(st["admin"], "📨 Листи сайту", external=True)] if st["admin"] else []
    if st["managed"]:
        site_send = _check("site_order_confirmation", shop.site_order_confirmation)
    else:
        site_send = _text({True: "так — налаштування сайту", False: "ні — налаштування сайту"}
                          .get(st["confirm"], "невідомо"))
    try:
        invoices = reverse("sales_invoice_list") + "#auto"
    except NoReverseMatch:
        invoices = ""
    return [
        {"key": "", "title": "🔑 Підтвердження email · новий пароль · запрошення",
         "when": "реєстрація в кабінеті, «Passwort vergessen», менеджер створив акаунт", "who": minerva,
         "sender": account, "send": _text("завжди"), "copy": False,
         "note": "без копій: в листі одноразове посилання", "text": [_templates(shop)]},
        {"key": "exists", "title": "ℹ️ «Konto existiert bereits»", "when": "реєстрація на email, для якого акаунт уже є",
         "who": minerva, "sender": account, "send": _text("завжди"), "copy": True, "text": [_templates(shop, "exists")]},
        {"key": "welcome", "title": "👋 «Willkommen»",
         "when": "після підтвердження реєстрації або першого пароля за запрошенням", "who": minerva, "sender": notices,
         "send": _check("mail_welcome", shop.mail_welcome), "copy": True, "text": [_templates(shop, "welcome")]},
        {"key": "paid", "title": "💳 «Zahlung erhalten»", "when": "онлайн-оплату (Stripe) отримано", "who": minerva,
         "sender": notices, "send": _text("завжди"), "copy": True, "text": [_templates(shop, "paid")]},
        {"key": "changed", "title": "🔒 «Passwort geändert»", "when": "покупець змінив пароль", "who": minerva,
         "sender": notices, "send": _text("завжди"), "copy": True, "text": [_templates(shop, "changed")]},
        {"key": "deleted", "title": "🗑 «Konto gelöscht»", "when": "акаунт покупця видалено", "who": minerva,
         "sender": notices, "send": _text("завжди"), "copy": True, "text": [_templates(shop, "deleted")]},
        {"key": "invoice", "title": "🧾 Рахунок (Rechnung, PDF)",
         "when": "Vorkasse, рахунок, Nachnahme — одразу після замовлення; Stripe — після оплати", "who": minerva,
         "sender": notices,
         "send": _text("невідомо" if auto is None else ("сам — так" if auto else "сам — ні (лише кнопкою в 🧾 Рахунках)")),
         "send_go": _go("invoice_auto_send", "змінити"), "copy": True,
         "text": [_url(invoices, "🧾 Рахунки")] if invoices else []},
        {"key": "order_customer", "title": "🛒 Підтвердження замовлення (Bestellbestätigung)",
         "when": "одразу після замовлення в кошику; текст про оплату — за способом оплати", "who": st["who"],
         "sender": st["sender"], "send": site_send, "copy": True, "site": True, "text": site_text},
        {"key": "quote_customer", "title": "📝 Підтвердження запиту пропозиції",
         "when": "покупець надіслав запит пропозиції (товари без ціни на сайті)", "who": st["who"],
         "sender": st["sender"], "send": _text("як підтвердження замовлення ↑"), "copy": True, "site": True,
         "text": site_text},
    ]


def team_rows(shop: Shop, st: dict | None = None) -> list[dict]:
    """③ Листи команді: [{key, title, when, who, sender, send, to, copy, text, site}]."""
    st = st or site_state(shop)
    ns = _notifications()
    team = mail.addresses(shop.team_emails)
    chats = mail.team_chats(shop)
    tg = shop.team_notify_telegram and ns["bot"] and bool(chats or ns["chat"])
    minerva_to = (", ".join(team) if team else
                  (", ".join(ns["to"]) + " (як «Кому» сповіщень)" if ns["to"] else
                   "нікому — email у «Налаштування → Сповіщення» вимкнено або без адрес")) + (" · 💬 Telegram" if tg else "")
    team_sender = (_minerva_sender(shop, "team") or "❌ не задано") if team else (ns["sender"] or "SMTP сповіщень Minerva")
    site_text = [_url(st["admin"], "📨 Листи сайту", external=True)] if st["admin"] else []
    minerva = "🏛 Minerva"
    return [
        {"key": "team_paid", "title": "💳 Оплату отримано", "when": "покупець оплатив онлайн (Stripe)", "who": minerva,
         "sender": team_sender, "send": _text("завжди"), "to": minerva_to, "copy": True,
         "text": [_templates(shop, "team_paid")]},
        {"key": "team_registered", "title": "👤 Новий акаунт покупця", "when": "покупець підтвердив реєстрацію",
         "who": minerva, "sender": team_sender, "send": _check("team_notify_registration", shop.team_notify_registration),
         "to": minerva_to, "copy": True, "text": [_templates(shop, "team_registered")]},
        {"key": "order_team", "title": "🛒 Нове замовлення з сайту",
         "when": "кожне замовлення в кошику; «Відповісти» — пише покупцю", "who": st["who"], "sender": st["sender"],
         "send": _text("завжди"), "to": st["notify"], "copy": True, "site": True, "text": site_text},
        {"key": "quote_team", "title": "📝 Новий запит пропозиції з сайту",
         "when": "кожен запит пропозиції; «Відповісти» — пише покупцю", "who": st["who"], "sender": st["sender"],
         "send": _text("завжди"), "to": st["notify"], "copy": True, "site": True, "text": site_text},
        {"key": "contact_team", "title": "💬 Контактна форма сайту",
         "when": "форма «Kontakt»; «Відповісти» — пише тому, хто заповнив", "who": st["who"], "sender": st["sender"],
         "send": _text("завжди"), "to": st["recipient"],
         "to_note": "змінюється на сайті: /admin/ → «✉️ Пошта» → «Отримувач запитів з форми»", "copy": True,
         "site": True, "text": site_text},
    ]


def warnings(shop: Shop, st: dict | None = None) -> list[str]:
    """Чому копії листів сайту зараз можуть не діяти."""
    st = st or site_state(shop)
    used = any((shop.mail_letters or {}).get(k) for k in mail.SITE_LETTERS)
    if not used:
        return []
    if st["managed"] and st["report"] and not st["letters_ok"]:
        return ["Сайт ще не додає копії окремо для кожного листа — потрібне оновлення файлів сайту. До того сайт бере "
                "лише приховану копію «Підтвердження замовлення» (для обох листів покупцю), інші копії листів сайту "
                "не діють."]
    if not st["managed"]:
        return ["Копії листів сайту діють, коли листи сайту йдуть зі скриньки Minerva (① «🌐 Листи сайту»). "
                "Зараз сайт пише зі своїх налаштувань."]
    return []


# ── HTML блоку (картка магазину, ShopAdmin.mail_map_col) ─────────────────────

def _links(items) -> str:
    return format_html_join(" · ", "{}", ((
        format_html('<a href="#" data-nt-go="{}">{} ↓</a>', i["go"], i["label"]) if i.get("go") else
        format_html('<a href="{}"{}>{} {}</a>', i["url"], format_html(' target="_blank" rel="noopener"')
                    if i.get("external") else "", i["label"], "↗" if i.get("external") else "→"),)
        for i in items or [] if i))


def _what(r) -> str:
    return format_html('<b>{}</b><div class="nt-when">{}</div>'
                       '<button type="button" class="mm-pv" data-letter="{}" title="{}">👁 {}</button>',
                       r["title"], r["when"], r["key"], "Від кого, кому, копії, тема й текст — приклад", "Приклад")


def _from(r) -> str:
    return format_html('{}<div class="nt-when" translate="no">{}</div>', r["who"], r["sender"])


def _send(r, w, synced) -> str:
    s = r["send"]
    if s["type"] == "check":
        synced.append(s["sync"])
        cell = w.check(s["label"], s["on"], sync="id_" + s["sync"], role="main")
    else:
        cell = format_html('<span>{}</span>', s["text"])
    if r.get("send_go"):
        cell = format_html('{} <a href="#" data-nt-go="{}">{} ↓</a>', cell, r["send_go"]["go"], r["send_go"]["label"])
    return cell


def _copy(shop, r, w, field) -> str:
    """Поле копії листа: field cc | bcc → mlc__<лист> / mlb__<лист>."""
    item = (shop.mail_letters or {}).get(r["key"])
    value = item.get(field) or "" if isinstance(item, dict) else ""
    return w.picker("email", value, name=("mlc__" if field == "cc" else "mlb__") + r["key"], compact=True,
                    empty="без копії" if field == "cc" else "без прихованої копії")


def _copies(shop, r, w) -> str:
    """Клітинки «Копія» і «Прихована копія» рядка листа покупцю (без копій — одна клітинка на дві колонки)."""
    if not r["copy"]:
        return format_html('<td colspan="2"><span class="nt-empty">{}</span></td>', r.get("note") or "—")
    return format_html('<td>{}</td><td>{}</td>', _copy(shop, r, w, "cc"), _copy(shop, r, w, "bcc"))


def render(shop: Shop) -> str:
    """Блок «Хто що кому шле»: ① скриньки, ② листи покупцю, ③ листи команді."""
    from dashboard import notify_routes, notify_widgets as w
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception:  # noqa: BLE001
        ns = None
    synced = ["mail_reply_to", "team_emails", "team_notify_telegram", "team_telegram"]
    st = site_state(shop)

    def route(c):
        options = format_html_join("", '<option value="{}"{}>{}</option>', (
            (v, format_html(" selected") if v == c["value"] else "", label) for v, label in c["options"]))
        return format_html('<select name="{}" class="mm-route">{}</select>', c["name"], options)

    box_rows = format_html_join("", '<tr><td><b>{}</b><div class="nt-when">{}</div></td><td>{}</td>'
                                    '<td><span translate="no">{}</span><div class="nt-when">{}</div></td></tr>', (
        (s["title"], s["letters"], route(s["route"]), s["sender"], s["note"]) for s in senders(shop, st)))
    reply = format_html(
        '{}<div class="nt-when">{} <span translate="no">{}</span> · <a href="#" data-nt-go="mail_from_name">{} ↓</a></div>',
        w.picker("email", shop.mail_reply_to, sync="id_mail_reply_to", empty="на адресу відправника"),
        "Ім'я відправника:", shop.mail_from_name or shop.name, "змінити")
    boxes = format_html(
        '<div class="mm-wrap"><table class="nt-table mm-table"><tr class="nt-sec"><td colspan="3">{}</td></tr>'
        '<tr><th>Листи</th><th>З якої скриньки</th><th>Отримувач бачить «Від»</th></tr>{}'
        '<tr><td><b>{}</b><div class="nt-when">{}</div></td><td colspan="2">{}</td></tr></table></div>',
        "① З якої адреси — скриньки", box_rows, "↩ Відповіді покупців (Reply-To)",
        "листи покупцю від Minerva й сайту; у листах команді «Відповісти» пише покупцю", reply)

    cust = format_html_join("", '<tr data-nt-row="{}"><td class="nt-what">{}</td><td>{}</td><td>{}</td>{}'
                                '<td>{}</td></tr>', (
        (r["key"] or "links", _what(r), _from(r), _send(r, w, synced), _copies(shop, r, w), _links(r["text"]))
        for r in customer_rows(shop, st)))
    customer = format_html(
        '<div class="mm-wrap"><table class="nt-table mm-table"><tr class="nt-sec"><td colspan="6">{}</td></tr>'
        '<tr><th>Лист і коли</th><th>Хто надсилає · від</th><th>Надсилається</th><th class="mm-copy">{}</th>'
        '<th class="mm-copy">{}</th><th>Текст</th></tr>{}</table></div>',
        "② Листи покупцю — кому: покупцю (email із замовлення або кабінету)", "Копія (CC) — адресу видно покупцю",
        "Прихована копія (BCC)", cust)

    team_cells = format_html_join("", '<tr data-nt-row="{}"><td class="nt-what">{}</td><td>{}</td><td>{}</td>'
                                      '<td><span translate="no">{}</span>{}</td><td>{}</td><td>{}</td></tr>', (
        (r["key"], _what(r), _from(r), _send(r, w, synced), r["to"],
         format_html('<div class="nt-when">{}</div>', r["to_note"]) if r.get("to_note") else "",
         _copy(shop, r, w, "cc"), _links(r["text"])) for r in team_rows(shop, st)))
    chat = notify_routes.default_chat(ns) if ns is not None else ""
    team_head = format_html(
        '<div class="nt-when">{}</div>{}<div style="margin-top:6px">{}</div>{}',
        "✉️ Адреси команди — «Кому» всіх листів нижче, крім контактної форми "
        "(порожньо — як «Кому (email)» у «Налаштування → Сповіщення»):",
        w.picker("email", shop.team_emails, sync="id_team_emails",
                 empty="як «Кому» сповіщень: " + (", ".join(_notifications()["to"]) or "нікому")),
        w.check("💬 Telegram — оплата й новий акаунт (листи від Minerva)", shop.team_notify_telegram,
                sync="id_team_notify_telegram"),
        w.picker("tg", shop.team_telegram, sync="id_team_telegram",
                 empty=f"основний чат сповіщень: {chat}" if chat else "основний чат — не задано"))
    team = format_html(
        '<div class="mm-wrap"><table class="nt-table mm-table"><tr class="nt-sec"><td colspan="6">{}</td></tr>'
        '<tr><td colspan="6">{}</td></tr>'
        '<tr><th>Лист і коли</th><th>Хто надсилає · від</th><th>Надсилається</th><th>Кому</th>'
        '<th class="mm-copy">{}</th><th>Текст</th></tr>{}</table></div>',
        "③ Листи команді", team_head, "Копія (CC)", team_cells)

    warn = format_html_join("", '<div class="mm-warn">⚠ {}</div>', ((t,) for t in warnings(shop, st)))
    other = format_html(
        '<div class="help">{} <a href="{}">{} →</a></div>',
        "Також покупцям магазину пише Minerva (налаштовується не тут): 📧 «посилку відправлено» і 📥 «замовлення "
        "отримано» — з копією, у",
        reverse("admin:config_notificationsettings_changelist"), "Налаштування → Сповіщення → «👤 Клієнтам»")
    from django.templatetags.static import static
    return format_html(
        '{}<input type="hidden" name="mail_letters_form" value="1">'
        '<div id="mm-pv-root" data-url="{}" hidden></div><script src="{}?v={}" defer></script>{}{}{}{}'
        '<div class="help">{}</div>{}<span data-nt-hide="{}"></span>',
        w.assets(notify_routes.email_options(ns), notify_routes.chat_options(ns)) + STYLE,
        reverse("admin:shop_shop_mail_preview", args=[shop.pk]), static("admin/js/mv_mail_preview.js"),
        PREVIEW_VERSION, warn, boxes, customer, team,
        "Скриньки, «Кому», копії й галочки змінюються тут (адреса — «＋ адреса з Minerva» або своя й Enter) і "
        "зберігаються кнопкою «Зберегти» внизу сторінки. Копія (CC) — адресу видно отримувачу, прихована копія (BCC) — "
        "ні. Листи з одноразовими посиланнями не копіюються. Дані сайту — з його останнього звіту; «📝 Текст» — "
        "шаблони листів Minerva, «📨 Листи сайту» — тексти сайту з тестовим надсиланням.",
        other, " ".join(dict.fromkeys(synced)))


PREVIEW_VERSION = "1"   # static/admin/js/mv_mail_preview.js — підняти при зміні

STYLE = mark_safe("<style>" + (
    ".mm-table{margin-bottom:12px}.mm-table select.mm-route{width:280px;max-width:100%}"
    ".mm-wrap{overflow-x:auto;margin-bottom:12px}.mm-wrap .mm-table{margin-bottom:0}"
    ".mm-table th.mm-copy{min-width:190px}.mm-table td{vertical-align:top}.mm-table td.nt-what{min-width:200px}"
    ".mm-warn{background:#fff3e0;color:#6d3500;border-left:3px solid #ef6c00;border-radius:4px;padding:4px 8px;"
    "margin:0 0 8px;line-height:1.45}"
    ".mm-pv{margin-top:4px;border:1px solid var(--hairline-color,#ccc);background:var(--body-bg,#fff);color:inherit;"
    "border-radius:12px;padding:1px 10px;font-size:12px;cursor:pointer}.mm-pv:hover{border-color:var(--link-fg,#447e9b)}"
    "#mm-pv-dlg{width:min(860px,96vw);max-height:92vh;border:1px solid var(--hairline-color,#ccc);border-radius:10px;"
    "padding:0;background:var(--body-bg,#fff);color:var(--body-fg,#333)}#mm-pv-dlg::backdrop{background:rgba(0,0,0,.45)}"
    "#mm-pv-dlg .pv-head{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid "
    "var(--hairline-color,#ddd);position:sticky;top:0;background:inherit}#mm-pv-dlg .pv-head b{flex:1}"
    "#mm-pv-dlg .pv-body{padding:10px 14px;overflow:auto;max-height:calc(92vh - 52px)}"
    "#mm-pv-dlg .pv-tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}"
    "#mm-pv-dlg table{width:100%;border-collapse:collapse;font-size:13px}#mm-pv-dlg th{text-align:left;"
    "white-space:nowrap;padding:3px 10px 3px 0;vertical-align:top;color:var(--body-quiet-color,#666);font-weight:600}"
    "#mm-pv-dlg td{padding:3px 0;word-break:break-word}#mm-pv-dlg .pv-chip{border-radius:10px;padding:1px 8px;"
    "font-size:12px}#mm-pv-dlg .pv-on{background:#e8f5e9;color:#1b5e20}#mm-pv-dlg .pv-off{background:#ffebee;color:#8e1b1b}"
    "#mm-pv-dlg .pv-note{background:#fff3e0;color:#6d3500;border-left:3px solid #ef6c00;border-radius:4px;"
    "padding:4px 8px;margin:6px 0}#mm-pv-dlg pre{white-space:pre-wrap;font-size:13px;background:var(--darkened-bg,#f6f6f6);"
    "padding:10px;border-radius:6px;margin:8px 0 0}#mm-pv-dlg iframe{width:100%;height:420px;border:1px solid "
    "var(--hairline-color,#ddd);border-radius:6px;background:#fff;margin-top:8px}") + "</style>")


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
