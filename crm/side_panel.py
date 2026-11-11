"""📌 Панель праворуч для списку клієнтів CRM (core/side_panel.py): клік по рядку чи картці клієнта — контакти,
продажі й RFM, замовлення, листування, нотатки, Angebote, проєкти поруч зі списком, без переходу в картку.
Швидкі зміни — ⭐ обраний, статус, сегмент (core/panel_edit.py: той самий шлях, що «Зберегти» в картці).

Правила:
  • замовлення клієнта — лише crm.utils.orders_of (ключ, потім email — те саме правило, що RFM і «Тип»), продажі й
    RFM — Customer.card_stats() / rfm_score() (crm/utils.py: лише продажі, у валюті Minerva);
  • листи — лише з власних скриньок людини (crm.views._own_messages), і для суперкористувача теж; посилання на лист —
    лише в основній скриньці (email_assistant.views._get_account: інакше сторінка листа — 404); текст листа тут
    лист прочитаним не робить;
  • нотатки й події — без подій листів (їхні заголовки — теми листів з будь-чиїх скриньок);
  • час askDANTE — опис запису лише свій, вартість — лише адміністраторам (worktime.services.is_manager);
    worktime.panels.customer_panel (усі описи) тут не вбудовується;
  • Kundennummer — лише читається (shop.numbering.for_crm видав би новий номер);
  • документи — файли MEDIA_ROOT/customers/<pk>/, посилання через /media/ (там — перевірка розділу CRM);
  • посилання на інші записи — sp.admin_link (лише якщо людина їх відкриє) або своя перевірка права.
Усе лише читає базу й екранується; дані записів — у translate="no".
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import quote, urlencode

from django.conf import settings
from django.db.models import Count, F, Min, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import conditional_escape, escape, format_html, format_html_join
from django.utils.safestring import mark_safe

from core import side_panel as sp
from crm.utils import is_repeat, orders_of

logger = logging.getLogger(__name__)

MODEL = "crm.customer"

STATUS_ICONS = {"active": "🟢", "inactive": "⚪", "vip": "⭐", "blocked": "🚫"}       # як дії «Статус →» у списку
STATUS_TONES = {"active": "ok", "inactive": "off", "vip": "warn", "blocked": "err"}
SEGMENT_ICONS = {"b2b": "🏢", "b2c": "👤", "distributor": "🔗", "reseller": "🛒", "other": "📎"}
RECENCY_TONES = {"crm-rec-fresh": "ok", "crm-rec-warn": "warn", "crm-rec-old": "err"}   # Customer.card_stats
ORDER_TONES = {"received": "new", "processing": "work", "shipped": "ship", "delivered": "done", "cancelled": "off"}
OFFER_TONES = {"draft": "new", "review": "work", "approved": "ok", "sent": "ship", "accepted": "done",
               "rejected": "err", "expired": "off", "cancelled": "off"}
PROJECT_TONES = {"lead": "new", "active": "ok", "on_hold": "warn", "done": "done", "cancelled": "off"}
INVOICE_TONES = {"draft": "new", "sent": "ship", "paid": "ok", "overdue": "err", "cancelled": "off"}
SHIPMENT_TONES = {"draft": "new", "submitted": "work", "label_ready": "work", "in_transit": "ship",
                  "delivered": "done", "error": "err", "cancelled": "off"}
EMAIL_EVENTS = ("email_in", "email_out")    # події листів у хронології: заголовок — тема листа (чужі скриньки)
UNPAID = ("unpaid", "pending")

ORDERS_SHOWN = 5
MAIL_SHOWN = 5
OFFERS_SHOWN = 5
PROJECTS_SHOWN = 8
SHIPMENTS_SHOWN = 5
INVOICES_SHOWN = 5
EVENTS_SHOWN = 4
REMINDERS_SHOWN = 3
NOTES_SHOWN = 3
MEETINGS_SHOWN = 4
FILES_SHOWN = 12
KNOWLEDGE_SHOWN = 5
RECORDS_SHOWN = 8
PREVIEW_MAX = 600           # символів тексту листа
NOTES_MAX = 300             # символів приміток клієнта / нотатки


# ── помічники ──────────────────────────────────────────────────────────────────

def _plural(n, one, few, many) -> str:
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _can_record(request, obj=None) -> bool:
    """«🎙 Записати нараду»: право meetings.add_meeting і записування ввімкнено (meetings.services.can_record)."""
    if not request.user.has_perm("meetings.add_meeting"):
        return False
    try:
        from meetings.services import can_record
    except ImportError:
        return False
    return bool(can_record(request.user))


def _url(name, *args, query=None) -> str:
    try:
        url = reverse(name, args=list(args))
    except NoReverseMatch:
        return ""
    return f"{url}?{urlencode(query)}" if query else url


def _join(parts, sep=" · "):
    """Частини (рядки — як дані запису, SafeString — як є) через « · », порожні пропускаються."""
    items = [_html(p) for p in parts if p not in (None, "") and str(p).strip()]
    return format_html_join(sep, "{}", ((p,) for p in items))


def _concat(items):
    """Готові шматки HTML (SafeString) підряд; звичайний рядок — екранується."""
    return mark_safe("".join(str(conditional_escape(x)) for x in items if x))


def _ui(text):
    """Підпис інтерфейсу в значенні (перекладається, тож не в translate="no")."""
    return format_html('<span class="mvp-ro">{}</span>', text)


def _html(v):
    """Готовий HTML (SafeString) — як є; рядок — як дані запису (translate="no")."""
    return v if hasattr(v, "__html__") else sp.data(v)


def _line(main, side="", note=""):
    return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span><span>{}</span></div>'
                       '{}</div>', _html(main), _html(side),
                       format_html('<div class="mvp-note">{}</div>', _html(note)) if note else "")


def _group(title, body):
    if not body or not str(body).strip():
        return mark_safe("")
    return format_html('<div class="mvp-fg"><h4 class="mvp-fg-h"><span>{}</span></h4>{}</div>', title, body)


def _clamp(text, limit):
    text = (text or "").strip()
    if not text:
        return mark_safe("")
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return format_html('<div class="mvp-clamp" data-mvp-clamp translate="no">{}</div>', text)


def _name(c) -> str:
    return c.company or c.name or "—"


def _tel(phone) -> str:
    digits = re.sub(r"[^\d+]", "", phone or "")
    return f"tel:{digits}" if digits else ""


def _country(code, lang="uk") -> str:
    if not code:
        return ""
    try:
        from shop.countries import name
        return name(code, lang)
    except Exception:  # noqa: BLE001
        return code


def _postal_country(code) -> str:
    """Країна для скопійованої адреси — англійською, без коду в дужках."""
    try:
        from shop.countries import COUNTRIES
        return (COUNTRIES.get((code or "").upper()) or {}).get("en") or (code or "")
    except Exception:  # noqa: BLE001
        return code or ""


def _stats(c) -> dict:
    """Продажі й RFM клієнта (crm/utils.py: лише продажі, ключ і email, валюта Minerva) — раз на запит."""
    return sp.memo(c, "crm:stats", lambda: {"card": c.card_stats(), "rfm": c.rfm_score()})


# ── назва й підзаголовок ───────────────────────────────────────────────────────

def title(c):
    star = format_html(' <span title="{}">⭐</span>', "Обраний клієнт") if c.is_favorite else ""
    return format_html("<b>{}</b>{}", sp.data(_name(c)), star)


def label(c) -> str:
    return _name(c)


def subtitle(c, request) -> str:
    parts = [_country(c.country), c.get_segment_display()]
    try:
        st = _stats(c)
        if st["card"]["count"]:
            parts.append(st["rfm"]["segment"])
    except Exception as exc:  # noqa: BLE001 — без RFM підзаголовок усе одно є
        logger.warning("crm side panel stats for %s failed: %s", c.pk, exc)
    return " · ".join(p for p in parts if p)


def _status_choices(c, request):
    from crm.models import Customer
    return [(v, f"{STATUS_ICONS.get(v, '')} {lab}".strip()) for v, lab in Customer.Status.choices]


def _segment_choices(c, request):
    from crm.models import Customer
    return [(v, f"{SEGMENT_ICONS.get(v, '')} {lab}".strip()) for v, lab in Customer.Segment.choices]


# ── 📇 Контакти ────────────────────────────────────────────────────────────────

def _kundennummer(c):
    """Kundennummer веб-магазину — лише читання (shop.numbering.for_crm видав би новий номер)."""
    def load():
        try:
            from shop.models import CustomerNumber
            return CustomerNumber.objects.filter(crm_customer=c).values_list("number", flat=True).first()
        except Exception as exc:  # noqa: BLE001
            logger.info("crm side panel Kundennummer skipped: %s", exc)
            return None
    return sp.memo(c, "crm:kdnr", load)


def _email_html(request, email):
    if not email:
        return ""
    shown = sp.data(email)
    if request.user.has_module_perms("email_assistant"):
        href = _url("email_assistant:compose", query={"to": email})
        if href:
            shown = sp.link(href, shown)
    return format_html("{} {}", shown, sp.copy(email, "Email"))


def contact_html(c, request):
    street = [p for p in (c.addr_street, " ".join(p for p in (c.addr_zip, c.addr_city) if p), c.addr_state) if p]
    address = ""
    if street:
        postal = [p for p in (c.company, c.name) if p] + street + ([_postal_country(c.country)] if c.country else [])
        address = format_html('<span translate="no">{}</span> {}',
                              format_html_join(mark_safe("<br>"), "{}", ((p,) for p in street)),
                              sp.copy("\n".join(postal), "адреса"))
    elif (c.shipping_address or "").strip():
        address = format_html("{}{}", _clamp(c.shipping_address, NOTES_MAX), sp.copy(c.shipping_address, "адреса"))
    kdnr = _kundennummer(c)
    phone = format_html("{} {}", sp.link(_tel(c.phone), sp.data(c.phone)), sp.copy(c.phone, "Телефон")) \
        if c.phone else ""
    return sp.kv([
        ("Компанія", c.company),
        ("Контактна особа", c.name),
        ("Email", _email_html(request, c.email)),
        ("Телефон", phone),
        ("Адреса", address),
        ("Країна", _country(c.country)),
        ("Kundennummer", format_html("{} {}", sp.data(kdnr), sp.copy(kdnr, "Kundennummer")) if kdnr else ""),
        ("Сегмент", sp.edit_html(request, c, "segment")),
        ("Статус", _ui(c.get_status_display()), STATUS_TONES.get(c.status, "")),
        ("Джерело", c.source),
        ("Створено", sp.dt(c.created_at)),
        ("Примітки", _clamp(c.notes, NOTES_MAX)),
    ])


def contact_summary(c, request) -> str:
    return sp.join([sp.data(c.email), sp.data(c.phone)])


# ── 📊 Продажі й RFM ───────────────────────────────────────────────────────────

def _rfm_tone(score) -> str:
    return "ok" if score >= 12 else ("warn" if score >= 8 else "err")       # як колонка «RFM Аналіз»


def sales_html(c, request):
    from sales.revenue import sale_orders
    st = _stats(c)
    card, rfm = st["card"], st["rfm"]
    if not card["count"]:
        return ""
    first = _orders(c, base=sale_orders()).aggregate(first=Min("order_date"))["first"]
    last = sp.date(card["last"])
    if last and card["days"] is not None:
        ago = "· сьогодні" if card["days"] == 0 else f"· {card['days']} дн. тому"
        last = format_html("{} {}", sp.data(last), _ui(ago))
    return format_html("{}{}", sp.kv([
        ("Замовлень", str(card["count"])),
        ("Виручка", card["rev"]),
        ("Середній чек", card["avg"]),
        ("Останнє", last, RECENCY_TONES.get(card["recency_cls"], "")),
        ("Перше", sp.date(first)),
        ("Тип", _ui("✅ Постійний" if is_repeat(card["count"]) else "🔸 Новий")),
        ("RFM", f"R {rfm['R']} · F {rfm['F']} · M {rfm['M']} = {rfm['score']}/15", _rfm_tone(rfm["score"])),
        ("Сегмент RFM", rfm["segment"]),
    ]), format_html('<p class="mvp-note">{}</p>',
                    "Лише продажі — без запитів пропозицій, скасованих і тестових; суми — у валюті Minerva."))


def sales_badge(c, request):
    st = _stats(c)
    if not st["card"]["count"]:
        return None
    rfm = st["rfm"]
    tone = "ok" if rfm["score"] >= 12 else ("warn" if "At Risk" in rfm["segment"] else "")
    return sp.Badge(None, tone, f"RFM {rfm['score']}/15 · {rfm['segment']}")


def sales_summary(c, request) -> str:
    st = _stats(c)
    n = st["card"]["count"]
    if not n:
        return ""
    return " · ".join(p for p in (f"{n} {_plural(n, 'замовлення', 'замовлення', 'замовлень')}", st["card"]["rev"],
                                  st["rfm"]["segment"]) if p and p != "—")


# ── 🧾 Замовлення ──────────────────────────────────────────────────────────────

def _orders(c, base=None):
    """orders_of(c) з номерами, знайденими один раз на запит (sp.memo): розділи, лічильник і підсумок питають
    замовлення клієнта п'ять разів, а кожен orders_of — підзапит за ключем і email по всіх замовленнях."""
    from sales.models import SalesOrder
    ids = sp.memo(c, "crm:order-ids", lambda: list(orders_of(c).values_list("pk", flat=True)))
    return (SalesOrder.objects.all() if base is None else base).filter(pk__in=ids)


