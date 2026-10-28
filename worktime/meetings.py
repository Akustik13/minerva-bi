"""Зустрічі з календаря → час на проєкти askDANTE за період (перегляд → вибір проєкту → запис «з … по …»).

Звідки: зустрічі календаря Minerva (🤝, зокрема прийняті запрошення з пошти) і події підключених календарів
Google / Outlook та підписок iCal, які показуються в Minerva (копія шару, ExternalEvent). Та сама зустріч у
Minerva і в Outlook — один рядок. Не беруться: події на весь день, довші за 12 год, ще не завершені.

Проєкт askDANTE підказується (людина може змінити перед записом), перша підходяща ознака:
  код проєкту Minerva P-… у назві → та сама зустріч уже записувалась → проєкт Minerva гілки листа-запрошення →
  клієнт CRM зустрічі → правило зв'язку (слово в назві, домен відправника запрошення, клієнт) → назва проєкту
  askDANTE у назві зустрічі.
Вже записане не пропонується: запис Minerva з тим самим часом або запис askDANTE того дня з тим самим описом;
перекриття з іншим записом askDANTE того дня — попередження (book_project все одно не запише).
"""
from __future__ import annotations

import datetime
import re

from django.utils import timezone

from . import services
from .client import DanteError
from .models import Booking, DanteProject, DanteSettings, DanteUser, MatchRule, ProjectMap, TimeRecord

MAX_MINUTES = 12 * 60
MAX_DAYS = 62
MAX_PER_SUBMIT = 25


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _bounds(date_from, date_to):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min), tz)
    end = timezone.make_aware(datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min), tz)
    return start, end


def _collect(user, start, end):
    """[(ключ, назва, початок, кінець, звідки, CalendarEvent|None)] — зустрічі користувача за період."""
    from calendar_app.models import CalendarEvent, ExternalEvent
    from django.db.models import Q
    out = []
    for ev in (CalendarEvent.objects.filter(user=user, event_type=CalendarEvent.TYPE_MEETING, all_day=False,
                                            start_at__gte=start, start_at__lt=end)
               .select_related("crm_customer", "email_message", "email_message__thread").order_by("start_at")):
        out.append((f"e{ev.pk}", ev.title, ev.start_at, ev.end_at, "Minerva", ev))
    ext = (ExternalEvent.objects.filter(Q(feed__user=user) | Q(remote__account__user=user), all_day=False,
                                        start_at__gte=start, start_at__lt=end)
           .select_related("feed", "remote").order_by("start_at"))
    seen = {(_norm(t), a, b) for _, t, a, b, _, _ in out}
    for x in ext:
        key = (_norm(x.title), x.start_at, x.end_at)
        if key in seen or x.title == "Зайнято":
            continue
        seen.add(key)
        where = x.remote.name if x.remote_id else (x.feed.name if x.feed_id else "календар")
        out.append((f"x{x.pk}", x.title, x.start_at, x.end_at, where, None))
    out.sort(key=lambda r: r[2])
    return out


class Hints:
    """Довідники для підказки проєкту — один раз на сторінку."""

    def __init__(self, user):
        from projects.services import CODE_RE
        self.code_re = CODE_RE
        self.active = {dp.pk: dp for dp in DanteProject.objects.filter(archived=False, gone=False,
                                                                       time_trackable=True)}
        self.pinned = set(DanteProject.objects.filter(pinned_by=user).values_list("pk", flat=True))
        self.maps = {m.project_id: m.dante_project_id for m in ProjectMap.objects.all()}
        from projects.models import Project
        self.codes = {p.code.upper(): p.pk for p in Project.objects.exclude(code="").filter(pk__in=list(self.maps))}
        self.previous = {}
        for comment, dp_id in (Booking.objects.filter(source=Booking.MEETING, status=Booking.SENT,
                                                      kind=Booking.PROJECT, dante_user__user=user)
                               .exclude(dante_project=None).order_by("created_at")
                               .values_list("comment", "dante_project_id")):
            self.previous[_norm(comment)] = dp_id
        self.rules = list(MatchRule.objects.all())
        self.by_customer = {}
        for dp in self.active.values():
            if dp.crm_customer_id:
                self.by_customer.setdefault(dp.crm_customer_id, []).append(dp.pk)

    def ok(self, pk):
        return self.active.get(pk)

    def pick(self, pks):
        """Серед кількох — спершу ☆ мої."""
        pks = [p for p in pks if p in self.active]
        mine = [p for p in pks if p in self.pinned]
        return self.active[(mine or pks)[0]] if pks else None

    def suggest(self, title, ev=None):
        """(DanteProject | None, причина)."""
        low = _norm(title)
        for y, n in self.code_re.findall(title or ""):
            pk = self.codes.get(f"P-{y}-{n}".upper())
            if pk and self.ok(self.maps[pk]):
                return self.active[self.maps[pk]], f"код P-{y}-{n} у назві"
        if low in self.previous and self.ok(self.previous[low]):
            return self.active[self.previous[low]], "ця зустріч уже записувалась сюди"
        msg = getattr(ev, "email_message", None) if ev is not None else None
        if msg is not None and msg.thread_id:
            from projects.models import ProjectLink
            for pid in (ProjectLink.objects.filter(thread_id=msg.thread_id, status=ProjectLink.CONFIRMED)
                        .values_list("project_id", flat=True)):
                if pid in self.maps and self.ok(self.maps[pid]):
                    return self.active[self.maps[pid]], "проєкт листа-запрошення"
        customer_id = getattr(ev, "crm_customer_id", None) if ev is not None else None
        if not customer_id and msg is not None and msg.thread_id:
            customer_id = msg.thread.crm_customer_id
        if customer_id and self.by_customer.get(customer_id):
            return self.pick(self.by_customer[customer_id]), "клієнт CRM зустрічі"
        domain = (msg.from_email.rsplit("@", 1)[-1].strip(">").lower() if msg is not None and msg.from_email else "")
        for r in self.rules:
            if not self.ok(r.dante_project_id):
                continue
            if r.kind == MatchRule.KEYWORD and r.value and r.value.lower() in low:
                return self.active[r.dante_project_id], f"правило: «{r.value}» у назві"
            if r.kind == MatchRule.DOMAIN and r.value and domain and domain.endswith(r.value.lower().lstrip("@")):
                return self.active[r.dante_project_id], f"правило: домен {r.value}"
            if r.kind == MatchRule.CUSTOMER and customer_id and r.customer_id == customer_id:
                return self.active[r.dante_project_id], "правило: клієнт CRM"
        named = [dp.pk for dp in self.active.values() if len(dp.name) >= 4 and _norm(dp.name) in low]
        if named:
            return self.pick(named), "назва проєкту askDANTE у назві зустрічі"
        return None, ""


