"""«🗺 Хто що отримує» вгорі «Налаштування → Сповіщення»: кожне сповіщення — з якої адреси, кому на пошту, в які
Telegram-чати, увімкнено чи ні, і де налаштувати детальніше. Дані — dashboard/notify_routes.py, поля —
dashboard/notify_widgets.py; зберігає NotificationSettingsAdmin.save_model (notify_routes.save) разом з формою.
«👤 Клієнтам — з адреси [▼]» і «з адреси» кожного листа клієнту — dashboard/customer_mail.py (меню cm_*).
"""
from __future__ import annotations

from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from dashboard import customer_mail as cm
from dashboard import notify_routes as nr
from dashboard import notify_widgets as w


def _sender(ns) -> str:
    return (ns.email_from or ns.email_host_user or "").strip()


def _from_select(ns, kind: str, user) -> str:
    """Меню «з адреси»: для всіх листів клієнтам (cm.GROUP) або одного листа (ship | confirm). Скриньки — ті, що
    людина може вибрати (свої, адміністратору — усі), і вже вибрані."""
    current = cm.route_of(ns, kind)
    ns_addr = cm.ns_address(ns)
    ns_label = f"{ns_addr} — SMTP сповіщень" if ns_addr else "SMTP сповіщень (не задано)"
    if kind == cm.GROUP:
        opts = [("", ns_label)]
    else:
        group = cm.sender(ns, cm.GROUP).address or "—"
        opts = [("", f"як для всіх листів клієнтам ({group})"), (cm.NS, ns_label)]
    opts += cm.options(ns, user, kind)
    return format_html(
        '<select name="cm_{}" class="nt-from" style="max-width:100%;font-size:12px">{}</select>', kind,
        format_html_join("", '<option value="{}"{}>{}</option>',
                         ((v, mark_safe(" selected") if v == current else "", text) for v, text in opts)))


def _from_note(ns, kind: str) -> str:
    """З якої адреси лист піде зараз; вибрана скринька недоступна — попередження."""
    s = cm.sender(ns, kind)
    warn = format_html(
        '<div class="nt-warn" style="color:var(--err,#c62828);font-weight:600;margin-top:2px">⚠ {} — листи '
        'клієнтам ідуть з SMTP сповіщень. Виберіть іншу адресу.</div>', s.problem) if s.problem else ""
    source = "скринька пошти Minerva, копія — у її «Надісланих»" if s.account is not None else "SMTP сповіщень"
    return format_html('{}<div class="nt-when">→ піде з <b translate="no">{}</b> · {}</div>',
                       warn, s.address or "— (SMTP не задано)", source)


def _from_block(ns, kind: str, user) -> str:
    return format_html('<div class="nt-when" style="margin-top:6px">з адреси:</div>{}{}',
                       _from_select(ns, kind, user), _from_note(ns, kind))


def _event_row(ns, row, default_to, default_chat) -> str:
    ev = row.event
    what = format_html('<b>{}</b><div class="nt-when">{}</div>', ev.title, ev.when)
    if row.enabled:
        what += format_html('<div style="margin-top:4px">{}</div>', format_html_join(
            "", "{}", ((w.check(label, on, sync=f"id_{f}", role="main"),) for f, label, on in row.enabled)))
    if ev.shared:
        what += format_html('<div class="nt-when">{}</div>', ev.shared)

    if ev.ext:
        email_on = format_html('<span class="nt-chk">{} ✉️ Email</span>', "✅" if row.email_on else "⏸")
        tg_on = format_html('<span class="nt-chk">{} 💬 Telegram</span>', "✅" if row.tg_on else "⏸")
    else:
        email_on = (w.check("✉️ Email", row.email_on, sync=f"id_{ev.email_field}") if ev.email_field
                    else w.check("✉️ Email", row.email_on, name=f"nr_{ev.key}_email"))
        tg_on = (w.check("💬 Telegram", row.tg_on, sync=f"id_{ev.tg_field}") if ev.tg_field
                 else w.check("💬 Telegram", row.tg_on, name=f"nr_{ev.key}_telegram"))
    email = email_on + w.picker("email", ", ".join(row.to), name=f"nr_{ev.key}_to",
                                empty=f"як «Кому» за замовчуванням: {default_to}")
    tg = tg_on + w.picker("tg", ", ".join(row.chats), name=f"nr_{ev.key}_tg",
                          empty=f"основний чат: {default_chat}" if default_chat else "основний чат — не задано")
    detail = [(f, label) for f, label in ev.detail]
    if ev.ext:
        detail.append((row.ext_url, f"{ev.ext[4]}: увімкнути / вимкнути"))
    return format_html('<tr data-nt-row="{}"><td class="nt-what">{}<input type="hidden" name="nr_{}_seen" value="1">'
                       '</td><td>{}</td><td>{}</td><td>{}</td></tr>',
                       ev.key, what, ev.key, email, tg, w.links(detail))