def _order_counts(c) -> dict:
    """Усього замовлень клієнта (orders_of), чекають відправки й прострочені (sales.revenue.to_ship)."""
    def load():
        from sales.revenue import to_ship
        qs = _orders(c)
        n = qs.count()
        if not n:
            return {"n": 0, "waiting": 0, "late": 0}
        agg = to_ship(qs).aggregate(waiting=Count("pk"),
                                    late=Count("pk", filter=Q(shipping_deadline__lt=timezone.localdate())))
        return {"n": n, "waiting": agg["waiting"] or 0, "late": agg["late"] or 0}
    return sp.memo(c, "crm:orders", load)


def _waiting_text(k) -> str:
    parts = []
    if k["waiting"]:
        parts.append(f"чекають відправки {k['waiting']}")
    if k["late"]:
        parts.append(f"прострочено {k['late']}")
    return " · ".join(parts)


def orders_html(c, request):
    from sales.models import SalesSource
    from sales.revenue import to_ship
    k = _order_counts(c)
    if not k["n"]:
        return ""
    qs = _orders(c)
    rows = list(qs.annotate(_mvp_lines=Count("lines"))
                .order_by(F("order_date").desc(nulls_last=True), "-pk")[:ORDERS_SHOWN])
    waiting = set(to_ship(qs.filter(pk__in=[o.pk for o in rows])).values_list("pk", flat=True))
    names = dict(SalesSource.objects.filter(slug__in={o.source for o in rows}).values_list("slug", "name"))
    today = timezone.localdate()
    out = []
    for o in rows:
        main = [sp.admin_link(request, o, f"№ {o.order_number}"),
                sp.badge(o.get_status_display(), ORDER_TONES.get(o.status, ""))]
        if o.document_type != "SALE":
            main.append(sp.badge(o.get_document_type_display(), "off"))
        deadline = ""
        if o.pk in waiting and o.shipping_deadline:
            late = o.shipping_deadline < today
            deadline = format_html('<span class="{}">{}{}</span>', "mvp-err" if late else "mvp-note",
                                   "⚠ " if late else "", f"відправити до {sp.date(o.shipping_deadline)}")
        n = o._mvp_lines
        out.append(_line(
            _join(main, " "),
            sp.data(sp.money(o.total_price, o.currency)) if o.total_price is not None else "",
            _join([sp.date(o.order_date), names.get(o.source) or o.source,
                   _ui(f"{n} {_plural(n, 'позиція', 'позиції', 'позицій')}") if n else "", deadline])))
    head = format_html('<p class="{}">{}</p>', "mvp-err" if k["late"] else "mvp-note", _waiting_text(k)) \
        if k["waiting"] or k["late"] else ""
    more = sp.more(_url("admin:sales_salesorder_changelist", query={"q": c.email or c.company or c.name}),
                   "🔎 Знайти в списку замовлень →")
    return _concat([head, _concat(out), more])


