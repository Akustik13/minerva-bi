"""Календарі через ICS (рівень 1, без доступу на запис до акаунтів).

Вхід: секретна адреса iCal Google («Налаштування календаря → Інтегрувати → Секретна адреса у форматі iCal») або
опублікований ICS Outlook → CalendarFeed → ExternalEvent (повторювані події розгорнуто на вікно −60…+365 днів) →
шар у календарі Minerva. Лише читання.
Вихід: календар Minerva за секретною адресою /calendar/feed/<token>.ics — підписати в Google і Outlook (там теж
лише читання; як часто оновлювати, вирішують Google і Outlook).
"""
from __future__ import annotations

import datetime
import logging
import re
import secrets
from urllib.parse import quote

import requests
from django.utils import timezone

from .invites import _lines, _unescape, _when

logger = logging.getLogger(__name__)

WINDOW_BEFORE, WINDOW_AFTER = 60, 365
MAX_BYTES = 10_000_000
MAX_OCCURRENCES = 400   # на одну повторювану подію


# ── читання ICS ──────────────────────────────────────────────────────────────
def parse(text: str) -> list[dict]:
    """VEVENT з RRULE / EXDATE / RECURRENCE-ID (для розгортання)."""
    events, cur, depth = [], None, 0
    for name, params, value in _lines(text or ""):
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur, depth = {"params": {}, "exdate": []}, 0
        elif cur is not None and name == "BEGIN":
            depth += 1
        elif cur is not None and name == "END" and value.strip().upper() == "VEVENT":
            events.append(cur)
            cur = None
        elif cur is not None and name == "END":
            depth -= 1
        elif cur is not None and depth == 0:
            if name == "EXDATE":
                for v in value.split(","):
                    w = _when(v, params)
                    if w:
                        cur["exdate"].append(w[0])
            else:
                cur.setdefault(name, value)
                cur["params"].setdefault(name, params)
    out = []
    for e in events:
        start = _when(e.get("DTSTART", ""), e["params"].get("DTSTART", {}))
        if not e.get("UID") or not start or e.get("STATUS", "").upper() == "CANCELLED":
            continue
        end = _when(e.get("DTEND", ""), e["params"].get("DTEND", {})) if e.get("DTEND") else None
        rid = _when(e.get("RECURRENCE-ID", ""), e["params"].get("RECURRENCE-ID", {})) if e.get("RECURRENCE-ID") else None
        out.append({"uid": e["UID"].strip()[:500], "title": _unescape(e.get("SUMMARY", "")) or "(без назви)",
                    "start": start[0], "all_day": start[1], "end": end[0] if end else None,
                    "location": _unescape(e.get("LOCATION", "")), "rrule": e.get("RRULE", ""),
                    "exdate": e["exdate"], "recurrence_id": rid[0] if rid else None})
    return out


_UNTIL_RE = re.compile(r"UNTIL=(\d{8})(?:T(\d{6})(Z)?)?", re.IGNORECASE)


def _until_like_start(rrule: str, aware: bool, tz) -> str:
    """UNTIL у RRULE — у тому ж вигляді, що й початок (dateutil інакше відмовляє): RFC 5545 дозволяє UNTIL датою
    (весь день, Google) або UTC-часом (Outlook). aware — початок з поясом: UNTIL → UTC (дата — кінець того дня
    місцевого часу); без пояса (події на весь день) → місцевий час без пояса."""
    def rep(m):
        day, hms, z = m.group(1), m.group(2), m.group(3)
        if aware:
            if z:
                return m.group(0)
            local = datetime.datetime.strptime(day + (hms or "235959"), "%Y%m%d%H%M%S").replace(tzinfo=tz)
            return "UNTIL=" + local.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if z:   # Outlook: «UNTIL=20261230T230000Z» = 31.12 00:00 за Берліном
            utc = datetime.datetime.strptime(day + hms, "%Y%m%d%H%M%S").replace(tzinfo=datetime.timezone.utc)
            return "UNTIL=" + utc.astimezone(tz).strftime("%Y%m%dT%H%M%S")
        return f"UNTIL={day}T{hms or '235959'}"
    return _UNTIL_RE.sub(rep, rrule or "")