def _customer_rows(ns, user=None) -> str:
    sources = (ns.order_confirm_notify_sources or "").strip()
    rows = [
        format_html(
            '<tr data-nt-row="customer_ship"><td class="nt-what"><b>{}</b><div class="nt-when">{}</div></td>'
            '<td>{}{}<div class="nt-when">{}</div>{}{}</td><td>—</td><td>{}</td></tr>',
            "📧 Клієнту: посилку відправлено (трек-номер)",
            "кнопка «📧 Надіслати клієнту» в замовленні, з «✉️ сам» — і після оформлення посилки",
            w.check("увімкнено", ns.customer_notify_enabled, sync="id_customer_notify_enabled", role="main"),
            w.check("✉️ сам", ns.customer_notify_auto, sync="id_customer_notify_auto", role="aux",
                    title="без кнопки — Minerva надсилає сама"),
            "кому: клієнту замовлення · копія:",
            w.picker("email", ns.customer_notify_cc or "", sync="id_customer_notify_cc", empty="без копії"),
            _from_block(ns, "ship", user),
            w.links([("customer_notify_subject", "Тема й текст листа")])),
        format_html(
            '<tr data-nt-row="customer_confirm"><td class="nt-what"><b>{}</b><div class="nt-when">{}</div></td>'
            '<td>{}{}<div class="nt-when">{}</div>{}{}</td><td>—</td><td>{}</td></tr>',
            "📥 Клієнту: замовлення отримано",
            "кнопка «📥 Підтвердження замовлення», з «✉️ сам» — при імпорті"
            + (f" · лише джерела: {sources}" if sources else " · усі джерела"),
            w.check("увімкнено", ns.order_confirm_notify_enabled, sync="id_order_confirm_notify_enabled", role="main"),
            w.check("✉️ сам", ns.order_confirm_notify_auto, sync="id_order_confirm_notify_auto", role="aux",
                    title="без кнопки — Minerva надсилає сама"),
            "кому: клієнту замовлення · копія:",
            w.picker("email", ns.order_confirm_notify_cc or "", sync="id_order_confirm_notify_cc",
                     empty="без копії"),
            _from_block(ns, "confirm", user),
            w.links([("order_confirm_notify_sources", "Джерела, тема й текст")])),
        format_html(
            '<tr><td class="nt-what"><b>{}</b><div class="nt-when">{}</div></td><td colspan="2">{}</td>'
            '<td>{}</td></tr>',
            "🚫 Не надсилати листи цим клієнтам",
            "ні автоматично, ні кнопкою: обидва листи вище",
            w.picker("block", ns.customer_block or "", sync="id_customer_block", empty="нікого не заблоковано",
                     hint="адреса (anna@firma.de) або весь домен (@firma.de)"),
            w.links([("customer_block", "Поле списку")])),
    ]
    return format_html_join("", "{}", ((r,) for r in rows))


def _other_rows(ns) -> str:
    return format_html_join("", '<tr><td class="nt-what"><b>{}</b></td><td>{}</td><td>{}</td><td>{}</td></tr>', (
        (o["title"], o["email"], o["telegram"], w.links([(o["link"][0], o["link"][1])])) for o in nr.others(ns)))