def orders_badge(c, request):
    k = _order_counts(c)
    if not k["n"]:
        return None
    return sp.Badge(k["n"], "err" if k["late"] else ("info" if k["waiting"] else ""), _waiting_text(k))


def orders_summary(c, request) -> str:
    k = _order_counts(c)
    if not k["n"]:
        return ""
    head = f"{k['n']} {_plural(k['n'], 'замовлення', 'замовлення', 'замовлень')}"
    return " · ".join(p for p in (head, _waiting_text(k)) if p)


# ── ✉️ Листування (лише власні скриньки) ───────────────────────────────────────

def _own(request):
    from crm.views import _own_messages
    return _own_messages(request.user)


def _mail_pks(c, request) -> set:
    """pk листів клієнта з власних скриньок людини — один раз на запит. Три окремі вибірки, кожна вже в межах
    своїх скриньок (не OR через три таблиці по всій пошті Minerva — це був повний перегляд таблиці листів)."""
    def load():
        own = _own(request)
        pks = set(own.filter(thread__crm_customer=c).values_list("pk", flat=True))
        pks.update(own.filter(crm_events__customer=c).values_list("pk", flat=True))
        email = (c.email or "").strip()
        if email:
            pks.update(own.filter(from_email__iexact=email).values_list("pk", flat=True))
        return pks
    return sp.memo(c, "crm:mail-pks", load)


