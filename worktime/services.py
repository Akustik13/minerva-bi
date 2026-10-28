"""Робочий час: askDANTE — головний облік, Minerva лише читає дні й дописує туди, де порожньо.

Порядок для кожного запису: свіже читання дня з askDANTE → рішення (propose) → запис (track) → журнал Booking.
Записи askDANTE через API не виправляються й не видаляються, тому:
- заповнення пропусків пише лише в робочі дні без жодного запису й без відсутностей;
- ручний запис і «Пішов» не можуть перекривати наявні записи;
- рішення завжди приймає сервер за свіжими даними, а не браузер;
- одночасно для працівника йде лише один запис (lock — захист від подвійного натискання);
- автозаповнення пропускає дні з попередженнями (їх бачить і вирішує людина в календарі).
"""
from __future__ import annotations

import contextlib
import datetime
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .client import DanteClient, DanteError, hhmm, minutes_between
from .models import (Booking, DanteActivity, DanteProject, DanteSettings, DanteUser, PeriodBalance, TimeRecord,
                     WorkDay)

ABSENCE_LABELS = {
    "WEEKEND": "вихідний", "HOLIDAY": "свято", "PUBLIC_HOLIDAY": "свято", "LEAVE": "відпустка",
    "VACATION": "відпустка", "DEFAULT_LEAVE": "відпустка", "SPECIAL_LEAVE": "спецвідпустка",
    "OVERTIME_REDUCTION": "відгул за переробки", "ILLNESS": "лікарняний", "DISEASE": "лікарняний",
    "SICK": "лікарняний",
}
ABSENCE_ICONS = {"відпустка": "🏖", "спецвідпустка": "🏖", "лікарняний": "🤒", "свято": "🎉",
                 "відгул за переробки": "⏳", "вихідний": ""}
PLANNED_LABELS = {WorkDay.LEAVE: "відпустка", WorkDay.ILLNESS: "лікарняний", WorkDay.NON_BUSINESS: "неробочий день"}
LONG_DAY_MINUTES = 10 * 60      # ArbZG: понад 10 годин — попередження
MAX_CLOCK_HOURS = 16            # «Прийшов» довше — забута відмітка, записувати вручну
LOCK_MINUTES = 10
REPORT_STATUSES = {             # звіт автозаповнення по днях; порядок — як у підсумку
    "sent": "записано", "would": "запишеться", "had": "вже було", "review": "на перевірку",
    "skipped": "пропущено", "failed": "помилка",
}
UNKNOWN_STOP = "askDANTE не відповів — решту днів не перевірено, наступний запуск продовжить."


def client(st: DanteSettings | None = None) -> DanteClient:
    return DanteClient(st or DanteSettings.get())


def local_today() -> datetime.date:
    return timezone.localdate()


def is_manager(user) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    try:
        return user.profile.role in ("superadmin", "admin")
    except Exception:
        return False


def users_for(request_user):
    """Чиї календарі бачить користувач: адміністратор — усіх, інший — лише свій."""
    qs = DanteUser.objects.select_related("user")
    if is_manager(request_user):
        return qs.filter(Q(archived=False) | Q(user__isnull=False))
    return qs.filter(user=request_user)


@contextlib.contextmanager
def lock(du: DanteUser):
    """Один запис в askDANTE для працівника одночасно (атомарний UPDATE, як «бронь» запланованих листів)."""
    now = timezone.now()
    got = DanteUser.objects.filter(pk=du.pk).filter(Q(busy_until__isnull=True) | Q(busy_until__lt=now)) \
        .update(busy_until=now + datetime.timedelta(minutes=LOCK_MINUTES))
    if not got:
        raise DanteError("busy", "Для цього працівника вже йде запис в askDANTE — зачекайте й оновіть сторінку.")
    try:
        yield
    finally:
        DanteUser.objects.filter(pk=du.pk).update(busy_until=None)


# ── довідники ────────────────────────────────────────────────────
def sync_directory(st: DanteSettings | None = None) -> dict:
    """Працівники, проєкти й види робіт з askDANTE. Працівник прив'язується до користувача Minerva за email."""
    st = st or DanteSettings.get()
    c = client(st)
    now = timezone.now()
    users = c.users()
    linked = 0
    User = get_user_model()
    for u in users:
        du, _ = DanteUser.objects.update_or_create(dante_id=u["id"], defaults={
            "first_name": u["first_name"][:100], "last_name": u["last_name"][:100], "email": u["email"][:254],
            "personnel_number": u["personnel_number"][:40], "archived": u["archived"], "synced_at": now})
        if du.user_id is None and u["email"]:
            match = User.objects.filter(email__iexact=u["email"], dante__isnull=True).first()
            if match:
                du.user = match
                du.save(update_fields=["user"])
                linked += 1

    projects = c.projects()
    seen = set()
    for p in projects:
        seen.add(p["id"])
        DanteProject.objects.update_or_create(dante_id=p["id"], defaults={
            "name": p["name"][:255], "reference_number": p["reference_number"][:100],
            "customer_name": p["customer_name"][:255], "customer_ref": p.get("customer_id", "")[:40],
            "archived": p["archived"],
            "time_trackable": p["time_trackable"], "gone": False, "synced_at": now})
    parents = {p.dante_id: p for p in DanteProject.objects.filter(dante_id__in=seen)}
    for p in projects:
        DanteProject.objects.filter(dante_id=p["id"]).update(parent=parents.get(p["parent"]))
    DanteProject.objects.exclude(dante_id__in=seen).update(gone=True)

    activities = c.activities()
    for a in activities:
        DanteActivity.objects.update_or_create(dante_id=a["id"], defaults={"name": a["name"][:255],
                                                                           "archived": a["archived"]})
    st.last_sync_at = now
    st.save(update_fields=["last_sync_at"])
    return {"users": len(users), "linked": linked, "projects": len(projects), "activities": len(activities)}


# ── дні з askDANTE ───────────────────────────────────────────────
class Days(dict):
    """{дата: WorkDay} + чи вдалося прочитати свята й відсутності (без них автоматично не заповнюємо)."""
    attendance_error = ""


def read_plan(du: DanteUser, date_from: datetime.date, date_to: datetime.date, c: DanteClient) -> dict:
    """План днів і відсутності: календар askDANTE (норма, статус «подано / погоджено»), запасний шлях — attendances.
    Порожня відповідь — теж збій: без плану свята й відпустки виглядали б робочими днями."""
    errors = []
    try:
        plan = c.calendar(date_from, date_to, du.dante_id)
        if plan:
            return plan
        errors.append("календар askDANTE не повернув днів")
    except DanteError as e:
        errors.append(e.message)
    if du.email or du.personnel_number:
        try:
            plan = c.attendances(date_from, date_to, email=du.email, personnel_number=du.personnel_number)
            if plan:
                return plan
            errors.append("відсутності askDANTE порожні")
        except DanteError as e:
            errors.append(e.message)
    else:
        errors.append("у працівника askDANTE немає логіна й табельного номера")
    raise DanteError("unavailable", "; ".join(dict.fromkeys(errors)))


