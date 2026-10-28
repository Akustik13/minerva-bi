"""Час на пошту → askDANTE: скільки часу пішло на листи своїх категорій (DigiKey, магазин…) і проєктів Minerva.

Оцінка — за нормами, без вгадування: за день по кожному проєкту askDANTE
    наші відповіді × «хв на відповідь» + отримані й прочитані листи × «хв на лист»
    + змінені вами замовлення джерел категорії (журнал дій) × «хв на замовлення»,
округлено вгору до кроку (`DanteSettings.mail_step`). Куди йде лист:
    1) гілка в проєкті Minerva, зв'язаному з askDANTE (якщо `mail_projects`), — туди;
    2) інакше своя категорія листа з проєктом askDANTE (`MailCategoryTime`);
    інакше лист не рахується (видно як «без прив'язки»).
Скриньки — користувача Minerva, прив'язаного до працівника askDANTE. Спам і видалені не рахуються.
askDANTE не дозволяє виправити запис, тож записане за день зберігається (`MailTimeEntry`) і наступного разу
дописується лише різниця. Запис — після ✓ на сторінці «✉️ Час на пошту» або щодня автоматично (за категорією).
"""
from __future__ import annotations

import datetime
import math

from django.utils import timezone

from . import services
from .client import DanteError
from .models import Booking, DanteSettings, DanteUser, MailCategoryTime, MailTimeEntry, ProjectMap, WorkDay

LOOKBACK_DAYS = 3        # автозапис: за скільки минулих днів дописати різницю
MAX_DAYS = 62


def _bounds(date_from, date_to):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min), tz)
    end = timezone.make_aware(datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min), tz)
    return start, end


def round_up(minutes: int, step: int) -> int:
    step = max(1, step or 1)
    return int(math.ceil(minutes / step) * step) if minutes > 0 else 0


def _project_targets(thread_ids, message_ids):
    """{('t', thread_id) | ('m', message_id): (Project, DanteProject)} — підтверджені зв'язки з askDANTE."""
    from projects.models import ProjectLink
    maps = {m.project_id: m.dante_project for m in ProjectMap.objects.select_related("dante_project")}
    out = {}
    if not maps:
        return out
    links = (ProjectLink.objects.filter(status=ProjectLink.CONFIRMED, project_id__in=list(maps))
             .select_related("project").order_by("created_at"))
    for link in links.filter(thread_id__in=list(thread_ids)):
        out.setdefault(("t", link.thread_id), (link.project, maps[link.project_id]))
    for link in links.filter(message_id__in=list(message_ids)):
        out.setdefault(("m", link.message_id), (link.project, maps[link.project_id]))
    return out


def _order_counts(user, start, end, categories):
    """{(день, category_id): к-сть замовлень} — замовлення джерел категорії, які користувач змінив (журнал дій)."""
    wanted = {c.category_id: {s.slug for s in c.category.sources.all()} for c in categories if c.order_minutes}
    wanted = {k: v for k, v in wanted.items() if v}
    if not wanted:
        return {}
    from django.contrib.contenttypes.models import ContentType
    from core.models import AuditLog
    from sales.models import SalesOrder
    ct = ContentType.objects.get_for_model(SalesOrder)
    rows = list(AuditLog.objects.filter(user=user, content_type=ct, timestamp__gte=start, timestamp__lt=end,
                                        action__in=[AuditLog.Action.CREATE, AuditLog.Action.UPDATE])
                .values_list("object_id", "timestamp"))
    ids = {int(o) for o, _ in rows if str(o).isdigit()}
    source = dict(SalesOrder.objects.filter(pk__in=ids).values_list("pk", "source"))
    seen, out = set(), {}
    for obj, ts in rows:
        if not str(obj).isdigit():
            continue
        day = timezone.localtime(ts).date()
        for cat_id, sources in wanted.items():
            if source.get(int(obj)) in sources and (day, cat_id, obj) not in seen:
                seen.add((day, cat_id, obj))
                out[(day, cat_id)] = out.get((day, cat_id), 0) + 1
    return out


