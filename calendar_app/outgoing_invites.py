"""Запрошення на зустріч з календаря Minerva: ми — організатор, лист іде зі скриньки людини.

Що робить:
- подія календаря + учасники (email) + посилання на зустріч (Teams / Zoom / Meet — людина вставляє своє, Minerva
  посилань не створює) → лист з iCalendar METHOD:REQUEST (Outlook / Google / Teams показують «Прийняти / Відхилити»);
- UID один на подію назавжди; SEQUENCE +1, коли змінились назва, час, місце, посилання чи повідомлення —
  «оновлене запрошення» всім; лише нові учасники — запрошення лише їм (та сама версія); прибрані — «скасовано» лише їм;
- «✖ Скасувати зустріч» і видалення події (сигнал pre_delete) — METHOD:CANCEL усім, хто отримав запрошення;
- відповіді (METHOD:REPLY / COUNTER) з нових листів — за UID нашого запрошення → ✅ / ❔ / ✕ біля учасника.

Пошта — лише своя скринька (EmailAccount.user = людина), подія — лише своя; зустріч, яку організовує хтось інший
(подія з MeetingInvite), — ні. Листи — лише після підтвердження людини (preview → confirm → send з `expect`).
"""
from __future__ import annotations

import datetime
import logging
import re
import uuid
from email.utils import getaddresses
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .invites import _fold, _lines, _mailto, _param, _when, calendars_of

logger = logging.getLogger(__name__)

MAX_ATTENDEES = 50
DEFAULT_MINUTES = 60          # подія без кінця — година
REPLY_SCAN_MAX = 300          # «↻ Перевірити відповіді»: листів за раз
CONTENT_KEYS = ("title", "start", "end", "all_day", "location", "join_url", "note")
TIME_KEYS = ("start", "end", "all_day")

PARTSTAT_OF = {"ACCEPTED": "accepted", "TENTATIVE": "tentative", "DECLINED": "declined",
               "DELEGATED": "delegated", "NEEDS-ACTION": "needs-action"}
ICON = {"needs-action": "⏳", "accepted": "✅", "tentative": "❔", "declined": "✕", "delegated": "↪"}

# Europe/Berlin і інші пояси CET / CEST (правила ЄС з 1996)
_CET = ("BEGIN:DAYLIGHT\nTZOFFSETFROM:+0100\nTZOFFSETTO:+0200\nTZNAME:CEST\nDTSTART:19700329T020000\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\nEND:DAYLIGHT\nBEGIN:STANDARD\nTZOFFSETFROM:+0200\n"
        "TZOFFSETTO:+0100\nTZNAME:CET\nDTSTART:19701025T030000\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\n"
        "END:STANDARD")
VTIMEZONES = {z: _CET for z in ("Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Amsterdam",
                                 "Europe/Brussels", "Europe/Paris", "Europe/Rome", "Europe/Madrid",
                                 "Europe/Warsaw", "Europe/Prague", "Europe/Copenhagen", "Europe/Stockholm",
                                 "Europe/Oslo", "Europe/Luxembourg", "Europe/Budapest")}