def refresh_days(du: DanteUser, date_from: datetime.date, date_to: datetime.date,
                 c: DanteClient | None = None) -> Days:
    """Свіже читання днів з askDANTE → кеш WorkDay."""
    c = c or client()
    times = c.working_time(date_from, date_to, du.dante_id)
    out = Days()
    try:
        att = read_plan(du, date_from, date_to, c)
    except DanteError as e:
        att, out.attendance_error = {}, e.message
    now = timezone.now()
    day = date_from
    while day <= date_to:
        a = att.get(day)
        t = times.get(day, [])
        defaults = {"times": t, "minutes": sum(x["minutes"] for x in t), "fetched_at": now}
        if a is not None or not out.attendance_error:
            defaults.update(planned_state=(a or {}).get("planned", ""), absences=(a or {}).get("absences", []))
            if (a or {}).get("target") is not None:
                defaults["target_minutes"] = a["target"]
        wd, _ = WorkDay.objects.update_or_create(dante_user=du, date=day, defaults=defaults)
        out[day] = wd
        day += datetime.timedelta(days=1)
    return out


def cached_days(du: DanteUser, date_from, date_to) -> dict[datetime.date, WorkDay]:
    return {wd.date: wd for wd in WorkDay.objects.filter(dante_user=du, date__range=(date_from, date_to))}


# ── активність у Minerva ─────────────────────────────────────────
def activity_span(user, day: datetime.date):
    """(перша, остання дія дня, кількість) за журналом дій і надісланими листами користувача; None — немає."""
    if user is None:
        return None
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min), tz)
    end = start + datetime.timedelta(days=1)
    from core.models import AuditLog
    stamps = list(AuditLog.objects.filter(user=user, timestamp__gte=start, timestamp__lt=end)
                  .values_list("timestamp", flat=True))
    try:
        from email_assistant.models import EmailMessage
        stamps += list(EmailMessage.objects.filter(account__user=user, folder=EmailMessage.FOLDER_SENT,
                                                   sent_at__gte=start, sent_at__lt=end)
                       .values_list("sent_at", flat=True))
    except Exception:   # пошта не встановлена — лише журнал дій
        pass
    stamps = [timezone.localtime(s, tz) for s in stamps if s]
    if not stamps:
        return None
    return min(stamps), max(stamps), len(stamps)


def activity_log(user, day: datetime.date, limit: int = 80) -> list[dict]:
    """👣 Що користувач робив у Minerva за день: журнал дій (вхід, створення, зміни) і надіслані листи — по часу."""
    if user is None:
        return []
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min), tz)
    end = start + datetime.timedelta(days=1)
    from core.models import AuditLog
    out = []
    for a in (AuditLog.objects.filter(user=user, timestamp__gte=start, timestamp__lt=end)
              .select_related("content_type").order_by("timestamp")[:limit]):
        what = a.content_type.model_class()._meta.verbose_name if a.content_type and a.content_type.model_class() \
            else ""
        out.append({"at": timezone.localtime(a.timestamp, tz), "icon": "🖊", "kind": a.get_action_display(),
                    "text": " · ".join(x for x in (str(what), a.object_repr) if x)})
    try:
        from email_assistant.models import EmailMessage
        for m in (EmailMessage.objects.filter(account__user=user, folder=EmailMessage.FOLDER_SENT,
                                              sent_at__gte=start, sent_at__lt=end).order_by("sent_at")[:limit]):
            out.append({"at": timezone.localtime(m.sent_at, tz), "icon": "📤", "kind": "лист",
                        "text": m.subject or "(без теми)"})
    except Exception:  # noqa: BLE001 — пошта не встановлена
        pass
    out.sort(key=lambda x: x["at"])
    return out[:limit]