def _starts(e: dict, window_start: datetime.datetime, window_end: datetime.datetime) -> list:
    """Початки повторюваної події у вікні. Весь день — розгортається в місцевих датах без пояса (UNTIL датою), потім
    кожен початок знову з поясом."""
    from dateutil.rrule import rrulestr
    tz = e["start"].tzinfo
    if e["all_day"]:
        naive = e["start"].replace(tzinfo=None)
        rule = rrulestr(_until_like_start(e["rrule"], False, tz), dtstart=naive)
        found = rule.between(window_start.astimezone(tz).replace(tzinfo=None),
                             window_end.astimezone(tz).replace(tzinfo=None), inc=True)[:MAX_OCCURRENCES]
        return [timezone.make_aware(s, tz) for s in found]
    rule = rrulestr(_until_like_start(e["rrule"], True, tz), dtstart=e["start"])
    return rule.between(window_start, window_end, inc=True)[:MAX_OCCURRENCES]


def expand(events: list[dict], window_start: datetime.datetime, window_end: datetime.datetime) -> list[dict]:
    """Події вікна: повторювані — розгорнуто (dateutil), винятки RECURRENCE-ID замінюють свої дати, EXDATE — ні."""
    overrides = {(e["uid"], e["recurrence_id"]) for e in events if e["recurrence_id"]}
    out = []
    for e in events:
        length = (e["end"] - e["start"]) if e["end"] else datetime.timedelta(days=1 if e["all_day"] else 0)
        if e["recurrence_id"] or not e["rrule"]:
            if e["start"] <= window_end and (e["end"] or e["start"]) >= window_start:
                out.append({**e, "end": e["start"] + length if e["end"] else None})
            continue
        try:
            starts = _starts(e, window_start, window_end)
        except (ValueError, TypeError) as ex:
            logger.info("rrule %s: %s", e["uid"], ex)
            # правило не розібрано — лише перший раз, і то якщо він у вікні (а не подія з 2019 року «сьогодні»)
            starts = [e["start"]] if e["start"] <= window_end and e["start"] + length >= window_start else []
        skip = {x.astimezone(datetime.timezone.utc) for x in e["exdate"]}
        for s in starts:
            if s.astimezone(datetime.timezone.utc) in skip or (e["uid"], s) in overrides:
                continue
            out.append({**e, "start": s, "end": s + length if e["end"] else None})
    return out


MAX_REDIRECTS = 5
NOT_PUBLIC = ("адреса календаря веде не в інтернет (локальна / приватна мережа або сам сервер) — Minerva такі "
              "адреси не відкриває")


def _public(host: str) -> bool:
    """Та сама перевірка, що для логотипів відправників (email_assistant/logos.py): усі IP хоста — публічні."""
    from email_assistant.logos import _public as public
    return public(host)


