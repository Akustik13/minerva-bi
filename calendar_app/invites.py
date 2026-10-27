"""Запрошення на зустрічі з пошти → календар Minerva.

Звідки: iCalendar (Outlook / Teams / Google / Zoom) — вкладення .ics або блок text/calendar листа; для старих листів,
де блок text/calendar не зберігся (Outlook шле його без імені файлу), пошук у пошті бере лист з IMAP ще раз.
Без iCalendar зустріч не вгадується (жодного AI) — дата з тексту ненадійна.

Правила:
- один рядок MeetingInvite на зустріч (UID); новіша версія (SEQUENCE) — зміна, METHOD:CANCEL — скасування;
- у календар — лише після ✓ (CalendarSettings.invites_auto — без підтвердження); «✕ Не заносити» не повертається;
- відповіді учасників (METHOD:REPLY / COUNTER) — не запрошення.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import threading
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

# Outlook / Exchange пише Windows-назви часових поясів
WINDOWS_TZ = {
    "W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "FLE Standard Time": "Europe/Kyiv", "E. Europe Standard Time": "Europe/Chisinau",
    "GTB Standard Time": "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul", "Israel Standard Time": "Asia/Jerusalem",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
    "India Standard Time": "Asia/Kolkata", "UTC": "UTC", "Coordinated Universal Time": "UTC",
}
JOIN_RE = re.compile(r"https://(?:teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet|[\w.-]*zoom\.us/[jw]/"
                     r"|meet\.google\.com/|[\w.-]*webex\.com/)[^\s<>\"')\]]+", re.I)
INVITE_HINT = re.compile(r"teams\.microsoft\.com/l/meetup-join|zoom\.us/j/|meet\.google\.com/|webex\.com/"
                         r"|^(?:einladung|invitation|updated invitation|aktualisierte einladung|abgesagt|canceled"
                         r"|cancelled|besprechung|meeting|termin)\b", re.I | re.M)
NOT_INVITES = ("REPLY", "COUNTER", "DECLINECOUNTER", "REFRESH")


# ── iCalendar ───────────────────────────────────────────────────────────────
def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";")
            .replace("\\\\", "\\")).strip()


def _lines(text: str):
    """(ім'я, {параметри}, значення) для кожного рядка iCalendar (з розгорнутими продовженнями)."""
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    for line in text.split("\n"):
        if ":" not in line:
            continue
        head, value = _split_head(line)
        parts = head.split(";")
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')
        yield parts[0].upper(), params, value


def _split_head(line: str):
    """«DTSTART;TZID="W. Europe Standard Time":20261103T100000» — двокрапка в лапках параметра не ділить."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[:i], line[i + 1:]
    return line, ""


def _tz(name: str):
    name = (name or "").strip()
    for candidate in (name, WINDOWS_TZ.get(name, "")):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except Exception:  # noqa: BLE001 — невідомий пояс
                continue
    return ZoneInfo(settings.TIME_ZONE)


def _when(value: str, params: dict):
    """(datetime aware, весь день?) з DTSTART / DTEND; None — не розібрано."""
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            d = datetime.datetime.strptime(value[:8], "%Y%m%d")
            return timezone.make_aware(d, ZoneInfo(settings.TIME_ZONE)), True
        utc = value.endswith("Z")
        fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) >= 15 else "%Y%m%dT%H%M"
        d = datetime.datetime.strptime(value.rstrip("Z"), fmt)
        tz = datetime.timezone.utc if utc else _tz(params.get("TZID", ""))
        return d.replace(tzinfo=tz), False
    except ValueError:
        return None


def parse_ics(text: str) -> list[dict]:
    """Події VEVENT: uid, sequence, method, title, start_at, end_at, all_day, location, organizer, join_url,
    description, status."""
    method, events, cur, depth = "", [], None, 0
    for name, params, value in _lines(text or ""):
        if name == "METHOD" and cur is None:
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur, depth = {"params": {}}, 0
        elif cur is not None and name == "BEGIN":
            depth += 1   # VALARM усередині події
        elif cur is not None and name == "END" and value.strip().upper() == "VEVENT":
            events.append(cur)
            cur = None
        elif cur is not None and name == "END":
            depth -= 1
        elif cur is not None and depth == 0:
            cur.setdefault(name, value)
            cur["params"].setdefault(name, params)
    out = []
    for e in events:
        start = _when(e.get("DTSTART", ""), e["params"].get("DTSTART", {}))
        if not e.get("UID") or not start:
            continue
        end = _when(e.get("DTEND", ""), e["params"].get("DTEND", {})) if e.get("DTEND") else None
        if end is None and e.get("DURATION"):
            end = (start[0] + _duration(e["DURATION"]), start[1])
        desc = _unescape(e.get("DESCRIPTION", ""))
        location = _unescape(e.get("LOCATION", ""))
        join = (e.get("X-MICROSOFT-SKYPETEAMSMEETINGURL") or e.get("URL") or "").strip()
        if not JOIN_RE.match(join or ""):
            m = JOIN_RE.search(location) or JOIN_RE.search(desc)
            join = m.group(0) if m else ""
        organizer = e["params"].get("ORGANIZER", {}).get("CN") or re.sub(r"^mailto:", "", e.get("ORGANIZER", ""),
                                                                          flags=re.I)
        out.append({
            "uid": e["UID"].strip()[:500], "sequence": int(re.sub(r"\D", "", e.get("SEQUENCE", "0")) or 0),
            "method": method or "PUBLISH", "status": e.get("STATUS", "").strip().upper(),
            "title": _unescape(e.get("SUMMARY", "")) or "Зустріч", "start_at": start[0], "all_day": start[1],
            "end_at": end[0] if end else None, "location": location, "organizer": organizer.strip(),
            "join_url": join, "description": desc,
        })
    return out


def _duration(value: str) -> datetime.timedelta:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", value.strip())
    if not m:
        return datetime.timedelta(hours=1)
    d, h, mi = (int(x or 0) for x in m.groups())
    return datetime.timedelta(days=d, hours=h, minutes=mi)


# ── iCalendar з листа ───────────────────────────────────────────────────────
def calendars_of(message) -> list[str]:
    """Тексти iCalendar листа: збережені вкладення .ics / text/calendar і тіло листа."""
    out = []
    for att in message.attachments or []:
        name, ctype = (att.get("name") or "").lower(), (att.get("content_type") or "").lower()
        if not (ctype == "text/calendar" or name.endswith(".ics")) or not att.get("file_path"):
            continue
        path = os.path.normpath(os.path.join(settings.MEDIA_ROOT, att["file_path"]))
        if not path.startswith(os.path.normpath(str(settings.MEDIA_ROOT))):
            continue
        try:
            with open(path, "rb") as fh:
                out.append(fh.read(2_000_000).decode("utf-8", errors="replace"))
        except OSError:
            continue
    if "BEGIN:VCALENDAR" in (message.body_text or ""):
        out.append(message.body_text)
    return out


def looks_like_invite(message) -> bool:
    return bool(INVITE_HINT.search(f"{message.subject or ''}\n{(message.body_text or '')[:20000]}"))


def fetch_calendar_from_imap(message) -> list[str]:
    """Блок text/calendar старого листа — з IMAP (Outlook шле його без імені файлу, раніше він не зберігався)."""
    import email
    from email_assistant.imap_client import IMAPClient
    folder = message.imap_folder_name or ""
    if not message.imap_uid or not folder or folder.startswith(("minerva:", "eml:")):
        return []
    try:
        with IMAPClient(message.account) as c:
            if not c.select_folder(folder):
                return []
            status, data = c.conn.uid("fetch", str(message.imap_uid), "(BODY.PEEK[])")
    except Exception as e:  # noqa: BLE001 — пошта недоступна: просто без цього листа
        logger.info("invite imap fetch %s: %s", message.pk, e)
        return []
    raw = next((part[1] for part in data or [] if isinstance(part, tuple) and len(part) > 1), None)
    if status != "OK" or not raw:
        return []
    out = []
    for part in email.message_from_bytes(raw).walk():
        if part.get_content_type() == "text/calendar":
            payload = part.get_payload(decode=True) or b""
            out.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return out


# ── запрошення ─────────────────────────────────────────────────────────────
def _customer(message):
    thread = getattr(message, "thread", None)
    return getattr(thread, "crm_customer", None) if thread else None


def detect(message, texts=None) -> list:
    """Запрошення з листа → MeetingInvite (нові / зміни / скасування); без підтвердження — одразу в календар."""
    from .models import CalendarSettings, MeetingInvite
    user = getattr(getattr(message, "account", None), "user", None)
    if user is None:
        return []
    texts = calendars_of(message) if texts is None else texts
    if not texts:
        return []
    auto = CalendarSettings.for_user(user).invites_auto
    touched = []
    for text in texts:
        for ev in parse_ics(text):
            if ev["method"] in NOT_INVITES:
                continue
            inv = _apply(user, message, ev)
            if inv is not None:
                touched.append(inv)
                if auto and inv.status in MeetingInvite.PENDING:
                    accept(inv, user)
    return touched


def _apply(user, message, ev):
    from .models import MeetingInvite
    cancel = ev["method"] == "CANCEL" or ev["status"] == "CANCELLED"
    inv = MeetingInvite.objects.filter(user=user, uid=ev["uid"]).first()
    fields = {k: ev[k] for k in ("title", "start_at", "end_at", "all_day", "location", "organizer", "join_url",
                                 "description", "method", "sequence")}
    fields["title"], fields["location"] = fields["title"][:300], fields["location"][:500]
    fields["organizer"], fields["join_url"] = fields["organizer"][:300], fields["join_url"][:1000]
    if inv is None:
        if cancel:
            return None   # скасування зустрічі, якої в Minerva не було
        return MeetingInvite.objects.create(user=user, uid=ev["uid"], email_message=message,
                                            crm_customer=_customer(message), status=MeetingInvite.NEW, **fields)
    if ev["sequence"] < inv.sequence or inv.status == MeetingInvite.DISMISSED:
        return None   # старіша версія або людина вирішила «не заносити»
    changed = any(getattr(inv, k) != v for k, v in fields.items() if k not in ("method", "sequence", "description"))
    if not changed and not cancel and ev["sequence"] == inv.sequence:
        return None   # той самий лист / повтор
    for k, v in fields.items():
        setattr(inv, k, v)
    inv.email_message = message
    if cancel:
        inv.status = MeetingInvite.CANCEL if inv.event_id else MeetingInvite.CANCELLED
    elif inv.status in (MeetingInvite.ADDED, MeetingInvite.UPDATE, MeetingInvite.CANCEL, MeetingInvite.CANCELLED):
        inv.status = MeetingInvite.UPDATE if inv.event_id else MeetingInvite.NEW   # перенесено / знову в силі
    inv.save()
    return inv


def _event_text(inv) -> str:
    parts = []
    if inv.location:
        parts.append(f"📍 {inv.location}")
    if inv.join_url:
        parts.append(f"🔗 {inv.join_url}")
    if inv.organizer:
        parts.append(f"👤 {inv.organizer}")
    if inv.description:
        parts.append(re.sub(r"\n{3,}", "\n\n", inv.description)[:1500])
    return "\n".join(parts)


def accept(inv, user=None):
    """✓: нове / змінене — у календар (подія «🤝 Зустріч» з листом і клієнтом), скасоване — знімається."""
    from .models import CalendarEvent, CalendarSettings, MeetingInvite
    if inv.status == MeetingInvite.CANCEL:
        if inv.event_id:
            ev = inv.event
            if not ev.title.startswith("✖ "):
                ev.title = ("✖ Скасовано: " + ev.title)[:300]
            ev.is_done = True
            ev.save(update_fields=["title", "is_done"])
        inv.status = MeetingInvite.CANCELLED
    else:
        remind = CalendarSettings.for_user(inv.user).default_remind_minutes
        values = {"title": inv.title[:300], "description": _event_text(inv), "start_at": inv.start_at,
                  "end_at": inv.end_at, "all_day": inv.all_day, "event_type": CalendarEvent.TYPE_MEETING,
                  "email_message": inv.email_message, "crm_customer": inv.crm_customer}
        if inv.event_id:
            ev = inv.event
            moved = ev.start_at != inv.start_at
            for k, v in values.items():
                setattr(ev, k, v)
            if moved:
                ev.remind_sent = ev.push_sent = False
            ev.is_done = False   # знову в силі після скасування
            ev.save()
        else:
            ev = CalendarEvent.objects.create(user=inv.user, remind_minutes_before=remind, **values)
            inv.event = ev
        inv.status = MeetingInvite.ADDED
    inv.decided_at = timezone.now()
    inv.save(update_fields=["status", "event", "decided_at"])
    return inv


def dismiss(inv, user=None):
    from .models import MeetingInvite
    inv.status = MeetingInvite.DISMISSED if inv.status != MeetingInvite.CANCEL else MeetingInvite.CANCELLED
    inv.decided_at = timezone.now()
    inv.save(update_fields=["status", "decided_at"])
    return inv


def pending(user) -> list:
    from .models import MeetingInvite
    if not getattr(user, "pk", None):
        return []
    return list(MeetingInvite.objects.filter(user=user, status__in=MeetingInvite.PENDING)
                .select_related("email_message").order_by("start_at")[:50])


# ── пошук в уже отриманій пошті ────────────────────────────────────────────
_scan_lock = threading.Lock()


def scan(user, since: datetime.date, progress=None, imap=True, trash: bool = False) -> tuple[int, int]:
    """Листи користувача від дати → запрошення. (перевірено листів, нових або змінених запрошень).
    trash — і листи з кошика (запрошення часто видаляють після того, як Outlook заніс їх у календар)."""
    from email_assistant.models import EmailMessage
    start = timezone.make_aware(datetime.datetime.combine(since, datetime.time.min))
    qs = EmailMessage.objects.filter(account__user=user, sent_at__gte=start, is_spam=False)
    if not trash:
        qs = qs.exclude(folder=EmailMessage.FOLDER_TRASH)
    qs = qs.select_related("account", "thread").order_by("sent_at")
    total, seen, found = qs.count(), 0, 0
    for msg in qs.iterator(chunk_size=200):
        seen += 1
        texts = calendars_of(msg)
        if not texts and imap and looks_like_invite(msg):
            texts = fetch_calendar_from_imap(msg)
        if texts:
            found += len(detect(msg, texts))
        if progress and seen % 100 == 0:
            progress(seen, total, found)
    return seen, found


def start_scan(user, since: datetime.date, trash: bool = False):
    """«🔎 Знайти запрошення» з налаштувань календаря: у фоні, стан — CalendarSettings.invites_scan_status."""
    from .models import CalendarSettings
    caller = threading.current_thread()

    def status(text):
        CalendarSettings.objects.filter(user=user).update(invites_scan_status=text[:255])

    def run():
        if not _scan_lock.acquire(blocking=False):
            status("⏳ Пошук уже йде — зачекайте")
            return
        try:
            status(f"⏳ Листи від {since:%d.%m.%Y}: підготовка…")
            seen, found = scan(user, since, trash=trash, progress=lambda done, total, n: status(
                f"⏳ Листи від {since:%d.%m.%Y}: перевірено {done} з {total}, запрошень {n}"))
            status(f"✅ {timezone.localtime():%d.%m.%Y %H:%M} · листи від {since:%d.%m.%Y}: перевірено {seen}, "
                   f"нових або змінених запрошень {found}")
        except Exception as e:  # noqa: BLE001
            logger.exception("invite scan failed: %s", e)
            status(f"❌ Пошук перервано ({type(e).__name__}) — спробуйте ще раз")
        finally:
            _scan_lock.release()
            if threading.current_thread() is not caller:
                connection.close()

    threading.Thread(target=run, daemon=True).start()
