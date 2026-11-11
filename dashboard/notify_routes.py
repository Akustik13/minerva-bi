"""«🗺 Хто що отримує» — які сповіщення шле Minerva, кому на пошту і в які Telegram-чати (Налаштування → Сповіщення).

Кожен вид сповіщення (EVENTS) може мати свої адреси й чати: NotificationSettings.routes {вид: {"to": [email],
"tg": [чат], "email": bool, "telegram": bool}} — порожньо — «Кому (email)» і Chat ID налаштувань. Увімкнено чи ні —
прапорці моделі (email_field / tg_field), а для видів без них — routes[вид]["email" / "telegram"]. Під час надсилання
адреси й чати бере dashboard/notifications.py (event, recipients, tg_chats). «🚫 Не надсилати клієнтам» — blocked.
Тут нічого не надсилається.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.urls import NoReverseMatch, reverse

CHAT_RE = re.compile(r"^(-?\d{3,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
MAX_TARGETS = 20


@dataclass(frozen=True)
class Event:
    key: str
    title: str
    when: str
    email_field: str = ""              # прапорець NotificationSettings; порожньо — routes[key]["email"]
    tg_field: str = ""
    enabled: tuple = ()                # ((поле, підпис), …) — чи взагалі надсилається
    shared: str = ""                   # примітка під прапорцями
    detail: tuple = ()                 # ((поле-якір на сторінці, підпис), …) → «↓»
    ext: tuple = ()                    # прапорці в іншій моделі: (app.Model, поле email, поле Telegram, url name, підпис)


EVENTS = (
    Event("new_order", "📬 Нове замовлення", "щойно з'явилось замовлення з будь-якого джерела",
          "new_order_email", "new_order_telegram",
          detail=(("new_order_preview", "📬 Нові замовлення"), ("notify_include_total", "⚙️ Що в повідомленні"))),
    Event("dk_auto_confirm", "🤖 DigiKey: замовлення авто-підтверджено", "Minerva сама підтвердила замовлення DigiKey",
          "dk_auto_confirm_email", "dk_auto_confirm_telegram",
          detail=(("dk_confirm_preview", "🤖 DigiKey авто-підтвердження"),)),
    Event("status_change", "🔄 Статус замовлення змінився", "в обробці / відправлено / доставлено / скасовано",
          "status_change_email", "status_change_telegram",
          detail=(("notify_on_processing", "Які статуси"),)),
    Event("shipment", "📦 Статус посилки", "посилка змінила статус — вручну або трекінг DHL / UPS",
          "shipment_email", "shipment_telegram",
          detail=(("shipment_on_submitted", "Які статуси"),)),
    Event("sync_result", "⚙️ Підсумок синхронізацій", "після автосинхронізації DigiKey і трекінгу, якщо були зміни",
          "sync_result_email", "sync_result_telegram",
          detail=(("sync_skip_if_no_changes", "⚙️ Оновлення системи"),)),
    Event("alerts", "🔥 Попередження: залишок і дедлайни",
          "критичний залишок, прострочені дедлайни — не частіше за інтервал",
          enabled=(("stock_alerts_enabled", "🔥 залишок"), ("deadline_alerts_enabled", "⏰ дедлайни")),
          detail=(("deadline_overdue_days", "🔔 Планові сповіщення"), ("alert_min_interval_hours", "⏱️ Інтервал"))),
    Event("digest", "📅 Щоденний звіт", "зведення за розкладом", "digest_email", "digest_telegram",
          enabled=(("digest_enabled", "увімкнено"),),
          detail=(("digest_frequency", "Розклад і що включати"),)),
    Event("digest_weekly", "📅 Тижневий звіт", "раз на тиждень", "digest_email", "digest_telegram",
          enabled=(("weekly_digest_enabled", "увімкнено"),), shared="✉️ / 💬 — спільні з щоденним звітом",
          detail=(("weekly_digest_day", "День і час"),)),
    Event("digest_monthly", "📅 Місячний звіт", "раз на місяць", "digest_email", "digest_telegram",
          enabled=(("monthly_digest_enabled", "увімкнено"),), shared="✉️ / 💬 — спільні з щоденним звітом",
          detail=(("monthly_digest_day", "День і час"),)),
    Event("incoming", "📥 Вхідну посилку доставлено", "трекінг показав «Доставлено» для посилки до нас із закупівлею",
          ext=("inventory.InventorySettings", "incoming_notify_email", "incoming_notify_telegram",
               "admin:inventory_inventorysettings_changelist", "Налаштування складу")),
    Event("packaging", "📦 Мало коробок", "запас коробки (з обліком на складі) дійшов до її мінімуму — треба купити",
          ext=("shipping.ShippingSettings", "packaging_alert_email", "packaging_alert_telegram",
               "admin:shipping_shippingsettings_changelist", "Налаштування доставки → 📦 Запас коробок")),
)
BY_KEY = {e.key: e for e in EVENTS}


# ── Читання ──────────────────────────────────────────────────────────────────

def route(ns, key: str) -> dict:
    r = (getattr(ns, "routes", None) or {}).get(key) if key else None
    return r if isinstance(r, dict) else {}


def route_to(ns, key: str) -> list:
    return [str(a) for a in route(ns, key).get("to") or [] if str(a).strip()]


def route_chats(ns, key: str) -> list:
    return [str(c) for c in route(ns, key).get("tg") or [] if str(c).strip()]


def flag(ns, key: str, channel: str) -> bool:
    """Чи вид шле на пошту (channel email) / в Telegram — без загальних перемикачів Email / Telegram."""
    ev = BY_KEY.get(key)
    if ev is None:
        return True
    name = ev.email_field if channel == "email" else ev.tg_field
    if name:
        return bool(getattr(ns, name, False))
    if ev.ext:
        model = _model(ev.ext[0])
        obj = _single(model)
        return bool(getattr(obj, ev.ext[1] if channel == "email" else ev.ext[2], False)) if obj else True
    return route(ns, key).get(channel, True) is not False


def default_chat(ns) -> str:
    return str(getattr(ns, "telegram_chat_id", "") or "").strip()


# ── «🚫 Не надсилати клієнтам» ──────────────────────────────────────────────

def block_rules(text) -> list:
    return [r.strip().lower() for r in re.split(r"[,;\s]+", str(text or "")) if r.strip()]


def blocked(ns, email) -> str:
    """Правило зі списку, під яке підпадає адреса ('' — можна надсилати): адреса або @домен (і його піддомени)."""
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1]
    for rule in block_rules(getattr(ns, "customer_block", "")):
        if rule.startswith("@"):
            d = rule[1:]
            if d and (domain == d or domain.endswith("." + d)):
                return rule
        elif rule == email:
            return rule
    return ""


def order_address(order) -> str:
    """Кому піде лист клієнту замовлення — як у листах про відправку / підтвердження."""
    addr = (getattr(order, "ship_email", "") or getattr(order, "email", "") or "").strip()
    if not addr:
        try:
            addr = (order.customer.email or "").strip()
        except Exception:
            addr = ""
    return addr


# ── Розбір і збереження ──────────────────────────────────────────────────────

def parse_emails(text) -> tuple:
    from dashboard.notifications import parse_recipients
    return parse_recipients(text)


def parse_chats(text) -> tuple:
    good, bad = [], []
    for raw in re.split(r"[,;\s]+", str(text or "")):
        chat = raw.strip()
        if not chat:
            continue
        if not CHAT_RE.match(chat):
            bad.append(chat)
        elif chat not in good:
            good.append(chat)
    return good, bad


def save(ns, post) -> tuple:
    """Поля таблиці nr_<вид>_to / _tg / _email / _telegram → ns.routes (ns ще не збережено).
    Лише види, рядки яких були в формі (nr_<вид>_seen). → (змінені види, [неправильні адреси / чати])."""
    routes = {k: dict(v) for k, v in (ns.routes or {}).items() if isinstance(v, dict)}
    changed, bad = [], []
    for ev in EVENTS:
        if f"nr_{ev.key}_seen" not in post:
            continue
        r = dict(routes.get(ev.key) or {})
        to, wrong = parse_emails(post.get(f"nr_{ev.key}_to", ""))
        chats, wrong_chats = parse_chats(post.get(f"nr_{ev.key}_tg", ""))
        bad += wrong + wrong_chats
        new = {"to": to[:MAX_TARGETS], "tg": chats[:MAX_TARGETS]}
        if not ev.email_field and not ev.ext:
            new["email"] = f"nr_{ev.key}_email" in post
        if not ev.tg_field and not ev.ext:
            new["telegram"] = f"nr_{ev.key}_telegram" in post
        new = {k: v for k, v in new.items() if v or v is False}
        if new != {k: v for k, v in r.items() if v or v is False}:
            changed.append(ev.key)
        if new:
            routes[ev.key] = new
        else:
            routes.pop(ev.key, None)
    ns.routes = routes
    return changed, bad


# ── Адреси й чати «з системи» (меню «＋ з Minerva») ───────────────────────────

def _name(user) -> str:
    return (user.get_full_name() or user.get_username()).strip()


def email_options(ns=None) -> list:
    """[(адреса, підпис)] — співробітники, скриньки пошти, «Кому (email)» сповіщень, адреси команди магазинів."""
    out, seen = [], set()

    def add(addr, label):
        addr = str(addr or "").strip()
        if addr and "@" in addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append((addr, label))

    try:
        from django.contrib.auth import get_user_model
        for u in get_user_model().objects.filter(is_active=True, is_staff=True).exclude(email="").order_by(
                "first_name", "last_name", "username"):
            add(u.email, f"👤 {_name(u)}")
    except Exception:
        pass
    try:
        from email_assistant.models import EmailAccount
        for a in EmailAccount.objects.filter(is_active=True).order_by("email_address"):
            add(a.email_address, "📮 скринька пошти Minerva")
    except Exception:
        pass
    if ns is not None:
        from dashboard.notifications import configured_recipients
        for a in configured_recipients(ns):
            add(a, "📢 «Кому (email)» сповіщень")
    try:
        from shop.mail import addresses
        from shop.models import Shop
        for s in Shop.objects.exclude(team_emails=""):
            for a in addresses(s.team_emails):
                add(a, f"🛒 команда магазину {s.name}")
    except Exception:
        pass
    return out


def chat_options(ns=None) -> list:
    """[(чат, підпис)] — основний чат сповіщень, особисті чати співробітників з ботом (Telegram ID), уже вибрані."""
    out, seen = [], set()

    def add(chat, label):
        chat = str(chat or "").strip()
        if chat and chat not in seen:
            seen.add(chat)
            out.append((chat, label))

    if ns is not None:
        add(default_chat(ns), "📢 основний чат сповіщень (Chat ID)")
    try:
        from core.models import UserProfile
        for p in UserProfile.objects.filter(telegram_id__isnull=False, user__is_active=True).select_related("user"):
            add(p.telegram_id, f"👤 {_name(p.user)} — особистий чат з ботом")
    except Exception:
        pass
    try:
        from shop.models import Shop
        for s in Shop.objects.exclude(team_telegram=""):
            for c in parse_chats(s.team_telegram)[0]:
                add(c, f"🛒 чат команди магазину {s.name}")
    except Exception:
        pass
    if ns is not None:
        for key in (ns.routes or {}):
            for c in route_chats(ns, key):
                add(c, "💬 чат з інших сповіщень")
    return out


def labels(value_list, options) -> list:
    known = {str(v).lower(): label for v, label in options}
    return [(v, known.get(str(v).lower(), "")) for v in value_list]


# ── Дані таблиці ─────────────────────────────────────────────────────────────

def _model(label):
    from django.apps import apps
    try:
        return apps.get_model(label)
    except (LookupError, ValueError):
        return None


def _single(model):
    if model is None:
        return None
    try:
        return model.objects.order_by("pk").first()
    except Exception:
        return None


def _url(name, *args) -> str:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


@dataclass
class Row:
    event: Event
    to: list = field(default_factory=list)
    chats: list = field(default_factory=list)
    email_on: bool = True
    tg_on: bool = True
    enabled: list = field(default_factory=list)     # [(поле, підпис, увімкнено)]
    ext_url: str = ""


def rows(ns) -> list:
    out = []
    for ev in EVENTS:
        out.append(Row(ev, route_to(ns, ev.key), route_chats(ns, ev.key), flag(ns, ev.key, "email"),
                       flag(ns, ev.key, "telegram"),
                       [(f, label, bool(getattr(ns, f, False))) for f, label in ev.enabled],
                       _url(ev.ext[3]) if ev.ext else ""))
    return out


def _on(v) -> str:
    return "✅" if v else "⏸"


def others(ns) -> list:
    """Сповіщення, що налаштовуються деінде: [{title, email, telegram, link: (url, підпис)}]. Лише читання."""
    from dashboard.notifications import configured_recipients
    default_to = ", ".join(configured_recipients(ns)) or "—"
    out = []

    def add(title, email, telegram, url, label):
        if url:
            out.append({"title": title, "email": email, "telegram": telegram, "link": (url, label)})

    try:
        from shop.mail import addresses
        from shop.models import Shop
        for s in Shop.objects.order_by("name"):
            team = ", ".join(addresses(s.team_emails))
            chats = ", ".join(parse_chats(s.team_telegram)[0])
            add(f"🛒 Магазин «{s.name}»: оплата отримана, новий акаунт покупця",
                team or f"як «Кому (email)»: {default_to}",
                ("⏸ вимкнено" if not s.team_notify_telegram else (chats or "основний чат")),
                _url("admin:shop_shop_change", s.pk), "Картка магазину → ✉️ Пошта магазину")
    except Exception:
        pass
    dk = _single(_model("bots.DigiKeyConfig"))
    if dk is not None:
        url = _url("admin:bots_digikeyconfig_change", dk.pk)
        add("💬 DigiKey: нові повідомлення покупців",
            f"{_on(getattr(dk, 'msg_notify_email', False))} {getattr(dk, 'msg_notify_email_to', '') or default_to}",
            _on(getattr(dk, "msg_notify_telegram", False)) + " "
            + (", ".join(parse_chats(getattr(dk, "msg_notify_tg_chats", ""))[0]) or "основний чат"),
            url, "Налаштування DigiKey → 💬 Повідомлення")
        add("🛒 DigiKey: лістинги опубліковано",
            f"{_on(getattr(dk, 'poll_notify_email', False))} {getattr(dk, 'poll_notify_email_to', '') or default_to}",
            _on(getattr(dk, "poll_notify_telegram", False)) + " основний чат", url, "Налаштування DigiKey")
        add("🔌 DigiKey: збій API",
            f"{_on(getattr(dk, 'api_notify_email', False))} {getattr(dk, 'api_notify_email_to', '') or default_to}",
            _on(getattr(dk, "api_notify_telegram", False)) + " основний чат", url, "Налаштування DigiKey")
    prices = _single(_model("bots.DigiKeyPriceSettings"))
    if prices is not None:
        add("💲 Ціни DigiKey оновлено / чекають підтвердження",
            f"{_on(prices.notify_email)} {prices.notify_email_to or default_to}",
            _on(prices.notify_telegram) + " основний чат",
            _url("admin:bots_digikeypricesettings_change", prices.pk), "Ціни DigiKey ← Minerva")
    jlc = _single(_model("jlcpcb.JLCConfig"))
    if jlc is not None:
        add("🏭 JLCPCB: замовлення відправлено / доставлено",
            f"{_on(jlc.notify_email)} {jlc.notify_email_to or default_to}",
            _on(jlc.notify_telegram) + " " + (jlc.telegram_personal_chat_id or "основний чат"),
            _url("admin:jlcpcb_jlcconfig_change", jlc.pk), "Налаштування JLCPCB")
    brief = _single(_model("config.BriefingSettings"))
    if brief is not None:
        add("🌅 Ранковий брифінг", "—", _on(brief.enabled) + " кожному співробітнику з Telegram ID",
            _url("admin:config_briefingsettings_change", brief.pk), "Налаштування брифінгу")
    add("🤖 Агент: завдання виконано, регулярні звіти", "власнику завдання (його email)",
        "власнику — у його чат з ботом (Telegram ID)", _url("admin:core_userprofile_changelist"),
        "Ролі користувачів → Telegram ID")
    add("⏱ Звіти робочого часу", "кому вказано в розкладі звіту", "—", _url("admin:worktime_workreport_changelist"),
        "Звіти робочого часу")
    proj = _single(_model("projects.ProjectSettings"))
    if proj is not None:
        add("📁 Тижневий звіт проєктів", "відповідальному — його email", "відповідальному — у чат з ботом",
            _url("admin:projects_projectsettings_change", proj.pk), "Налаштування проєктів")
    return out