def _get(url: str):
    """GET календаря: адреса людини — лише публічний інтернет, і кожна переадресація перевіряється заново
    (інакше «календар» з https://evil.example → 302 → http://192.168.2.123/… прочитав би внутрішню мережу)."""
    from urllib.parse import urljoin, urlparse
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("потрібна адреса https:// (або webcal://)")
        if not _public(parts.hostname):
            raise ValueError(NOT_PUBLIC)
        r = requests.get(url, timeout=20, headers={"User-Agent": "Minerva-Calendar/1.0"}, stream=True,
                         allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            r.close()
            if not location:
                raise ValueError("переадресація без адреси")
            url = urljoin(url, location)
            continue
        return r
    raise ValueError("забагато переадресацій")


def fetch(feed) -> int:
    """Оновити підписаний календар. Збій — у last_error, старі події лишаються."""
    from .models import ExternalEvent
    url = feed.url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    try:
        r = _get(url)
        r.raise_for_status()
        raw = r.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(raw) > MAX_BYTES:
            raise ValueError("календар завеликий (понад 10 МБ)")
        text = raw.decode(r.encoding or "utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text:
            raise ValueError("за адресою не календар iCal — перевірте, що це секретна адреса .ics")
        if not feed.name:   # швидке підключення: назва — як у Google / Outlook
            m = re.search(r"^X-WR-CALNAME:(.+)$", text, re.M)
            feed.name = (_unescape(m.group(1).strip()) if m else PROVIDERS[detect(feed.url)["provider"]]["label"])[:100]
            feed.save(update_fields=["name"])
    except (requests.RequestException, ValueError) as e:
        msg = f"HTTP {e.response.status_code}" if getattr(e, "response", None) is not None else str(e)
        feed.last_error = f"{msg}"[:255]
        feed.last_fetch = timezone.now()
        feed.save(update_fields=["last_error", "last_fetch"])
        return 0
    now = timezone.now()
    items = expand(parse(text), now - datetime.timedelta(days=WINDOW_BEFORE), now + datetime.timedelta(days=WINDOW_AFTER))
    ExternalEvent.objects.filter(feed=feed).delete()
    ExternalEvent.objects.bulk_create([ExternalEvent(
        feed=feed, uid=e["uid"], title=e["title"][:300], start_at=e["start"], end_at=e["end"], all_day=e["all_day"],
        location=e["location"][:500]) for e in items])
    feed.last_error, feed.last_fetch, feed.events_count = "", now, len(items)
    feed.save(update_fields=["last_error", "last_fetch", "events_count"])
    return len(items)


# ── ⚡ швидке підключення: одне посилання, без ключів застосунку ──────────────
PROVIDERS = {
    "google": {"label": "Google Calendar", "color": "#1a73e8"},
    "microsoft": {"label": "Outlook", "color": "#f97316"},
    "other": {"label": "Календар", "color": "#8e24aa"},
}


def detect(url: str) -> dict:
    """Що за посилання вставили: {"provider", "error", "warning"} (error — не підключати)."""
    u = (url or "").strip()
    low = u.lower()
    out = {"provider": "other", "error": "", "warning": ""}
    if not low.startswith(("https://", "http://", "webcal://")):
        out["error"] = "Потрібне посилання, що починається з https:// (або webcal://)."
        return out
    if "calendar.google.com" in low or "googleusercontent.com" in low:
        out["provider"] = "google"
        if "/calendar/ical/" not in low:
            out["error"] = ("Це не посилання iCal. У Google: налаштування календаря → «Інтеграція календаря» → "
                            "«Секретна адреса у форматі iCal» (закінчується на basic.ics).")
        elif "/public/" in low:
            out["warning"] = ("Це публічна адреса — Minerva побачить події, лише якщо календар відкрито для всіх. "
                              "Для свого календаря краще «Секретна адреса у форматі iCal».")
    elif "outlook." in low or "office365.com" in low or "live.com" in low:
        out["provider"] = "microsoft"
        if low.split("?")[0].endswith(".html"):
            out["error"] = ("Це посилання HTML (для браузера). В Outlook після «Опублікувати» скопіюйте "
                            "посилання ICS (закінчується на .ics).")
    return out


def quick_add(user, url: str):
    """Підключити календар за одним посиланням. → (CalendarFeed | None, подій, помилка, попередження)."""
    from .models import CalendarFeed
    url = (url or "").strip()
    info = detect(url)
    if info["error"]:
        return None, 0, info["error"], ""
    existing = CalendarFeed.objects.filter(user=user, url=url[:1000]).first()
    if existing:
        n = fetch(existing)
        return existing, n, existing.last_error, "Цей календар уже підключено — оновлено."
    feed = CalendarFeed.objects.create(user=user, name="", url=url[:1000], refresh_minutes=15,
                                       color=PROVIDERS[info["provider"]]["color"])
    n = fetch(feed)
    if feed.last_error:
        err = feed.last_error
        feed.delete()   # не прийнялось — у списку не лишаємо
        if err.startswith("HTTP 404") or err.startswith("HTTP 403"):
            err += (" — посилання не діє: його скинули або календар не опубліковано. Скопіюйте ще раз."
                    if info["provider"] != "google" or "/public/" not in url else
                    " — календар не публічний: візьміть «Секретна адреса у форматі iCal».")
        return None, 0, err, ""
    return feed, n, "", info["warning"]


QUICK_SETTINGS = {
    "google": [("Відкрити налаштування Google Calendar", "https://calendar.google.com/calendar/r/settings")],
    "microsoft": [("Outlook (робочий / Microsoft 365)",
                   "https://outlook.office.com/calendar/options/calendar/SharedCalendars"),
                  ("outlook.com (особистий)", "https://outlook.live.com/calendar/options/calendar/SharedCalendars")],
}
QUICK_HOWTO = {
    "google": "ліворуч — ваш календар («Налаштування моїх календарів») → «Інтеграція календаря» → "
              "«Секретна адреса у форматі iCal» → копіювати",
    "microsoft": "«Опублікувати календар» → виберіть календар і «Може переглядати всі відомості» → «Опублікувати» → "
                 "скопіюйте посилання ICS (не HTML)",
}
ORIGINAL = {"google": "https://calendar.google.com/calendar/r/week",
            "microsoft": "https://outlook.office.com/calendar/view/week"}


def quick_roadmap(user, feed_token: str = "") -> list[dict]:
    """⚡ Просте підключення по кроках (як «Підключення по кроках», але без ключів): для Google і Outlook
    підключити посилання → видно в Minerva → звірити з оригіналом → Minerva туди (за бажанням) → влаштовує?
    Лише читає базу. [{provider, label, feeds, steps: [{key, title, state, detail}], done, total}]"""
    from .models import CalendarFeed
    mine = list(CalendarFeed.objects.filter(user=user).order_by("pk"))
    out = []
    for provider in ("google", "microsoft"):
        own = [f for f in mine if detect(f.url)["provider"] == provider]
        shown = [f for f in own if f.enabled and not f.last_error and f.events_count]
        broken = [f for f in own if f.last_error]
        steps = []

        def step(key, title, done, detail=""):
            steps.append({"key": key, "title": title, "state": "done" if done else "todo", "detail": detail})

        step("link", "Вставити посилання на календар", bool(own),
             ", ".join(f"«{f.name}»" for f in own) if own else "одне посилання з налаштувань календаря")
        if broken and not shown:
            detail = f"не читається: {broken[0].last_error}"
        elif own and not shown:
            detail = "приховано або в календарі немає подій"
        else:
            detail = f"подій: {sum(f.events_count for f in shown)}" if shown else ""
        step("show", "Видно в календарі Minerva", bool(shown), detail)
        step("compare", "Звірити з оригіналом", bool(shown), "тиждень у Minerva поруч із оригіналом")
        step("back", "Minerva → у " + ("Google" if provider == "google" else "Outlook") + " (за бажанням)",
             bool(feed_token) and bool(shown), "одна кнопка — календар Minerva з'явиться там")
        step("decide", "Влаштовує?", False, "так — готово; ні — «🔄 Повне підключення» (запис і правила)")
        for s in steps:   # перший незроблений — поточний
            if s["state"] == "todo":
                s["state"] = "current"
                break
        out.append({"provider": provider, "label": PROVIDERS[provider]["label"], "feeds": own, "shown": shown,
                    "broken": broken, "settings": QUICK_SETTINGS[provider], "howto": QUICK_HOWTO[provider],
                    "original": ORIGINAL[provider], "steps": steps,
                    "done": sum(1 for s in steps if s["state"] == "done"), "total": len(steps)})
    return out


def add_links(feed_url: str) -> dict:
    """Додати календар Minerva в Google / Outlook одним кліком (вони самі підпишуться на адресу)."""
    if not feed_url:
        return {}
    webcal = re.sub(r"^https?://", "webcal://", feed_url)
    https = re.sub(r"^webcal://", "https://", feed_url)
    q = quote(https, safe="")
    return {"google": "https://calendar.google.com/calendar/r?cid=" + quote(webcal, safe=""),
            "outlook": f"https://outlook.office.com/calendar/0/addfromweb?url={q}&name=Minerva",
            "outlook_live": f"https://outlook.live.com/calendar/0/addfromweb?url={q}&name=Minerva"}


def refresh_due(now=None) -> int:
    from .models import CalendarFeed
    now = now or timezone.now()
    n = 0
    for feed in CalendarFeed.objects.filter(enabled=True):
        if feed.last_fetch and now - feed.last_fetch < datetime.timedelta(minutes=max(5, feed.refresh_minutes)):
            continue
        fetch(feed)
        n += 1
    return n


def masked(url: str) -> str:
    url = url or ""
    return url[:35] + "…" if len(url) > 40 else url


# ── календар Minerva як ICS ──────────────────────────────────────────────────
def new_token(cfg) -> str:
    cfg.feed_token = secrets.token_urlsafe(32)
    cfg.save(update_fields=["feed_token"])
    return cfg.feed_token


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "") \
        .replace("\n", "\\n")


def _fold(line: str) -> str:
    """Рядки iCalendar ≤ 75 байт: довгі переносяться з пробілом на початку."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 73:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def _dt(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def export(user, host: str) -> str:
    """ICS календаря Minerva користувача: події (−30…+365 днів) і, за налаштуванням, відпустки/лікарняні askDANTE."""
    from .models import CalendarEvent, CalendarSettings
    cfg = CalendarSettings.for_user(user)
    now = timezone.now()
    stamp = _dt(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Minerva//Calendar//UK", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Minerva", "X-PUBLISHED-TTL:PT30M"]
    events = CalendarEvent.objects.filter(user=user, start_at__gte=now - datetime.timedelta(days=30),
                                          start_at__lte=now + datetime.timedelta(days=365))
    for ev in events:
        lines += ["BEGIN:VEVENT", f"UID:minerva-{ev.pk}@{host}", f"DTSTAMP:{stamp}"]
        if ev.all_day:
            d = timezone.localtime(ev.start_at).date()
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + datetime.timedelta(days=1):%Y%m%d}"]
        else:
            end = ev.end_at if ev.end_at and ev.end_at > ev.start_at else ev.start_at + datetime.timedelta(minutes=30)
            lines += [f"DTSTART:{_dt(ev.start_at)}", f"DTEND:{_dt(end)}"]
        if cfg.feed_busy_only:
            lines += ["SUMMARY:Зайнято", "CLASS:PRIVATE"]
        else:
            lines.append(_fold("SUMMARY:" + _esc(("✓ " if ev.is_done else "") + ev.title)))
            if ev.description:
                lines.append(_fold("DESCRIPTION:" + _esc(ev.description[:2000])))
        lines.append("END:VEVENT")
    if cfg.feed_worktime:
        lines += _absences(user, host, stamp, cfg.feed_busy_only)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _absences(user, host, stamp, busy_only) -> list[str]:
    try:
        from worktime import services
        from worktime.models import DanteUser, WorkDay
    except ImportError:
        return []
    du = DanteUser.objects.filter(user=user).first()
    if du is None:
        return []
    today = timezone.localdate()
    out = []
    for wd in WorkDay.objects.filter(dante_user=du, date__gte=today - datetime.timedelta(days=30),
                                     date__lte=today + datetime.timedelta(days=365)).exclude(absences=[]):
        text = services.absences_text(wd)
        if not text:
            continue
        out += ["BEGIN:VEVENT", f"UID:minerva-absence-{wd.pk}@{host}", f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{wd.date:%Y%m%d}",
                f"DTEND;VALUE=DATE:{wd.date + datetime.timedelta(days=1):%Y%m%d}",
                _fold("SUMMARY:" + _esc("Відсутність" if busy_only else text)), "TRANSP:OPAQUE", "END:VEVENT"]
    return out