def _mail_qs(c, request):
    """Листи клієнта з власних скриньок людини: гілка клієнта, подія хронології або лист з адреси клієнта."""
    return _own(request).filter(pk__in=_mail_pks(c, request))


def _mail_counts(c, request) -> dict:
    def load():
        from email_assistant.models import EmailMessage
        agg = _mail_qs(c, request).aggregate(
            n=Count("pk"), unread=Count("pk", filter=Q(is_read=False, folder=EmailMessage.FOLDER_INBOX)))
        return {"n": agg["n"] or 0, "unread": agg["unread"] or 0}
    return sp.memo(c, "crm:mail", load)


_NO_ACCOUNT = object()


def _primary_account_id(request):
    """Основна активна скринька людини (як сторінка листа): лише там посилання на лист відкриється."""
    got = getattr(request, "_crm_mail_account", _NO_ACCOUNT)
    if got is _NO_ACCOUNT:
        try:
            from email_assistant.views import _get_account
            account = _get_account(request)
            got = account.pk if account is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.info("crm side panel mail account skipped: %s", exc)
            got = None
        try:
            request._crm_mail_account = got
        except Exception:  # noqa: BLE001
            pass
    return got


def _preview(body) -> str:
    from email_assistant.quotes import new_text, strip_signature
    text = strip_signature(new_text(body or "")).strip()
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text if len(text) <= PREVIEW_MAX else text[:PREVIEW_MAX - 1].rstrip() + "…"


def _mail_row(m, request, primary):
    from email_assistant.models import EmailMessage
    sent = m.folder == EmailMessage.FOLDER_SENT
    if sent:
        to = m.to_emails[0] if isinstance(m.to_emails, list) and m.to_emails else ""
        who = format_html('<span title="{}">→</span> {}', "Надіслано", sp.data(str(to)))
    else:
        who = format_html('<span title="{}">←</span> {}', "Отримано", sp.data(m.from_name or m.from_email))
    subject = sp.data(m.subject) if (m.subject or "").strip() else format_html('<span class="mvp-muted">{}</span>',
                                                                               "(без теми)")
    if primary is not None and m.account_id == primary:
        subject = sp.link(reverse("email_assistant:message", args=[m.pk]), subject)
    else:
        subject = format_html('{} <span class="mvp-note">· {} {}</span>', subject, "інша скринька:",
                              sp.data(getattr(m.account, "email_address", "")))
    unread = sp.badge("нове", "info") if not m.is_read and not sent else ""
    text = _preview(m.body_text)
    details = format_html('<details class="mvp-det"><summary>{}</summary><div class="mvp-quote" translate="no">{}'
                          '</div></details>', "Текст листа",
                          mark_safe(escape(text).replace("\n", "<br>"))) if text else ""
    return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span>'
                       '<span class="mvp-note">{}</span></div><div>{}{}</div>{}</div>',
                       who, sp.dt(m.sent_at), format_html("{} ", unread) if unread else "", subject, details)


def mail_html(c, request):
    k = _mail_counts(c, request)
    if not k["n"]:
        return ""
    msgs = list(_mail_qs(c, request).select_related("account")
                .order_by(F("sent_at").desc(nulls_last=True), "-pk")[:MAIL_SHOWN])
    primary = _primary_account_id(request)
    out = [_mail_row(m, request, primary) for m in msgs]
    foot = [sp.more(_url("email_assistant:inbox", query={"customer": c.pk}), f"🔍 Усі листи клієнта ({k['n']}) →")]
    try:
        from offers.models import QuoteRequest
        quotes = QuoteRequest.objects.filter(thread__crm_customer=c, status=QuoteRequest.NEW,
                                             message__in=_own(request).values("pk")).count()
    except Exception as exc:  # noqa: BLE001 — без Angebote лічильника немає
        logger.info("crm side panel quote requests skipped: %s", exc)
        quotes = 0
    if quotes:
        foot.append(sp.more(_url("email_assistant:inbox", query={"quote": 1, "customer": c.pk}),
                            f"📝 Запитів без Angebot: {quotes}"))
    return _concat([_concat(out), _join(foot, " ")])


def mail_badge(c, request):
    k = _mail_counts(c, request)
    if not k["n"]:
        return None
    return sp.Badge(k["n"], "info" if k["unread"] else "", f"непрочитаних: {k['unread']}" if k["unread"] else "")


def mail_summary(c, request) -> str:
    k = _mail_counts(c, request)
    if not k["n"]:
        return ""
    head = f"{k['n']} {_plural(k['n'], 'лист', 'листи', 'листів')}"
    return f"{head} · нових {k['unread']}" if k["unread"] else head


# ── 📝 Нотатки й нагадування ───────────────────────────────────────────────────

def _events(c):
    """Хронологія клієнта без подій листів (заголовок такої події — тема листа з будь-чиєї скриньки)."""
    return c.timeline_events.filter(email_message__isnull=True).exclude(event_type__in=EMAIL_EVENTS)


