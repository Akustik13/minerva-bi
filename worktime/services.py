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
from .models import Booking, DanteActivity, DanteProject, DanteSettings, DanteUser, WorkDay

ABSENCE_LABELS = {
    "WEEKEND": "вихідний", "HOLIDAY": "свято", "PUBLIC_HOLIDAY": "свято", "LEAVE": "відпустка",
    "VACATION": "відпустка", "ILLNESS": "лікарняний", "DISEASE": "лікарняний", "SICK": "лікарняний",
}
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
            "customer_name": p["customer_name"][:255], "archived": p["archived"],
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


def refresh_days(du: DanteUser, date_from: datetime.date, date_to: datetime.date,
                 c: DanteClient | None = None) -> Days:
    """Свіже читання днів з askDANTE → кеш WorkDay."""
    c = c or client()
    times = c.working_time(date_from, date_to, du.dante_id)
    out = Days()
    try:
        if not (du.email or du.personnel_number):
            raise DanteError("bad_request", "у працівника askDANTE немає логіна й табельного номера")
        att = c.attendances(date_from, date_to, email=du.email, personnel_number=du.personnel_number)
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


def day_blocker(st: DanteSettings, wd: WorkDay | None, day: datetime.date) -> str:
    """Чому в день не можна автоматично ставити прихід / відхід; '' — можна."""
    if wd and wd.times:
        return "вже є: " + ", ".join(f"{t['coming']}–{t.get('leaving') or '…'}" for t in wd.times)
    planned = wd.planned_state if wd else ""
    absences = (wd.absences if wd else None) or []
    labels = [ABSENCE_LABELS.get((a.get("type") or "").upper(), (a.get("type") or "відсутність").lower())
              for a in absences]
    if planned in PLANNED_LABELS:
        return labels[0] if labels else PLANNED_LABELS[planned]
    if absences:
        partial = any(float(a.get("ratio") or 1) < 1 for a in absences)
        return f"{labels[0]} частину дня — заповніть вручну" if partial else labels[0]
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