WEEKDAYS = {"de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "uk": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]}
TEXT = {
    "de": {"new": "Einladung", "update": "Aktualisierte Einladung", "cancel": "Abgesagt",
           "lead_new": "{name} lädt Sie zu einer Besprechung ein.",
           "lead_update": "{name} hat die Besprechung geändert — bitte prüfen Sie die neuen Angaben.",
           "lead_cancel": "{name} hat die Besprechung abgesagt.",
           "when": "Wann", "where": "Wo", "join": "Online teilnehmen", "allday": "ganztägig",
           "rsvp": "Bitte antworten Sie über „Annehmen“ / „Ablehnen“ in Ihrem Kalender."},
    "en": {"new": "Invitation", "update": "Updated invitation", "cancel": "Cancelled",
           "lead_new": "{name} invites you to a meeting.",
           "lead_update": "{name} has changed the meeting — please check the new details.",
           "lead_cancel": "{name} has cancelled the meeting.",
           "when": "When", "where": "Where", "join": "Join online", "allday": "all day",
           "rsvp": "Please reply with “Accept” / “Decline” in your calendar."},
    "uk": {"new": "Запрошення", "update": "Оновлене запрошення", "cancel": "Скасовано",
           "lead_new": "{name} запрошує вас на зустріч.",
           "lead_update": "{name} змінює зустріч — перевірте нові дані.",
           "lead_cancel": "{name} скасовує зустріч.",
           "when": "Коли", "where": "Де", "join": "Приєднатися онлайн", "allday": "увесь день",
           "rsvp": "Відповідайте кнопками «Прийняти» / «Відхилити» у своєму календарі."},
}


class InviteError(Exception):
    """Нічого не надіслано й не збережено; текст — для людини."""


# ── дозволи ─────────────────────────────────────────────────────────────────
def accounts(user) -> list:
    """Власні активні скриньки людини — з них можна запрошувати (основна першою)."""
    from email_assistant.models import EmailAccount
    if not getattr(user, "pk", None):
        return []
    return list(EmailAccount.objects.filter(user=user, is_active=True).order_by("-is_primary", "pk"))


def foreign_organizer(event) -> str:
    """Подія з чужого запрошення (MeetingInvite) — організатор інший: запрошувати від свого імені не можна."""
    if not getattr(event, "pk", None):
        return ""
    inv = event.invites.exclude(organizer="").first() or event.invites.first()
    return (inv.organizer or "організатор із запрошення") if inv else ""


def for_event(event):
    from .models import OutgoingInvite
    if not getattr(event, "pk", None):
        return None
    return OutgoingInvite.objects.filter(event=event).first()


def owns_uid(user, uid: str) -> bool:
    """UID — нашого запрошення: копія з «Надісланих» не стає «запрошенням з пошти» (invites.detect)."""
    from .models import OutgoingInvite
    return bool(uid) and OutgoingInvite.objects.filter(user=user, uid=uid.strip()).exists()


# ── дані з вікна ────────────────────────────────────────────────────────────
def parse_people(value) -> list[tuple[str, str]]:
    """«Anna <a@x.de>, b@y.com; c@z.org» або список → [(email, ім'я)]; помилкова адреса → InviteError."""
    if isinstance(value, (list, tuple)):
        items = []
        for v in value:
            if isinstance(v, dict):
                items.append((str(v.get("name") or ""), str(v.get("email") or "")))
            else:
                items += getaddresses([str(v)])
    else:
        items = getaddresses([re.sub(r"[;\n\r]+", ",", str(value or ""))])
    out, seen = [], set()
    for name, addr in items:
        addr = (addr or "").strip().strip("<>").lower()
        if not addr:
            continue
        try:
            validate_email(addr)
        except ValidationError:
            raise InviteError(f"незрозуміла адреса: {addr[:80]}")
        if addr in seen:
            continue
        seen.add(addr)
        out.append((addr, (name or "").strip().strip('"')[:200]))
    if len(out) > MAX_ATTENDEES:
        raise InviteError(f"забагато учасників — не більше {MAX_ATTENDEES}")
    return out


def _parse_dt(value: str, all_day: bool):
    value = (value or "").strip()
    if not value:
        return None
    tz = ZoneInfo(settings.TIME_ZONE)
    try:
        if all_day or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            d = datetime.date.fromisoformat(value[:10])
            return timezone.make_aware(datetime.datetime.combine(d, datetime.time.min), tz)
        d = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise InviteError(f"незрозуміла дата: {value[:30]}")
    return d if timezone.is_aware(d) else timezone.make_aware(d, tz)


def clean_url(value: str) -> str:
    value = (value or "").strip()
    if value and not re.match(r"https?://[^\s<>\"']+$", value, re.I):
        raise InviteError("посилання на зустріч має починатися з https:// (Teams / Zoom / Google Meet)")
    if len(value) > 1000:
        raise InviteError("задовге посилання на зустріч")
    return value


def apply(user, event, data: dict):
    """Дані вікна → подія (назва, час) + запрошення (скринька, місце, посилання, повідомлення, учасники).
    event None — нова зустріч. Нічого не надсилає. → OutgoingInvite."""
    from .models import CalendarEvent, CalendarSettings, InviteAttendee, OutgoingInvite
    if event is not None and event.user_id != user.pk:
        raise InviteError("це не ваша подія")
    if event is not None and foreign_organizer(event):
        raise InviteError(f"організатор цієї зустрічі — {foreign_organizer(event)}; запрошувати учасників може "
                          f"лише він")
    title = (data.get("title") or (event.title if event else "") or "").strip()[:300]
    if not title:
        raise InviteError("вкажіть назву зустрічі")
    all_day = bool(data.get("all_day")) if "all_day" in data else bool(event and event.all_day)
    start = _parse_dt(data.get("start"), all_day) if data.get("start") else (event.start_at if event else None)
    if start is None:
        raise InviteError("вкажіть початок зустрічі")
    if "end" in data:
        end = _parse_dt(data.get("end"), all_day)
    else:
        end = event.end_at if event else None
    if end is not None and end < start:
        raise InviteError("кінець раніше за початок")
    if end is None and not all_day:
        end = start + datetime.timedelta(minutes=DEFAULT_MINUTES)
    mine = accounts(user)
    if not mine:
        raise InviteError("у вас немає власної скриньки в Minerva — запрошення йде з вашої пошти")
    acc_pk = str(data.get("account") or "")
    account = next((a for a in mine if str(a.pk) == acc_pk), None)
    inv = for_event(event) if event is not None else None
    if account is None:
        if acc_pk:
            raise InviteError("це не ваша скринька")
        account = (inv.account if inv and inv.account_id and inv.account in mine else None) or mine[0]
    people = parse_people(data.get("attendees", [])) if "attendees" in data else None
    location = (data.get("location") if "location" in data else (inv.location if inv else "")) or ""
    join_url = clean_url(data.get("join_url") if "join_url" in data else (inv.join_url if inv else ""))
    note = (data.get("note") if "note" in data else (inv.note if inv else "")) or ""
    lang = data.get("lang") or (inv.lang if inv else "de_en")
    if lang not in dict(OutgoingInvite.LANGS):
        lang = "de_en"

    if event is None:
        event = CalendarEvent.objects.create(
            user=user, title=title, start_at=start, end_at=end, all_day=all_day,
            event_type=CalendarEvent.TYPE_MEETING,
            remind_minutes_before=CalendarSettings.for_user(user).default_remind_minutes)
    else:
        moved = event.start_at != start
        event.title, event.start_at, event.end_at, event.all_day = title, start, end, all_day
        if moved:
            event.remind_sent = event.push_sent = False
        event.save()
    if inv is None:
        domain = (account.email_address or "minerva").rpartition("@")[2] or "minerva"
        inv = OutgoingInvite.objects.create(user=user, event=event, uid=f"minerva-{uuid.uuid4().hex}@{domain}")
    inv.account, inv.location, inv.join_url = account, location.strip()[:500], join_url
    inv.note, inv.lang = note.strip()[:5000], lang
    inv.save(update_fields=["account", "location", "join_url", "note", "lang"])

    if people is not None:
        organizer = (account.email_address or "").lower()
        wanted = {e: n for e, n in people if e != organizer}
        rows = {a.email: a for a in inv.attendees.all()}
        for addr, row in rows.items():
            if addr in wanted:
                changes = []
                if row.removed:
                    row.removed = False
                    changes.append("removed")
                if wanted[addr] and wanted[addr] != row.name:
                    row.name = wanted[addr]
                    changes.append("name")
                if changes:
                    row.save(update_fields=changes)
            elif row.invited_at is None or inv.status != OutgoingInvite.SENT:
                row.delete()                      # ще не отримав запрошення — просто прибрати
            elif not row.removed:
                row.removed = True                # отримав — «скасовано» піде з наступним надсиланням
                row.save(update_fields=["removed"])
        for addr, name in wanted.items():
            if addr not in rows:
                InviteAttendee.objects.create(invite=inv, email=addr, name=name)
    return inv


# ── що піде й кому ─────────────────────────────────────────────────────────
def _iso(d) -> str:
    return d.astimezone(datetime.timezone.utc).isoformat() if d else ""


def snapshot(inv) -> dict:
    ev = inv.event
    return {"title": ev.title, "start": _iso(ev.start_at), "end": _iso(ev.end_at), "all_day": ev.all_day,
            "location": inv.location, "join_url": inv.join_url, "note": inv.note}


def changed_keys(inv) -> list:
    """Що змінилось після надсилання (назва, час, місце…) — учасники ще бачать старе."""
    from .models import OutgoingInvite
    if inv.status != OutgoingInvite.SENT or not inv.sent or inv.event_id is None:
        return []
    now = snapshot(inv)
    return [k for k in CONTENT_KEYS if str(now.get(k, "")) != str(inv.sent.get(k, ""))]


def plan(inv, resend: bool = False) -> dict:
    """Кому що піде, якщо натиснути «Надіслати»: kind new / update / added / resend / none;
    request — отримають запрошення (REQUEST), cancel — прибрані (CANCEL лише їм)."""
    from .models import OutgoingInvite
    rows = list(inv.attendees.all())
    current = [a for a in rows if not a.removed]
    removed = [a for a in rows if a.removed and a.invited_at]
    changed = changed_keys(inv)
    if inv.status != OutgoingInvite.SENT or not inv.sent:
        kind, request = "new", current
    elif changed:
        kind, request = "update", current
    elif [a for a in current if a.invited_at is None]:
        kind, request = "added", [a for a in current if a.invited_at is None]
    elif resend:
        kind, request = "resend", current
    else:
        kind, request = "none", []
    cancel = removed if inv.status == OutgoingInvite.SENT else []
    return {"kind": kind, "request": [a.email for a in request], "cancel": [a.email for a in cancel],
            "changed": changed, "time_changed": any(k in changed for k in TIME_KEYS),
            "sequence": inv.sequence + (1 if kind == "update" or (kind == "new" and inv.first_sent_at) else 0)}


def preview(user, event, data: dict) -> dict:
    """Те саме, що send, але нічого не зберігається й не надсилається — для confirm з адресами."""
    with transaction.atomic():
        inv = apply(user, event, data)
        out = plan(inv, resend=bool(data.get("resend")))
        out["account"] = inv.account.email_address if inv.account_id else ""
        out["past"] = _is_past(inv)
        transaction.set_rollback(True)
    return out


# ── iCalendar ───────────────────────────────────────────────────────────────
def _esc(value: str) -> str:
    return (str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def _utc(d) -> str:
    return d.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _times(data: dict) -> list[str]:
    """DTSTART / DTEND: весь день — VALUE=DATE (кінець не включно); з часом — TZID поясу Minerva + VTIMEZONE,
    пояс без опису — UTC."""
    start = datetime.datetime.fromisoformat(data["start"])
    end = datetime.datetime.fromisoformat(data["end"]) if data.get("end") else None
    tzname = settings.TIME_ZONE
    if data.get("all_day"):
        d0 = timezone.localtime(start).date()
        d1 = (timezone.localtime(end).date() if end else d0) + datetime.timedelta(days=1)
        return [f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{max(d1, d0 + datetime.timedelta(days=1)):%Y%m%d}"]
    end = end or start + datetime.timedelta(minutes=DEFAULT_MINUTES)
    if tzname in VTIMEZONES:
        tz = ZoneInfo(tzname)
        return [f"DTSTART;TZID={tzname}:{start.astimezone(tz):%Y%m%dT%H%M%S}",
                f"DTEND;TZID={tzname}:{end.astimezone(tz):%Y%m%dT%H%M%S}"]
    return [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}"]


def _description(data: dict) -> str:
    parts = [data.get("note") or ""]
    if data.get("join_url"):
        parts.append(data["join_url"])
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def build_ics(inv, method: str, data: dict, people: list, sequence: int, partstat: dict | None = None) -> str:
    """iCalendar METHOD:REQUEST / CANCEL: UID, SEQUENCE, ORGANIZER (скринька), ATTENDEE кожного (PARTSTAT, RSVP),
    час з поясом + VTIMEZONE; рядки ≤ 75 байт."""
    account = inv.account
    org_name = account.display_name or inv.user.get_full_name() or ""
    lines = ["BEGIN:VCALENDAR", "PRODID:-//Minerva//Meeting invite//EN", "VERSION:2.0", "CALSCALE:GREGORIAN",
             f"METHOD:{method}"]
    if not data.get("all_day") and settings.TIME_ZONE in VTIMEZONES:
        lines += ["BEGIN:VTIMEZONE", f"TZID:{settings.TIME_ZONE}"] + VTIMEZONES[settings.TIME_ZONE].split("\n") \
                 + ["END:VTIMEZONE"]
    lines += ["BEGIN:VEVENT", f"UID:{inv.uid}", f"SEQUENCE:{sequence}", f"DTSTAMP:{_utc(timezone.now())}"]
    lines += _times(data)
    lines.append(f"SUMMARY:{_esc(data.get('title') or '')}")
    location = data.get("location") or data.get("join_url") or ""
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    desc = _description(data)
    if desc:
        lines.append(f"DESCRIPTION:{_esc(desc)}")
    if data.get("join_url"):
        lines.append(f"URL:{data['join_url']}")
        if re.search(r"teams\.(microsoft|live)\.com/", data["join_url"], re.I):
            lines.append(f"X-MICROSOFT-SKYPETEAMSMEETINGURL:{data['join_url']}")
    lines.append("ORGANIZER" + (f";CN={_param(org_name)}" if org_name else "")
                 + f":mailto:{(account.email_address or '').lower()}")
    for p in people:
        state = (partstat or {}).get(p.email, "needs-action")
        lines.append(f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT={state.upper()}"
                     + (";RSVP=TRUE" if method == "REQUEST" else "")
                     + (f";CN={_param(p.name)}" if p.name else "") + f":mailto:{p.email}")
    lines += ["STATUS:CANCELLED" if method == "CANCEL" else "STATUS:CONFIRMED", "TRANSP:OPAQUE",
              "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


# ── лист ────────────────────────────────────────────────────────────────────
def _langs(lang: str) -> list:
    return ["de", "en"] if lang == "de_en" else [lang if lang in TEXT else "en"]


def _when_text(data: dict, lang: str) -> str:
    start = timezone.localtime(datetime.datetime.fromisoformat(data["start"]))
    end = timezone.localtime(datetime.datetime.fromisoformat(data["end"])) if data.get("end") else None
    day = f"{WEEKDAYS[lang][start.weekday()]} {start:%d.%m.%Y}"
    if data.get("all_day"):
        if end and end.date() > start.date():
            return f"{day} – {WEEKDAYS[lang][end.weekday()]} {end:%d.%m.%Y} ({TEXT[lang]['allday']})"
        return f"{day} ({TEXT[lang]['allday']})"
    if end and end.date() != start.date():
        return f"{day}, {start:%H:%M} – {WEEKDAYS[lang][end.weekday()]} {end:%d.%m.%Y}, {end:%H:%M} ({settings.TIME_ZONE})"
    return f"{day}, {start:%H:%M}" + (f"–{end:%H:%M}" if end else "") + f" ({settings.TIME_ZONE})"


def compose(inv, kind: str, data: dict) -> tuple[str, str]:
    """(тема, текст) листа: kind new / update / cancel — мовою запрошення (de_en — обома)."""
    langs = _langs(inv.lang)
    key = "cancel" if kind == "cancel" else ("update" if kind == "update" else "new")
    start = timezone.localtime(datetime.datetime.fromisoformat(data["start"]))
    when = f"{start:%d.%m.%Y}" if data.get("all_day") else f"{start:%d.%m.%Y %H:%M}"
    subject = f"{' / '.join(TEXT[lg][key] for lg in langs)}: {data.get('title', '')} — {when}"[:300]
    name = (inv.account.display_name if inv.account_id else "") or inv.user.get_full_name() or \
        (inv.account.email_address if inv.account_id else "")
    blocks = []
    for lg in langs:
        t = TEXT[lg]
        rows = [t[f"lead_{key}"].format(name=name), "", data.get("title", ""), f"{t['when']}: {_when_text(data, lg)}"]
        if data.get("location"):
            rows.append(f"{t['where']}: {data['location']}")
        if data.get("join_url") and key != "cancel":
            rows.append(f"{t['join']}: {data['join_url']}")
        if key != "cancel":
            rows += ["", t["rsvp"]]
        blocks.append("\n".join(rows))
    body = "\n\n— — —\n\n".join(blocks)
    if data.get("note") and key != "cancel":
        body = data["note"].strip() + "\n\n" + body
    return subject, body


def _send_mail(inv, method: str, kind: str, data: dict, recipients: list, people: list, sequence: int,
               partstat: dict | None = None):
    from email_assistant.smtp_client import SMTPClient
    ics = build_ics(inv, method, data, people, sequence, partstat)
    subject, body = compose(inv, kind, data)
    result = SMTPClient(inv.account).send([a.email for a in recipients], subject, body, calendar=(method, ics))
    if not result.get("ok"):
        raise InviteError("лист не надіслано" + (f": {result['error']}" if result.get("error") else ""))
    return result


def _is_past(inv) -> bool:
    ev = inv.event
    if ev is None:
        return False
    end = ev.end_at or ev.start_at + datetime.timedelta(minutes=DEFAULT_MINUTES)
    if ev.all_day:
        end = timezone.make_aware(datetime.datetime.combine(timezone.localtime(end).date(), datetime.time.max),
                                  ZoneInfo(settings.TIME_ZONE))
    return end < timezone.now()


def _usable_account(inv, user):
    acc = inv.account
    if acc is None or acc.user_id != user.pk or not acc.is_active:
        raise InviteError("запрошення надсилається лише з вашої активної скриньки — виберіть її")
    return acc


# ── дії ─────────────────────────────────────────────────────────────────────
def send(user, event, data: dict) -> dict:
    """Зберегти й надіслати за планом (kind new / update / added / resend + «скасовано» прибраним).
    `expect` — адреси, які людина підтвердила (preview): план інший → InviteError, нічого не надіслано."""
    from .models import CalendarEvent, InviteAttendee, OutgoingInvite
    resend = bool(data.get("resend"))
    with transaction.atomic():
        inv = apply(user, event, data)
        p = plan(inv, resend=resend)
    if "expect" in data:
        expect = sorted({str(x).lower() for x in (data.get("expect") or [])})
        if expect != sorted(set(p["request"]) | set(p["cancel"])):
            raise InviteError("список адресатів змінився — перевірте й надішліть ще раз")
    _usable_account(inv, user)
    if p["kind"] == "none" and not p["cancel"]:
        raise InviteError("нічого не змінилось — усі учасники вже мають це запрошення")
    if not p["request"] and p["kind"] != "none":
        raise InviteError("додайте хоча б одного учасника (або «✖ Скасувати зустріч»)")
    if _is_past(inv) and p["request"]:
        raise InviteError("зустріч уже минула — змініть час або не надсилайте")
    data_now = snapshot(inv)
    rows = list(inv.attendees.all())
    current = [a for a in rows if not a.removed]
    removed = [a for a in rows if a.removed and a.invited_at]
    now = timezone.now()
    was_cancelled = inv.status == OutgoingInvite.CANCELLED
    fresh = p["time_changed"] or p["kind"] == "new"            # новий час / заново — відповіді заново
    known = {a.email: (InviteAttendee.NEEDS if fresh else a.partstat) for a in current}
    sent_to, cancelled = [], []
    try:
        if p["request"]:
            to = [a for a in current if a.email in p["request"]]
            _send_mail(inv, "REQUEST", "update" if p["kind"] == "update" else "new", data_now, to, current,
                       p["sequence"], known)
            sent_to = [a.email for a in to]
            if fresh:
                inv.attendees.filter(removed=False).exclude(partstat=InviteAttendee.NEEDS).update(
                    partstat=InviteAttendee.NEEDS, comment="час змінено — чекаємо нової відповіді")
                inv.rsvp_sequence = p["sequence"]
            inv.attendees.filter(email__in=sent_to).update(invited_at=now)
            inv.sequence = p["sequence"]
            inv.sent = dict(data_now, attendees=[a.email for a in current])
            inv.status = OutgoingInvite.SENT
            inv.first_sent_at = inv.first_sent_at or now
            inv.last_sent_at, inv.last_error = now, ""
            inv.save(update_fields=["sequence", "rsvp_sequence", "sent", "status", "first_sent_at", "last_sent_at",
                                    "last_error"])
            if inv.event_id:
                ev = inv.event
                updates = {}
                if ev.event_type != CalendarEvent.TYPE_MEETING:
                    updates.update(event_type=CalendarEvent.TYPE_MEETING, custom_category=None)
                if was_cancelled:                                  # знову в силі
                    updates.update(is_done=False, title=re.sub(r"^✖ Скасовано: ", "", ev.title)[:300])
                if updates:
                    CalendarEvent.objects.filter(pk=ev.pk).update(**updates)
        if removed:
            # «скасовано» лише прибраним: нова версія, щоб їхній календар прийняв її як новішу
            seq = inv.sequence if sent_to and p["kind"] == "update" else inv.sequence + 1
            _send_mail(inv, "CANCEL", "cancel", inv.sent or data_now, removed, removed, seq)
            cancelled = [a.email for a in removed]
            inv.attendees.filter(pk__in=[a.pk for a in removed]).delete()
            OutgoingInvite.objects.filter(pk=inv.pk).update(sequence=seq, last_sent_at=timezone.now(), last_error="")
            inv.sequence = seq
    except InviteError as e:
        OutgoingInvite.objects.filter(pk=inv.pk).update(last_error=str(e)[:255])
        if sent_to:   # запрошення пішло, «скасовано» прибраним — ні
            raise InviteError(f"запрошення надіслано ({', '.join(sent_to)}), але «скасовано» прибраним — ні: {e}")
        raise
    return {"ok": True, "kind": p["kind"], "to": sent_to, "cancelled": cancelled, "invite": inv.pk,
            "event": inv.event_id}


def cancel(user, inv, *, deleted: bool = False) -> dict:
    """«✖ Скасувати зустріч» / подію видалено: METHOD:CANCEL усім, хто отримав запрошення. Подія (якщо є) —
    «✖ Скасовано: …» і виконана."""
    from .models import OutgoingInvite
    if inv.user_id != user.pk:
        raise InviteError("це не ваше запрошення")
    if inv.status != OutgoingInvite.SENT:
        raise InviteError("запрошення ще не надсилали — скасовувати нікому")
    _usable_account(inv, user)
    people = [a for a in inv.attendees.all() if a.invited_at]
    if not people:
        raise InviteError("ніхто ще не отримав запрошення")
    seq = inv.sequence + 1
    data = inv.sent or snapshot(inv)
    try:
        _send_mail(inv, "CANCEL", "cancel", data, people, people, seq)
    except InviteError as e:
        OutgoingInvite.objects.filter(pk=inv.pk).update(last_error=str(e)[:255])
        raise
    inv.status, inv.sequence, inv.cancel_pending, inv.last_error = OutgoingInvite.CANCELLED, seq, False, ""
    inv.last_sent_at = timezone.now()
    inv.save(update_fields=["status", "sequence", "cancel_pending", "last_error", "last_sent_at"])
    ev = inv.event if inv.event_id and not deleted else None
    if ev is not None:
        if not ev.title.startswith("✖ "):
            ev.title = ("✖ Скасовано: " + ev.title)[:300]
        ev.is_done = True
        ev.save(update_fields=["title", "is_done"])
    return {"ok": True, "to": [a.email for a in people]}


def cancel_targets(event, inv=None) -> list:
    """Хто отримає «скасовано», якщо подію видалити (майбутня зустріч, запрошення надіслано)."""
    from .models import OutgoingInvite
    inv = inv if inv is not None else for_event(event)
    if inv is None or inv.status != OutgoingInvite.SENT or _is_past(inv):
        return []
    return [a.email for a in inv.attendees.all() if a.invited_at]


def on_event_deleting(event):
    """pre_delete події: майбутня зустріч з надісланими запрошеннями → після commit — «скасовано» учасникам.
    Не вийшло (пошта) — cancel_pending, банер у календарі з повтором."""
    from .models import OutgoingInvite
    targets = cancel_targets(event)
    if not targets:
        return
    inv = for_event(event)
    OutgoingInvite.objects.filter(pk=inv.pk).update(cancel_pending=True)
    pk, user = inv.pk, event.user

    def run():
        fresh = OutgoingInvite.objects.filter(pk=pk, cancel_pending=True).first()
        if fresh is None:
            return
        try:
            cancel(user, fresh, deleted=True)
        except InviteError as e:
            logger.info("cancel of deleted meeting %s: %s", pk, e)
    transaction.on_commit(run)


def pending_cancels(user) -> list:
    from .models import OutgoingInvite
    if not getattr(user, "pk", None):
        return []
    return list(OutgoingInvite.objects.filter(user=user, cancel_pending=True).prefetch_related("attendees")[:20])


def drop_cancel(user, inv):
    """«Не надсилати скасування»: учасники не дізнаються — людина вирішила сама."""
    from .models import OutgoingInvite
    if inv.user_id != user.pk:
        raise InviteError("це не ваше запрошення")
    OutgoingInvite.objects.filter(pk=inv.pk).update(cancel_pending=False, last_error="скасування не надсилали")


# ── відповіді учасників (iCalendar REPLY / COUNTER) ─────────────────────────
def _raw_events(text: str):
    """(METHOD, [{ім'я: [(параметри, значення)]}]) — кожен VEVENT з усіма рядками."""
    method, events, cur, depth = "", [], None, 0
    for name, params, value in _lines(text or ""):
        if name == "METHOD" and cur is None:
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT" and cur is None:
            cur, depth = {}, 0
        elif cur is not None and name == "BEGIN":
            depth += 1
        elif cur is not None and name == "END" and value.strip().upper() == "VEVENT":
            events.append(cur)
            cur = None
        elif cur is not None and name == "END":
            depth -= 1
        elif cur is not None and depth == 0:
            cur.setdefault(name, []).append((params, value))
    return method, events


def _first(ev, key):
    return (ev.get(key) or [({}, "")])[0]


def _counter_text(ev) -> str:
    start = _when(_first(ev, "DTSTART")[1], _first(ev, "DTSTART")[0]) if ev.get("DTSTART") else None
    end = _when(_first(ev, "DTEND")[1], _first(ev, "DTEND")[0]) if ev.get("DTEND") else None
    if not start:
        return "пропонує інший час"
    s = timezone.localtime(start[0])
    text = f"пропонує інший час: {s:%d.%m.%Y}" if start[1] else f"пропонує інший час: {s:%d.%m.%Y %H:%M}"
    if end and not start[1]:
        text += f"–{timezone.localtime(end[0]):%H:%M}"
    return text


def detect_replies(message, texts=None) -> list:
    """Новий лист → відповіді на НАШІ запрошення (UID) → ✅ / ❔ / ✕ учасника. Лише скриньки організатора-людини;
    старіша відповідь не перебиває новішу; відповідь на версію до зміни часу — не рахується."""
    from .models import InviteAttendee, OutgoingInvite
    user = getattr(getattr(message, "account", None), "user", None)
    if user is None:
        return []
    texts = calendars_of(message) if texts is None else texts
    when = message.sent_at or timezone.now()
    touched = []
    for text in texts:
        if "VCALENDAR" not in (text or ""):
            continue
        method, events = _raw_events(text)
        if method not in ("REPLY", "COUNTER"):
            continue
        for ev in events:
            uid = _first(ev, "UID")[1].strip()
            inv = OutgoingInvite.objects.filter(user=user, uid=uid).first() if uid else None
            if inv is None:
                continue
            seq = re.sub(r"\D", "", _first(ev, "SEQUENCE")[1])
            if seq and int(seq) < inv.rsvp_sequence:
                continue                                    # відповідь на старий час
            comment = _first(ev, "COMMENT")[1].strip()
            people = ev.get("ATTENDEE") or []
            for params, value in people:
                addr = _mailto(value)
                if "@" not in addr:
                    continue
                row = inv.attendees.filter(email=addr).first()
                if row is None and len(people) == 1:
                    sender = (message.from_email or "").strip().lower()
                    row = inv.attendees.filter(email=sender).first() if sender else None
                if row is None:
                    row = InviteAttendee.objects.create(invite=inv, email=addr[:254],
                                                        name=(params.get("CN") or "")[:200], invited_at=when,
                                                        comment="відповів, хоча не був у списку")
                if row.responded_at and when < row.responded_at:
                    continue                                # новіша відповідь уже є
                state = PARTSTAT_OF.get((params.get("PARTSTAT") or "").upper())
                if method == "COUNTER":
                    row.comment = _counter_text(ev)[:500]
                    state = state or InviteAttendee.TENTATIVE
                else:
                    row.comment = comment[:500] if comment else ("" if row.comment.startswith("час змінено")
                                                                  else row.comment)
                if state:
                    row.partstat = state
                row.responded_at, row.reply_message = when, message
                row.save(update_fields=["partstat", "comment", "responded_at", "reply_message"])
                touched.append(row)
    return touched


def check_replies(inv) -> int:
    """«↻ Перевірити відповіді»: листи скриньок людини після першого надсилання з календарним вкладенням → REPLY.
    Лише збережене (без IMAP). → скільки відповідей враховано."""
    from django.db.models import Q

    from email_assistant.models import EmailMessage
    if not inv.first_sent_at:
        return 0
    qs = (EmailMessage.objects.filter(account__user=inv.user, sent_at__gte=inv.first_sent_at - datetime.timedelta(hours=1),
                                      is_spam=False)
          .exclude(folder=EmailMessage.FOLDER_SENT)
          .filter(Q(attachments__icontains="calendar") | Q(attachments__icontains=".ics")
                  | Q(body_text__contains="BEGIN:VCALENDAR"))
          .select_related("account").order_by("sent_at"))
    n = 0
    for msg in qs[:REPLY_SCAN_MAX]:
        texts = calendars_of(msg)
        if texts and inv.uid in "".join(texts).replace("\r\n ", "").replace("\n ", ""):
            n += len(detect_replies(msg, texts))
    return n


# ── для вікна й календаря ──────────────────────────────────────────────────
def counts(inv) -> dict:
    out = {"accepted": 0, "tentative": 0, "declined": 0, "delegated": 0, "needs-action": 0}
    for a in inv.attendees.all():
        if not a.removed:
            out[a.partstat] = out.get(a.partstat, 0) + 1
    return out


def badge(inv) -> str:
    """«👥 3 · ✅ 2 ❔ 1» — у рядку події календаря."""
    from .models import OutgoingInvite
    if inv is None:
        return ""
    c = counts(inv)
    n = sum(c.values())
    if not n:
        return ""
    if inv.status == OutgoingInvite.DRAFT:
        return f"👥 {n} · 📝 ще не надіслано"
    if inv.status == OutgoingInvite.CANCELLED:
        return f"👥 {n} · ✖ скасовано"
    parts = [f"{ICON[k]} {v}" for k, v in c.items() if v]
    return f"👥 {n} · " + " ".join(parts)


def state(user, event) -> dict:
    """Дані вікна «👥 Учасники й запрошення» (GET) і відповідь після дій."""
    from .models import OutgoingInvite
    mine = accounts(user)
    out = {"accounts": [{"pk": a.pk, "email": a.email_address, "name": a.display_name} for a in mine],
           "langs": [{"key": k, "label": v} for k, v in OutgoingInvite.LANGS]}
    if event is None:
        out.update({"can": bool(mine), "why": "" if mine else "немає власної скриньки в Minerva",
                    "status": "", "attendees": [], "lang": "de_en"})
        return out
    org = foreign_organizer(event)
    inv = for_event(event)
    local_start = timezone.localtime(event.start_at)
    local_end = timezone.localtime(event.end_at) if event.end_at else None
    out.update({
        "event": event.pk, "title": event.title, "all_day": event.all_day,
        "message": event.email_message_id or "",             # підказки адрес: учасники листа (лише своя скринька)
        "start": local_start.strftime("%Y-%m-%d" if event.all_day else "%Y-%m-%dT%H:%M"),
        "end": (local_end.strftime("%Y-%m-%d" if event.all_day else "%Y-%m-%dT%H:%M") if local_end else ""),
        "can": bool(mine) and not org,
        "why": (f"організатор цієї зустрічі — {org}; запрошувати може лише він" if org
                else ("" if mine else "немає власної скриньки в Minerva")),
    })
    if inv is None:
        out.update({"status": "", "attendees": [], "lang": "de_en",
                    "account": mine[0].pk if mine else None})
        return out
    out.update({
        "status": inv.status, "status_label": inv.get_status_display(), "account": inv.account_id,
        "location": inv.location, "join_url": inv.join_url, "note": inv.note, "lang": inv.lang,
        "first_sent": timezone.localtime(inv.first_sent_at).strftime("%d.%m.%Y %H:%M") if inv.first_sent_at else "",
        "last_sent": timezone.localtime(inv.last_sent_at).strftime("%d.%m.%Y %H:%M") if inv.last_sent_at else "",
        "sequence": inv.sequence, "changed": changed_keys(inv), "error": inv.last_error,
        "cancel_pending": inv.cancel_pending, "past": _is_past(inv),
        "attendees": [{"email": a.email, "name": a.name, "partstat": a.partstat, "icon": ICON.get(a.partstat, "⏳"),
                       "label": a.get_partstat_display(), "invited": bool(a.invited_at), "removed": a.removed,
                       "comment": a.comment,
                       "responded": (timezone.localtime(a.responded_at).strftime("%d.%m.%Y %H:%M")
                                     if a.responded_at else ""),
                       "reply": a.reply_message_id or None}
                      for a in inv.attendees.all()],
    })
    return out