def _reminders(c) -> list:
    return sp.memo(c, "crm:reminders", lambda: list(
        _events(c).filter(remind_at__gte=timezone.now(), remind_sent=False).order_by("remind_at")[:REMINDERS_SHOWN]))


def _reminder_count(c) -> int:
    return sp.memo(c, "crm:reminders-n", lambda: _events(c).filter(remind_at__gte=timezone.now(),
                                                                   remind_sent=False).count())


def _event_line(e, when=None):
    return _line(_join([_ui(e.get_event_type_display()), e.title], " "), sp.dt(when or e.created_at),
                 _clamp(e.body, NOTES_MAX) if e.event_type in ("note", "call", "meeting", "reminder") else "")


def notes_html(c, request):
    reminders = _reminders(c)
    pinned = list(_events(c).filter(is_pinned=True).order_by(F("created_at").desc(nulls_last=True))[:EVENTS_SHOWN])
    seen = {e.pk for e in reminders} | {e.pk for e in pinned}
    latest = [e for e in _events(c).order_by(F("created_at").desc(nulls_last=True))[:EVENTS_SHOWN + len(seen)]
              if e.pk not in seen][:EVENTS_SHOWN]
    notes = list(c.notes_crm.select_related("created_by").order_by("-created_at")[:NOTES_SHOWN])
    note_lines = []
    for n in notes:
        author = (n.created_by.get_full_name() or n.created_by.get_username()) if n.created_by_id else ""
        due = format_html('<span class="mvp-warn">⏰ {}</span>', sp.date(n.due_date)) if n.due_date else ""
        note_lines.append(_line(_join([_ui(n.get_note_type_display()), n.subject], " "), sp.dt(n.created_at),
                                _concat([_join([author, due]), _clamp(n.body, NOTES_MAX)])))
    return _concat([
        _group("⏰ Нагадування", _concat(_event_line(e, e.remind_at) for e in reminders)),
        _group("📌 Закріплене", _concat(_event_line(e) for e in pinned)),
        _group("📝 Нотатки", _concat(note_lines)),
        _group("🕘 Останні події", _concat(_event_line(e) for e in latest)),
    ])


def notes_badge(c, request):
    n = _reminder_count(c)
    return sp.Badge(n, "info", f"нагадувань: {n}") if n else None


def notes_summary(c, request) -> str:
    n = _reminder_count(c)
    if not n:
        return ""
    first = _reminders(c)[0] if _reminders(c) else None
    return f"нагадувань {n}" + (f" · найближче {sp.dt(first.remind_at)}" if first else "")


# ── 📑 Angebote ────────────────────────────────────────────────────────────────

def _open_offers() -> tuple:
    from offers.models import Offer
    return (Offer.DRAFT, Offer.REVIEW, Offer.APPROVED, Offer.SENT)   # ще без рішення клієнта


def _offer_counts(c) -> dict:
    def load():
        from offers.models import Offer
        agg = c.offers.aggregate(n=Count("pk"), open=Count("pk", filter=Q(status__in=_open_offers())),
                                 late=Count("pk", filter=Q(status=Offer.SENT,
                                                            valid_until__lt=timezone.localdate())))
        return {k: v or 0 for k, v in agg.items()}
    return sp.memo(c, "crm:offers", load)


def offers_html(c, request):
    k = _offer_counts(c)
    if not k["n"]:
        return ""
    today = timezone.localdate()
    out = []
    for o in c.offers.order_by("-created_at")[:OFFERS_SHOWN]:
        number = sp.link(_url("offers:quick_edit", o.pk), sp.data(o.display_number))
        valid = ""
        if o.valid_until:
            late = o.valid_until < today and o.status in _open_offers()
            valid = format_html('<span class="{}">{}{}</span>', "mvp-err" if late else "mvp-note",
                                "⚠ строк дії минув " if late else "діє до ", sp.date(o.valid_until))
        out.append(_line(_join([number, sp.badge(o.get_status_display(), OFFER_TONES.get(o.status, ""))], " "),
                         sp.data(sp.money(o.gross_total, o.currency)),
                         _join([sp.date(o.created_at), valid,
                                _ui(f"надіслано {sp.date(o.sent_at)}") if o.sent_at else ""])))
    more = sp.more(_url("admin:offers_offer_changelist", query={"customer__id__exact": c.pk}),
                   f"Усі Angebote клієнта ({k['n']}) →") if k["n"] > len(out) else ""
    return _concat([_concat(out), more])


def offers_badge(c, request):
    k = _offer_counts(c)
    if not k["open"]:
        return None
    text = " · ".join(p for p in (f"відкритих: {k['open']}" if k["open"] else "",
                                  f"строк дії минув: {k['late']}" if k["late"] else "") if p)
    return sp.Badge(k["open"], "warn" if k["late"] else ("info" if k["open"] else ""), text)


def offers_summary(c, request) -> str:
    k = _offer_counts(c)
    if not k["n"]:
        return ""
    return " · ".join(p for p in (f"усього {k['n']}", f"відкритих {k['open']}" if k["open"] else "",
                                  f"строк дії минув {k['late']}" if k["late"] else "") if p)


# ── 📁 Проєкти ─────────────────────────────────────────────────────────────────

def _project_counts(c) -> dict:
    def load():
        from projects.models import Project
        agg = c.projects.aggregate(open=Count("pk", filter=Q(status__in=Project.OPEN_STATUSES)),
                                   closed=Count("pk", filter=~Q(status__in=Project.OPEN_STATUSES)))
        return {k: v or 0 for k, v in agg.items()}
    return sp.memo(c, "crm:projects", load)


