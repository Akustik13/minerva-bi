"""📨 Запрошення на зустрічі в проєкті (calendar_app.MeetingInvite): що пропонують, що вже підтвердили, що змінилось.

Запрошення проєкту — з його листів (підтверджені гілки й прив'язані листи), запрошення, чия подія в календарі
прив'язана до проєкту, і запрошення клієнта проєкту, якщо це його єдиний відкритий проєкт. Запрошення особисте
(прийшло в чиюсь скриньку): пульт показує лише свої, «потребує уваги» для інших — без назви (owner + private_text).

- `state` — що з запрошенням: чекає відповіді, організатор змінив / скасував, у календарі без відповіді, ✅ / ❔ / ✕;
- `groups` — варіанти однієї зустрічі (той самий організатор, назва без «Option 1 / Variante B / 2/3»): з кількох
  відкритих треба обрати один — «Пропонують N варіанти зустрічі — оберіть»;
- `answer` — ✅ / ❔ / ✕ з пульта = `calendar_app.invites.respond` (лист організатору з iCalendar REPLY зі скриньки,
  куди прийшло запрошення, і подія в календарі Minerva); обрали варіант — решту за бажанням відхилити так само або
  лише не заносити; скасоване організатором — прибрати з календаря (`invites.accept`);
- відповідь з Outlook / телефона — `calendar_app.invites.track_reply` (лист з «Надісланих»), пульт показує її так само.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

PAST_DAYS = 14             # відповіли / минули — ще стільки днів у блоці «📨 Запрошення»
SOON_HOURS = 48            # запрошення без відповіді, що почнеться раніше, — «high»
OPEN, MOVED, ADDED, CANCEL = "open", "moved", "added", "cancel"
ACCEPTED, TENTATIVE, DECLINED, DISMISSED, CANCELLED = "accepted", "tentative", "declined", "dismissed", "cancelled"
HELD, MISSED = "held", "missed"
ACTIONABLE = (OPEN, MOVED, ADDED)             # чекають нашого рішення
LABELS = {
    OPEN: "📨 чекає відповіді", MOVED: "♻ організатор змінив — підтвердіть ще раз",
    ADDED: "📅 у календарі · організатору не відповіли", CANCEL: "✖ скасовано організатором — приберіть з календаря",
    ACCEPTED: "✅ Буду", TENTATIVE: "❔ Можливо", DECLINED: "✕ Відхилено", DISMISSED: "— не заносили",
    CANCELLED: "✖ скасовано", HELD: "відбулась", MISSED: "минула без відповіді",
}
CLS = {OPEN: "warn", MOVED: "warn", ADDED: "warn", CANCEL: "bad", ACCEPTED: "ok", TENTATIVE: "open",
       DECLINED: "off", DISMISSED: "off", CANCELLED: "off", HELD: "ok", MISSED: "off"}
ANSWERS = {"accepted": ("✅ Буду", "Підтвердити: лист організатору «Буду» і зустріч у ваш календар"),
           "tentative": ("❔ Можливо", "Попередньо: лист організатору «Можливо», у календарі — з ❔"),
           "declined": ("✕ Відхилити", "Відхилити: лист організатору «Не буду», з календаря прибирається")}
_VARIANT_RE = re.compile(r"\b(option|variante?|variant|vorschlag|alternative|alternativ|termin|choice|wahl|slot|"
                         r"варіант|вариант|опція|термін)\b\s*[\w]?", re.I)


_TAIL_RE = re.compile(r"\s*[-–—:|,(]*\s*(?:option|variante?|vorschlag|alternative|termin|choice|wahl|slot|варіант|вариант|"
                      r"опція|термін)\s*\w{0,2}\s*\)?\s*$|\s*\(?\d+\s*/\s*\d+\)?\s*$", re.I)


def base_title(title) -> str:
    """Назва зустрічі без «– Option 1 / (2/3) / Variante B» — заголовок групи варіантів."""
    t = _TAIL_RE.sub("", str(title or "")).strip(" -–—:|,")
    return t or str(title or "")


class InviteError(Exception):
    pass


def _now():
    return timezone.now()


def _model():
    try:
        from calendar_app.models import MeetingInvite
    except ImportError:          # без календаря — без запрошень
        return None
    return MeetingInvite


def _only_open_project(project) -> bool:
    if not project.customer_id:
        return False
    from .models import Project
    ids = list(Project.objects.filter(customer_id=project.customer_id, status__in=Project.OPEN_STATUSES)
               .values_list("pk", flat=True)[:2])
    return ids == [project.pk]


def related(project, messages, deadlines, user=None, now=None) -> list:
    """Запрошення проєкту (усіх людей, user — лише її): з листів проєкту, з подією проєкту, клієнта з єдиним
    відкритим проєктом; без тих, що закінчились понад PAST_DAYS тому."""
    Invite = _model()
    if Invite is None or project is None or not getattr(project, "pk", None):
        return []
    now = now or _now()
    msg_ids = [x["msg"].pk for x in messages or []]
    cond = Q(email_message_id__in=msg_ids) | Q(event_id__in=[e.pk for e in deadlines or []])
    if _only_open_project(project):       # клієнт запрошення — з гілки листа (CRM прив'язує її після сигналу)
        cond |= Q(crm_customer_id=project.customer_id) | Q(email_message__thread__crm_customer_id=project.customer_id)
    qs = Invite.objects.filter(cond).filter(start_at__gte=now - timedelta(days=PAST_DAYS))
    if user is not None:
        qs = qs.filter(user_id=getattr(user, "pk", None))
    return list(qs.select_related("email_message__account", "event").order_by("start_at", "pk"))


def state(inv, now=None) -> str:
    """Що з запрошенням зараз (LABELS)."""
    Invite = _model()
    now = now or _now()
    past = (inv.end_at or inv.start_at) < now
    if inv.status == Invite.CANCEL:
        return CANCEL
    if inv.status == Invite.CANCELLED:
        return CANCELLED
    if inv.status == Invite.UPDATE or (inv.status == Invite.NEW and inv.response):
        return MISSED if past else MOVED             # перенесли чи надіслали знову після нашої відповіді
    if inv.response == Invite.DECLINED:
        return DECLINED
    if inv.status == Invite.DISMISSED:
        return DISMISSED
    if inv.status == Invite.NEW:
        return MISSED if past else OPEN
    if inv.response == Invite.ACCEPTED:
        return HELD if past else ACCEPTED
    if inv.response == Invite.TENTATIVE:
        return HELD if past else TENTATIVE
    return HELD if past else ADDED


def _when(inv) -> str:
    start = timezone.localtime(inv.start_at)
    if inv.all_day:
        return start.strftime("%d.%m.%Y")
    end = timezone.localtime(inv.end_at) if inv.end_at else None
    tail = f"–{end:%H:%M}" if end and end.date() == start.date() else ""
    return f"{start:%d.%m.%Y %H:%M}{tail}"


def _norm_title(title) -> str:
    t = _VARIANT_RE.sub(" ", str(title or "").lower())
    t = re.sub(r"\(?\d+\s*/\s*\d+\)?|\d+", " ", t)
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


def _key(inv):
    return (inv.user_id, (inv.organizer or "").strip().lower(), _norm_title(inv.title))


def _own(inv, user) -> bool:
    msg = inv.email_message
    return bool(user and inv.user_id == user.pk and msg is not None and getattr(msg.account, "user_id", None) == user.pk)


def _answers(inv, st, own) -> list:
    if not own:
        return []
    if st == CANCEL:
        return [{"key": "remove", "label": "✓ Прибрати з календаря", "tip": "Організатор скасував зустріч — прибрати її з вашого календаря", "main": True}]
    keys = {OPEN: ("accepted", "tentative", "declined"), MOVED: ("accepted", "tentative", "declined"),
            ADDED: ("accepted", "tentative", "declined"), ACCEPTED: ("tentative", "declined"),
            TENTATIVE: ("accepted", "declined"), DECLINED: ("accepted",), DISMISSED: ("accepted",)}.get(st, ())
    return [{"key": k, "label": ANSWERS[k][0], "tip": ANSWERS[k][1], "main": k == "accepted" and st in ACTIONABLE}
            for k in keys]


def groups(invites, user=None, now=None) -> list:
    """Запрошення, згруповані за зустріччю: {title, organizer, rows, open, choose, chosen, others}. Варіанти —
    той самий організатор і назва без «Option 1 / Variante B / 2/3»."""
    now = now or _now()
    out, by = [], {}
    for inv in invites:
        st = state(inv, now)
        own = _own(inv, user)
        msg = inv.email_message
        row = {"pk": inv.pk, "inv": inv, "title": inv.title, "when": _when(inv), "start": inv.start_at,
               "state": st, "label": LABELS[st], "cls": CLS[st], "place": inv.location[:120] if inv.location else "",
               "join": inv.join_url if inv.join_url.startswith("https://") else "",
               "mail_url": f"/email/message/{msg.pk}/" if own and msg is not None else "",
               "answers": _answers(inv, st, own), "own": own,
               "responded": timezone.localtime(inv.responded_at).strftime("%d.%m %H:%M") if inv.responded_at else ""}
        k = _key(inv)
        if k not in by:
            by[k] = {"title": inv.title, "organizer": inv.organizer, "user_id": inv.user_id, "rows": []}
            out.append(by[k])
        by[k]["rows"].append(row)
    for g in out:
        rows = g["rows"]
        live = [r for r in rows if r["state"] in ACTIONABLE]
        chosen = [r for r in rows if r["state"] in (ACCEPTED, TENTATIVE)]
        g["open"] = len(live)
        g["chosen"] = chosen[0] if chosen else None
        g["choose"] = len(rows) > 1 and len(live) >= 2 and not chosen
        g["others"] = len(rows) > 1 and len(live) + len(chosen) >= 2      # «решту варіантів відхилити»
        if len(rows) > 1:
            g["title"] = base_title(rows[0]["title"])
            for n, r in enumerate(rows, 1):
                r["variant"] = f"варіант {n} з {len(rows)}"
    return out


# ── «Потребує уваги» (overview.collect) ───────────────────────────────────────

def attention(invites, add, now=None) -> set:
    """Пункти «потребує уваги» для запрошень проєкту → id листів, на які відповідь — саме відповідь на запрошення
    (overview не показує їх ще й як «📨 Чекає нашої відповіді»)."""
    now = now or _now()
    handled = set()
    for g in groups(invites, None, now):
        rows = [r for r in g["rows"] if r["start"] >= now]
        live = [r for r in rows if r["state"] in ACTIONABLE]
        uid = g["user_id"]
        for r in g["rows"]:
            if r["state"] in ACTIONABLE + (CANCEL,) and r["inv"].email_message_id:
                handled.add(r["inv"].email_message_id)

        def url(r):
            return f"/email/message/{r['inv'].email_message_id}/" if r["inv"].email_message_id else ""

        if g["choose"] and len(live) >= 2:
            dates = ", ".join(r["when"] for r in live[:4])
            soon = any(r["start"] - now <= timedelta(hours=SOON_HOURS) for r in live)
            add("high" if soon else "medium", "📨",
                f"Пропонують {len(live)} варіанти зустрічі «{g['title']}» ({dates}) — оберіть і підтвердіть",
                url(live[0]), kind="todo", owner=uid,
                private_text=f"Пропонують {len(live)} варіанти зустрічі (запрошення в чужій скриньці) — чекають вибору")
            live = []
        elif g["chosen"] and live and len(g["rows"]) > 1:
            c = g["chosen"]
            add("info", "📨", f"Обрано «{c['title']}» {c['when']} — ще {len(live)} варіант(и) без відповіді",
                url(live[0]), kind="todo", owner=uid,
                private_text="Варіант зустрічі обрано, решта варіантів без відповіді (запрошення в чужій скриньці)")
            live = []
        for r in live:
            soon = r["start"] - now <= timedelta(hours=SOON_HOURS)
            if r["state"] == MOVED:
                add("high" if soon else "medium", "♻", f"Зустріч змінено: {r['title']} — тепер {r['when']}, підтвердіть",
                    url(r), kind="todo", owner=uid, private_text=f"Зустріч змінено організатором ({r['when']}) — підтвердіть")
            elif r["state"] == ADDED:
                add("info", "📨", f"Зустріч {r['title']} ({r['when']}) у календарі, а організатору не відповіли",
                    url(r), kind="todo", owner=uid,
                    private_text=f"Зустріч у календарі без відповіді організатору ({r['when']})")
            else:
                add("high" if soon else "medium", "📨", f"Запрошення на зустріч чекає відповіді: {r['title']} ({r['when']})",
                    url(r), kind="todo", owner=uid,
                    private_text=f"Запрошення на зустріч чекає відповіді ({r['when']}, чужа скринька)")
        for r in rows:
            if r["state"] == CANCEL:
                add("info", "✖", f"Організатор скасував зустріч: {r['title']} ({r['when']}) — приберіть з календаря",
                    url(r), kind="todo", owner=uid, private_text=f"Організатор скасував зустріч ({r['when']})")
    return handled


# ── Пульт: блок «📨 Запрошення на зустрічі» і відповідь з нього ────────────────

def cockpit(project, user, facts, now=None) -> dict:
    """Лише свої запрошення того, хто дивиться: {groups, open}."""
    now = now or _now()
    uid = getattr(user, "pk", None)
    mine = [inv for inv in facts.get("invites") or [] if inv.user_id == uid]
    gs = groups(mine, user, now)
    gs.sort(key=lambda g: (not any(r["state"] in ACTIONABLE + (CANCEL,) for r in g["rows"]),
                           min(r["start"] for r in g["rows"])))
    return {"groups": gs, "open": sum(g["open"] for g in gs)}


def answer(project, user, pk, what, others="keep") -> dict:
    """✅ / ❔ / ✕ з пульта; remove — скасоване організатором прибрати з календаря. others: decline — решту варіантів
    відхилити листом, dismiss — лише не заносити, keep — не чіпати. → {to, declined, dismissed, errors}."""
    from calendar_app import invites as cal

    from . import overview
    from . import state as project_state
    if what not in ("accepted", "tentative", "declined", "remove"):
        raise InviteError("Невідома відповідь")
    messages = overview._messages(project)
    deadlines = overview._deadlines([x["msg"].pk for x in messages], project)
    mine = related(project, messages, deadlines, user=user)
    inv = next((i for i in mine if str(i.pk) == str(pk)), None)
    if inv is None:
        raise InviteError("Запрошення не знайдено серед ваших запрошень цього проєкту — оновіть сторінку")
    out = {"to": "", "declined": 0, "dismissed": 0, "errors": []}
    if what == "remove":
        if state(inv) != CANCEL:
            raise InviteError("Організатор цю зустріч не скасовував")
        cal.accept(inv, user)
    else:
        try:
            out["to"] = cal.respond(inv, user, what)["to"]
        except cal.RespondError as e:
            raise InviteError(f"Не вдалося відповісти: {e}") from None
    if what == "accepted" and others in ("decline", "dismiss"):
        key = _key(inv)
        rest = [i for i in mine if i.pk != inv.pk and _key(i) == key and state(i) in ACTIONABLE + (TENTATIVE,)]
        for o in rest:
            if others == "dismiss":
                cal.dismiss(o, user)
                out["dismissed"] += 1
                continue
            try:
                cal.respond(o, user, "declined")
                out["declined"] += 1
            except cal.RespondError as e:
                out["errors"].append(f"{_when(o)}: {e}")
    project_state.mark_dirty([project.pk])
    return out


def notice(what, result) -> str:
    """Підсумок для пульта після відповіді."""
    head = {"accepted": "✅ Відповіли «Буду»", "tentative": "❔ Відповіли «Можливо»",
            "declined": "✕ Відхилили", "remove": "✓ Прибрали з календаря"}[what]
    parts = [head + (f" — лист організатору {result['to']}" if result.get("to") else "")]
    if result.get("declined"):
        parts.append(f"решту варіантів відхилено: {result['declined']}")
    if result.get("dismissed"):
        parts.append(f"решту варіантів не заносимо: {result['dismissed']}")
    if result.get("errors"):
        parts.append("⚠️ не вдалося: " + "; ".join(result["errors"]))
    return " · ".join(parts)
