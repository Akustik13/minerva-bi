"""
bots/services/dk_message_watch.py — 💬 нові повідомлення DigiKey Marketplace → Telegram і пошта.

Раніше сповіщення не приходили, бо:
  1. перевірка падала на першому ж новому повідомленні покупця з номером замовлення
     (`select_related("customer")` — у замовлення немає такого поля), тож нічого не надсилалось і не зберігалось;
  2. розмову перечитувала лише тоді, коли в списку тем змінився `lastUpdateDateUtc` проти кешу хабу — а ця дата
     на нові повідомлення не зважає, і «🔄 Оновити» в хабі перезаписував той самий кеш;
  3. «уже повідомили» й «людина прочитала» були одним полем: сповіщення робило розмову прочитаною в хабі;
  4. текст покупця йшов у Telegram без екранування — «<», «&» ламали все повідомлення.

Тепер `check`: список тем (до LIST_MAX, сторінками) → свіжі розмови найновіших тем (до FETCH_MAX, WORKERS
потоків) → нові повідомлення покупця й оператора DigiKey після `DigiKeyMessageSeen.notified_upto` (тема, яку
Minerva бачить уперше або давно не перевіряла, — не старші за FIRST_DAYS, щоб не розіслати весь архів) → одне
сповіщення на тему: Telegram і лист з повним текстом, попередніми повідомленнями й посиланнями. Відповіді з нашого
боку (Supplier: колега чи ви — з Minerva або supplier.digikey.com) — теж, якщо `DigiKeyConfig.msg_notify_own`
(`senders`). «Прочитано» в хабі (`last_message_id`) перевірка не змінює. `run` — розклад, токен, повтори (cron
`check_digikey_messages`, кнопка «🔄 Перевірити зараз»); `status` — що зараз діє (панель у налаштуваннях DigiKey);
`forward` / `forward_order` — «✉️ Чат на пошту» в хабі й у картці замовлення: увесь чат теми / усіх тем замовлення
з DigiKey наживо → лист будь-кому (`chat_email`); `order_topics` — теми замовлення (API фільтрує лише за UUID).
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

LIST_MAX = 500                  # тем у списку DigiKey (сторінками по 50)
FETCH_MAX = 150                 # розмов, які перечитуємо щоразу — найновіші теми
WORKERS = 4
FIRST_DAYS = 7                  # повідомлення, старші за стільки днів, не розсилаються (архів, довга перерва)
CONTEXT_MESSAGES = 6            # попередніх повідомлень розмови в листі
NOTIFY_SENDERS = ("customer", "operator")
WHO = {"customer": "покупець", "operator": "оператор DigiKey", "supplier": "наша відповідь"}
AUTH_WORDS = ("401", "unauthorized", "token refresh error", "не авторизовано", "invalid_grant")
LOCK_KEY = "dk-messages-check"
LOCK_SECONDS = 15 * 60
BACKGROUND = True


def _cache():
    from core.shared_cache import cache
    return cache


def _when(value):
    """ISO-дата DigiKey → aware datetime або None."""
    dt = parse_datetime(str(value or "")) if value else None
    if dt is not None and timezone.is_naive(dt):
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


def _local(dt) -> str:
    return timezone.localtime(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def _sender(m) -> str:
    return (m.get("sender") or "").strip().lower()


def senders(config) -> tuple:
    """Про чиї повідомлення сповіщати: покупець і оператор DigiKey, з `msg_notify_own` — і наш бік (Supplier)."""
    return NOTIFY_SENDERS + (("supplier",) if config.msg_notify_own else ())


class ForwardError(Exception):
    """Зрозуміла причина, чому «✉️ На пошту» не надіслав."""


# ── Перевірка ─────────────────────────────────────────────────────────────────

def check(config, token, *, notify=True) -> dict:
    """Одна перевірка з готовим токеном. Помилка списку тем — назовні (`run` повторює), окремої розмови — у errors."""
    from bots.models import DigiKeyMessageSeen
    from bots.services.digikey_messages import get_all_topics_paginated, get_topic

    items = [t for t in get_all_topics_paginated(config, token, max_total=LIST_MAX) if t.get("id")]
    items.sort(key=lambda t: t.get("lastUpdateDateUtc") or "", reverse=True)
    want = items[:FETCH_MAX]

    def one(item):
        try:
            return item, get_topic(config, token, str(item["id"])), ""
        except Exception as exc:  # noqa: BLE001 — одна розмова не зупиняє решту
            return item, None, str(exc)[:200]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(one, want))

    old_cache = {str(t.get("id")): t for t in (config.msg_topics_cache or []) if t.get("id")}
    cache, topics, errors = [], [], []
    for item, full, err in fetched:
        tid = str(item["id"])
        if full is None:
            errors.append(f"{item.get('orderNumber') or tid}: {err}")
            if tid in old_cache:
                cache.append(old_cache[tid])
            continue
        full["orderNumber"] = item.get("orderNumber", "")
        if item.get("lastUpdateDateUtc"):
            full.setdefault("lastUpdateDateUtc", item["lastUpdateDateUtc"])
        cache.append(full)
        topics.append(full)
    fetched_ids = {str(i["id"]) for i, _, _ in fetched}
    cache += [t for tid, t in old_cache.items() if tid not in fetched_ids and tid in {str(i["id"]) for i in items}]

    rows = {s.topic_id: s for s in DigiKeyMessageSeen.objects.filter(topic_id__in=[str(t["id"]) for t in topics])}
    floor = timezone.now() - timedelta(days=FIRST_DAYS)
    found, sent = [], {"telegram": 0, "email": 0}
    channels = _channels(config) if notify else {}
    whose = senders(config)
    for topic in topics:
        tid = str(topic["id"])
        conv = [m for m in (topic.get("conversation") or []) if _when(m.get("createDateUtc"))]
        conv.sort(key=lambda m: _when(m.get("createDateUtc")))
        row = rows.get(tid)
        since = row.notified_upto if row and row.notified_upto else None
        start = max(since, floor) if since else floor
        new = [m for m in conv if _sender(m) in whose and _when(m.get("createDateUtc")) > start]
        if new:
            found.append({"topic": topic, "messages": new})
            if notify:
                for ch, ok in _send(config, topic, new, conv, channels).items():
                    sent[ch] += ok
        if conv:
            last = conv[-1]
            upto = _when(last.get("createDateUtc"))
            if row is None:
                row = DigiKeyMessageSeen(topic_id=tid)
            if row.notified_upto != upto:
                row.notified_upto, row.notified_message_id = upto, str(last.get("id", ""))[:64]
                row.save()
    return {"topics": len(items), "fetched": len(topics), "found": found, "sent": sent, "errors": errors,
            "channels": channels, "cache": cache}


def run(*, force=False, notify=True, log=None, locked=False) -> dict | None:
    """Перевірка за розкладом (cron) або зараз (force): увімкнено, інтервал, токен, повтори списку тем.
    None — пропущено (вимкнено / ще рано / немає авторизації / уже йде інша); інакше — підсумок `check`.
    Одна перевірка одночасно (LOCK_KEY): інакше cron і «🔄 Перевірити зараз» надіслали б те саме двічі."""
    say = log or (lambda text: None)
    if not locked:
        if not _cache().add(LOCK_KEY, 1, LOCK_SECONDS):
            say("Уже йде інша перевірка повідомлень.")
            return None
        try:
            return _run(force=force, notify=notify, say=say)
        finally:
            _cache().delete(LOCK_KEY)
    return _run(force=force, notify=notify, say=say)


def _run(*, force, notify, say):
    from bots.models import DigiKeyConfig
    config = DigiKeyConfig.get()
    if not config.msg_check_enabled and not force:
        say("Авто-перевірка повідомлень вимкнена.")
        return None
    if not force and config.msg_last_checked_at and config.msg_check_interval:
        if timezone.now() - config.msg_last_checked_at < timedelta(minutes=config.msg_check_interval):
            say("Ще рано — інтервал перевірки не минув.")
            return None
    if not config.marketplace_access_token and not config.marketplace_refresh_token:
        _finish(config, "✗ Немає авторизації DigiKey Marketplace (OAuth)")
        say("Немає авторизації DigiKey Marketplace.")
        return None
    from bots.services.api_health import notify_connection_failure, notify_reauth_needed
    from bots.services.digikey import get_marketplace_token
    tries = max(1, min(10, config.api_retry_count or 3))
    delay = max(1, config.api_retry_delay or 10)
    for attempt in range(1, tries + 1):
        try:
            result = check(config, get_marketplace_token(config), notify=notify)
            break
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if any(w in text.lower() for w in AUTH_WORDS):
                notify_reauth_needed(config, text)
                _finish(config, f"✗ DigiKey не пустив (авторизація): {text[:200]}", checked=False)
                say(f"Авторизація: {text}")
                return None
            say(f"Помилка API (спроба {attempt}/{tries}): {text}")
            if attempt == tries:
                notify_connection_failure(config, text, tries, "check_digikey_messages")
                _finish(config, f"✗ Список тем не прочитано: {text[:200]}", checked=False)
                return None
            time.sleep(delay)
    _finish(config, summary(result), cache=result["cache"])
    say(summary(result))
    return result


def _finish(config, text, *, cache=None, checked=True):
    """Підсумок і час перевірки — UPDATE (токен, який щойно оновив get_marketplace_token, не перезаписується);
    cache — свіжі розмови для хабу «💬 Messages Hub»."""
    from bots.models import DigiKeyConfig
    now = timezone.now()
    fields = {"msg_last_result": f"{_local(now)} · {text}"[:300]}
    if checked:
        fields["msg_last_checked_at"] = now
    if cache is not None:
        fields.update(msg_topics_cache=cache, msg_cache_at=now)
    DigiKeyConfig.objects.filter(pk=config.pk).update(**fields)


def summary(result: dict) -> str:
    n = sum(len(f["messages"]) for f in result["found"])
    parts = [f"тем {result['topics']}, розмов прочитано {result['fetched']}",
             f"нових повідомлень {n}" if n else "нових повідомлень немає"]
    if n:
        ch = result.get("channels") or {}
        for key, label in (("telegram", "Telegram"), ("email", "пошта")):
            parts.append(f"{label} ✓ {result['sent'][key]}" if ch.get(key, {}).get("on")
                         else f"{label} — {ch.get(key, {}).get('why') or 'вимкнено'}")
    if result["errors"]:
        parts.append(f"не прочитано розмов {len(result['errors'])}")
    return "✓ " + " · ".join(parts)


def start_now(user="") -> bool:
    """«🔄 Перевірити зараз» — у фоні; False — перевірка вже йде."""
    if not _cache().add(LOCK_KEY, user or 1, LOCK_SECONDS):
        return False
    if not BACKGROUND:
        try:
            run(force=True, locked=True)
        finally:
            _cache().delete(LOCK_KEY)
        return True
    threading.Thread(target=_run_thread, daemon=True, name="MinervaDKMessages").start()
    return True


def _run_thread():
    from django.db import connection
    try:
        run(force=True, locked=True)
    except Exception:  # noqa: BLE001
        logger.exception("DigiKey messages check")
    finally:
        _cache().delete(LOCK_KEY)
        connection.close()


# ── Куди надсилати ────────────────────────────────────────────────────────────

def _ns():
    from config.models import NotificationSettings
    return NotificationSettings.objects.filter(pk=1).first()


def recipients(config, ns=None) -> list:
    ns = ns if ns is not None else _ns()
    raw = (config.msg_notify_email_to or "").strip() or (getattr(ns, "email_to", "") or "")
    return [e.strip() for e in raw.replace(";", ",").replace("\n", ",").split(",") if e.strip()]


def _channels(config) -> dict:
    """{telegram|email: {on, why, to}} — чи піде сповіщення і чому ні."""
    ns = _ns()
    tg = {"on": False, "why": ""}
    if not config.msg_notify_telegram:
        tg["why"] = "вимкнено для повідомлень DigiKey"
    elif not ns or not ns.telegram_enabled:
        tg["why"] = "Telegram вимкнено в «Налаштування → Сповіщення»"
    elif not (ns.telegram_bot_token and ns.telegram_chat_id):
        tg["why"] = "у «Налаштування → Сповіщення» немає бота або чату Telegram"
    else:
        tg["on"] = True
    em = {"on": False, "why": "", "to": recipients(config, ns) if ns else []}
    if not config.msg_notify_email:
        em["why"] = "вимкнено для повідомлень DigiKey"
    elif not ns or not ns.email_enabled:
        em["why"] = "пошту вимкнено в «Налаштування → Сповіщення»"
    elif not ns.email_host:
        em["why"] = "у «Налаштування → Сповіщення» немає SMTP"
    elif not em["to"]:
        em["why"] = "не вказано, кому"
    else:
        em["on"] = True
    return {"telegram": tg, "email": em, "ns": ns}


def _base(config) -> str:
    from django.conf import settings
    base = (config.public_base_url or getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    from dashboard.notifications import _abs_url
    url = _abs_url("/")
    return url.rstrip("/") if url.startswith("http") else ""


def _context(config, topic) -> dict:
    """Замовлення Minerva й клієнт CRM для теми, посилання на хаб і замовлення."""
    from django.urls import reverse

    from sales.models import SalesOrder
    base = _base(config)
    number = str(topic.get("orderNumber") or "").strip()
    order = SalesOrder.objects.filter(order_number=number).first() if number else None
    customer = order.crm_customer if order else None
    who = ""
    if customer:
        who = " · ".join(x for x in (customer.company, customer.name) if x)
    elif order:
        who = order.client
    return {"number": number or str(topic.get("orderId") or ""), "order": order, "customer": who,
            "hub": base + reverse("admin:bots_digikeyconfig_messages"),
            "order_url": base + reverse("admin:sales_salesorder_change", args=[order.pk]) if order else ""}


def _send(config, topic, new, conv, channels) -> dict:
    sent = {"telegram": 0, "email": 0}
    ctx = _context(config, topic)
    if channels["telegram"]["on"]:
        try:
            from dashboard.notifications import _send_telegram
            _send_telegram(channels["ns"], telegram_text(topic, new, ctx))
            sent["telegram"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("DigiKey message → Telegram: %s", exc)
    if channels["email"]["on"]:
        try:
            from dashboard.notifications import _send_event_email, send_only_to
            subject, html = email_message(topic, new, conv, ctx)
            with send_only_to(channels["email"]["to"], telegram=False):
                _send_event_email(channels["ns"], subject, html)
            sent["email"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("DigiKey message → email: %s", exc)
    return sent


# ── Тексти ────────────────────────────────────────────────────────────────────

def _who_line(new) -> str:
    kinds = {_sender(m) for m in new}
    return " і ".join(WHO.get(k, k) for k in ("customer", "operator", "supplier") if k in kinds) or "покупець"


def telegram_text(topic, new, ctx) -> str:
    from dashboard.notifications import esc
    head = "💬 <b>DigiKey: нове повідомлення</b>" if len(new) == 1 else f"💬 <b>DigiKey: нових повідомлень {len(new)}</b>"
    lines = [f"{head} — {esc(_who_line(new))}",
             f"🛒 Замовлення #{esc(ctx['number'])}" + (f" · {esc(ctx['customer'])}" if ctx["customer"] else ""),
             f"📋 {esc(topic.get('topic') or '—')}", ""]
    for m in new:
        lines.append(f"<i>{esc(WHO.get(_sender(m), _sender(m)).capitalize())}, {esc(_local(_when(m.get('createDateUtc'))))}:</i>")
        lines.append(esc((m.get("content") or "").strip()))
        if m.get("files"):
            lines.append(f"📎 файлів: {len(m['files'])} — дивіться в DigiKey")
        lines.append("")
    if ctx["hub"].startswith("http"):
        lines.append(f"↩ Відповісти: {esc(ctx['hub'])}")
    if ctx["order_url"].startswith("http"):
        lines.append(f"📦 Замовлення: {esc(ctx['order_url'])}")
    return "\n".join(lines).strip()


def email_message(topic, new, conv, ctx) -> tuple[str, str]:
    """(тема, HTML) сповіщення — повний текст нових повідомлень, кілька попередніх, посилання."""
    from django.template.defaultfilters import linebreaksbr
    from django.utils.html import escape
    first = (new[-1].get("content") or "").strip().replace("\n", " ")
    subject = f"💬 DigiKey #{ctx['number']}: {topic.get('topic') or ''} — {first[:60]}".strip()
    intro = f'<p><b>Нове повідомлення в DigiKey Marketplace</b> — {escape(_who_line(new))}.</p>'

    def block(m, strong):
        who = WHO.get(_sender(m), _sender(m)).capitalize()
        files = (f'<div style="color:#666;font-size:12px">📎 файлів: {len(m["files"])} — дивіться в DigiKey</div>'
                 if m.get("files") else "")
        style = ("border-left:4px solid #1565c0;background:#f3f7ff;padding:10px 14px;margin:8px 0" if strong else
                 "border-left:3px solid #ccc;padding:6px 12px;margin:6px 0;color:#555;font-size:13px")
        return (f'<div style="{style}"><div style="font-size:12px;color:#666">{escape(who)} · '
                f'{escape(_local(_when(m.get("createDateUtc"))))}</div>'
                f'<div>{linebreaksbr(escape((m.get("content") or "").strip()))}</div>{files}</div>')

    earlier = [m for m in conv if m not in new][-CONTEXT_MESSAGES:]
    rows = [("Замовлення", f"#{ctx['number']}"), ("Клієнт", ctx["customer"]), ("Тема", topic.get("topic") or "—")]
    table = "".join(f'<tr><td style="color:#666;padding:2px 12px 2px 0">{escape(k)}</td><td><b>{escape(v)}</b></td></tr>'
                    for k, v in rows if v)
    links = []
    if ctx["hub"].startswith("http"):
        links.append(f'<a href="{escape(ctx["hub"])}">💬 Відповісти в Minerva</a>')
    if ctx["order_url"].startswith("http"):
        links.append(f'<a href="{escape(ctx["order_url"])}">📦 Замовлення в Minerva</a>')
    html = (
        '<html><head></head><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
        '<div style="max-width:640px">'
        + intro +
        f'<table style="border-collapse:collapse;font-size:13px;margin-bottom:8px">{table}</table>'
        + "".join(block(m, True) for m in new)
        + (('<p style="color:#666;margin:14px 0 4px">Раніше в розмові:</p>' + "".join(block(m, False) for m in earlier))
           if earlier else "")
        + (f'<p style="margin-top:14px">{" · ".join(links)}</p>' if links else "")
        + '<p style="color:#888;font-size:12px">Відповісти листом не можна — DigiKey приймає відповідь лише в '
          'Marketplace: у Minerva («💬 Messages Hub») або на supplier.digikey.com.</p></div></body></html>')
    return subject[:200], html


# ── «✉️ Чат на пошту»: хаб і картка замовлення ───────────────────────────────

def _conv(topic) -> list:
    return sorted((m for m in (topic.get("conversation") or []) if _when(m.get("createDateUtc"))),
                  key=lambda m: _when(m.get("createDateUtc")))


def order_topics(config, token, order_number) -> list:
    """Усі теми замовлення з повними розмовами. API DigiKey фільтрує теми лише за UUID замовлення, тож — увесь
    список (до LIST_MAX) і відбір за номером; розмову не прочитано — тема без неї."""
    from bots.services.digikey_messages import get_all_topics_paginated, get_topic
    number = str(order_number or "").strip()
    out = []
    for item in get_all_topics_paginated(config, token, max_total=LIST_MAX):
        if not item.get("id") or str(item.get("orderNumber", "")) != number:
            continue
        try:
            full = get_topic(config, token, str(item["id"]))
        except Exception:  # noqa: BLE001
            full = dict(item)
        full["orderNumber"] = item.get("orderNumber", "")
        out.append(full)
    return out


def chat_email(topics, ctx, forwarded_by="") -> tuple[str, str]:
    """(тема, HTML) — увесь чат: кожна тема з усіма повідомленнями по порядку, найновіше виділено; посилання."""
    from django.template.defaultfilters import linebreaksbr
    from django.utils.html import escape
    convs = [(t, _conv(t)) for t in topics]
    total = sum(len(c) for _, c in convs)
    last = max((m for _, c in convs for m in c), key=lambda m: _when(m.get("createDateUtc")), default=None)
    title = topics[0].get("topic") or "" if len(topics) == 1 else f"чат ({len(topics)} теми)"
    subject = f"↪ DigiKey #{ctx['number']}: {title} — повідомлень {total}".strip()

    def block(m):
        strong = m is last
        who = WHO.get(_sender(m), _sender(m)).capitalize()
        files = (f'<div style="color:#666;font-size:12px">📎 файлів: {len(m["files"])} — дивіться в DigiKey</div>'
                 if m.get("files") else "")
        mine = _sender(m) == "supplier"
        style = ("border-left:4px solid #1565c0;background:#f3f7ff;padding:10px 14px;margin:8px 0" if strong else
                 "border-left:3px solid " + ("#2e7d32" if mine else "#ccc") + ";padding:6px 12px;margin:6px 0")
        return (f'<div style="{style}"><div style="font-size:12px;color:#666">{escape(who)} · '
                f'{escape(_local(_when(m.get("createDateUtc"))))}{" · найновіше" if strong else ""}</div>'
                f'<div>{linebreaksbr(escape((m.get("content") or "").strip()))}</div>{files}</div>')

    parts = []
    for t, conv in convs:
        parts.append(f'<h3 style="font-size:15px;margin:16px 0 4px">📋 {escape(t.get("topic") or "—")}</h3>')
        parts.append("".join(block(m) for m in conv) or '<p style="color:#888">Повідомлень немає.</p>')
    rows = [("Замовлення", f"#{ctx['number']}"), ("Клієнт", ctx["customer"])]
    table = "".join(f'<tr><td style="color:#666;padding:2px 12px 2px 0">{escape(k)}</td><td><b>{escape(v)}</b></td></tr>'
                    for k, v in rows if v)
    links = []
    if ctx["hub"].startswith("http"):
        links.append(f'<a href="{escape(ctx["hub"])}">💬 Відповісти в Minerva</a>')
    if ctx["order_url"].startswith("http"):
        links.append(f'<a href="{escape(ctx["order_url"])}">📦 Замовлення в Minerva</a>')
    who = f" — переслав {escape(forwarded_by)}" if forwarded_by else ""
    html = (
        '<html><head></head><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
        '<div style="max-width:640px">'
        f'<p><b>↪ Чат DigiKey Marketplace</b>{who} · повідомлень: {total}.</p>'
        f'<table style="border-collapse:collapse;font-size:13px;margin-bottom:8px">{table}</table>'
        + "".join(parts)
        + (f'<p style="margin-top:14px">{" · ".join(links)}</p>' if links else "")
        + '<p style="color:#888;font-size:12px">Відповісти листом не можна — DigiKey приймає відповідь лише в '
          'Marketplace: у Minerva («💬 Messages Hub») або на supplier.digikey.com.</p></div></body></html>')
    return subject[:200], html


def _deliver(config, ns, to, topics, user) -> dict:
    from dashboard.notifications import _send_event_email, send_only_to
    if not topics:
        raise ForwardError("у DigiKey немає розмов цього замовлення")
    if not any(_conv(t) for t in topics):
        raise ForwardError("у розмові ще немає повідомлень")
    subject, html = chat_email(topics, _context(config, topics[0]), forwarded_by=user)
    with send_only_to(to, telegram=False):
        _send_event_email(ns, subject, html)
    return {"to": to, "subject": subject, "messages": sum(len(_conv(t)) for t in topics)}


def _prepare(to):
    """(config, ns, адреси) — SMTP «Налаштування → Сповіщення» (перемикач «Email» там не потрібен: ручне
    надсилання); без адрес — «Email одержувачів» повідомлень DigiKey або «Кому (email)» сповіщень."""
    from bots.models import DigiKeyConfig
    config = DigiKeyConfig.get()
    ns = _ns()
    if not ns or not ns.email_host:
        raise ForwardError("у «Налаштування → Сповіщення» немає SMTP")
    to = list(to or []) or recipients(config, ns)
    if not to:
        raise ForwardError("не вказано, кому")
    return config, ns, to


def forward(topic_id, to=None, user="") -> dict:
    """Хаб «✉️ Чат на пошту»: уся розмова теми з DigiKey наживо → лист. → {to, subject, messages}."""
    from bots.services.digikey import get_marketplace_token
    from bots.services.digikey_messages import get_all_topics_paginated, get_topic
    config, ns, to = _prepare(to)
    token = get_marketplace_token(config)
    topic = get_topic(config, token, str(topic_id))
    if not topic.get("orderNumber"):                     # GET теми номера замовлення не віддає — кеш хабу, список тем
        known = next((t for t in (config.msg_topics_cache or []) if str(t.get("id")) == str(topic_id)), None)
        if not known:
            known = next((t for t in get_all_topics_paginated(config, token, max_total=LIST_MAX)
                          if str(t.get("id")) == str(topic_id)), None)
        topic["orderNumber"] = (known or {}).get("orderNumber", "")
    return _deliver(config, ns, to, [topic], user)


def forward_order(order, to=None, user="") -> dict:
    """Картка замовлення «✉️ Чат на пошту»: усі теми замовлення з DigiKey наживо → один лист."""
    from bots.services.digikey import get_marketplace_token
    config, ns, to = _prepare(to)
    return _deliver(config, ns, to, order_topics(config, get_marketplace_token(config), order.order_number), user)


# ── Стан для панелі ───────────────────────────────────────────────────────────

def status(config) -> dict:
    """Що зараз діє: авто-перевірка, інтервал, остання перевірка й підсумок, куди підуть сповіщення."""
    ch = _channels(config)
    late = None
    if config.msg_check_enabled and config.msg_last_checked_at and config.msg_check_interval:
        late = timezone.now() - config.msg_last_checked_at > timedelta(minutes=3 * config.msg_check_interval + 10)
    return {"enabled": config.msg_check_enabled, "interval": config.msg_check_interval, "own": config.msg_notify_own,
            "last": _local(config.msg_last_checked_at), "result": config.msg_last_result, "late": late,
            "telegram": ch["telegram"], "email": {k: v for k, v in ch["email"].items()},
            "running": bool(_cache().get(LOCK_KEY))}