def projects_html(c, request):
    from projects.models import Project
    k = _project_counts(c)
    if not k["open"] and not k["closed"]:
        return ""
    today = timezone.localdate()
    out = []
    for p in (c.projects.filter(status__in=Project.OPEN_STATUSES).select_related("owner")
              .order_by(F("due_date").asc(nulls_last=True), "-updated_at")[:PROJECTS_SHOWN]):
        due = ""
        if p.due_date:
            late = p.due_date < today
            due = format_html('<span class="{}">{}{}</span>', "mvp-err" if late else "mvp-note",
                              "⚠ строк " if late else "строк ", sp.date(p.due_date))
        owner = (p.owner.get_full_name() or p.owner.get_username()) if p.owner_id else ""
        out.append(_line(_join([sp.admin_link(request, p, f"{p.code} · {p.name}"),
                                sp.badge(p.get_status_display(), PROJECT_TONES.get(p.status, ""))], " "),
                         due, _join([owner, p.next_action and f"→ {p.next_action}"])))
    tail = []
    if k["closed"]:
        tail.append(format_html('<p class="mvp-note">{}</p>', f"завершених і скасованих: {k['closed']}"))
    tail.append(sp.more(_url("admin:projects_project_changelist", query={"customer__id__exact": c.pk}),
                        "Усі проєкти клієнта →"))
    return _concat([_concat(out) if out else format_html('<p class="mvp-note">{}</p>', "Відкритих проєктів немає."),
                    _concat(tail)])


def projects_badge(c, request):
    k = _project_counts(c)
    return sp.Badge(k["open"], "info", f"відкритих: {k['open']}") if k["open"] else None


def projects_summary(c, request) -> str:
    k = _project_counts(c)
    return " · ".join(p for p in (f"відкритих {k['open']}" if k["open"] else "",
                                  f"завершених {k['closed']}" if k["closed"] else "") if p)


# ── 🚚 Відправлення ────────────────────────────────────────────────────────────

def shipments_html(c, request):
    from api.serializers import carrier_tracking_url
    from shipping.models import Shipment
    ships = list(Shipment.objects.filter(order__in=_orders(c)).select_related("carrier", "order")
                 .order_by("-pk")[:SHIPMENTS_SHOWN])
    out = []
    for s in ships:
        carrier = getattr(s.carrier, "name", "") or "Відправлення"
        status = s.carrier_status_label or s.get_status_display()
        tn = s.tracking_number or ""
        track = ""
        if tn:
            url = carrier_tracking_url(getattr(s.carrier, "carrier_type", ""), tn)
            track = format_html("{} {}", sp.link(url, sp.data(tn), new_tab=True) if url else sp.data(tn),
                                sp.copy(tn, "Трек-номер"))
        eta = ""
        if s.eta_from or s.eta_to:
            eta = " – ".join(sp.date(d) for d in (s.eta_from, s.eta_to) if d)
        elif s.carrier_eta:
            eta = sp.date(s.carrier_eta)
        delayed = format_html('<span class="mvp-warn">{}</span>', "⚠ затримка") if s.carrier_delayed else ""
        order = sp.admin_link(request, s.order, f"№ {s.order.order_number}") if s.order_id else ""
        out.append(_line(_join([sp.admin_link(request, s, carrier), sp.badge(status, SHIPMENT_TONES.get(s.status, ""))],
                               " "),
                         track,
                         _join([order, _ui(f"очікується {eta}") if eta else "", delayed])))
    return _concat(out)


# ── 💶 Рахунки й оплати ────────────────────────────────────────────────────────

def _money_perm(request, obj=None) -> bool:
    return request.user.has_perm("accounting.view_invoice") or request.user.has_perm("sales.view_salesorder")


def money_html(c, request):
    today = timezone.localdate()
    parts = []
    if request.user.has_perm("accounting.view_invoice"):
        lines = []
        for inv in (c.invoice_set.exclude(status="cancelled").prefetch_related("lines")
                    .order_by("-pk")[:INVOICES_SHOWN]):
            due = ""
            if inv.due_date:
                late = inv.due_date < today and inv.status not in ("paid", "cancelled")
                due = format_html('<span class="{}">{}{}</span>', "mvp-err" if late else "mvp-note",
                                  "⚠ оплатити до " if late else "оплатити до ", sp.date(inv.due_date))
            lines.append(_line(_join([sp.admin_link(request, inv, inv.number or f"#{inv.pk}"),
                                      sp.badge(inv.get_status_display(), INVOICE_TONES.get(inv.status, ""))], " "),
                               sp.data(sp.money(inv.total, inv.currency)),
                               _join([sp.date(inv.issue_date), due])))
        parts.append(_group("🧾 Рахунки", _concat(lines)))
    if request.user.has_perm("sales.view_salesorder"):
        lines = []
        for o in (_orders(c).filter(payment_status__in=UNPAID)
                  .order_by(F("order_date").desc(nulls_last=True), "-pk")[:ORDERS_SHOWN]):
            lines.append(_line(_join([sp.admin_link(request, o, f"№ {o.order_number}"),
                                      sp.badge(o.get_payment_status_display(), "warn")], " "),
                               sp.data(sp.money(o.total_price, o.currency)) if o.total_price is not None else "",
                               _join([sp.date(o.order_date),
                                      _ui(o.get_payment_method_display()) if o.payment_method else ""])))
        parts.append(_group("⏳ Неоплачені замовлення", _concat(lines)))
    return _concat(parts)


# ── 📅 Зустрічі й терміни (лише власний календар) ──────────────────────────────

def _meetings(c, request):
    return c.calendar_events.filter(user=request.user, is_done=False, start_at__gte=timezone.now())


