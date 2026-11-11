"""🎙 Наради — уся логіка (сторінки й API для RAG-ПК лише викликають ці функції).

Потік: підготовка (prefill → create, без галочок згоди) → «🎙 Відкрити вікно запису» (issue_ticket: одноразове
посилання на RAG-ПК) → RAG-ПК обмінює квиток (redeem → push_token) → перед ▶ людина підтверджує згоду у вікні запису →
статус recording із згодою (push_status) → recorded → розшифровка частинами (push_transcript) → ключові моменти
(push_moments) → review: ✓ (accept → 📌 момент / ⚠️ ризик / 📅 крок плану / ⏰ дедлайн) / ✕ (reject).

Приватність: нараду, розшифровку й пункти бачить лише той, хто записав (owner) — і суперкористувач, і адміністратор
Minerva — ні (own / get_own усюди). Прийняті пункти (назва + цитата) бачать усі, хто бачить проєкт.

Протокол RAG-ПК ↔ Minerva (v2): ідентичність запису — push_token (redeem) + Meeting.uid; кожен статус має seq
(монотонний; старий — 200 без змін) і at; стани наради лише вперед (ready → recording → processing → review | failed;
failed → review — лише через зібрану розшифровку); розшифровка — частинами з upload_id; видалення на RAG-ПК — лише
за слідом (MeetingTombstone), видаленою розшифровкою, зниклим власником, старою сесією.
"""
import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import consent, quotes
from .models import (AUDIO_TEXT, LANG_CHOICES, RETENTION_CHOICES, TEXT_ONLY, Meeting, MeetingItem, MeetingSettings,
                     MeetingTombstone, MeetingTranscript)

logger = logging.getLogger(__name__)

# ── межі ─────────────────────────────────────────────────────────────────────
TICKET_MIN, TICKET_MAX = 16, 200
SESSION_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,40}$")
PARTS_MAX = 200
SEGMENTS_PER_PART_MAX = 5000
SEGMENT_TEXT_MAX = 2000                # довший текст сегмента обрізається (не відкидається)
TOTAL_CHARS_MAX = 3_000_000
ITEMS_MAX = 500
SUMMARY_MAX = 4000
TITLE_MAX, QUOTE_MAX, WHO_MAX, DUE_MAX = 300, 2000, 120, 120
MARKS_MAX = 300
PARTICIPANTS_MAX = 50
SYNC_MAX = 500
HOTWORDS_MAX, HOTWORD_SKUS = 300, 20
CLOCK_SLACK = timedelta(minutes=5)     # at / started_at — у межах [квиток − 5 хв, зараз + 5 хв], інакше — час прийому
STALE_HOURS = 24                       # запис / обробка без звісток від RAG-ПК — «не вдалося»
BENCH_RECENT = timedelta(minutes=30)   # … лише якщо RAG-ПК недавно виходив на зв'язок
ABANDONED_DAYS = 30                    # підготовлена, але ні разу не записана нарада
TOMBSTONE_DAYS = 60
CLEANUP_EVERY = timedelta(hours=20)
MIN_RETENTION_DAYS = 30

# протокол згоди: уривок розшифровки довкола «✅ Згоду підтверджено вголос» / «🙋 Новий учасник погодився»
EXCERPT_BEFORE, EXCERPT_AFTER = 45, 30
EXCERPT_ITEM, EXCERPT_TOTAL = 300, 2000
EXCERPT_FALLBACK_SECONDS = 90
EXCERPT_RE = re.compile(r"aufnahm|aufzeichn|mitschn|einverstand|recording|consent|згодн|записуєм")
CONSENT_MARKS = {"consent_ok": "✅ Згоду підтверджено вголос", "consent_join": "🙋 Новий учасник погодився"}

STATES = ("recording", "recorded", "transcribing", "transcribed", "finding_moments", "done", "error",
          "audio_deleted", "discarded")
FINAL = (Meeting.REVIEW, Meeting.FAILED)
PARTICIPANT_SOURCES = ("invite", "mail", "project", "customer", "manual")
LANGS = tuple(code for code, _ in LANG_CHOICES)
RETENTIONS = tuple(code for code, _ in RETENTION_CHOICES)
MARK_DEFAULT = "📌 Позначка під час запису"

DISABLED_TEXT = ("Записування нарад ще не ввімкнено — адміністратор вмикає його в «⚙️ Налаштування нарад» після "
                 "перевірки текстів згоди.")
NO_RIGHT_TEXT = "Немає права записувати наради — попросіть адміністратора відкрити розділ «🎙 Наради»."
EMPTY_TEXT = ("У записі не знайдено мови: мікрофон мовчав і звуку наради не було. Перед наступним записом натисніть "
              "у вікні запису «🎤 Перевірити мікрофон».")
MOMENTS_FAILED_TEXT = "Ключові моменти знайти не вдалося — розшифровка є."
STALE_TEXT = "RAG-ПК не повідомив результат за добу"
RETRY_TEXT = "🔄 Ще одна спроба — RAG-ПК візьме її під час наступної перевірки (до 2 хв)."
LOCKED_TEXT = ("Мови й режим уже передано у вікно запису. Мову можна змінити там до ▶; щоб змінити режим, закрийте "
               "вікно й натисніть «🎙 Відкрити вікно запису» ще раз.")
WORKING_STAGES = ("queued", "recorded", "transcribing", "transcribed", "finding_moments", "moments")


class MeetingError(ValueError):
    """Зрозуміла людині причина (сторінки показують її як є)."""


class TicketError(Exception):
    """Квиток вікна запису: ticket_invalid (404) — невідомий / використаний, ticket_expired (410)."""
    STATUS = {"ticket_invalid": 404, "ticket_expired": 410}
    TEXT = {"ticket_invalid": "Посилання на вікно запису недійсне або вже використане — відкрийте вікно з Minerva ще раз.",
            "ticket_expired": "Посилання на вікно запису застаріло — натисніть «🎙 Відкрити вікно запису» в Minerva ще раз."}

    def __init__(self, code):
        self.code = code
        self.status = self.STATUS.get(code, 404)
        self.message = self.TEXT.get(code, self.TEXT["ticket_invalid"])
        super().__init__(self.message)


class PushError(Exception):
    """Відмова на запит RAG-ПК: code (meeting_unknown 404, meeting_gone 410, push_forbidden 403, session_mismatch 409,
    consent_missing 409, transcript_exists 409, transcript_incomplete 409 (retryable), invalid_request 400 + field)."""

    def __init__(self, code, status, message, *, retryable=False, field=""):
        self.code, self.status, self.message, self.retryable, self.field = code, status, message, retryable, field
        super().__init__(message)

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.field:
            out["details"] = {"field": self.field}
        return out


def _bad(field, message="Неправильні дані запиту"):
    return PushError("invalid_request", 400, message, field=field)


# ── дрібниці ─────────────────────────────────────────────────────────────────