def _round(t: datetime.datetime, step: int, up: bool) -> datetime.time:
    step = max(1, step or 1)
    minutes = t.hour * 60 + t.minute + (1 if up and (t.second or t.microsecond) else 0)
    minutes = (-(-minutes // step) if up else minutes // step) * step
    minutes = min(minutes, 24 * 60 - 1)
    return datetime.time(minutes // 60, minutes % 60)


def _span(start: str, stop: str | None) -> tuple[int, int]:
    """'HH:MM' → хвилини від початку доби; кінець 00:00 або відкритий запис — 24:00."""
    h, m = map(int, start.split(":")[:2])
    a = h * 60 + m
    if not stop:
        return a, 24 * 60
    h, m = map(int, stop.split(":")[:2])
    b = h * 60 + m
    return a, (b if b > a else 24 * 60)


def overlap(wd: WorkDay, start: datetime.time, stop: datetime.time) -> str:
    """Наявний запис askDANTE, який перекривається з новим інтервалом; '' — немає."""
    a, b = _span(hhmm(start), hhmm(stop))
    for t in wd.times or []:
        if not t.get("coming"):
            continue
        c, d = _span(t["coming"], t.get("leaving"))
        if a < d and c < b:
            return f"{t['coming']}–{t.get('leaving') or '…'}"
    return ""


# ── рішення для дня ──────────────────────────────────────────────
@dataclass
class Proposal:
    date: datetime.date
    book: bool
    start: datetime.time | None = None
    stop: datetime.time | None = None
    reason: str = ""
    warnings: list = field(default_factory=list)
    existing: list = field(default_factory=list)   # записи askDANTE, що вже є в дні

    @property
    def minutes(self) -> int:
        return minutes_between(hhmm(self.start), hhmm(self.stop)) if self.start and self.stop else 0


def absence_label(a: dict) -> str:
    """«відпустка», «лікарняний ½», «AWH (подано)» — тип askDANTE, власна назва, частина дня, статус."""
    kind = (a.get("type") or "").upper()
    label = (a.get("name") or "").strip() if kind == "CUSTOM" or kind not in ABSENCE_LABELS else ""
    label = label or ABSENCE_LABELS.get(kind) or (kind or "відсутність").lower()
    if float(a.get("ratio") if a.get("ratio") is not None else 1) < 1:
        label += " ½" if float(a["ratio"]) == 0.5 else f" {float(a['ratio']):.0%}"
    if (a.get("state") or "").upper() == "SUBMITTED":
        label += " (подано)"
    return label


def absences_text(wd: WorkDay | None) -> str:
    """Відсутності дня для календарів, без вихідних: «🏖 відпустка (подано)»."""
    out = []
    for a in (wd.absences if wd else None) or []:
        label = absence_label(a)
        if label.startswith("вихідний"):
            continue
        icon = next((v for k, v in ABSENCE_ICONS.items() if label.startswith(k)), "📌")
        out.append(f"{icon} {label}".strip())
    return ", ".join(out)


def day_blocker(st: DanteSettings, wd: WorkDay | None, day: datetime.date) -> str:
    """Чому в день не можна автоматично ставити прихід / відхід; '' — можна."""
    if wd and wd.times:
        return "вже є: " + ", ".join(f"{t['coming']}–{t.get('leaving') or '…'}" for t in wd.times)
    planned = wd.planned_state if wd else ""
    absences = (wd.absences if wd else None) or []
    labels = [absence_label(a) for a in absences]
    if planned in PLANNED_LABELS:
        return labels[0] if labels else PLANNED_LABELS[planned]
    if absences:
        partial = any(float(a.get("ratio") or 1) < 1 for a in absences)
        return f"{labels[0]} — частина дня, заповніть вручну" if partial else labels[0]
    if not st.is_workday(day):
        return "вихідний"
    return ""


def propose(st: DanteSettings, du: DanteUser, days: dict, date_from, date_to, mode: str,
            start: datetime.time | None = None, stop: datetime.time | None = None,
            fallback: bool | None = None) -> list[Proposal]:
    """Що поставити в кожен день періоду. days — свіжі WorkDay з askDANTE (refresh_days)."""
    fallback = st.activity_fallback if fallback is None else fallback
    def_start, def_stop = du.times(st)
    start, stop = start or def_start, stop or def_stop
    att_error = getattr(days, "attendance_error", "")
    now = timezone.localtime()
    out = []
    day = date_from
    while day <= date_to:
        wd = days.get(day)
        blocker = day_blocker(st, wd, day)
        p = Proposal(date=day, book=False, existing=list(wd.times) if wd and wd.times else [])
        if day > now.date():
            p.reason = "майбутній день"
        elif blocker:
            p.reason = blocker
        elif mode == DanteSettings.MODE_ACTIVITY:
            span = activity_span(du.user, day)
            if span and (span[1] - span[0]).total_seconds() / 60 >= st.min_activity_minutes:
                p.book, p.start = True, _round(span[0], st.round_minutes, up=False)
                p.stop = _round(span[1], st.round_minutes, up=True)
                p.reason = f"активність у Minerva: {span[2]} дій"
            elif fallback:
                p.book, p.start, p.stop = True, start, stop
                p.reason = "без активності в Minerva — стандартний день"
            elif du.user_id is None:
                p.reason = "працівник не прив'язаний до користувача Minerva"
            else:
                p.reason = "немає активності в Minerva" if not span else "замало активності в Minerva"
        else:
            p.book, p.start, p.stop, p.reason = True, start, stop, "стандартний день"
        if p.book and p.minutes <= 0:
            p.book, p.reason = False, "відхід раніше за прихід"
        if p.book and day == now.date() and p.stop > now.time():
            p.book, p.reason = False, f"сьогодні ще до {hhmm(p.stop)}"
        if p.book and p.minutes > LONG_DAY_MINUTES:
            p.warnings.append("понад 10 годин")
        if p.book and att_error:
            p.warnings.append("свята й відпустки askDANTE не прочитано")
        out.append(p)
        day += datetime.timedelta(days=1)
    return out


def report_row(p: Proposal, status: str = "", error: str = "") -> dict:
    """Рядок звіту: що з днем зроблено (або зробилось би) і чому."""
    if not status:
        status = "had" if p.existing else "skipped" if not p.book else "review" if p.warnings else "would"
    span = f"{hhmm(p.start)}–{hhmm(p.stop)}" if p.start and p.stop else ""
    if status == "had":
        text = ", ".join(f"{t['coming']}–{t.get('leaving') or '…'}" for t in p.existing)
        if any(not t.get("leaving") for t in p.existing):
            text += " · немає відходу — допишіть в askDANTE"
    elif status == "skipped":
        text = p.reason
    elif status == "review":
        text = f"{span} · {'; '.join(p.warnings)} — перевірте й запишіть у календарі"
    else:
        text = f"{span} · {error or p.reason}"
    return {"date": p.date.isoformat(), "status": status, "text": text}


# ── запис ────────────────────────────────────────────────────────
def _send(c: DanteClient, du: DanteUser, day: datetime.date, start: datetime.time, stop: datetime.time,
          source: str, user=None, reason: str = "") -> Booking:
    record = {"date": day.isoformat(), "start": hhmm(start), "stop": hhmm(stop)}
    booking = Booking(dante_user=du, date=day, kind=Booking.WORK, source=source, start=start, stop=stop,
                      duration=minutes_between(record["start"], record["stop"]), reason=reason[:255],
                      created_by=user if getattr(user, "pk", None) else None)
    try:
        c.track(du.dante_id, record)
        booking.status = Booking.SENT
    except DanteError as e:
        booking.status, booking.error = Booking.FAILED, e.message
    booking.save()
    if booking.status == Booking.SENT:   # кеш дня — одразу з новим записом (наступне читання перезапише)
        wd, _ = WorkDay.objects.get_or_create(dante_user=du, date=day)
        wd.times = sorted((wd.times or []) + [{"coming": record["start"], "leaving": record["stop"],
                                               "source": "MINERVA", "minutes": booking.duration}],
                          key=lambda t: t["coming"])
        wd.minutes = sum(t.get("minutes") or 0 for t in wd.times)
        wd.save(update_fields=["times", "minutes"])
    return booking


def fill(du: DanteUser, date_from, date_to, mode: str, start=None, stop=None, fallback=None,
         only_dates=None, user=None, source: str = Booking.FILL, st: DanteSettings | None = None) -> dict:
    """Заповнити порожні робочі дні: свіже читання askDANTE → propose → запис.

    only_dates — дні, які людина підтвердила в перегляді (тоді й дні з попередженнями — людина їх бачила);
    рішення все одно перераховується за свіжими даними: день, що тим часом з'явився в askDANTE, пропускається.
    Без only_dates (автозаповнення) дні з попередженнями пропускаються.
    """
    st = st or DanteSettings.get()
    c = client(st)
    result = {"sent": [], "failed": [], "skipped": [], "days": [], "stopped": ""}
    with lock(du):
        days = refresh_days(du, date_from, date_to, c)
        for p in propose(st, du, days, date_from, date_to, mode, start, stop, fallback):
            if only_dates is not None and p.date not in only_dates:
                continue
            if not p.book or (only_dates is None and p.warnings):
                if only_dates is not None or p.warnings:
                    result["skipped"].append((p.date, p.reason if not p.book else "; ".join(p.warnings)))
                result["days"].append(report_row(p))
                continue
            b = _send(c, du, p.date, p.start, p.stop, source, user, p.reason)
            sent = b.status == Booking.SENT
            (result["sent"] if sent else result["failed"]).append((p.date, b.error or p.reason))
            result["days"].append(report_row(p, "sent" if sent else "failed", b.error))
            if not sent and b.error and "невідомо" in b.error:
                result["stopped"] = UNKNOWN_STOP   # не продовжувати наосліп; наступне читання покаже, що записано
                break
    return result


def book_day(du: DanteUser, day: datetime.date, start: datetime.time, stop: datetime.time, user=None,
             st: DanteSettings | None = None) -> Booking:
    """Ручний запис одного інтервалу дня. Не перекриває наявні записи askDANTE (свіже читання дня)."""
    st = st or DanteSettings.get()
    now = timezone.localtime()
    if day > now.date() or (day == now.date() and stop > now.time()):
        raise DanteError("bad_request", "Час, який ще не настав, записувати не можна.")
    if stop != datetime.time(0, 0) and stop <= start:
        raise DanteError("bad_request", "Відхід має бути пізніше за прихід (той самий день; 00:00 — кінець доби).")
    c = client(st)
    with lock(du):
        wd = refresh_days(du, day, day, c)[day]
        busy = overlap(wd, start, stop)
        if busy:
            raise DanteError("bad_request", f"Перекривається з наявним записом {busy} в askDANTE.")
        return _send(c, du, day, start, stop, Booking.MANUAL, user, "вручну в календарі")


# ── «Прийшов» / «Пішов» ──────────────────────────────────────────
def clock_in(du: DanteUser) -> datetime.datetime:
    """Запам'ятати прихід (askDANTE приймає лише готовий запис — він піде по «Пішов»)."""
    if du.open_since:
        return du.open_since
    today = local_today()
    wd = refresh_days(du, today, today)[today]
    opened = [t for t in wd.times if t.get("coming") and not t.get("leaving")]
    if opened:
        raise DanteError("bad_request", f"В askDANTE ви вже відмічені з {opened[0]['coming']} (додаток або термінал) "
                                        "— «Пішов» відмітьте там же.")
    du.open_since = timezone.localtime().replace(second=0, microsecond=0)
    du.save(update_fields=["open_since"])
    return du.open_since


def clock_cancel(du: DanteUser) -> None:
    du.open_since = None
    du.save(update_fields=["open_since"])


def clock_out(du: DanteUser, user=None) -> list[Booking]:
    """«Пішов»: запис від «Прийшов» до зараз; через північ — два записи. Після кожного успішного «Прийшов» зсувається,
    тож повтор після збою не задвоює вже записану частину."""
    if not du.open_since:
        raise DanteError("bad_request", "Спершу відмітьте «Прийшов».")
    since = timezone.localtime(du.open_since)
    now = timezone.localtime().replace(second=0, microsecond=0)
    if now - since > datetime.timedelta(hours=MAX_CLOCK_HOURS):
        raise DanteError("bad_request", f"«Прийшов» відмічено {since:%d.%m %H:%M} — понад {MAX_CLOCK_HOURS} годин "
                                        "тому. Скасуйте відмітку й запишіть день вручну в календарі.")
    if now <= since:
        raise DanteError("bad_request", "Минуло менше хвилини.")
    midnight = timezone.make_aware(datetime.datetime.combine(now.date(), datetime.time.min), now.tzinfo)
    parts = []
    if since.date() < now.date():
        parts.append((since.date(), since, midnight))
        if now > midnight:
            parts.append((now.date(), midnight, now))
    else:
        parts.append((now.date(), since, now))
    c = client()
    bookings = []
    with lock(du):
        days = refresh_days(du, since.date(), now.date(), c)
        for day, a, b in parts:
            stop = b.time() if b != midnight else datetime.time(0, 0)
            busy = overlap(days[day], a.time(), stop)
            if busy:
                raise DanteError("bad_request", f"Перекривається з наявним записом {busy} в askDANTE — "
                                                "перевірте день у календарі.")
            booking = _send(c, du, day, a.time(), stop, Booking.CLOCK, user, "«Прийшов» / «Пішов» у Minerva")
            bookings.append(booking)
            if booking.status != Booking.SENT:
                break
            du.open_since = b if b < now else None
            du.save(update_fields=["open_since"])
    return bookings


# ── автозаповнення (cron) ────────────────────────────────────────
def autofill_due(st: DanteSettings, now=None) -> bool:
    now = timezone.localtime(now) if now else timezone.localtime()
    if not st.enabled or not st.configured or st.autofill_mode == DanteSettings.MODE_OFF:
        return False
    if now.hour < st.autofill_hour:
        return False
    return not st.last_autofill_at or timezone.localtime(st.last_autofill_at).date() < now.date()


CRON_STALE_MINUTES = 15
CRON_RESTART_HINT = ("Перезапустіть контейнер cron на NAS: DSM → Container Manager → «Контейнер» → tabele_mvp-cron-1 → "
                     "«Дія» → «Перезапустити» (або SSH у теці проєкту: docker-compose restart cron). Консоль команд "
                     "Minerva і «Повне оновлення» цього не вміють — вони перезапускають лише веб-частину.")


def autofill_schedule(st: DanteSettings, now=None) -> dict:
    """Чи спрацює щоденний запуск і коли: {"next": текст, "warning": текст або ''}."""
    now = timezone.localtime(now) if now else timezone.localtime()
    if not st.enabled or not st.configured:
        return {"next": "вимкнено (askDANTE не увімкнено або немає ключа)", "warning": ""}
    if st.autofill_mode == DanteSettings.MODE_OFF:
        return {"next": "вимкнено (режим «Вимкнено»)", "warning": ""}
    done_today = st.last_autofill_at and timezone.localtime(st.last_autofill_at).date() >= now.date()
    if done_today:
        nxt = f"завтра після {st.autofill_hour:02d}:00"
    elif now.hour < st.autofill_hour:
        nxt = f"сьогодні після {st.autofill_hour:02d}:00"
    else:
        nxt = "зараз — протягом хвилини, коли cron перевірить"
    warning = ""
    if not st.cron_seen_at:
        warning = ("Контейнер cron ще жодного разу не викликав автозаповнення — щоденний запуск не працює. "
                   + CRON_RESTART_HINT + " Потрібно один раз: далі після оновлень cron перезапускається сам.")
    elif now - timezone.localtime(st.cron_seen_at) > datetime.timedelta(minutes=CRON_STALE_MINUTES):
        warning = (f"Контейнер cron не звертався з {timezone.localtime(st.cron_seen_at):%d.%m.%Y %H:%M} — "
                   "щоденні запуски стоять (контейнер зупинено або він завис). " + CRON_RESTART_HINT
                   + " Журнал — там само, вкладка «Журнал» контейнера.")
    return {"next": nxt, "warning": warning}


def autofill_range(st: DanteSettings):
    today = local_today()
    date_from = today - datetime.timedelta(days=max(1, st.autofill_lookback_days))
    if st.autofill_since:
        date_from = max(date_from, st.autofill_since)
    return date_from, today - datetime.timedelta(days=1)


def autofill(st: DanteSettings | None = None, dry_run: bool = False, manual: bool = False) -> str:
    """Порожні робочі дні за останні N днів (не раніше autofill_since) до вчора — для працівників з автозаповненням.
    Повертає підсумок одним рядком; звіт по днях — autofill_report."""
    return autofill_report(st, dry_run, manual)["summary"]


def autofill_report(st: DanteSettings | None = None, dry_run: bool = False, manual: bool = False) -> dict:
    """Автозаповнення зі звітом по кожному дню кожного працівника: записано / вже було в askDANTE / на перевірку /
    пропущено (вихідний, відпустка, свято, без активності…) / помилка. Не dry_run — звіт зберігається в налаштуваннях
    (`last_autofill_report`): і щоденний запуск, і «Заповнити зараз» (manual)."""
    st = st or DanteSettings.get()
    date_from, date_to = autofill_range(st)
    if not st.last_sync_at or timezone.now() - st.last_sync_at > datetime.timedelta(hours=20):
        try:
            sync_directory(st)
        except DanteError:
            pass   # довідник — не головне; читання днів нижче покаже, чи є зв'язок
    people = list(DanteUser.objects.filter(autofill=True, archived=False).select_related("user"))
    report = {"at": timezone.now().isoformat(), "manual": manual, "dry_run": dry_run, "mode": st.autofill_mode,
              "from": date_from.isoformat(), "to": date_to.isoformat(), "people": len(people), "users": [],
              "counts": dict.fromkeys(REPORT_STATUSES, 0)}
    for du in people if date_from <= date_to else []:
        entry = {"id": du.pk, "name": str(du), "days": [], "error": ""}
        try:
            if dry_run:
                days = refresh_days(du, date_from, date_to)
                entry["days"] = [report_row(p) for p in propose(st, du, days, date_from, date_to, st.autofill_mode)]
            else:
                r = fill(du, date_from, date_to, st.autofill_mode, source=Booking.AUTO, st=st)
                entry["days"], entry["error"] = r["days"], r["stopped"]
        except DanteError as e:
            entry["error"] = e.message
        for row in entry["days"]:
            report["counts"][row["status"]] += 1
        report["users"].append(entry)
    report["summary"] = _summary(report)
    if not dry_run:
        st.last_autofill_at, st.last_autofill_result = timezone.now(), report["summary"][:255]
        st.last_autofill_report = report
        st.save(update_fields=["last_autofill_at", "last_autofill_result", "last_autofill_report"])
    return report


def _summary(report: dict) -> str:
    if not report["people"]:
        return "немає працівників з галочкою «Автозаповнення» (👥 Працівники askDANTE)"
    if report["from"] > report["to"]:
        return "немає днів для перевірки — дата «Не раніше» ще не минула"
    n = report["counts"]
    parts = [f"перевірка, записалось би днів: {n['would']}" if report["dry_run"] else f"записано днів: {n['sent']}"]
    parts += [f"{label}: {n[key]}" for key, label in (("had", "вже було в askDANTE"),
                                                     ("review", "на перевірку в календарі"),
                                                     ("skipped", "пропущено"), ("failed", "помилок")) if n[key]]
    parts += [f"{u['name']}: {u['error']}" for u in report["users"] if u["error"]]
    return " · ".join(parts)


# ── записи часу askDANTE: години на проєктах і клієнтах ──────────
def sync_records(st: DanteSettings | None = None, date_from=None, date_to=None, c: DanteClient | None = None) -> dict:
    """Записи часу askDANTE за період → TimeRecord (копія для звітів). Чого askDANTE більше не повертає — видаляється:
    головне — askDANTE, правки там після наступного оновлення видно й у Minerva."""
    st = st or DanteSettings.get()
    today = local_today()
    date_to = date_to or today
    date_from = date_from or today - datetime.timedelta(days=max(1, st.records_days))
    records = (c or client(st)).time_records(date_from, date_to)
    users = {u.dante_id: u for u in DanteUser.objects.all()}
    projects = {p.dante_id: p for p in DanteProject.objects.all()}
    seen = set()
    for r in records:
        seen.add(r["id"])
        TimeRecord.objects.update_or_create(dante_id=r["id"], defaults={
            "dante_user": users.get(r["person_id"]), "person_id": r["person_id"][:40], "day": r["day"],
            "duration": max(0, r["duration"]), "start": r["start"], "stop": r["stop"],
            "dante_project": projects.get(r["item_id"]) or projects.get(r["project_id"]),
            "project_name": (r["item_name"] or r["project_name"])[:255], "activity_name": r["activity_name"][:255],
            "description": r["description"], "hourly_rate": r["hourly_rate"]})
    removed = TimeRecord.objects.filter(day__range=(date_from, date_to)).exclude(dante_id__in=seen).delete()[0]
    if date_to - date_from >= datetime.timedelta(days=7):   # не після кожного запису одного дня
        st.records_synced_at = timezone.now()
        st.save(update_fields=["records_synced_at"])
    return {"records": len(records), "removed": removed, "from": date_from, "to": date_to}


def _tree(dante_projects) -> list[int]:
    """pk проєктів askDANTE з усіма підпроєктами."""
    ids, todo = set(), [p.pk for p in dante_projects]
    while todo:
        ids.update(todo)
        todo = [pk for pk in DanteProject.objects.filter(parent_id__in=todo).values_list("pk", flat=True)
                if pk not in ids]
    return sorted(ids)


def hours(dante_projects) -> dict | None:
    """Години на проєктах askDANTE (з підпроєктами) з копії записів: усього, по працівниках, по місяцях, останні."""
    from django.db.models import Sum
    from django.db.models.functions import TruncMonth
    ids = _tree(dante_projects)
    if not ids:
        return None
    qs = TimeRecord.objects.filter(dante_project_id__in=ids)
    total = qs.aggregate(m=Sum("duration"))["m"] or 0
    people = [(row["dante_user__first_name"] or row["person_id"], row["m"]) for row in
              qs.values("dante_user__first_name", "person_id").annotate(m=Sum("duration")).order_by("-m")]
    months = [(row["month"], row["m"]) for row in
              qs.annotate(month=TruncMonth("day")).values("month").annotate(m=Sum("duration")).order_by("-month")[:6]]
    cost = sum((r.hourly_rate or 0) * r.duration for r in qs.exclude(hourly_rate=None).only("hourly_rate", "duration"))
    st = DanteSettings.get()
    return {"total": total, "people": people, "months": months, "recent": list(qs.select_related("dante_user")[:8]),
            "cost": round(cost / 60, 2) if cost else None, "projects": list(DanteProject.objects.filter(pk__in=ids)),
            "synced_at": st.records_synced_at, "days": st.records_days}


def project_hours(project) -> dict | None:
    """Години проєкту Minerva — з проєкту askDANTE, з яким він зв'язаний (ProjectMap)."""
    from .models import ProjectMap
    m = ProjectMap.objects.filter(project=project).select_related("dante_project").first() if project.pk else None
    if m is None:
        return None
    data = hours([m.dante_project])
    if data is not None:
        data["shared"] = m.dante_project.maps.exclude(project=project).count()
    return data


def customer_hours(customer) -> dict | None:
    """Години клієнта CRM: проєкти askDANTE, прив'язані до клієнта, і проєкти askDANTE його проєктів Minerva."""
    from .models import ProjectMap
    dps = set(DanteProject.objects.filter(crm_customer=customer))
    dps |= {m.dante_project for m in
            ProjectMap.objects.filter(project__customer=customer).select_related("dante_project")}
    return hours(dps) if dps else None


def balance(du: DanteUser, year: int, month: int, c: DanteClient | None = None, max_age_minutes: int = 60):
    """Місячний підсумок askDANTE (кеш на годину; askDANTE віддає всіх працівників одразу — зберігаються всі)."""
    period = f"{year:04d}-{month:02d}"
    pb = PeriodBalance.objects.filter(dante_user=du, period=period).first()
    if pb and pb.fetched_at > timezone.now() - datetime.timedelta(minutes=max_age_minutes):
        return pb
    try:
        rows = (c or client()).performance_period(f"{month:02d}-{year:04d}")
    except DanteError as e:
        PeriodBalance.objects.update_or_create(dante_user=du, period=period, defaults={"error": e.message[:255]})
        return PeriodBalance.objects.get(dante_user=du, period=period)
    for other in DanteUser.objects.filter(dante_id__in=list(rows)):
        PeriodBalance.objects.update_or_create(dante_user=other, period=period,
                                               defaults={"data": rows[other.dante_id], "error": ""})
    if du.dante_id not in rows:
        PeriodBalance.objects.update_or_create(dante_user=du, period=period, defaults={
            "data": {}, "error": "askDANTE не повернув підсумок цього працівника за місяць"})
    return PeriodBalance.objects.get(dante_user=du, period=period)


def balance_lines(pb) -> list[tuple[str, str]]:
    """Підсумок місяця для показу: [(назва, значення)]; хвилини — як 160:00."""
    d = (pb.data if pb else None) or {}

    def hm(key):
        v = d.get(key)
        if not isinstance(v, (int, float)):
            return None
        sign, v = ("−" if v < 0 else ""), abs(int(round(v)))
        return f"{sign}{v // 60}:{v % 60:02d}"

    def days(key):
        v = d.get(key)
        return f"{v:g}" if isinstance(v, (int, float)) else None

    rows = [("Норма", hm("targetTime")), ("Відпрацьовано", hm("workingTime")),
            ("Переробки за місяць", hm("overTime")), ("Баланс переробок", hm("overTimeTotal")),
            ("Відпустка: залишок, днів", days("remainingLeave")), ("Відпустка цього місяця, днів", days("leaveDays")),
            ("Лікарняні цього місяця, днів", days("diseaseDays")), ("Лікарняні з початку року", days("diseaseDaysTotal"))]
    return [(k, v) for k, v in rows if v is not None]


# ── «мої проєкти» і швидкий запис часу на проєкт ─────────────────
NAME_MAX = 50   # askDANTE: «name: Eine Eingabe bis 50 Zeichen ist zulässig»


def fit_name(text: str, limit: int = NAME_MAX) -> str:
    """Підказка назви нового проєкту askDANTE: не довше `limit`, обрізано по слову з «…»."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" -–,.:;") + "…"


def check_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise DanteError("bad_request", "Вкажіть назву проєкту askDANTE.")
    if len(name) > NAME_MAX:
        raise DanteError("bad_request", f"Назва проєкту askDANTE — не довше {NAME_MAX} символів (зараз {len(name)}): "
                                        f"«{name}». Скоротіть.")
    return name


def create_dante(name: str, parent: DanteProject | None = None, st: DanteSettings | None = None,
                 customer: tuple | None = None) -> DanteProject:
    """Новий проєкт askDANTE (parent=None; customer=(id, назва) — проєкт цього клієнта askDANTE, напр.
    «000 :: Eigene Projekte …») або підпроєкт під `parent` — одразу і в довіднику Minerva.
    Такий самий підпроєкт / проєкт уже є — повертається наявний (без дубля)."""
    st = st or DanteSettings.get()
    name = check_name(name)
    same = DanteProject.objects.filter(name__iexact=name, parent=parent, gone=False)
    if parent is None and customer:
        same = same.filter(customer_ref=customer[0])
    if same.first():
        return same.first()
    c = client(st)
    if parent is None:
        made = c.create_project(name, customer_id=customer[0] if customer else "")
    else:
        made = c.create_project_item(name, parent.dante_id, parent_is_item=bool(parent.parent_id))
    if not made.get("id"):
        raise DanteError("unavailable", "askDANTE не повернув ID нового проєкту — оновіть довідник і перевірте.")
    return DanteProject.objects.create(dante_id=made["id"], name=made["name"][:255], parent=parent,
                                       customer_ref=customer[0] if customer and not parent else "",
                                       customer_name=(customer[1] if customer and not parent else "")[:255])


def dante_customers() -> list:
    """[(id, назва)] клієнтів askDANTE з довідника проєктів — для «новий проєкт у клієнта»."""
    seen = {}
    for ref, name in (DanteProject.objects.filter(gone=False).exclude(customer_ref="")
                      .values_list("customer_ref", "customer_name").order_by("customer_name")):
        seen.setdefault(ref, name or f"#{ref}")
    return list(seen.items())


def create_target(target: str, name: str, st: DanteSettings | None = None):
    """Вибір «або новий»: «p:<pk>» — підпроєкт під проєктом, «c:<id клієнта>» — новий проєкт клієнта askDANTE.
    → DanteProject або None, якщо нічого не вибрано."""
    kind, _, value = (target or "").partition(":")
    if kind == "p" and value.isdigit():
        parent = DanteProject.objects.filter(pk=int(value), gone=False).first()
        return create_dante(name, parent, st) if parent else None
    if kind == "c" and value:
        names = dict(dante_customers())
        if value not in names:
            raise DanteError("not_found", "Клієнта askDANTE не знайдено — оновіть довідник askDANTE.")
        return create_dante(name, None, st, customer=(value, names[value]))
    return None


def my_projects(user):
    """☆ проєкти askDANTE користувача, у які можна писати час."""
    return (DanteProject.objects.filter(pinned_by=user, archived=False, gone=False, time_trackable=True)
            .select_related("parent").order_by("name"))


def toggle_pin(user, dp: DanteProject) -> bool:
    if dp.pinned_by.filter(pk=user.pk).exists():
        dp.pinned_by.remove(user)
        return False
    dp.pinned_by.add(user)
    return True


def project_overlap(du: DanteUser, day: datetime.date, start: datetime.time, stop: datetime.time) -> str:
    """Запис часу на проєкт askDANTE того самого дня, що перекривається з «з … по …» (з копії записів)."""
    a, b = _span(hhmm(start), hhmm(stop))
    for r in TimeRecord.objects.filter(dante_user=du, day=day).exclude(start="").exclude(stop=""):
        c, d = _span(r.start, r.stop)
        if a < d and c < b:
            return f"{r.start}–{r.stop} {r.project_name}".strip()
    return ""


ACTIVITY_MISSING = ("askDANTE вимагає вид роботи (Tätigkeit). Виберіть «Вид роботи за замовчуванням» у ⚙️ Налаштуваннях "
                    "askDANTE (блок «📁 Час на проєкти») — або вид роботи в категорії листів чи на сторінці.")


def _minutes(t: datetime.time) -> int:
    return t.hour * 60 + t.minute


def _clock_of(m: int) -> datetime.time:
    return datetime.time(0, 0) if m >= 24 * 60 else datetime.time(m // 60, m % 60)


def find_slot(du: DanteUser, day: datetime.date, minutes: int, prefer: datetime.time | None = None,
              st: DanteSettings | None = None) -> tuple[datetime.time, datetime.time]:
    """Вільний відрізок «з … по …» на `minutes` для часу на проєкт, якщо askDANTE не приймає тривалість.
    У межах приходу–відходу дня (якщо записані), інакше від свого / стандартного приходу до кінця доби; не
    перекривається з іншими записами на проєкти (копія записів askDANTE); сьогодні — не пізніше, ніж зараз.
    Спершу — від `prefer` (напр. час першого листа), далі — з початку вікна; крок 5 хв."""
    st = st or DanteSettings.get()
    step = 5
    wd = WorkDay.objects.filter(dante_user=du, date=day).first()
    windows = [_span(t["coming"], t.get("leaving")) for t in (wd.times if wd else None) or [] if t.get("coming")]
    if not windows:
        windows = [(_minutes(du.times(st)[0]), 24 * 60)]
    limit = 24 * 60
    if day == local_today():
        now = timezone.localtime()
        limit = (now.hour * 60 + now.minute) // step * step
    busy = sorted(_span(r.start, r.stop) for r in TimeRecord.objects.filter(dante_user=du, day=day)
                  .exclude(start="").exclude(stop=""))

    def fit(s, end):
        s = -(-s // step) * step
        while s + minutes <= min(end, limit):
            clash = next(((a, b) for a, b in busy if a < s + minutes and s < b), None)
            if clash is None:
                return s
            s = -(-clash[1] // step) * step
        return None

    want = _minutes(prefer) // step * step if prefer else None
    for a, b in sorted(windows):
        if want is not None and want < b:
            s = fit(max(a, want), b)
            if s is not None:
                return _clock_of(s), _clock_of(s + minutes)
    for a, b in sorted(windows):
        s = fit(a, b)
        if s is not None:
            return _clock_of(s), _clock_of(s + minutes)
    raise DanteError("bad_request", f"askDANTE не приймає тривалість, а {day:%d.%m} немає вільних {minutes} хв поза "
                                    "іншими записами на проєкти (у межах приходу–відходу) — запишіть «з … по …» вручну.")


def _pick_activity(st: DanteSettings):
    """Вид роботи, коли askDANTE його вимагає, а не вибрано: за замовчуванням або єдиний наявний."""
    if st.default_activity_id:
        return st.default_activity
    active = list(DanteActivity.objects.filter(archived=False)[:2])
    return active[0] if len(active) == 1 else None


NO_WORKTIME = ("У askDANTE за {day:%d.%m} немає робочого часу (прихід / відхід), а askDANTE приймає час на проєкт "
               "лише в його межах. Спершу внесіть прихід і відхід (📅 клік на дату → «👣 З активності в Minerva» "
               "або вручну) — або не записуйте цей день.")


def has_worktime(du: DanteUser, day: datetime.date) -> bool | None:
    """Чи є в копії askDANTE прихід за день (WorkDay.times); None — день ще не читався з askDANTE."""
    wd = WorkDay.objects.filter(dante_user=du, date=day).only("times").first()
    if wd is None:
        return None
    return any(t.get("coming") for t in (wd.times or []))


def ensure_attendance(du: DanteUser, day: datetime.date, mode: str, user=None,
                      st: DanteSettings | None = None) -> str:
    """День без приходу / відходу в askDANTE → записати їх, як «🧩 Заповнити пропуски» (`fill` для одного дня:
    стандартний день працівника або за активністю в Minerva; свята, відпустки, вихідні й день, що ще триває, —
    ні). → '' — прихід є або записано, інакше причина."""
    st = st or DanteSettings.get()
    if has_worktime(du, day):
        return ""
    result = fill(du, day, day, mode, only_dates=[day], user=user, source=Booking.FILL, st=st)
    if result["sent"]:
        return ""
    if has_worktime(du, day):   # тим часом з'явився в askDANTE
        return ""
    return (result["failed"] or result["skipped"] or [(day, result["stopped"] or "не вдалося")])[0][1]


def _no_worktime_error(e: DanteError) -> bool:
    """askDANTE: «start: Es wurden keine Arbeitszeiten erfasst» — час на проєкт поза робочим часом дня."""
    return "arbeitszeit" in (e.fields.get("start") or "").lower()


def _remember(st: DanteSettings, **flags):
    DanteSettings.objects.filter(pk=st.pk).update(**flags)
    for k, v in flags.items():
        setattr(st, k, v)


def book_project(du: DanteUser, day: datetime.date, dp: DanteProject, minutes: int = 0, activity=None,
                 comment: str = "", user=None, source: str = Booking.MANUAL, st: DanteSettings | None = None,
                 start: datetime.time | None = None, stop: datetime.time | None = None,
                 prefer: datetime.time | None = None, attendance: str = "") -> Booking:
    """Час на проєкт askDANTE: «з … по …» (start/stop) або тривалість у хвилинах. Повтор того самого запису за
    2 хв — відмова (подвійне натискання), бо записи askDANTE через API не видаляються.
    askDANTE може не приймати тривалість і вимагати вид роботи (налаштування фірми): Minerva запам'ятовує це з
    першої відмови (`durations_allowed`, `activity_required`), пише тривалість як «з … по …» у вільний відрізок
    (`find_slot`, від `prefer`) і бере вид роботи за замовчуванням або єдиний — і повторює запит один раз."""
    st = st or DanteSettings.get()
    now = timezone.localtime()
    if day > local_today():
        raise DanteError("bad_request", "Час на проєкт за майбутній день записувати не можна.")
    if start and stop:
        if stop != datetime.time(0, 0) and stop <= start:
            raise DanteError("bad_request", "«По» має бути пізніше за «з» (00:00 — кінець доби).")
        if day == now.date() and (stop == datetime.time(0, 0) or stop > now.time()):
            raise DanteError("bad_request", "Час, який ще не настав, записувати не можна.")
        minutes = minutes_between(hhmm(start), hhmm(stop))
    elif start or stop:
        raise DanteError("bad_request", "Вкажіть і «з», і «по» — або лише тривалість.")
    if not 0 < minutes <= 24 * 60:
        raise DanteError("bad_request", "Тривалість — від 1 хвилини до 24 годин.")
    if dp.archived or dp.gone or not dp.time_trackable:
        raise DanteError("bad_request", f"У проєкт askDANTE «{dp}» час записувати не можна (архів або вимкнено).")
    activity = activity or st.default_activity
    if activity is None and st.activity_required:
        activity = _pick_activity(st)
        if activity is None:
            raise DanteError("bad_request", ACTIVITY_MISSING)
    comment = (comment or "").strip()[:500]
    fixed_times = bool(start)
    recent = timezone.now() - datetime.timedelta(minutes=2)
    if Booking.objects.filter(dante_user=du, kind=Booking.PROJECT, date=day, dante_project=dp, duration=minutes,
                              start=start, comment=comment, status=Booking.SENT, created_at__gte=recent).exists():
        raise DanteError("bad_request", "Такий самий запис щойно вже пішов в askDANTE — оновіть сторінку.")
    def record():
        # підпроєкт askDANTE (project item) — інше поле, ніж проєкт
        data = {"projectItemId": dp.dante_id} if dp.parent_id else {"projectId": dp.dante_id}
        if activity:
            data["activityId"] = activity.dante_id
        if comment:
            data["comment"] = comment
        r = {"date": day.isoformat(), "projectData": data}
        r.update({"start": hhmm(start), "stop": hhmm(stop)} if start else {"duration": minutes})
        return r

    c = client(st)
    if attendance in (DanteSettings.MODE_FIXED, DanteSettings.MODE_ACTIVITY):
        # людина вибрала: у дні без приходу спершу записати прихід / відхід (askDANTE пише час на проєкт лише в них)
        try:
            refresh_days(du, day, day, c)
        except DanteError:
            pass
        why = ensure_attendance(du, day, attendance, user, st)
        if why:
            raise DanteError("bad_request", f"Прихід / відхід за {day:%d.%m} не записано: {why}. Внесіть їх вручну "
                                            "(📅 клік на дату) — або не записуйте цей день.")
    with lock(du):
        if st.attendance_required:   # askDANTE не прийме час на проєкт без приходу / відходу дня
            try:
                refresh_days(du, day, day, c)
            except DanteError:
                pass
            if not has_worktime(du, day):
                raise DanteError("bad_request", NO_WORKTIME.format(day=day))
        if start or not st.durations_allowed:   # свіжа копія записів дня — без перекриття з іншим проєктом
            try:
                sync_records(st, day, day, c)
            except DanteError:
                pass
        if not start and not st.durations_allowed:
            start, stop = find_slot(du, day, minutes, prefer, st)
        if fixed_times:
            busy = project_overlap(du, day, start, stop)
            if busy:
                raise DanteError("bad_request", f"Перекривається з записом askDANTE {busy}.")
        booking = Booking(dante_user=du, date=day, kind=Booking.PROJECT, source=source, duration=minutes,
                          start=start, stop=stop, dante_project=dp, activity=activity, comment=comment,
                          reason="час на проєкт", created_by=user if getattr(user, "pk", None) else None)
        for attempt in range(2):
            try:
                c.track(du.dante_id, record())
                booking.status, booking.error = Booking.SENT, ""
                break
            except DanteError as e:
                booking.status, booking.error = Booking.FAILED, e.message
                if attempt or e.status != 400:
                    break
                if _no_worktime_error(e):   # повтор не допоможе — спершу прихід / відхід дня
                    _remember(st, attendance_required=True)
                    booking.error = NO_WORKTIME.format(day=day)
                    break
                retry = False
                if not start and "duration" in e.fields:   # «Die Verwendung einer Dauer ist nicht erlaubt»
                    _remember(st, durations_allowed=False)
                    try:
                        sync_records(st, day, day, c)
                        start, stop = find_slot(du, day, minutes, prefer, st)
                    except DanteError as slot_error:
                        booking.error = slot_error.message
                        break
                    booking.start, booking.stop, retry = start, stop, True
                if len(comment) > 250 and any(k.endswith("comment") for k in e.fields):   # задовгий опис
                    comment = comment[:249].rstrip() + "…"
                    booking.comment, retry = comment, True
                if activity is None and any(k.endswith("activityId") for k in e.fields):
                    _remember(st, activity_required=True)
                    activity = _pick_activity(st)
                    if activity is None:
                        booking.error = ACTIVITY_MISSING
                        break
                    booking.activity, retry = activity, True
                if not retry:
                    break
        booking.save()
    if booking.status == Booking.SENT:
        try:
            sync_records(st, day, day, c)   # одразу видно в годинах; збій — підтягнеться нічним оновленням
        except DanteError:
            pass
    return booking


def day_records(du: DanteUser, date_from, date_to) -> dict:
    """{дата: [TimeRecord з проєктом]} — час на проєкти по днях (з копії записів)."""
    out = {}
    for r in (TimeRecord.objects.filter(dante_user=du, day__range=(date_from, date_to))
              .exclude(dante_project=None, project_name="").select_related("dante_project")):
        out.setdefault(r.day, []).append(r)
    return out