def calendar_html(c, request):
    out = []
    for e in _meetings(c, request).order_by("start_at")[:MEETINGS_SHOWN]:
        start = timezone.localtime(e.start_at) if timezone.is_aware(e.start_at) else e.start_at
        href = _url("calendar_app:calendar", query={"year": start.year, "month": start.month})
        out.append(_line(_join([_ui(e.get_event_type_display()), sp.link(href, sp.data(e.title))], " "),
                         sp.date(start) if e.all_day else sp.dt(start)))
    return _concat(out)


def calendar_badge(c, request):
    n = sp.memo(c, "crm:meetings", lambda: _meetings(c, request).count())
    return sp.Badge(n, "info", f"найближчих: {n}") if n else None


# ── ⏱ Час askDANTE ─────────────────────────────────────────────────────────────

def hours_html(c, request):
    from worktime import services as wt
    from worktime.panels import MONTHS, hm
    d = wt.customer_hours(c)
    if not d:
        return ""
    me = request.user.pk
    manager = wt.is_manager(request.user)
    rows = [("Усього", format_html("{} {}", sp.data(hm(d["total"])), _ui(f"год за останні {d['days']} днів")))]
    if manager and d.get("cost"):
        rows.append(("Вартість", format_html("≈ {} {}", sp.data(sp.money(d["cost"])), _ui("за ставками askDANTE"))))
    rows.append(("Хто", _join([f"{name} {hm(m)}" for name, m in d["people"]])))
    rows.append(("Місяці", _join([format_html("{} {}", _ui(f"{MONTHS[m.month]} {m:%Y}") if m else "—",
                                              sp.data(hm(v))) for m, v in d["months"]])))
    records = []
    for r in d["recent"][:RECORDS_SHOWN]:
        own = bool(r.dante_user_id and r.dante_user and r.dante_user.user_id == me)
        what = [r.activity_name, (r.description or "")[:120] if own else ""]
        records.append((sp.date(r.day), str(r.dante_user) if r.dante_user_id else r.person_id,
                        " · ".join(p for p in what if p) or "—", hm(r.duration)))
    synced = f"оновлено {sp.dt(d['synced_at'])}" if d.get("synced_at") else "ще не оновлювалось"
    names = ", ".join(str(p) for p in d["projects"][:4]) + (" …" if len(d["projects"]) > 4 else "")
    more = sp.more(_url("admin:worktime_danteproject_changelist", query={"crm": c.pk}), "📁 Проєкти askDANTE →") \
        if request.user.has_perm("worktime.view_danteproject") else ""
    return _concat([
        sp.kv(rows),
        sp.table(("Дата", "Хто", "Що", "Час"), records),
        format_html('<p class="mvp-note">{} {} · {}</p>', "askDANTE:", sp.data(names), synced),
        format_html('<p class="mvp-note">{}</p>', "Опис запису видно лише його автору."),
        more,
    ])


# ── 📄 Документи ───────────────────────────────────────────────────────────────

def _files(c) -> list:
    """Файли в MEDIA_ROOT/customers/<pk>/ — новіші першими, без прихованих і посилань на інші місця."""
    def load():
        root = Path(settings.MEDIA_ROOT) / "customers" / str(c.pk)
        found = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    found.append((entry.name, st.st_size, st.st_mtime))
        except OSError:
            return []
        found.sort(key=lambda f: -f[2])
        return found
    return sp.memo(c, "crm:files", load)


def _size(n):
    return _ui(f"{n / 1048576:.1f} МБ".replace(".", ",") if n >= 1048576 else f"{max(1, n // 1024)} КБ")


def documents_html(c, request):
    import datetime as _dt
    files = _files(c)
    base = f"{settings.MEDIA_URL}customers/{c.pk}/"
    lines = [_line(sp.link(base + quote(name), format_html("📄 {}", sp.data(name)), new_tab=True), _size(size),
                   sp.date(_dt.datetime.fromtimestamp(mtime)))
             for name, size, mtime in files[:FILES_SHOWN]]
    if len(files) > FILES_SHOWN:
        lines.append(format_html('<p class="mvp-note">{}</p>', f"ще файлів: {len(files) - FILES_SHOWN} — у картці"))
    knowledge = []
    try:
        from rag_assistant import access
        if access.has(request.user, "documents"):
            from rag_assistant import knowledge_items
            for item in knowledge_items.items_for(request.user, "customer", c, limit=KNOWLEDGE_SHOWN):
                knowledge.append(_line(sp.link(_url("rag_assistant:knowledge_item", item.pk), sp.data(item.title)),
                                       _ui(item.get_kind_display()), sp.date(item.created_at)))
    except Exception as exc:  # noqa: BLE001 — без бази знань — лише файли
        logger.info("crm side panel knowledge items skipped: %s", exc)
    return _concat([_concat(lines), _group("📎 База знань", _concat(knowledge))])


def documents_badge(c, request):
    n = len(_files(c))
    return sp.Badge(n) if n else None


# ── 🏪 Акаунти магазину ────────────────────────────────────────────────────────

def shop_html(c, request):
    from shop.models import ShopCustomer
    out = []
    for acc in ShopCustomer.objects.filter(crm_customer=c).select_related("shop").order_by("shop__name", "email"):
        state = sp.badge("активний", "ok") if acc.is_active else sp.badge("вимкнено", "off")
        verified = _ui(f"email підтверджено {sp.date(acc.email_verified_at)}") if acc.email_verified_at \
            else _ui("email не підтверджено")
        out.append(_line(_join([sp.admin_link(request, acc, acc.email), state], " "),
                         sp.data(getattr(acc.shop, "name", "")), verified))
    return _concat(out)


# ── дії ────────────────────────────────────────────────────────────────────────

def _compose(c, request):
    return _url("email_assistant:compose", query={"to": c.email}) if c.email else ""


def _no_email(c, request):
    return "" if c.email else "Немає email"