def render(ns, user=None) -> str:
    """user — хто дивиться: які скриньки він може вибрати в «з адреси» (без нього — лише SMTP і вже вибрані)."""
    from dashboard.notifications import configured_recipients
    default_to = ", ".join(configured_recipients(ns)) or "нікому (порожньо)"
    default_chat = nr.default_chat(ns)
    sender = _sender(ns)
    head = format_html(
        '<div style="margin-bottom:8px;line-height:1.7">{}{} · листи команді йдуть з <b translate="no">{}</b> {} · '
        'бот Telegram — {} {}</div>'
        '<div style="margin-bottom:10px"><b>За замовчуванням «Кому (email)»:</b>{}</div>',
        w.check("✉️ Email увімкнено", ns.email_enabled, sync="id_email_enabled"),
        w.check("💬 Telegram увімкнено", ns.telegram_enabled, sync="id_telegram_enabled"),
        sender or "— (SMTP не задано)", w.links([("email_host", "SMTP")]),
        "заданий" if ns.telegram_bot_token else "не заданий", w.links([("telegram_chat_id", "бот і основний чат")]),
        w.picker("email", ns.email_to or "", sync="id_email_to", empty="нікому"))
    team = format_html_join("", "{}", ((_event_row(ns, r, default_to, default_chat),) for r in nr.rows(ns)))
    table = format_html(
        '<table class="nt-table"><tr><th style="width:24%">Сповіщення</th><th style="width:30%">✉️ Email — кому</th>'
        '<th style="width:26%">💬 Telegram — куди</th><th>Детальніше</th></tr>'
        '<tr class="nt-sec"><td colspan="4">👥 Команді</td></tr>{}'
        '<tr class="nt-sec"><td colspan="4">👤 Клієнтам — з адреси {}'
        '<input type="hidden" name="cm_seen" value="1">{}</td></tr>{}'
        '<tr class="nt-sec"><td colspan="4">↗ Налаштовується деінде — лише показ, змінити на своїй сторінці</td></tr>{}'
        '</table>', team, _from_select(ns, cm.GROUP, user), _from_note(ns, cm.GROUP), _customer_rows(ns, user),
        _other_rows(ns))
    help_ = format_html(
        '<div class="help" style="margin-top:6px">{}</div>',
        "Порожнє «кому» — діють адреси «Кому (email)» і основний чат Telegram. «＋ з Minerva» — співробітники, "
        "скриньки пошти, особисті чати з ботом (Telegram ID у «Ролі користувачів»); свою адресу чи Chat ID — "
        "впишіть і Enter. «Клієнтам — з адреси» — SMTP сповіщень або скринька пошти Minerva (свою може вибрати "
        "кожен, чужу — лише адміністратор); у кожного листа клієнту — своя адреса або «як для всіх». "
        "Збереже кнопка «Зберегти» внизу сторінки. «↓» — до розділу на цій сторінці, «→» — інша сторінка.")
    return w.assets(nr.email_options(ns), nr.chat_options(ns)) + head + table + help_


def save(request, ns) -> None:
    """Таблиця → ns.routes і ns.customer_senders; неправильні адреси / чати й скриньки, яких людина вибрати не
    може, — попередження (вони не зберігаються)."""
    from django.contrib import messages
    if any(k.startswith("nr_") for k in request.POST):
        changed, bad = nr.save(ns, request.POST)
        if bad:
            messages.warning(request, "Не збережено — не схоже на email чи Chat ID: " + ", ".join(bad[:10]))
    refused = cm.save(ns, request.POST, request.user)
    if refused:
        messages.warning(request, "Адресу листів клієнтам не змінено: скриньку може вибрати лише її власник або "
                                  "адміністратор Minerva, і вона має бути активна з SMTP — " + ", ".join(refused[:5]))


def url() -> str:
    return reverse("admin:config_notificationsettings_changelist")