def estimate(du: DanteUser, date_from, date_to, st: DanteSettings | None = None) -> dict:
    """{день: {"rows": [...], "unmapped": N}} — оцінка часу на пошту по проєктах askDANTE (нічого не записує)."""
    st = st or DanteSettings.get()
    if du.user_id is None:
        return {}
    from email_assistant.models import EmailAccount, EmailMessage
    start, end = _bounds(date_from, date_to)
    accounts = list(EmailAccount.objects.filter(user_id=du.user_id).values_list("pk", flat=True))
    msgs = list(EmailMessage.objects.filter(account_id__in=accounts, sent_at__gte=start, sent_at__lt=end,
                                            is_spam=False, is_deleted=False)
                .exclude(folder__in=[EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_TRASH])
                .values("pk", "thread_id", "work_category_id", "folder", "is_read", "sent_at", "subject", "from_email",
                        "message_id").order_by("sent_at", "pk"))
    cats = {c.category_id: c for c in MailCategoryTime.objects.filter(dante_project__isnull=False,
                                                                       category__is_active=True)
            .select_related("dante_project", "activity", "category").prefetch_related("category__sources")}
    projects = _project_targets({m["thread_id"] for m in msgs if m["thread_id"]}, {m["pk"] for m in msgs}) \
        if st.mail_projects else {}
    rows, unmapped = {}, {}

    def row(day, dp, label, kind, auto, activity):
        r = rows.setdefault((day, dp.pk), {"day": day, "dp": dp, "labels": [], "kinds": set(), "replies": 0,
                                            "received": 0, "orders": 0, "raw": 0, "auto": False, "activity": activity,
                                            "subjects": [], "first": None})
        if label not in r["labels"]:
            r["labels"].append(label)
        r["kinds"].add(kind)
        r["auto"] = r["auto"] or auto
        return r

    from email_assistant.crm_link import is_own, own_addresses
    own, seen = own_addresses(), set()
    for m in msgs:
        if m["message_id"]:   # той самий лист у двох папках (копія собі в CC + «Надіслані») — один раз
            if m["message_id"] in seen:
                continue
            seen.add(m["message_id"])
        day = timezone.localtime(m["sent_at"]).date()
        outgoing = m["folder"] == EmailMessage.FOLDER_SENT or is_own(m["from_email"], own)
        if not outgoing and not m["is_read"]:
            continue   # непрочитаний — ще не опрацьований
        target = projects.get(("m", m["pk"])) or projects.get(("t", m["thread_id"]))
        if target:
            project, dp = target
            r = row(day, dp, f"📁 {project.code}", "project", st.mail_projects_auto, st.default_activity)
            r["raw"] += st.mail_reply_minutes if outgoing else st.mail_read_minutes
        elif m["work_category_id"] in cats:
            c = cats[m["work_category_id"]]
            r = row(day, c.dante_project, c.category.label, "category", c.auto, c.activity or st.default_activity)
            r["raw"] += c.reply_minutes if outgoing else c.read_minutes
        else:
            unmapped[day] = unmapped.get(day, 0) + 1
            continue
        r["replies" if outgoing else "received"] += 1
        at = timezone.localtime(m["sent_at"]).time()
        if r["first"] is None or at < r["first"]:
            r["first"] = at   # askDANTE без тривалості — відрізок «з … по …» починається від першого листа
        if len(r["subjects"]) < 5 and m["subject"]:
            r["subjects"].append(("📤 " if outgoing else "📥 ") + m["subject"][:80])

    for (day, cat_id), n in _order_counts(du.user, start, end, cats.values()).items():
        c = cats[cat_id]
        r = row(day, c.dante_project, c.category.label, "category", c.auto, c.activity or st.default_activity)
        r["orders"] += n
        r["raw"] += n * c.order_minutes

    booked = {(e.day, e.dante_project_id): e.minutes_booked
              for e in MailTimeEntry.objects.filter(dante_user=du, day__range=(date_from, date_to))}
    out = {}
    for (day, dp_pk), r in sorted(rows.items(), key=lambda kv: (kv[0][0], str(kv[1]["dp"]))):
        r["minutes"] = round_up(r["raw"], st.mail_step)
        r["booked"] = booked.get((day, dp_pk), 0)
        r["to_book"] = max(0, r["minutes"] - r["booked"])
        r["label"] = " · ".join(r["labels"])
        r["kinds"] = sorted(r["kinds"])
        out.setdefault(day, {"rows": [], "unmapped": 0})["rows"].append(r)
    for day, n in unmapped.items():
        out.setdefault(day, {"rows": [], "unmapped": 0})["unmapped"] = n
    return dict(sorted(out.items()))