ACTIONS = [
    sp.Action("mail", "✉️ Написати", _compose, perm="module:email_assistant", reason=_no_email, hotkey="KeyM"),
    sp.Action("inbox", "🔍 Листи клієнта",
              lambda c, r: _url("email_assistant:inbox", query={"customer": c.pk}), perm="module:email_assistant"),
    sp.Action("new_order", "🧾 Нове замовлення", lambda c, r: _url("admin:crm_customer_new_order", c.pk),
              perm="sales.add_salesorder"),
    sp.Action("orders", "📦 Замовлення клієнта",
              lambda c, r: _url("admin:sales_salesorder_changelist", query={"q": c.email or c.company or c.name}),
              default=False, perm="sales.view_salesorder"),
    sp.Action("offers_list", "📑 Angebote клієнта",
              lambda c, r: _url("admin:offers_offer_changelist", query={"customer__id__exact": c.pk}),
              default=False, perm="offers.view_offer"),
    sp.Action("projects_list", "📁 Проєкти клієнта",
              lambda c, r: _url("admin:projects_project_changelist", query={"customer__id__exact": c.pk}),
              default=False, perm="projects.view_project"),
    sp.Action("timeline", "🕘 Хронологія",
              lambda c, r: _url("admin:crm_customertimeline_changelist", query={"customer__id__exact": c.pk}),
              default=False, perm="crm.view_customertimeline"),
    sp.Action("note_add", "📝 Нотатка", lambda c, r: _url("admin:crm_customernote_add", query={"customer": c.pk}),
              default=False, perm="crm.add_customernote"),
    sp.Action("hours_list", "⏱ Час askDANTE",
              lambda c, r: _url("admin:worktime_danteproject_changelist", query={"crm": c.pk}),
              default=False, perm="worktime.view_danteproject"),
    sp.Action("phone", "📞 Подзвонити", lambda c, r: _tel(c.phone), default=False),
    sp.Action("meeting", "🎙 Записати нараду", lambda c, r: _url("admin:meetings_meeting_new", query={"customer": c.pk}),
              default=False, new_tab=True, perm=_can_record),
]

EDITS = [
    sp.Edit("status", "status", "Статус", "choice", place="head", choices=_status_choices, undo=True,
            cells=("status_badge",), hotkey="KeyS", tones=STATUS_TONES),
    sp.Edit("favorite", "is_favorite", "⭐ Обраний", "bool", place="bar", undo=True, cells=("favorite_col",),
            hotkey="KeyF"),
    sp.Edit("segment", "segment", "Сегмент", "choice", place="block", choices=_segment_choices, undo=True,
            cells=("segment_badge",)),
]

BLOCKS = [
    sp.Block("contact", "Контакти", contact_html, icon="📇", summary=contact_summary, affects=("segment", "status"),
             fieldset=0),
    sp.Block("sales", "Продажі й RFM", sales_html, icon="📊", short="RFM", badge=sales_badge, summary=sales_summary,
             empty="Продажів ще немає.", fieldset=6),
    sp.Block("orders", "Замовлення", orders_html, icon="🧾", perm="sales.view_salesorder", badge=orders_badge,
             summary=orders_summary, empty="Замовлень ще немає.", fieldset=8),
    sp.Block("mail", "Листування", mail_html, icon="✉️", short="Листи", lazy=True, perm="module:email_assistant",
             badge=mail_badge, summary=mail_summary, empty="У ваших скриньках листів з цим клієнтом немає."),
    sp.Block("notes", "Нотатки й нагадування", notes_html, icon="📝", short="Нотатки", badge=notes_badge,
             summary=notes_summary, empty="Нотаток і подій ще немає."),
    sp.Block("offers", "Angebote", offers_html, icon="📑", perm="offers.view_offer", badge=offers_badge,
             summary=offers_summary, empty="Angebote ще немає."),
    sp.Block("projects", "Проєкти", projects_html, icon="📁", perm="projects.view_project", badge=projects_badge,
             summary=projects_summary, empty="Проєктів ще немає."),
    sp.Block("shipments", "Відправлення", shipments_html, icon="🚚", default=False, lazy=True,
             perm="shipping.view_shipment", empty="Відправлень ще немає."),
    sp.Block("money", "Рахунки й оплати", money_html, icon="💶", short="Рахунки", default=False, lazy=True,
             perm=_money_perm, empty="Рахунків і неоплачених замовлень немає."),
    sp.Block("calendar", "Зустрічі й терміни", calendar_html, icon="📅", short="Зустрічі", default=False,
             perm="module:calendar_app", badge=calendar_badge, empty="Найближчих зустрічей і термінів немає."),
    sp.Block("hours", "Час askDANTE", hours_html, icon="⏱", short="Час", default=False, lazy=True,
             perm="module:worktime", empty="Немає проєктів askDANTE цього клієнта.", fieldset=9),
    sp.Block("documents", "Документи", documents_html, icon="📄", default=False, perm="crm.view_customer",
             badge=documents_badge, empty="Документів ще немає.", fieldset=5),
    sp.Block("shop", "Акаунти магазину", shop_html, icon="🏪", short="Магазин", default=False,
             perm="shop.view_shopcustomer", empty="Акаунтів у магазинах немає."),
]

PANEL = sp.register(
    MODEL, title=title, label=label, subtitle=subtitle,
    blocks=BLOCKS, actions=ACTIONS, edits=EDITS,
    hide_cols=("sources_display", "avg_order_display", "repeat_badge", "rfm_display", "strategy_btn"),
    # злиття — щонайменше двох клієнтів: для одного запису в панелі не має сенсу
    admin_perms={"action_merge_customers": lambda request, obj=None: False},
    extra_fields=("is_favorite",),          # ⭐ немає в групах полів картки — зміна з панелі все одно дозволена
)