def plan(du: DanteUser, date_from, date_to, now=None) -> list:
    """Зустрічі за період з підказкою проєкту і станом (нічого не записує).
    stan: ready / booked / overlap / skip; checked — запропоновано до запису."""
    if du.user_id is None:
        return []
    now = now or timezone.now()
    start, end = _bounds(date_from, date_to)
    hints = Hints(du.user)
    sent = {(b.date, b.start, b.stop) for b in Booking.objects.filter(dante_user=du, kind=Booking.PROJECT,
                                                                      status=Booking.SENT,
                                                                      date__range=(date_from, date_to))}
    described = {}
    for r in TimeRecord.objects.filter(dante_user=du, day__range=(date_from, date_to)).exclude(description=""):
        described.setdefault(r.day, set()).add(_norm(r.description))
    rows = []
    for key, title, a, b, where, ev in _collect(du.user, start, end):
        la = timezone.localtime(a)
        day = la.date()
        row = {"key": key, "title": title, "day": day, "where": where, "start": la.time().replace(second=0,
                                                                                                    microsecond=0),
               "stop": None, "minutes": 0, "dp": None, "reason": "", "state": "ready", "note": "", "checked": False}
        lb = timezone.localtime(b) if b else None
        if lb is None or b <= a:
            row.update(state="skip", note="без часу завершення")
        elif (b - a).total_seconds() > MAX_MINUTES * 60:
            row.update(state="skip", note="довше 12 годин")
        elif b > now:
            row.update(state="skip", note="ще не закінчилась")
        elif lb.date() != day and lb.time() != datetime.time(0, 0):
            row.update(state="skip", note="через північ — запишіть вручну")
        else:
            row["stop"] = lb.time().replace(second=0, microsecond=0)
            row["minutes"] = int((b - a).total_seconds() // 60)
            if (day, row["start"], row["stop"]) in sent:
                row.update(state="booked", note="уже записано з Minerva")
            elif _norm(title) in described.get(day, set()):
                row.update(state="booked", note="в askDANTE того дня вже є запис з такою назвою")
            else:
                busy = services.project_overlap(du, day, row["start"], row["stop"])
                if busy:
                    row.update(state="overlap", note=f"перекривається з записом askDANTE {busy}")
        row["dp"], row["reason"] = hints.suggest(title, ev)
        row["checked"] = row["state"] == "ready" and row["dp"] is not None
        rows.append(row)
    return rows


def book(du: DanteUser, picks: dict, date_from, date_to, user=None, activity=None,
         st: DanteSettings | None = None) -> dict:
    """picks = {ключ зустрічі: pk проєкту askDANTE}. Пише лише ті, що в плані зараз «ready» (план перераховується
    на сервері: браузеру не довіряємо час і стан). → {"written", "minutes", "errors": [...], "left"}"""
    st = st or DanteSettings.get()
    try:   # свіжа копія записів askDANTE — щоб «уже записано» й перекриття були правдою
        services.sync_records(st, date_from, date_to)
    except DanteError:
        pass
    rows = {r["key"]: r for r in plan(du, date_from, date_to)}
    written = minutes = 0
    errors, todo = [], [k for k in picks if k in rows]
    for key in todo[:MAX_PER_SUBMIT]:
        r = rows[key]
        dp = DanteProject.objects.filter(pk=picks[key], archived=False, gone=False).first()
        if r["state"] != "ready":
            errors.append(f"{r['day']:%d.%m} {r['title'][:40]}: {r['note']}")
            continue
        if dp is None:
            errors.append(f"{r['day']:%d.%m} {r['title'][:40]}: не вибрано проєкт askDANTE")
            continue
        try:
            b = services.book_project(du, r["day"], dp, activity=activity, comment=r["title"][:500], user=user,
                                      source=Booking.MEETING, st=st, start=r["start"], stop=r["stop"])
        except DanteError as e:
            errors.append(f"{r['day']:%d.%m} {r['title'][:40]}: {e.message}")
            continue
        if b.status == Booking.SENT:
            written += 1
            minutes += r["minutes"]
        else:
            errors.append(f"{r['day']:%d.%m} {r['title'][:40]}: askDANTE — {b.error}")
    return {"written": written, "minutes": minutes, "errors": errors, "left": max(0, len(todo) - MAX_PER_SUBMIT)}
