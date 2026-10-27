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
import secrets

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


def expand(events: list[dict], window_start: datetime.datetime, window_end: datetime.datetime) -> list[dict]:
    """Події вікна: повторювані — розгорнуто (dateutil), винятки RECURRENCE-ID замінюють свої дати, EXDATE — ні."""
    from dateutil.rrule import rrulestr
    overrides = {(e["uid"], e["recurrence_id"]) for e in events if e["recurrence_id"]}
    out = []
    for e in events:
        length = (e["end"] - e["start"]) if e["end"] else datetime.timedelta(days=1 if e["all_day"] else 0)
        if e["recurrence_id"] or not e["rrule"]:
            if e["start"] <= window_end and (e["end"] or e["start"]) >= window_start:
                out.append({**e, "end": e["start"] + length if e["end"] else None})
            continue
        try:
            rule = rrulestr(e["rrule"], dtstart=e["start"])
            starts = rule.between(window_start, window_end, inc=True)[:MAX_OCCURRENCES]
        except (ValueError, TypeError) as ex:
            logger.info("rrule %s: %s", e["uid"], ex)
            starts = [e["start"]]
        skip = {x.astimezone(datetime.timezone.utc) for x in e["exdate"]}
        for s in starts:
            if s.astimezone(datetime.timezone.utc) in skip or (e["uid"], s) in overrides:
                continue
            out.append({**e, "start": s, "end": s + length if e["end"] else None})
    return out


def fetch(feed) -> int:
    """Оновити підписаний календар. Збій — у last_error, старі події лишаються."""
    from .models import ExternalEvent
    url = feed.url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Minerva-Calendar/1.0"}, stream=True)
        r.raise_for_status()
        raw = r.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(raw) > MAX_BYTES:
            raise ValueError("календар завеликий (понад 10 МБ)")
        text = raw.decode(r.encoding or "utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text:
            raise ValueError("за адресою не календар iCal — перевірте, що це секретна адреса .ics")
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