def _sha(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _int(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f and f not in (float("inf"), float("-inf")) else default


def _str(value, limit) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _pk(value):
    if value is None or value == "":
        return None
    pk = getattr(value, "pk", value)
    return _int(pk)


def _iso(dt) -> str:
    return timezone.localtime(dt).isoformat(timespec="seconds") if dt else ""


def _parse_dt(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = parse_datetime(value.strip())
    except ValueError:
        return None
    if dt is None:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _clamp(dt, m, now):
    """Час від RAG-ПК — у межах [квиток − 5 хв, зараз + 5 хв], інакше час прийому (годинник RAG-ПК збився)."""
    if dt is None:
        return now
    low = (m.ticket_redeemed_at or m.created_at or now) - CLOCK_SLACK
    return dt if low <= dt <= now + CLOCK_SLACK else now


def parse_day(value):
    """Дата з поля або з того, як сказали: «2026-11-14» / «14.11.2026» / «14.11.26»; інакше None."""
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text[:10])
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\b", text)
        if not m:
            return None
        d, mo, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def settings_obj():
    return MeetingSettings.load()


def enabled() -> bool:
    return bool(settings_obj().enabled)


def user_name(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or user.get_username() or "").strip()


def page_path(meeting) -> str:
    pk = getattr(meeting, "pk", meeting)
    try:
        return reverse("admin:meetings_meeting_page", args=[pk])
    except NoReverseMatch:
        return f"/admin/meetings/meeting/{pk}/page/"


def site_link(path) -> str:
    """Повна адреса сторінки (SystemSettings.site_url) — для листа, Telegram і кнопки «← Повернутися в Minerva»."""
    try:
        from config.models import SystemSettings
        s = SystemSettings.objects.filter(pk=1).first()
        if s is not None and s.site_domain:
            return s.site_url.rstrip("/") + path
    except Exception:  # noqa: BLE001
        pass
    return path


def bench_url(settings=None) -> str:
    return ((settings or settings_obj()).bench_url or "").rstrip("/")


def record_url(ticket, settings=None) -> str:
    """Куди переходить «🎙 Відкрити вікно запису»: квиток лише у фрагменті (#) — не йде в журнали й Referer."""
    return f"{bench_url(settings)}/#minerva={ticket}"


def speaker_labels(meeting) -> dict:
    """Хто говорить у каналі — за тим, що записували: обидва — «Я» / «Співрозмовники»; лише мікрофон (очна
    зустріч) — усі в кімнаті; лише звук наради — «Нарада»."""
    mode = getattr(meeting, "capture_mode", "") or ""
    if mode == Meeting.MIC:
        return {"L": "У кімнаті (мікрофон)", "R": "Співрозмовники", "M": "Мовець"}
    if mode == Meeting.MEETING_ONLY:
        return {"L": "Я", "R": "Нарада", "M": "Мовець"}
    return {"L": "Я", "R": "Співрозмовники", "M": "Мовець"}


# ── доступ ───────────────────────────────────────────────────────────────────

def can_record(user) -> bool:
    """Записувати може активний співробітник із правом на розділ, коли записування ввімкнено в налаштуваннях."""
    if not (user and getattr(user, "is_authenticated", False) and user.is_active and user.is_staff):
        return False
    if not enabled():
        return False
    return user.has_perm("meetings.add_meeting")


def record_refusal(user) -> str:
    """Чому не можна записувати ('' — можна)."""
    if not enabled():
        return DISABLED_TEXT
    return "" if can_record(user) else NO_RIGHT_TEXT


def own(user):
    """Наради людини — лише свої (і для суперкористувача)."""
    if not (user and getattr(user, "is_authenticated", False)):
        return Meeting.objects.none()
    return Meeting.objects.filter(owner=user)


def get_own(user, pk) -> Meeting:
    pk = _int(pk)
    m = own(user).filter(pk=pk).first() if pk else None
    if m is None:
        raise Http404("Нараду не знайдено")
    return m


def _check_owner(user, meeting):
    if user is None or not getattr(user, "is_authenticated", False) or meeting.owner_id != user.pk:
        raise Http404("Нараду не знайдено")


def owner_gone(m, settings=None, now=None) -> bool:
    """Власника немає або його вимкнено довше за inactive_owner_days — розшифровку й пункти пора прибрати."""
    if not m.owner_id:
        return True
    if m.owner_inactive_since:
        s = settings or settings_obj()
        now = now or timezone.now()
        return m.owner_inactive_since < now - timedelta(days=s.inactive_owner_days or 30)
    return False


def _project(user, value):
    from projects.models import Project
    pk = _pk(value)
    if not pk:
        return None
    if not user.has_perm("projects.view_project"):
        raise MeetingError("Немає права бачити проєкти")
    p = Project.objects.filter(pk=pk).first()
    if p is None:
        raise MeetingError("Проєкт не знайдено")
    return p


def _customer(user, value):
    from crm.models import Customer
    pk = _pk(value)
    if not pk:
        return None
    if not user.has_perm("crm.view_customer"):
        raise MeetingError("Немає права бачити клієнтів CRM")
    c = Customer.objects.filter(pk=pk).first()
    if c is None:
        raise MeetingError("Клієнта не знайдено")
    return c


def _event(user, value):
    from calendar_app.models import CalendarEvent
    pk = _pk(value)
    if not pk:
        return None
    ev = CalendarEvent.objects.filter(pk=pk, user=user).first()
    if ev is None:
        raise MeetingError("Подію календаря не знайдено")
    return ev


# ── підготовка (форма «🎙 Записати нараду») ─────────────────────────────────

def clean_participants(raw) -> list:
    """[{name, email, source}] — лише позначені людиною (форма передає вже відмічених), без дублів."""
    out, seen = [], set()
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        if "checked" in p and not p.get("checked"):
            continue
        name = " ".join(str(p.get("name") or "").split())[:200]
        email = str(p.get("email") or "").strip().lower()[:254]
        if email and ("@" not in email or " " in email):
            email = ""
        if not (name or email):
            continue
        key = email or name.lower()
        if key in seen:
            continue
        seen.add(key)
        source = p.get("source") if p.get("source") in PARTICIPANT_SOURCES else "manual"
        out.append({"name": name, "email": email, "source": source})
        if len(out) >= PARTICIPANTS_MAX:
            break
    return out


def _invite_people(user, event) -> list:
    """Учасники зі СВОГО запрошення на цю подію (iCalendar листа): організатор і ті, хто прийняв, — відмічені;
    решта — підказки. Кімнати й обладнання (CUTYPE ROOM / RESOURCE) — ні. Будь-який збій — порожньо."""
    try:
        from calendar_app import invites
        from calendar_app.models import MeetingInvite
        inv = MeetingInvite.objects.filter(user=user, event=event).select_related("email_message").first()
        if inv is None:
            return []
        ev = invites._source_event(inv)
    except Exception as e:  # noqa: BLE001 — немає .ics, пошта недоступна…
        logger.info("meeting prefill invite %s: %s", getattr(event, "pk", None), e)
        return []
    people = []
    for key, checked in (("ORGANIZER", True), ("ATTENDEE", None)):
        for params, value in ev.get(key) or []:
            if (params.get("CUTYPE") or "").upper() in ("ROOM", "RESOURCE"):
                continue
            email = invites._mailto(value)
            ok = checked if checked is not None else (params.get("PARTSTAT") or "").upper() == "ACCEPTED"
            people.append({"name": (params.get("CN") or "").strip()[:200], "email": email, "source": "invite",
                           "checked": bool(ok)})
    return people


def _mail_people(event) -> list:
    msg = getattr(event, "email_message", None)
    if msg is None:
        return []
    people = [{"name": (msg.from_name or "").strip()[:200], "email": (msg.from_email or "").strip().lower(),
               "source": "mail", "checked": True}]
    for a in list(msg.to_emails or []) + list(msg.cc_emails or []):
        people.append({"name": "", "email": str(a or "").strip().lower(), "source": "mail", "checked": False})
    return people


def _own_addresses(user) -> set:
    out = {(user.email or "").strip().lower()}
    try:
        from email_assistant.models import EmailAccount
        out |= {a.strip().lower() for a in EmailAccount.objects.filter(user=user)
                .values_list("email_address", flat=True) if a}
    except Exception:  # noqa: BLE001
        pass
    return out - {""}


def _event_projects(event) -> list:
    from projects.models import Project, ProjectLink
    cond = Q(links__event=event)
    msg = getattr(event, "email_message", None)
    if msg is not None:
        cond |= Q(links__message_id=msg.pk)
        if msg.thread_id:
            cond |= Q(links__thread_id=msg.thread_id)
    found = list(Project.objects.filter(cond, links__status=ProjectLink.CONFIRMED).distinct()[:5])
    if not found:
        from projects.services import CODE_RE
        codes = {f"P-{y}-{n}" for y, n in CODE_RE.findall(f"{event.title} {event.description}")}
        if codes:
            found = list(Project.objects.filter(code__in=codes)[:5])
    return found


def default_title(project=None, customer=None, event=None, day=None) -> str:
    day = day or timezone.localdate()
    if event is not None and (event.title or "").strip():
        return event.title.strip()[:200]
    if project is not None:
        return f"Нарада {project.code} · {day:%d.%m.%Y}"[:200]
    if customer is not None:
        who = (customer.company or customer.name or "").strip()
        if who:
            return f"Нарада {who} · {day:%d.%m.%Y}"[:200]
    return f"Нарада {day:%d.%m.%Y}"


def prefill(user, *, project=None, event=None, customer=None, continues=None) -> dict:
    """Що підставити у форму підготовки: назва, проєкт, клієнт, подія, мови, режим, мова тексту згоди, хто на нараді
    ({name, email, source, checked} — checked: організатор і ті, хто прийняв запрошення). Чого бракує або що людині
    не можна бачити — просто не підставляється (ніколи не падає)."""
    from crm.models import Customer
    from projects.models import Project
    s = settings_obj()
    data = {"title": "", "project": None, "customer": None, "calendar_event": None, "continues": None,
            "lang_l": "", "lang_r": "", "retention": s.default_retention, "consent_lang": consent.DEFAULT_LANG,
            "participants": [], "projects": []}
    can_projects = user.has_perm("projects.view_project")
    can_customers = user.has_perm("crm.view_customer")
    people = []
    try:
        if continues is not None:
            base = own(user).filter(pk=_pk(continues)).first()
            if base is not None:
                data.update(title=f"{base.title} (продовження)"[:200], continues=base, lang_l=base.lang_l,
                            lang_r=base.lang_r, retention=base.retention,
                            consent_lang=base.consent_lang or consent.DEFAULT_LANG)
                project = project or base.project_id
                customer = customer or base.customer_id
                event = event or base.calendar_event_id
                people += [dict(p, checked=True) for p in base.participants or []]
        ev = None
        if event is not None:
            from calendar_app.models import CalendarEvent
            ev = CalendarEvent.objects.filter(pk=_pk(event), user=user).select_related(
                "email_message", "crm_customer").first()
        if ev is not None:
            data["calendar_event"] = ev
            cust = ev.crm_customer
            if cust is None and ev.email_message_id and ev.email_message.thread_id:
                cust = getattr(ev.email_message.thread, "crm_customer", None)
            if cust is not None and can_customers and customer is None:
                data["customer"] = cust
            if can_projects and project is None:
                found = _event_projects(ev)
                data["projects"] = found
                if len(found) == 1:
                    data["project"] = found[0]
            people += _invite_people(user, ev) or _mail_people(ev)
        if project is not None and can_projects:
            data["project"] = Project.objects.filter(pk=_pk(project)).select_related("customer").first()
        if customer is not None and can_customers:
            data["customer"] = Customer.objects.filter(pk=_pk(customer)).first()
        p, c = data["project"], data["customer"]
        if c is None and p is not None and p.customer_id and can_customers:
            data["customer"] = c = p.customer
        if p is None and c is not None and can_projects:
            opened = list(Project.objects.filter(customer=c, status__in=Project.OPEN_STATUSES)[:2])
            if len(opened) == 1:
                data["project"] = p = opened[0]
        if p is not None:
            people += [{"name": x.name, "email": x.email, "source": "project", "checked": False}
                       for x in p.participants.all()[:PARTICIPANTS_MAX]]
        if c is not None and (c.email or c.name):
            people.append({"name": c.name or "", "email": (c.email or "").lower(), "source": "customer",
                           "checked": False})
        day = timezone.localtime(ev.start_at).date() if ev is not None else timezone.localdate()
        if not data["title"]:
            data["title"] = default_title(p, c, ev, day)
    except Exception as e:  # noqa: BLE001 — підказка не має ламати форму
        logger.warning("meeting prefill: %s", e)
        data["title"] = data["title"] or default_title()
    mine = _own_addresses(user)
    out, seen = [], set()
    for person in people:
        email = (person.get("email") or "").strip().lower()
        name = (person.get("name") or "").strip()
        if email in mine or not (email or name):
            continue
        key = email or name.lower()
        if key in seen:
            for o in out:                       # той самий — відмічений, якщо хоч одне джерело відмічає
                if (o["email"] or o["name"].lower()) == key:
                    o["checked"] = o["checked"] or bool(person.get("checked"))
                    o["name"] = o["name"] or name[:200]
            continue
        seen.add(key)
        out.append({"name": name[:200], "email": email[:254], "source": person.get("source") or "manual",
                    "checked": bool(person.get("checked"))})
        if len(out) >= PARTICIPANTS_MAX:
            break
    data["participants"] = out
    return data


def create(user, data) -> Meeting:
    """Підготувати нараду (стан «Готово до запису»). Згоду підтверджують у вікні запису перед ▶ — тут галочок немає.
    data: title, project, customer, calendar_event (або event), continues, lang_l, lang_r, retention, consent_lang,
    participants [{name, email, source, checked?}], consent_note. Проєкт / клієнт / подія перевіряються ще раз."""
    if not can_record(user):
        raise MeetingError(record_refusal(user) or NO_RIGHT_TEXT)
    s = settings_obj()
    project = _project(user, data.get("project"))
    customer = _customer(user, data.get("customer"))
    event = _event(user, data.get("calendar_event", data.get("event")))
    base = None
    if _pk(data.get("continues")):
        base = own(user).filter(pk=_pk(data.get("continues"))).first()
        if base is None:
            raise MeetingError("Нараду, яку продовжуєте, не знайдено")
    if customer is None and project is not None and project.customer_id and user.has_perm("crm.view_customer"):
        customer = project.customer
    title = " ".join(str(data.get("title") or "").split())[:200] or default_title(project, customer, event)
    retention = data.get("retention") if data.get("retention") in RETENTIONS else s.default_retention
    return Meeting.objects.create(
        owner=user, title=title, project=project, customer=customer, calendar_event=event, continues=base,
        retention=retention, retention_days=max(s.transcript_retention_days or 365, MIN_RETENTION_DAYS),
        audio_days=min(max(s.audio_retention_days or 30, 1), 30),
        lang_l=data.get("lang_l") if data.get("lang_l") in LANGS else "",
        lang_r=data.get("lang_r") if data.get("lang_r") in LANGS else "",
        consent_lang=consent.lang_of(data.get("consent_lang")),
        participants=clean_participants(data.get("participants")),
        consent_note=str(data.get("consent_note") or "").strip()[:2000])


def continue_prefill(user, meeting) -> dict:
    """«🎙 Записати продовження»: нова підготовка з назвою «… (продовження)», тими самими проєктом, клієнтом,
    учасниками й мовами; continues — ця нарада."""
    _check_owner(user, meeting)
    return prefill(user, continues=meeting)


def edit(user, meeting, data) -> Meeting:
    """«✏️ Змінити»: назва, проєкт, клієнт, учасники, примітка (до запису — ще мови, режим і мова тексту згоди).
    Зміни учасників і примітки дописуються в протокол (consent_log), знімок при підготовці не зникає."""
    _check_owner(user, meeting)
    m = meeting
    fields, now = set(), timezone.now()
    if "title" in data:
        title = " ".join(str(data.get("title") or "").split())[:200]
        if not title:
            raise MeetingError("Напишіть назву наради")
        m.title = title
        fields.add("title")
    if "project" in data:
        m.project = _project(user, data.get("project"))
        fields.add("project")
    if "customer" in data:
        m.customer = _customer(user, data.get("customer"))
        fields.add("customer")
    log = list(m.consent_log or [])
    if "participants" in data:
        people = clean_participants(data.get("participants"))
        if people != (m.participants or []):
            m.participants = people
            log.append({"at": _iso(now), "kind": "edit", "user": user.pk, "field": "participants"})
            fields |= {"participants", "consent_log"}
    if "consent_note" in data:
        note = str(data.get("consent_note") or "").strip()[:2000]
        if note != (m.consent_note or ""):
            m.consent_note = note
            log.append({"at": _iso(now), "kind": "edit", "user": user.pk, "field": "consent_note"})
            fields |= {"consent_note", "consent_log"}
    if m.status == Meeting.READY:
        wanted = {key: data.get(key) for key in ("lang_l", "lang_r") if key in data and data.get(key) in LANGS}
        if "retention" in data and data.get("retention") in RETENTIONS:
            wanted["retention"] = data.get("retention")
        changed = {k: v for k, v in wanted.items() if v != getattr(m, k)}
        if changed and m.ticket_redeemed_at:
            # вікно запису вже отримало мови й режим цієї спроби — інакше в Minerva й на RAG-ПК було б різне
            raise MeetingError(LOCKED_TEXT)
        for key, value in changed.items():
            setattr(m, key, value)
            fields.add(key)
        if "consent_lang" in data:
            m.consent_lang = consent.lang_of(data.get("consent_lang"))
            fields.add("consent_lang")
    m.consent_log = log
    if fields:
        m.save(update_fields=sorted(fields | {"updated_at"}))
    return m


# ── вікно запису: квиток і обмін ─────────────────────────────────────────────

RECORDING_FIELDS = ("started_at", "ended_at", "duration_s", "levels", "silent", "marks", "capture_mode", "no_headset",
                    "transcript_at", "transcript_chars", "moments_at", "moments_error", "consent_excerpt",
                    "consent_announced", "consent_all_agreed", "consent_at", "audio_deleted_at", "retry_requested_at",
                    "retry_sent_at", "notified_at")


def _reset_recording(m) -> list:
    """Попередній запис наради скасовано (записати заново / RAG-ПК скасував): поля запису й результату — порожні,
    розшифровка й неприйняті пункти — геть, документи бази знань — прибрано, повідомлення про новий результат знову
    прийде (notified_at). Протокол (consent_log) лишається."""
    m.started_at = m.ended_at = m.duration_s = m.transcript_at = m.moments_at = m.consent_at = None
    m.audio_deleted_at = m.retry_requested_at = m.retry_sent_at = m.notified_at = None
    m.levels, m.silent, m.marks, m.consent_excerpt = {}, [], [], []
    m.capture_mode, m.no_headset, m.transcript_chars, m.moments_error = "", False, 0, ""
    m.consent_announced = m.consent_all_agreed = False
    MeetingTranscript.objects.filter(meeting=m).delete()
    m.items.exclude(status=MeetingItem.ACCEPTED).delete()
    if m.approved_at:
        from . import knowledge
        knowledge.unpublish(m)
        m.approved_at = None
    return list(RECORDING_FIELDS) + ["approved_at"]


def _forget_session(m):
    if m.bench_session and m.bench_session not in (m.old_sessions or []):
        m.old_sessions = (list(m.old_sessions or []) + [m.bench_session])[-20:]
    m.bench_session, m.push_hash = "", ""


def issue_ticket(meeting, user=None, now=None) -> str:
    """Одноразове посилання на вікно запису (діє ticket_ttl_seconds і лише раз). Із «Не вдалося» — запис заново:
    попередній запис цієї наради RAG-ПК видалить (стара сесія → old_sessions). Попередній push_token одразу не діє.
    → сирий квиток (у базі лише sha256)."""
    now = now or timezone.now()
    s = settings_obj()
    with transaction.atomic():
        m = Meeting.objects.select_for_update(of=("self",)).filter(pk=meeting.pk).first()
        if m is None:
            raise Http404("Нараду не знайдено")
        if user is not None:
            _check_owner(user, m)
            if not can_record(user):
                raise MeetingError(record_refusal(user))
        if m.transcript_deleted_at:
            raise MeetingError("Розшифровку цієї наради вже видалено — підготуйте нову нараду.")
        if m.status not in (Meeting.READY, Meeting.FAILED):
            raise MeetingError("Нарада вже записується або записана. Щоб записати решту — «🎙 Записати продовження».")
        fields = set()
        if m.status == Meeting.FAILED:
            fields |= set(_reset_recording(m))
            m.status = Meeting.READY
            fields.add("status")
        _forget_session(m)
        ticket = secrets.token_urlsafe(32)
        m.ticket_hash, m.ticket_expires_at, m.ticket_redeemed_at = _sha(ticket), now + timedelta(
            seconds=s.ticket_ttl_seconds or 120), None
        m.bench_state, m.bench_detail, m.bench_progress, m.bench_updated_at = "", "", None, None
        m.bench_seq = 0
        fields |= {"old_sessions", "bench_session", "push_hash", "ticket_hash", "ticket_expires_at",
                   "ticket_redeemed_at", "bench_state", "bench_detail", "bench_progress", "bench_updated_at",
                   "bench_seq", "updated_at"}
        m.save(update_fields=sorted(fields))
    meeting.status = m.status
    return ticket


def hotwords(m) -> str:
    """Підказка розпізнаванню: клієнт, код і назва проєкту, до 20 артикулів проєкту — цілими словами, ≤ 300 знаків."""
    items = []
    if m.customer_id:
        items.append((m.customer.company or m.customer.name or "").strip())
    if m.project_id:
        items += [m.project.code, (m.project.name or "").strip()]
        items += [sku for sku in m.project.products.order_by("sku").values_list("sku", flat=True)[:HOTWORD_SKUS]]
    out, seen = [], set()
    for item in items:
        item = " ".join(str(item or "").split())
        if not item or item.lower() in seen:
            continue
        if len(", ".join(out + [item])) > HOTWORDS_MAX:
            continue
        seen.add(item.lower())
        out.append(item)
    return ", ".join(out)


def meeting_info(m) -> dict:
    """Що отримує вікно запису під час обміну квитка (без змісту наради, лише назва для банера)."""
    return {
        "id": m.pk, "uid": str(m.uid), "title": m.title, "project_code": m.project.code if m.project_id else "",
        "retention": m.retention, "retention_days": m.retention_days, "audio_days": m.audio_days,
        "lang_l": m.lang_l, "lang_r": m.lang_r, "consent_lang": consent.lang_of(m.consent_lang),
        "consent_texts": consent.texts(), "hotwords": hotwords(m), "return_url": site_link(page_path(m)),
    }


def redeem(ticket, now=None) -> dict:
    """RAG-ПК обмінює квиток (раз): → {"meeting": meeting_info, "push_token"}. Помилки — TicketError."""
    now = now or timezone.now()
    if not isinstance(ticket, str) or not TICKET_MIN <= len(ticket.strip()) <= TICKET_MAX:
        raise TicketError("ticket_invalid")
    digest = _sha(ticket.strip())
    with transaction.atomic():
        # без select_related під FOR UPDATE (PostgreSQL: nullable outer join) — зв'язки читаються після блокування
        m = Meeting.objects.select_for_update(of=("self",)).filter(ticket_hash=digest).first()
        if m is None:
            raise TicketError("ticket_invalid")
        if m.ticket_redeemed_at is not None or m.status != Meeting.READY or m.transcript_deleted_at \
                or owner_gone(m, now=now):
            raise TicketError("ticket_invalid")
        if m.ticket_expires_at is None or m.ticket_expires_at < now:
            raise TicketError("ticket_expired")
        token = secrets.token_urlsafe(32)
        m.ticket_hash, m.ticket_redeemed_at, m.push_hash, m.bench_session = "", now, _sha(token), ""
        m.save(update_fields=["ticket_hash", "ticket_redeemed_at", "push_hash", "bench_session", "updated_at"])
        info = meeting_info(m)
    return {"meeting": info, "push_token": token}


# ── запити RAG-ПК (status / transcript / moments) ────────────────────────────

def _tombstoned(pk=None, uid=None) -> bool:
    if uid:
        try:
            return MeetingTombstone.objects.filter(uid=uuid.UUID(str(uid))).exists()
        except ValueError:
            return False
    return bool(pk) and MeetingTombstone.objects.filter(meeting_id=pk).exists()


def check_push(meeting_pk, token, session, uid=None, *, lock=True) -> Meeting:
    """Чий це запис: нарада є (інакше слід → 410 meeting_gone, ні — 404 meeting_unknown), uid збігається, розшифровку
    не видалено й власник є (410), push_token з останнього обміну (403 push_forbidden), сесія — та сама (перший запит
    прив'язує; інша — 409 session_mismatch). lock — select_for_update(of=self): викликати в transaction.atomic."""
    pk = _int(meeting_pk)
    gone = PushError("meeting_gone", 410, "Нараду видалено в Minerva — запис на RAG-ПК можна видалити")
    unknown = PushError("meeting_unknown", 404, "Minerva не знає цієї наради")
    if not pk or pk < 1:
        raise unknown
    qs = Meeting.objects.select_for_update(of=("self",)) if lock else Meeting.objects
    m = qs.filter(pk=pk).first()
    if m is None:
        raise gone if _tombstoned(pk=pk, uid=uid) else unknown
    if uid and str(uid) != str(m.uid):
        raise gone if _tombstoned(uid=uid) else unknown
    if m.transcript_deleted_at or owner_gone(m):
        raise gone
    if not m.push_hash or not token or not hmac.compare_digest(_sha(str(token)), m.push_hash):
        raise PushError("push_forbidden", 403, "Це вікно запису застаріло — відкрийте його знову з Minerva")
    session = str(session or "").strip()
    if not SESSION_RE.match(session):
        raise _bad("session")
    if not m.bench_session:
        m.bench_session = session
        m.save(update_fields=["bench_session", "updated_at"])
    elif m.bench_session != session:
        raise PushError("session_mismatch", 409, "Цю нараду вже записує інше вікно запису")
    return m


def push_status(meeting_pk, token, data) -> dict:
    data = data if isinstance(data, dict) else {}
    with transaction.atomic():
        m = check_push(meeting_pk, token, data.get("session"), data.get("uid"))
        return apply_status(m, data)


def push_transcript(meeting_pk, token, data) -> dict:
    data = data if isinstance(data, dict) else {}
    with transaction.atomic():
        m = check_push(meeting_pk, token, data.get("session"), data.get("uid"))
        return add_transcript_part(m, data)


def push_moments(meeting_pk, token, data) -> dict:
    data = data if isinstance(data, dict) else {}
    with transaction.atomic():
        m = check_push(meeting_pk, token, data.get("session"), data.get("uid"))
        return add_moments(m, data)


def clean_levels(raw) -> dict:
    """Лише пік гучності каналу (для пояснення «мікрофон мовчав»). Хто скільки говорив (sound_share / sound_s) —
    ніколи не зберігається."""
    out = {}
    if isinstance(raw, dict):
        for ch in ("L", "R"):
            lv = raw.get(ch)
            if isinstance(lv, dict):
                peak = _float(lv.get("peak_dbfs"))
                out[ch] = {"peak_dbfs": round(max(min(peak, 0.0), -200.0), 1) if peak is not None else None}
    return out


def clean_silent(raw) -> list:
    return [ch for ch in ("L", "R") if isinstance(raw, list) and ch in raw]


def clean_marks(raw) -> list:
    """[{t, label, kind}] — kind: user (📌 людини → пункт для ✓ / ✕), auto (мікрофон відпав, пауза… — лише мітка
    в розшифровці), consent_ok / consent_join (у протокол згоди)."""
    out = []
    for mk in raw if isinstance(raw, list) else []:
        if not isinstance(mk, dict):
            continue
        t = _float(mk.get("t"))
        if t is None or t < 0:
            continue
        note = str(mk.get("note") if mk.get("note") is not None else mk.get("label") or "").strip()
        if note in CONSENT_MARKS:
            kind, label = note, CONSENT_MARKS[note]
        else:
            kind = "auto" if mk.get("auto") is True else "user"
            label = note[:200]
        item = {"t": round(t, 2), "label": label, "kind": kind}
        at = _parse_dt(mk.get("at"))            # годинник ПК у мить позначки (ISO зі зсувом), якщо RAG-ПК його дав
        if at is not None:
            item["at"] = at.isoformat(timespec="seconds")
        out.append(item)
        if len(out) >= MARKS_MAX:
            break
    return sorted(out, key=lambda x: x["t"])


def _mark_time(m, mk, now=None):
    """Коли поставлено позначку: її at (у межах запису: від початку − 5 хв до кінця + 5 хв), інакше початок + t."""
    now = now or timezone.now()
    base = m.started_at
    at = _parse_dt(mk.get("at"))
    if at is not None and base is not None:
        if base - CLOCK_SLACK <= at <= (m.ended_at or now) + CLOCK_SLACK:
            return at
    return base + timedelta(seconds=mk["t"]) if base else None


def _consent_marks_log(m, marks):
    """Позначки згоди → протокол (consent_log): «🙋 приєднався й погодився», «✅ згоду підтверджено вголос»."""
    log = list(m.consent_log or [])
    known = {(e.get("kind"), e.get("t")) for e in log}
    for mk in marks:
        if mk["kind"] not in CONSENT_MARKS:
            continue
        kind = "join" if mk["kind"] == "consent_join" else "confirmed"
        if (kind, mk["t"]) in known:
            continue
        log.append({"at": _iso(_mark_time(m, mk)), "kind": kind, "t": mk["t"]})
    m.consent_log = log


def _attempt(m) -> int:
    """Номер спроби запису = скільки разів уже підтверджували згоду + 1."""
    return 1 + sum(1 for e in m.consent_log or [] if e.get("kind") == "recording")


def apply_status(m, data, now=None) -> dict:
    """Стан запису від RAG-ПК (уже перевірений check_push і заблокований рядок). → {ok, applied, status}.
    Старий seq — без змін (200). Невідомий стан — ігнорується (сумісність уперед). recording без згоди обох галочок
    → 409 consent_missing."""
    now = now or timezone.now()
    seq = _int(data.get("seq"))
    if seq is None or seq < 1:
        raise _bad("seq")
    if seq <= (m.bench_seq or 0):
        return {"ok": True, "applied": False, "status": m.status}
    state = str(data.get("state") or "").strip()
    fields = {"bench_seq", "updated_at"}
    m.bench_seq = seq
    if state not in STATES:
        m.save(update_fields=sorted(fields))
        return {"ok": True, "applied": False, "status": m.status}
    at = _clamp(_parse_dt(data.get("at")), m, now)
    before = m.status

    if m.status == Meeting.READY and state not in ("recording", "discarded", "audio_deleted"):
        # запис без підтвердженої згоди Minerva не приймає (дані ігноруються; розшифровку — 409 consent_missing)
        m.save(update_fields=sorted(fields))
        return {"ok": True, "applied": False, "status": m.status}

    if state == "recording":
        fields |= _on_recording(m, data, at, now)
    elif state == "recorded":
        fields |= _on_recorded(m, data, at, now)
    elif state in ("transcribing", "transcribed", "finding_moments"):
        if m.status == Meeting.RECORDING:
            m.status = Meeting.PROCESSING
            fields.add("status")
    elif state == "done":
        if m.transcript_at:
            if m.status in (Meeting.RECORDING, Meeting.PROCESSING, Meeting.FAILED):
                m.status = Meeting.REVIEW
                fields.add("status")
            m.bench_detail = m.moments_error or ""
            fields.add("bench_detail")
        elif (_int(data.get("segments"), 0) or 0) <= 0 and m.status in (Meeting.RECORDING, Meeting.PROCESSING):
            m.status, m.bench_detail = Meeting.FAILED, EMPTY_TEXT
            fields |= {"status", "bench_detail"}
    elif state == "error":
        fields |= _on_error(m, data)
    elif state == "audio_deleted":
        m.audio_deleted_at = at
        m.bench_updated_at = now
        fields |= {"audio_deleted_at", "bench_updated_at"}
        m.save(update_fields=sorted(fields))
        return {"ok": True, "applied": True, "status": m.status}
    elif state == "discarded":
        fields |= _on_discarded(m, at)

    if state in ("recording", "recorded", "transcribing", "transcribed", "finding_moments"):
        if "detail" in data:
            m.bench_detail = _str(data.get("detail"), 300)
            fields.add("bench_detail")
        elif state != m.bench_state:            # новий крок без пояснення — старе (напр. «🔄 Ще одна спроба») геть
            m.bench_detail = ""
            fields.add("bench_detail")
    m.bench_state = state[:20]
    if _retry_pending(m) and at >= m.retry_requested_at:
        m.retry_sent_at = now                    # RAG-ПК узяв повтор: далі sync уже не відповідає retry
        fields.add("retry_sent_at")
    progress = _int(data.get("progress"))
    m.bench_progress = max(0, min(progress, 100)) if progress is not None and state != "discarded" else None
    m.bench_updated_at = now
    fields |= {"bench_state", "bench_progress", "bench_updated_at"}
    m.save(update_fields=sorted(fields))
    if m.status in FINAL and before not in FINAL:
        _schedule_notify(m)
    return {"ok": True, "applied": True, "status": m.status}


def lang_code(value):
    """Мова каналу від RAG-ПК: «uk» або {language: «uk», probability: 0.97} → «uk» (лише код, без імовірності)."""
    if isinstance(value, dict):
        value = value.get("language")
    return str(value).strip().lower()[:10] if isinstance(value, str) else None


def clean_languages(raw) -> dict:
    """{L: «uk», R: «de»} — з {ch: «uk»} або {ch: {language, probability}}; інше відкидається."""
    out = {}
    if isinstance(raw, dict):
        for ch, value in list(raw.items())[:10]:
            code = lang_code(value)
            if code:
                out[str(ch)[:5]] = code
    return out


def _languages(m, data) -> set:
    fields = set()
    langs = data.get("languages") if isinstance(data.get("languages"), dict) else {}
    for key, alt in (("lang_l", "L"), ("lang_r", "R")):
        value = data.get(key, langs.get(alt, langs.get(alt.lower())))
        if value is None:
            continue
        value = lang_code(value)
        if value is None:
            continue
        value = "" if value == "auto" else value
        if value in LANGS:
            setattr(m, key, value)
            fields.add(key)
    return fields


def _capture(m, data) -> set:
    cap = data.get("capture") if isinstance(data.get("capture"), dict) else {}
    mode = cap.get("mode", data.get("mode"))
    fields = set()
    if mode in (Meeting.BOTH, Meeting.MIC, Meeting.MEETING_ONLY):
        m.capture_mode = mode
        fields.add("capture_mode")
    if isinstance(cap.get("headset"), bool):
        m.no_headset = not cap["headset"]
        fields.add("no_headset")
    elif cap.get("processing") is not None:
        m.no_headset = cap.get("processing") == "system"   # «Без гарнітури, нарада в Teams / Zoom»
        fields.add("no_headset")
    return fields


def _on_recording(m, data, at, now) -> set:
    fields = set()
    if m.status != Meeting.READY:
        if m.status == Meeting.RECORDING and not m.started_at:
            m.started_at = _clamp(_parse_dt(data.get("started_at")), m, now) if data.get("started_at") else at
            fields.add("started_at")
        return fields
    c = data.get("consent") if isinstance(data.get("consent"), dict) else {}
    if not (c.get("announced") is True and c.get("all_agreed") is True):
        raise PushError("consent_missing", 409, "Немає підтвердження згоди — запис без неї Minerva не приймає")
    lang = consent.lang_of(c.get("lang") or m.consent_lang)
    m.status = Meeting.RECORDING
    m.consent_announced = m.consent_all_agreed = True
    m.consent_lang, m.consent_text = lang, consent.text_for(lang)
    m.consent_at = _clamp(_parse_dt(c.get("at")), m, now) if c.get("at") else at
    m.started_at = _clamp(_parse_dt(data.get("started_at")), m, now) if data.get("started_at") else at
    m.consent_log = list(m.consent_log or []) + [{
        "at": _iso(m.consent_at), "kind": "recording", "announced": True, "all_agreed": True, "lang": lang,
        "attempt": _attempt(m)}]
    fields |= {"status", "consent_announced", "consent_all_agreed", "consent_lang", "consent_text", "consent_at",
               "started_at", "consent_log"}
    fields |= _capture(m, data) | _languages(m, data)
    return fields


def _on_recorded(m, data, at, now) -> set:
    fields = set()
    if m.status not in (Meeting.RECORDING, Meeting.PROCESSING):
        return fields
    if m.status == Meeting.RECORDING:
        m.status = Meeting.PROCESSING
        fields.add("status")
    m.ended_at = at
    duration = _float(data.get("duration"))
    if duration is not None and 0 <= duration <= 86400:
        m.duration_s = round(duration, 1)
        fields.add("duration_s")
    if not m.started_at:
        m.started_at = _clamp((at - timedelta(seconds=m.duration_s)) if m.duration_s else at, m, now)
        fields.add("started_at")
    if "levels" in data:
        m.levels = clean_levels(data.get("levels"))
        fields.add("levels")
    if "silent" in data:
        m.silent = clean_silent(data.get("silent"))
        fields.add("silent")
    if "marks" in data:
        m.marks = clean_marks(data.get("marks"))
        _consent_marks_log(m, m.marks)
        fields |= {"marks", "consent_log"}
    if not m.capture_mode:
        fields |= _capture(m, data)
    fields.add("ended_at")
    return fields


def _on_error(m, data) -> set:
    stage = _str(data.get("stage"), 20)
    message = _str(data.get("message"), 300)
    if m.transcript_at:
        fields = {"bench_detail"}
        if not m.moments_at:                # розшифровка є, ключових моментів — ні
            m.moments_error = MOMENTS_FAILED_TEXT
            fields.add("moments_error")
        m.bench_detail = m.moments_error or message
        if m.status in (Meeting.RECORDING, Meeting.PROCESSING, Meeting.FAILED):
            m.status = Meeting.REVIEW
            fields.add("status")
        return fields
    m.bench_detail = message or "RAG-ПК повідомив про помилку"
    fields = {"bench_detail"}
    if m.status in (Meeting.RECORDING, Meeting.PROCESSING):
        m.status = Meeting.FAILED
        fields.add("status")
    return fields


def _on_discarded(m, at) -> set:
    """«⛔ Хтось не згоден — зупинити й видалити»: RAG-ПК уже видалив запис; нарада знову «Готово до запису»."""
    if m.status not in (Meeting.RECORDING, Meeting.PROCESSING, Meeting.READY):
        return set()
    fields = set(_reset_recording(m))
    log = list(m.consent_log or []) + [{"at": _iso(at), "kind": "discarded", "attempt": max(_attempt(m) - 1, 1),
                                        "reason": "participant"}]
    m.consent_log = log
    m.status = Meeting.READY
    _forget_session(m)
    m.bench_detail = f"Запис скасовано на RAG-ПК о {timezone.localtime(at):%H:%M} (учасник не погодився)"
    return fields | {"consent_log", "status", "old_sessions", "bench_session", "push_hash", "bench_detail"}


def _segments(raw, part) -> list:
    if not isinstance(raw, list):
        raise _bad("segments")
    if len(raw) > SEGMENTS_PER_PART_MAX:
        raise _bad("segments", "Забагато сегментів в одній частині")
    out = []
    for i, s in enumerate(raw):
        where = f"segments[{i}]"
        if not isinstance(s, dict):
            raise _bad(where)
        n = _int(s.get("n"))
        start, end = _float(s.get("start")), _float(s.get("end"))
        ch = s.get("ch") or "M"
        text = s.get("text")
        if n is None or n < 1:
            raise _bad(f"{where}.n")
        if start is None or start < 0:
            raise _bad(f"{where}.start")
        if end is None or end < start:
            raise _bad(f"{where}.end")
        if ch not in ("L", "R", "M"):
            raise _bad(f"{where}.ch")
        if not isinstance(text, str):
            raise _bad(f"{where}.text")
        out.append({"n": n, "start": round(start, 2), "end": round(end, 2), "ch": ch,
                    "text": text.strip()[:SEGMENT_TEXT_MAX]})
    return out


def add_transcript_part(m, data, now=None) -> dict:
    """Частина розшифровки (upload_id, part з parts, segments [{n, start, end, ch, text}]). Частини однієї передачі
    збираються в MeetingTranscript.incoming; інший upload_id / інша кількість частин — збирання заново. Усі частини
    є → n мають іти поспіль від 1, розшифровка готова (стан лишається «Обробка», поки RAG-ПК шукає ключові моменти).
    Після збирання той самий upload_id → {complete: true} без змін, інший → 409 transcript_exists."""
    now = now or timezone.now()
    if m.status == Meeting.READY:
        raise PushError("consent_missing", 409, "Немає підтвердження згоди — розшифровку Minerva не приймає")
    upload_id = _str(data.get("upload_id"), 64)
    if not upload_id:
        raise _bad("upload_id")
    parts, part = _int(data.get("parts")), _int(data.get("part"))
    if parts is None or not 1 <= parts <= PARTS_MAX:
        raise _bad("parts")
    if part is None or not 1 <= part <= parts:
        raise _bad("part")
    tr, _ = MeetingTranscript.objects.get_or_create(meeting=m)
    if m.transcript_at:
        if tr.upload_id == upload_id:
            return {"complete": True, "segments": len(tr.segments or [])}
        raise PushError("transcript_exists", 409, "Розшифровку цієї наради вже отримано")
    segs = _segments(data.get("segments"), part)
    stage = tr.incoming if isinstance(tr.incoming, dict) else {}
    if stage.get("upload_id") != upload_id or stage.get("parts") != parts:
        stage = {"upload_id": upload_id, "parts": parts, "got": {}, "meta": {}}
    stage["got"][str(part)] = segs
    meta = stage.setdefault("meta", {})
    if isinstance(data.get("languages"), dict):
        meta["languages"] = clean_languages(data["languages"])
    for key, limit in (("model", 60),):
        if data.get(key):
            meta[key] = _str(data.get(key), limit)
    if _float(data.get("duration")) is not None:
        meta["duration"] = _float(data.get("duration"))
    if "marks" in data:
        meta["marks"] = clean_marks(data.get("marks"))
    total = sum(len(s["text"]) for got in stage["got"].values() for s in got)
    if total > TOTAL_CHARS_MAX:
        raise _bad("segments", "Розшифровка задовга")
    if len(stage["got"]) < parts:
        tr.incoming = stage
        tr.save(update_fields=["incoming"])
        return {"complete": False, "missing": parts - len(stage["got"])}

    segments = [s for p in range(1, parts + 1) for s in stage["got"].get(str(p), [])]
    if [s["n"] for s in segments] != list(range(1, len(segments) + 1)):
        raise _bad("segments.n", "Номери сегментів мають іти поспіль від 1")
    tr.segments, tr.languages, tr.model = segments, meta.get("languages", {}), meta.get("model", "")
    tr.upload_id, tr.parts, tr.chars, tr.at, tr.incoming = upload_id, parts, total, now, {}
    tr.save(update_fields=["segments", "languages", "model", "upload_id", "parts", "chars", "at", "incoming"])
    fields = {"transcript_at", "transcript_chars", "consent_excerpt", "updated_at"}
    m.transcript_at, m.transcript_chars = now, total
    if "marks" in meta:
        m.marks = meta["marks"]
        _consent_marks_log(m, m.marks)
        fields |= {"marks", "consent_log"}
    if meta.get("duration") is not None and not m.duration_s:
        m.duration_s = round(meta["duration"], 1)
        fields.add("duration_s")
    if m.status in (Meeting.RECORDING, Meeting.FAILED):
        m.status = Meeting.PROCESSING          # ключові моменти ще шукаються; розшифровку вже видно
        fields.add("status")
    m.consent_excerpt = consent_excerpt(m, segments)
    m.save(update_fields=sorted(fields))
    _mark_items(m, segments)
    return {"complete": True, "segments": len(segments)}


def _segment_at(segments, t):
    best = None
    for s in segments:
        if s["start"] <= t <= s["end"]:
            return s
        if best is None or abs(s["start"] - t) < abs(best["start"] - t):
            best = s
    return best


def _mark_items(m, segments) -> int:
    """📌 позначки людини під час запису → пункти для ✓ / ✕ (автоматичні й позначки згоди — ні)."""
    known = set(m.items.values_list("key", flat=True))
    made = 0
    for mk in m.marks or []:
        if mk.get("kind") != "user":
            continue
        key = quotes.mark_key(mk.get("t"))
        if key in known:
            continue
        seg = _segment_at(segments, float(mk.get("t") or 0)) if segments else None
        MeetingItem.objects.create(
            meeting=m, key=key, kind=MeetingItem.MARK, title=(mk.get("label") or MARK_DEFAULT)[:TITLE_MAX],
            quote=(seg["text"] if seg else "")[:QUOTE_MAX], start_s=mk.get("t"), end_s=seg["end"] if seg else None,
            ch=seg["ch"] if seg else "", segment=seg["n"] if seg else None)
        known.add(key)
        made += 1
    return made


def consent_excerpt(m, segments) -> list:
    """Уривок розшифровки для протоколу згоди: сегменти довкола позначок згоди [t − 45 с, t + 30 с]; без позначок —
    перші 90 с, лише з «запис / згода» (aufnahm, einverstand, recording, згодн…) і відповідь одразу після.
    Кожен текст ≤ 300, разом ≤ 2000 знаків."""
    marks = [mk["t"] for mk in m.marks or [] if mk.get("kind") in CONSENT_MARKS]
    picked = []
    if marks:
        picked = [s for s in segments if any(s["end"] >= t - EXCERPT_BEFORE and s["start"] <= t + EXCERPT_AFTER
                                             for t in marks)]
    else:
        for i, s in enumerate(segments):
            if s["start"] > EXCERPT_FALLBACK_SECONDS:
                break
            if EXCERPT_RE.search(quotes.normalize(s["text"])):
                picked.append(s)
                for nxt in segments[i + 1:i + 3]:
                    if nxt["start"] <= min(s["end"] + 30, EXCERPT_FALLBACK_SECONDS):
                        picked.append(nxt)
    out, total, seen = [], 0, set()
    for s in sorted(picked, key=lambda x: (x["start"], x["n"])):
        if s["n"] in seen or not s["text"].strip():
            continue
        text = s["text"].strip()[:EXCERPT_ITEM]
        if total + len(text) > EXCERPT_TOTAL:
            break
        seen.add(s["n"])
        total += len(text)
        out.append({"n": s["n"], "start": s["start"], "ch": s["ch"], "text": text})
    return out


def add_moments(m, data, now=None) -> dict:
    """Ключові моменти з RAG-ПК (локальна модель): лише ті, чия цитата справді є в розшифровці (quotes.find —
    ті самі правила, що на RAG-ПК); новий ключ — новий пункт «чекає ✓ / ✕», ✕ і ✓ не повертаються. Розшифровки ще
    немає → 409 transcript_incomplete (RAG-ПК повторить). → {added, skipped, known}."""
    now = now or timezone.now()
    if not m.transcript_at:
        raise PushError("transcript_incomplete", 409, "Розшифровку ще не зібрано — спробуйте трохи згодом",
                        retryable=True)
    items = data.get("items")
    if not isinstance(items, list) or len(items) > ITEMS_MAX:
        raise _bad("items")
    tr = MeetingTranscript.objects.get(meeting=m)
    segments = tr.segments or []
    by_n = {s["n"]: s for s in segments}
    known = set(m.items.values_list("key", flat=True))
    added = skipped = dup = 0
    for raw in items:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        kind = raw.get("kind") if raw.get("kind") in MeetingItem.BENCH_KINDS else MeetingItem.INFO
        quote = " ".join(str(raw.get("quote") or "").split())
        n = quotes.find(quote, raw.get("line"), segments)
        if n is None:
            skipped += 1
            continue
        key = quotes.item_key(kind, quote)
        if key in known:
            dup += 1
            continue
        seg = by_n[n]
        MeetingItem.objects.create(
            meeting=m, key=key, kind=kind, title=_str(raw.get("text"), TITLE_MAX), quote=quote[:QUOTE_MAX],
            start_s=seg["start"], end_s=seg["end"], ch=seg["ch"], who=_str(raw.get("who"), WHO_MAX),
            due_text=_str(raw.get("due"), DUE_MAX), segment=n)
        known.add(key)
        added += 1
    tr.summary = _str(data.get("summary"), SUMMARY_MAX)
    tr.moments_model = _str(data.get("model"), 60)
    tr.save(update_fields=["summary", "moments_model"])
    before = m.status
    m.moments_at, m.moments_error, m.bench_detail = now, "", ""
    fields = {"moments_at", "moments_error", "bench_detail", "updated_at"}
    if m.status in (Meeting.RECORDING, Meeting.PROCESSING, Meeting.FAILED):
        m.status = Meeting.REVIEW
        fields.add("status")
    m.save(update_fields=sorted(fields))
    if m.status in FINAL and before not in FINAL:
        _schedule_notify(m)
    return {"added": added, "skipped": skipped, "known": dup}


def _schedule_notify(m):
    from . import notify
    notify.schedule(m.pk)


# ── синхронізація з RAG-ПК ───────────────────────────────────────────────────

BENCH_INFO_KEYS = {"version": 40, "last_error": 200}
BENCH_INFO_INTS = ("queue", "outbox", "failed", "retention_days")
BENCH_INFO_BOOLS = ("live_busy", "standalone")


def clean_bench_info(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, limit in BENCH_INFO_KEYS.items():
        if key in raw and raw[key] is not None:
            out[key] = _str(raw[key], limit)
    for key in BENCH_INFO_INTS:
        v = _int(raw.get(key))
        if v is not None:
            out[key] = max(0, min(v, 1_000_000))
    for key in BENCH_INFO_BOOLS:
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    return out


def _sync_action(m, e, s, now) -> str:
    session = str(e.get("session") or "")
    if m is None:
        return "delete" if _tombstoned(pk=_int(e.get("meeting")), uid=e.get("uid")) else "unknown"
    if not session:
        return "unknown"
    if e.get("uid") and str(e.get("uid")) != str(m.uid):
        return "delete" if _tombstoned(uid=e.get("uid")) else "unknown"
    if m.transcript_deleted_at or owner_gone(m, s, now):
        return "delete"
    if session and session in (m.old_sessions or []):
        return "delete"
    if m.bench_session and session != m.bench_session:
        return "delete"
    if not m.bench_session:
        return "keep"                       # запис ще не встиг прив'язатись — ніколи не видаляти
    if _retry_pending(m) and str(e.get("stage") or "") not in WORKING_STAGES:
        return "retry"                      # поки RAG-ПК не взяв повтор (стан після запиту / робочий крок) — щоразу
    if e.get("audio_on_bench") is True:
        clock = m.transcript_at or m.ended_at or m.created_at
        days = min(m.audio_days or 30, s.audio_retention_days or 30, m.retention_days or 365)
        if (m.retention == TEXT_ONLY and m.transcript_at) or (m.retention == AUDIO_TEXT and (
                m.approved_at or (clock and clock < now - timedelta(days=days)))):
            return "drop_audio"
    return "keep"


def sync(entries, bench=None, now=None) -> list:
    """Що робити з кожним записом RAG-ПК: keep | delete | unknown | retry | drop_audio.
    entries — [{meeting, uid, session, token_sha256, stage, progress, queue_pos, audio_on_bench}].
    Видалити — лише слід (tombstone), видалена розшифровка, зниклий власник, стара сесія або інша прив'язана сесія;
    поки запис не прив'язався (bench_session порожній) — ніколи. bench (якщо є) → «RAG-ПК на зв'язку»."""
    now = now or timezone.now()
    s = settings_obj()
    if bench is not None and isinstance(bench, dict):
        MeetingSettings.objects.filter(pk=1).update(bench_seen_at=now, bench_info=clean_bench_info(bench))
    entries = [e for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict)][:SYNC_MAX]
    pks = {_int(e.get("meeting")) for e in entries} - {None}
    meetings = {m.pk: m for m in Meeting.objects.filter(pk__in=pks).select_related("owner")}
    out = []
    for e in entries:
        pk = _int(e.get("meeting"))
        m = meetings.get(pk)
        action = _sync_action(m, e, s, now)
        if m is not None and action in ("keep", "retry", "drop_audio") and m.bench_session \
                and str(e.get("session") or "") == m.bench_session:
            _sync_refresh(m, e, action, now)
        out.append({"meeting": pk, "uid": str(e.get("uid") or (m.uid if m else "")), "session": str(e.get("session")
                                                                                                      or "")[:40],
                    "action": action})
    return out


def _retry_pending(m) -> bool:
    """«🔄 Спробувати ще раз» натиснуто, а RAG-ПК ще не підтвердив, що взяв повтор (retry_sent_at — коли підтвердив:
    статус з at після запиту або робочий крок у синхронізації). Загублена відповідь sync не губить повтор."""
    return bool(m.retry_requested_at) and (m.retry_sent_at is None or m.retry_sent_at < m.retry_requested_at)


def _sync_refresh(m, e, action, now):
    """Хід запису зі списку RAG-ПК (черга, %…). Збій на RAG-ПК («failed») не оновлює час — тоді через добу нарада
    стане «Не вдалося» (cleanup). Робочий крок після «🔄 Спробувати ще раз» — RAG-ПК повтор узяв."""
    upd = {}
    stage = str(e.get("stage") or "")
    if action != "retry" and _retry_pending(m) and stage in WORKING_STAGES:
        upd["retry_sent_at"] = now
    if m.status in (Meeting.RECORDING, Meeting.PROCESSING) and re.fullmatch(r"[a-z_]{1,20}", stage):
        upd["bench_state"] = stage
        progress = _int(e.get("progress"))
        upd["bench_progress"] = max(0, min(progress, 100)) if progress is not None else None
        if stage != "failed":
            upd["bench_updated_at"] = now
    if upd:
        Meeting.objects.filter(pk=m.pk).update(**upd)


# ── сторінка наради: стан, повтор, ✓ / ✕ ─────────────────────────────────────

def poll_state(m) -> dict:
    """Для опитування сторінки (кожні 5 с) — без розшифровки."""
    return {"status": m.status, "label": m.get_status_display(), "bench_state": m.bench_state,
            "bench_detail": m.bench_detail, "progress": m.bench_progress, "transcript": m.has_transcript,
            "moments": bool(m.moments_at), "pending": m.items.filter(status=MeetingItem.PENDING).count(),
            "changed": m.updated_at.isoformat() if m.updated_at else "",
            "bench_updated_at": m.bench_updated_at.isoformat() if m.bench_updated_at else ""}


def bench_health(settings=None, now=None) -> dict:
    """«RAG-ПК на зв'язку: 3 хв тому» / ніколи / давно; busy — на RAG-ПК зараз іде інший запис."""
    s = settings or settings_obj()
    now = now or timezone.now()
    info = s.bench_info or {}
    if not s.bench_seen_at:
        return {"never": True, "ok": False, "minutes": None, "busy": False, "info": info}
    minutes = int((now - s.bench_seen_at).total_seconds() // 60)
    return {"never": False, "ok": minutes <= 10, "minutes": minutes, "busy": bool(info.get("live_busy")),
            "seen_at": s.bench_seen_at, "info": info}


def can_retry(m) -> bool:
    """«🔄 Спробувати ще раз»: запис ще на RAG-ПК (сесія прив'язана, розшифровку не видалено) і
    - не вдалося (аудіо ще є або розшифровка вже є — тоді повториться пошук ключових моментів), або
    - розшифровка є, а ключові моменти знайти не вдалося (review + moments_error, моментів ще не отримано)."""
    if not m.bench_session or m.transcript_deleted_at:
        return False
    if m.status == Meeting.FAILED:
        return m.audio_deleted_at is None or bool(m.transcript_at)
    return m.status == Meeting.REVIEW and bool(m.moments_error) and not m.moments_at and bool(m.transcript_at)


def retry(user, meeting, now=None) -> Meeting:
    """Попросити RAG-ПК спробувати ще раз (синхронізація відповідає retry, доки RAG-ПК його не візьме). Нарада —
    знову «Обробка»; розшифровка лишається, moments_error знімуть ключові моменти, що прийдуть. Повідомлення про
    результат прийде знову (notified_at)."""
    _check_owner(user, meeting)
    now = now or timezone.now()
    with transaction.atomic():
        m = Meeting.objects.select_for_update(of=("self",)).get(pk=meeting.pk)
        if not can_retry(m):
            raise MeetingError("Спробувати ще раз уже не можна: запису цієї наради на RAG-ПК немає.")
        m.retry_requested_at, m.status, m.bench_detail = now, Meeting.PROCESSING, RETRY_TEXT
        m.bench_updated_at, m.notified_at = now, None
        m.save(update_fields=["retry_requested_at", "status", "bench_detail", "bench_updated_at", "notified_at",
                              "updated_at"])
    return m


def item_for(user, meeting, item_pk) -> MeetingItem:
    _check_owner(user, meeting)
    item = MeetingItem.objects.filter(meeting=meeting, pk=_int(item_pk)).first()
    if item is None:
        raise Http404("Пункт не знайдено")
    return item


def has_plan(project) -> bool:
    from projects.models import ProjectPhase, ProjectStep
    return (ProjectStep.objects.filter(project=project).exists()
            or ProjectPhase.objects.filter(project=project).exists())


DEFAULT_MOMENT_KIND = {"decision": "decision", "deadline": "deadline", "risk": "problem"}
DEFAULT_TARGET = {"decision": "moment", "info": "moment", "question": "moment", "mark": "moment", "risk": "risk",
                  "task": "step", "promise": "step", "deadline": "deadline"}


def targets(user, m, item=None) -> dict:
    """Куди можна додати пункт: {target: '' (можна) | причина}; default — що вибрати першим."""
    out = {}
    if not m.project_id:
        reason = "Нарада без проєкту — виберіть проєкт: ✏️ Змінити"
        out = {"moment": reason, "risk": reason, "step": reason}
    elif not user.has_perm("projects.change_project"):
        reason = "Немає права змінювати цей проєкт — доступний лише ⏰ дедлайн."
        out = {"moment": reason, "risk": reason, "step": reason}
    else:
        out = {"moment": "", "risk": "",
               "step": "" if has_plan(m.project) else ("У проєкті ще немає плану. Додайте як ⏰ дедлайн або спершу "
                                                       "створіть план (вкладка «📅 План»).")}
    out["deadline"] = ""
    default = DEFAULT_TARGET.get(item.kind if item else "", "moment")
    if out.get(default):
        default = "deadline"
    return {"targets": out, "default": default}


def _step_side(m, item, data) -> str:
    from projects.models import ProjectStep
    if data.get("side") in dict(ProjectStep.SIDE_CHOICES):
        return data["side"]
    if (m.capture_mode in ("", Meeting.BOTH)) and item.kind in (MeetingItem.TASK, MeetingItem.PROMISE) \
            and item.ch == "R":
        return ProjectStep.CUSTOMER
    return ProjectStep.WE


def accept(user, item, target, data=None) -> MeetingItem:
    """✓ — пункт у проєкт (📌 момент, ⚠️ ризик, 📅 крок плану) або ⏰ дедлайн у СВІЙ календар (до проєкту не
    прив'язується; назва без цитати). data: title, kind (моменту), category / probability / impact / owner (ризику),
    date, assignee, side (кроку / дедлайну)."""
    from projects import plan, risk_rules, risks, services as project_services
    from projects.models import ProjectHighlight
    data = data or {}
    m = item.meeting
    _check_owner(user, m)
    if item.status != MeetingItem.PENDING:
        raise MeetingError("Пункт уже вирішено")
    allowed = targets(user, m, item)["targets"]
    if target not in allowed:
        raise MeetingError("Виберіть, куди додати пункт")
    if allowed[target]:
        raise MeetingError(allowed[target])
    title = " ".join(str(data.get("title") or item.title or "").split()) or item.quote[:200]
    second = int(item.start_s) if item.start_s is not None else None
    fields = ["status", "target", "decided_at"]
    try:
        with transaction.atomic():
            # подвійний ✓ (два POST поспіль): другий чекає на блокуванні й бачить «уже вирішено» — без дубля
            locked = MeetingItem.objects.select_for_update(of=("self",)).filter(pk=item.pk).first()
            if locked is None or locked.status != MeetingItem.PENDING:
                raise MeetingError("Пункт уже вирішено")
            item.status = locked.status
            if target == "moment":
                kind = data.get("kind") if data.get("kind") in dict(ProjectHighlight.KIND_CHOICES) else \
                    DEFAULT_MOMENT_KIND.get(item.kind, ProjectHighlight.OTHER)
                item.highlight = project_services.add_highlight(
                    m.project, title=title, date=m.day, kind=kind, quote=item.quote, user=user,
                    source=ProjectHighlight.MEETING, meeting=m, meeting_second=second)
                fields.append("highlight")
            elif target == "risk":
                payload = {"title": title, "description": "", "category": data.get("category") or "other",
                           "probability": data.get("probability") or 2, "impact": data.get("impact") or 2,
                           "quote": item.quote, "review_on": risk_rules.review_default().isoformat()}
                if _int(data.get("owner")):
                    payload["owner"] = data.get("owner")
                item.risk = risks.save(m.project, payload, user=user, source="meeting", meeting=m,
                                       meeting_second=second)
                fields.append("risk")
            elif target == "step":
                side = _step_side(m, item, data)
                if "assignee" in data:
                    assignee = _int(data.get("assignee"))
                else:
                    assignee = user.pk if side == "we" else None
                due = parse_day(data.get("date")) if data.get("date") else None
                item.step = plan.meeting_step(m.project, title, assignee=assignee, due=due, side=side,
                                              quote=item.quote, meeting=m, meeting_second=second, user=user)
                fields.append("step")
            else:
                item.event = _deadline_event(user, m, title, data.get("date"))
                fields.append("event")
            item.status, item.target, item.decided_at = MeetingItem.ACCEPTED, target, timezone.now()
            item.save(update_fields=fields)
    except (project_services.HighlightError, risks.RiskError, plan.PlanError) as e:
        raise MeetingError(str(e)) from e
    return item


def _deadline_event(user, m, title, value):
    from calendar_app.models import CalendarEvent
    day = parse_day(value)
    if day is None:
        raise MeetingError("Виберіть дату дедлайну")
    start = timezone.make_aware(datetime.combine(day, time(9, 0)))
    return CalendarEvent.objects.create(user=user, title=title[:200], description="",
                                        event_type=CalendarEvent.TYPE_DEADLINE, start_at=start, all_day=True,
                                        crm_customer=m.customer)


def reject(user, item) -> MeetingItem:
    _check_owner(user, item.meeting)
    if item.status != MeetingItem.PENDING:
        raise MeetingError("Пункт уже вирішено")
    item.status, item.decided_at = MeetingItem.REJECTED, timezone.now()
    item.save(update_fields=["status", "decided_at"])
    return item


def restore(user, item) -> MeetingItem:
    _check_owner(user, item.meeting)
    if item.status != MeetingItem.REJECTED:
        raise MeetingError("Повернути можна лише пункт «Не те»")
    item.status, item.decided_at = MeetingItem.PENDING, None
    item.save(update_fields=["status", "decided_at"])
    return item


# ── видалення й прибирання ───────────────────────────────────────────────────

def accepted_items(meeting) -> list:
    """Прийняті пункти (для вікна «🗑 Видалити нараду»: що лишиться в проєкті / календарі)."""
    return list(meeting.items.filter(status=MeetingItem.ACCEPTED)
                .select_related("highlight", "risk", "step", "event").order_by("start_s", "pk"))


_NOTE_QUOTE_RE = re.compile(r"(🎙 з наради від \d{2}\.\d{2}\.\d{4}): «[^\n]*")


def scrub_quotes(meeting) -> int:
    """«Також прибрати цитати з цих пунктів»: цитата моменту, підстава ризику, цитата в примітці кроку — геть
    (лишається позначка «🎙 з наради DD.MM»)."""
    n = 0
    label = f"🎙 Нарада {meeting.day:%d.%m.%Y}"
    for item in accepted_items(meeting):
        if item.highlight is not None and item.highlight.quote:
            item.highlight.quote = ""
            item.highlight.save(update_fields=["quote"])
            n += 1
        if item.risk is not None and item.risk.evidence != label:
            item.risk.evidence = label
            item.risk.save(update_fields=["evidence", "updated_at"])
            n += 1
        if item.step is not None and _NOTE_QUOTE_RE.search(item.step.notes or ""):
            item.step.notes = _NOTE_QUOTE_RE.sub(r"\1", item.step.notes)
            item.step.save(update_fields=["notes", "updated_at"])
            n += 1
    return n


def delete_meeting(user, meeting, *, scrub=False, request=None, reason="deleted") -> dict:
    """«🗑 Видалити нараду» (лише власник): розшифровка, пункти, документи бази знань, протокол. Пункти, уже додані
    в проєкт / календар, лишаються (scrub — без цитат). RAG-ПК дізнається через слід (410 / sync). У журнал аудиту —
    лише номер, час згоди й кількість пунктів (без назви)."""
    _check_owner(user, meeting)
    from core.models import AuditLog
    accepted = accepted_items(meeting)
    scrubbed = scrub_quotes(meeting) if scrub else 0
    pk = meeting.pk
    extra = {"consent_at": _iso(meeting.consent_at), "consent_all_agreed": meeting.consent_all_agreed,
             "accepted_items": len(accepted), "quotes_removed": scrubbed}
    with transaction.atomic():
        meeting._delete_reason = reason
        meeting.delete()
        try:
            AuditLog.log(AuditLog.Action.DELETE, module="meetings", model_name="Meeting", object_id=pk,
                         object_repr=f"🎙 Нарада #{pk}", extra=extra, user=user, request=request)
        except Exception as e:  # noqa: BLE001
            logger.warning("meeting %s audit: %s", pk, e)
    return {"accepted": len(accepted), "scrubbed": scrubbed}


PROTOCOLS_MAX = 500


def _is_admin(user) -> bool:
    try:
        from core.utils import is_minerva_admin
        return bool(user and user.is_active and is_minerva_admin(user))
    except Exception:  # noqa: BLE001
        return False


def protocols(user, q="") -> list:
    """«🧾 Протоколи згоди» (лише адміністратор Minerva): метадані без змісту — день, хто записав, код проєкту, час і
    галочки згоди, мова, стан, чи є розшифровка, адреси учасників (пошук q — за адресою). Ніколи — назва,
    розшифровка, пункти, уривок."""
    if not _is_admin(user):
        raise Http404("Сторінку не знайдено")
    qs = Meeting.objects.select_related("owner", "project").only(
        "pk", "created_at", "started_at", "status", "consent_at", "consent_announced", "consent_all_agreed",
        "consent_lang", "participants", "transcript_at", "transcript_deleted_at", "owner__username",
        "owner__first_name", "owner__last_name", "project__code").order_by("-created_at")
    q = str(q or "").strip().lower()
    out = []
    for m in qs.iterator(chunk_size=200):
        emails = [p.get("email", "") for p in m.participants or [] if p.get("email")]
        if q and not any(q in e for e in emails):
            continue
        out.append({"pk": m.pk, "day": m.day, "recorder": user_name(m.owner) if m.owner_id else "",
                    "project_code": m.project.code if m.project_id else "", "status": m.status,
                    "status_label": m.get_status_display(), "consent_at": m.consent_at,
                    "announced": m.consent_announced, "all_agreed": m.consent_all_agreed,
                    "consent_lang": m.consent_lang, "transcript": m.has_transcript, "emails": emails})
        if len(out) >= PROTOCOLS_MAX:
            break
    return out


def admin_delete(user, meeting, request=None) -> dict:
    """«🗑 Видалити» зі сторінки «🧾 Протоколи згоди» (запит учасника за ст. 17 DSGVO, власника немає): як
    delete_meeting, але може адміністратор Minerva і без перегляду змісту; у журнал аудиту — те саме без назви."""
    if not _is_admin(user):
        raise Http404("Сторінку не знайдено")
    from core.models import AuditLog
    accepted = meeting.items.filter(status=MeetingItem.ACCEPTED).count()
    pk = meeting.pk
    extra = {"consent_at": _iso(meeting.consent_at), "consent_all_agreed": meeting.consent_all_agreed,
             "accepted_items": accepted, "by_admin": True}
    with transaction.atomic():
        meeting._delete_reason = "admin"
        meeting.delete()
        try:
            AuditLog.log(AuditLog.Action.DELETE, module="meetings", model_name="Meeting", object_id=pk,
                         object_repr=f"🎙 Нарада #{pk}", extra=extra, user=user, request=request)
        except Exception as e:  # noqa: BLE001
            logger.warning("meeting %s audit: %s", pk, e)
    return {"accepted": accepted}


def drop_transcript(m, now=None) -> None:
    """Строк зберігання минув / власника немає: розшифровка, підсумок, позначки, неприйняті пункти, документи бази
    знань — геть; прийняті пункти — без назви й цитати (ключ, стан, куди додано лишаються). Протокол згоди лишається."""
    now = now or timezone.now()
    from . import knowledge
    knowledge.unpublish(m)
    MeetingTranscript.objects.filter(meeting=m).delete()
    m.items.exclude(status=MeetingItem.ACCEPTED).delete()
    m.items.filter(status=MeetingItem.ACCEPTED).update(title="", quote="", who="", due_text="")
    m.marks = [{"t": mk.get("t"), "label": "", "kind": mk.get("kind")} for mk in m.marks or []]
    m.transcript_deleted_at, m.moments_error, m.bench_detail = now, "", ""
    m.save(update_fields=["marks", "transcript_deleted_at", "moments_error", "bench_detail", "updated_at"])


def claim_cleanup(now=None) -> bool:
    """Атомарно: прибирання цього разу робить цей процес (не частіше, ніж раз на 20 год)."""
    now = now or timezone.now()
    MeetingSettings.load()
    return bool(MeetingSettings.objects.filter(pk=1).filter(
        Q(cleanup_at__isnull=True) | Q(cleanup_at__lt=now - CLEANUP_EVERY)).update(cleanup_at=now))


def cleanup(now=None) -> dict:
    """Щоденне прибирання (manage.py meetings_cleanup --if-due)."""
    now = now or timezone.now()
    s = settings_obj()
    out = {"transcripts": 0, "owner_gone": 0, "protocols": 0, "abandoned": 0, "stale": 0, "tombstones": 0,
           "tickets": 0}
    # власника вимкнено — відлік inactive_owner_days; знову активний — відлік знімається
    Meeting.objects.filter(owner__is_active=False, owner_inactive_since__isnull=True).update(owner_inactive_since=now)
    Meeting.objects.filter(owner__is_active=True, owner_inactive_since__isnull=False).update(owner_inactive_since=None)

    for m in Meeting.objects.filter(transcript_deleted_at__isnull=True).select_related("owner"):
        try:
            if owner_gone(m, s, now):
                drop_transcript(m, now)
                out["owner_gone"] += 1
                continue
            days = max(min(m.retention_days or 365, s.transcript_retention_days or 365), MIN_RETENTION_DAYS)
            clock = m.transcript_at or m.ended_at or m.created_at
            if m.status != Meeting.READY and clock and clock < now - timedelta(days=days):
                drop_transcript(m, now)
                out["transcripts"] += 1
        except Exception as e:  # noqa: BLE001 — одна нарада не зупиняє прибирання
            logger.warning("meetings cleanup %s: %s", m.pk, e)

    protocol_days = max(s.protocol_retention_days or 1095, MIN_RETENTION_DAYS)
    for m in Meeting.objects.filter(created_at__lt=now - timedelta(days=protocol_days)):
        m._delete_reason = "retention"
        m.delete()
        out["protocols"] += 1

    for m in Meeting.objects.filter(status=Meeting.READY, created_at__lt=now - timedelta(days=ABANDONED_DAYS),
                                    started_at__isnull=True):
        if not m.consent_log:               # ні разу не записували — протоколу немає
            m._delete_reason = "abandoned"
            m.delete()
            out["abandoned"] += 1

    out["stale"] = sweep_stale(now, s)
    out["tombstones"] = MeetingTombstone.objects.filter(
        created_at__lt=now - timedelta(days=TOMBSTONE_DAYS)).delete()[0]
    out["tickets"] = Meeting.objects.filter(ticket_expires_at__lt=now).exclude(ticket_hash="").update(
        ticket_hash="")
    return out


def _last_news(m):
    return m.bench_updated_at or m.ticket_redeemed_at or m.created_at


def sweep_stale(now=None, settings=None) -> int:
    """Запис / обробка без звісток від RAG-ПК понад STALE_HOURS → «Не вдалося» (з розшифровкою — «Розшифровка
    готова», ключові моменти — ні). Лише коли RAG-ПК щойно виходив на зв'язок (sync), але цього запису вже не
    показує або показує «failed» (його хід sync оновлює bench_updated_at): RAG-ПК вимкнено на вихідні — чекаємо.
    Дешевий запит — cron кличе щоциклу (manage.py meetings_cleanup --if-due), не лише раз на добу."""
    now = now or timezone.now()
    s = settings or settings_obj()
    if not (s.bench_seen_at and s.bench_seen_at >= now - BENCH_RECENT):
        return 0
    limit = now - timedelta(hours=STALE_HOURS)
    n = 0
    for m in Meeting.objects.filter(status__in=(Meeting.RECORDING, Meeting.PROCESSING)).only(
            "pk", "status", "bench_updated_at", "ticket_redeemed_at", "created_at"):
        last = _last_news(m)
        if last and last < limit:
            n += bool(_mark_stale(m, limit))
    return n


def _mark_stale(m, limit=None) -> bool:
    with transaction.atomic():
        m = Meeting.objects.select_for_update(of=("self",)).get(pk=m.pk)
        if m.status not in (Meeting.RECORDING, Meeting.PROCESSING):
            return False
        if limit is not None and _last_news(m) and _last_news(m) >= limit:
            return False                         # поки чекали блокування, RAG-ПК озвався
        if m.transcript_at:
            m.status, m.moments_error, m.bench_detail = Meeting.REVIEW, MOMENTS_FAILED_TEXT, MOMENTS_FAILED_TEXT
            fields = ["status", "moments_error", "bench_detail", "updated_at"]
        else:
            m.status, m.bench_detail = Meeting.FAILED, STALE_TEXT
            fields = ["status", "bench_detail", "updated_at"]
        m.save(update_fields=fields)
        _schedule_notify(m)
    return True


# ── налаштування: що ще треба до першого запису ──────────────────────────────

def bench_keys() -> list:
    """Активні ключі API з правом meetings:write (службовий ключ RAG-ПК)."""
    try:
        from api.models import APIKey
    except ImportError:
        return []
    return [k for k in APIKey.objects.filter(is_active=True).order_by("-created_at")
            if "meetings:write" in (k.scopes or [])]


def rollout_checklist(settings=None) -> list:
    """[{key, ok, label}] — для «⚙️ Налаштування нарад»."""
    s = settings or settings_obj()
    return [
        {"key": "texts", "ok": bool(s.texts_reviewed_at), "label": "Тексти згоди перевірив юрист / DSB"},
        {"key": "privacy", "ok": bool(s.privacy_url), "label": "Посилання на інформацію за ст. 13 DSGVO"},
        {"key": "council", "ok": bool(s.works_council_informed), "label": "Betriebsrat поінформовано"},
        {"key": "key", "ok": bool(bench_keys()), "label": "Ключ для RAG-ПК створено"},
        {"key": "bench", "ok": bool(s.bench_seen_at), "label": "RAG-ПК виходив на зв'язок"},
    ]


def mark_texts_reviewed(settings, user, reviewed: bool):
    """Галочка «тексти перевірив юрист / DSB»: хто й коли (знята — порожньо)."""
    settings.texts_reviewed_at = timezone.now() if reviewed else None
    settings.texts_reviewed_by = user if reviewed else None


def pending_counts(meetings) -> dict:
    """{meeting_pk: скільки пунктів чекає ✓ / ✕} — для списку й пульта (один запит)."""
    ids = [getattr(m, "pk", m) for m in meetings]
    rows = (MeetingItem.objects.filter(meeting_id__in=ids, status=MeetingItem.PENDING).values("meeting_id")
            .annotate(n=Count("pk")))
    return {r["meeting_id"]: r["n"] for r in rows}