def _plain(text: str) -> str:
    """Без емодзі: askDANTE може не прийняти 4-байтові символи (і варіаційні селектори)."""
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF and ch != "️" and not 0x2600 <= ord(ch) <= 0x27BF)
    return " ".join(text.split())


def _comment(r) -> str:
    parts = []
    if r["replies"]:
        parts.append(f"відповідей {r['replies']}")
    if r["received"]:
        parts.append(f"листів {r['received']}")
    if r["orders"]:
        parts.append(f"замовлень {r['orders']}")
    return _plain(f"Пошта Minerva: {r['label']} — " + ", ".join(parts))


def book(du: DanteUser, day: datetime.date, dp_pk: int, user=None, source: str = Booking.MAIL,
         st: DanteSettings | None = None, activity=None) -> Booking:
    """Записати в askDANTE оцінку дня по проєкту (лише ще не записану різницю)."""
    st = st or DanteSettings.get()
    if day > services.local_today():
        raise DanteError("bad_request", "Час за майбутній день записувати не можна.")
    r = next((x for x in estimate(du, day, day, st).get(day, {}).get("rows", []) if x["dp"].pk == int(dp_pk)), None)
    if r is None or r["to_book"] <= 0:
        raise DanteError("bad_request", "Цей час на пошту вже записано в askDANTE.")
    booking = services.book_project(du, day, r["dp"], minutes=r["to_book"], activity=r["activity"] or activity,
                                    comment=_comment(r), user=user, source=source, st=st, prefer=r["first"])
    if booking.status == Booking.SENT:
        entry, _ = MailTimeEntry.objects.get_or_create(dante_user=du, day=day, dante_project=r["dp"])
        entry.minutes_booked += r["to_book"]
        entry.booking = booking
        entry.details = {"replies": r["replies"], "received": r["received"], "orders": r["orders"],
                         "labels": r["labels"]}
        entry.save()
    return booking


def _absent_whole_day(du, day) -> bool:
    wd = WorkDay.objects.filter(dante_user=du, date=day).first()
    if wd is None:
        return False
    if wd.planned_state in (WorkDay.LEAVE, WorkDay.ILLNESS):
        return True
    return any(float(a.get("ratio") or 1) >= 1 for a in (wd.absences or []))


def due(st: DanteSettings, now=None) -> bool:
    now = timezone.localtime(now) if now else timezone.localtime()
    return (st.enabled and st.configured and st.mail_time and now.hour >= st.autofill_hour
            and st.mail_time_last_day != now.date())


def auto(st: DanteSettings | None = None, dry_run: bool = False, today=None) -> str:
    """Щоденний запуск: за останні дні до вчора — записати різницю там, де категорія (або проєкти) «автоматично».
    Дні відпустки / лікарняного пропускаються."""
    st = st or DanteSettings.get()
    today = today or services.local_today()
    if not (st.enabled and st.configured and st.mail_time):
        return "Час на пошту вимкнено."
    date_from, date_to = today - datetime.timedelta(days=LOOKBACK_DAYS), today - datetime.timedelta(days=1)
    written = failed = skipped = minutes = 0
    errors = []
    for du in DanteUser.objects.filter(archived=False).exclude(user=None):
        for day, info in estimate(du, date_from, date_to, st).items():
            todo = [r for r in info["rows"] if r["auto"] and r["to_book"] > 0]
            if not todo:
                continue
            if _absent_whole_day(du, day):
                skipped += len(todo)
                continue
            for r in todo:
                if dry_run:
                    written += 1
                    minutes += r["to_book"]
                    continue
                try:
                    b = book(du, day, r["dp"].pk, source=Booking.MAIL, st=st)
                except DanteError as e:
                    failed += 1
                    errors.append(e.message)
                    continue
                if b.status == Booking.SENT:
                    written += 1
                    minutes += r["to_book"]
                else:
                    failed += 1
                    errors.append(b.error)
    text = (f"{timezone.localtime():%d.%m.%Y %H:%M} · записано {written} ({minutes // 60}:{minutes % 60:02d})"
            + (f", пропущено (відсутність) {skipped}" if skipped else "")
            + (f", помилок {failed}: {errors[0][:80]}" if failed else ""))
    if not dry_run:
        DanteSettings.objects.filter(pk=st.pk).update(mail_time_last_day=today, mail_time_result=text[:255])
    return text
