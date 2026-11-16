"""📋 Агенда наради — що обговорити, щоб не забути (MeetingAgendaItem). Лише власник наради, як і сама нарада.

- пункти: додати, змінити текст і «що вирішили», ☑ обговорили / ⏭ не потрібно, порядок, прибрати;
- «🧭 Підказати з проєкту» (`suggest`, без AI) — з кожного проєкту наради, який людина бачить:
  ⏭ не обговорене на попередніх нарадах цього проєкту (її власних), 🎯 «Що зараз» пульта (overview.collect →
  visible: тема чужого листа чи подія чужого календаря — без тексту), ⚠️ відкриті ризики 🔴 / 🟡, 📅 найближчі віхи й
  кроки клієнта чи постачальника, ❓ відкриті питання й обіцянки з AI-огляду проєкту; уже додане (ref) — не
  пропонується;
- узяли пункт з минулої наради (carry:<pk>) — там він стає «перенесено» (moved_to); прибрали — знову «обговорити»;
- `copy_text` — агенда текстом для запрошення чи листа учасникам.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import Meeting, MeetingAgendaItem

TEXT_MAX = 300
NOTE_MAX = 300
ITEMS_MAX = 60
SUGGEST_MAX = 40
STEP_DAYS = 14                     # віхи й кроки клієнта / постачальника — на стільки днів уперед
SKIP_ICONS = {"🤝", "➡️", "💤"}    # «Скоро зустріч» (це ж і є нарада), «не задано наступну дію», «тиша» — не теми
SKIP_TEXTS = ("Тижневого статусу",)   # нагадування для Minerva («📝 Статус»), а не тема наради
GROUPS = [("carry", "⏭ Не обговорили минулого разу"), ("attn", "🎯 Що зараз потребує уваги"),
          ("risk", "⚠️ Відкриті ризики"), ("step", "📅 Найближчі віхи й кроки"),
          ("q", "❓ З AI-огляду проєкту")]


def _err(text):
    from .services import MeetingError
    return MeetingError(text)


def _owner(user, m):
    from .services import _check_owner
    _check_owner(user, m)


def _clean(text, limit) -> str:
    return " ".join(str(text or "").split())[:limit]


def _hash(text) -> str:
    return hashlib.sha1(_clean(text, 2000).lower().encode("utf-8")).hexdigest()[:16]


def _fmt(d) -> str:
    return d.strftime("%d.%m") if d else ""


def items(m) -> list:
    return list(MeetingAgendaItem.objects.filter(meeting=m).select_related("project").order_by("order", "pk"))


def progress(m, rows=None) -> dict:
    rows = items(m) if rows is None else rows
    live = [i for i in rows if i.status != MeetingAgendaItem.MOVED]
    return {"total": len(live), "done": sum(1 for i in live if i.status == MeetingAgendaItem.DONE),
            "open": sum(1 for i in live if i.status == MeetingAgendaItem.OPEN)}


def counts(meeting_ids) -> dict:
    """{pk наради: (обговорили, усього)} — для «🎙 Мої наради» в пульті проєкту (один запит)."""
    from django.db.models import Count
    out = {}
    rows = (MeetingAgendaItem.objects.filter(meeting_id__in=list(meeting_ids))
            .exclude(status=MeetingAgendaItem.MOVED).values("meeting_id")
            .annotate(total=Count("pk"), done=Count("pk", filter=Q(status=MeetingAgendaItem.DONE))))
    for r in rows:
        out[r["meeting_id"]] = (r["done"], r["total"])
    return out


def _projects(user, m) -> list:
    """Проєкти наради, які людина бачить (головний першим)."""
    if not user.has_perm("projects.view_project"):
        return []
    from . import parts
    return parts.links(m)


def _item(user, m, pk) -> MeetingAgendaItem:
    _owner(user, m)
    item = MeetingAgendaItem.objects.filter(meeting=m, pk=pk).first() if str(pk or "").isdigit() else None
    if item is None:
        raise _err("Пункт агенди не знайдено — оновіть сторінку")
    return item


@transaction.atomic
def add(user, m, text, *, why="", ref="", url="", project=None) -> MeetingAgendaItem:
    _owner(user, m)
    text = _clean(text, TEXT_MAX)
    if not text:
        raise _err("Напишіть, що обговорити")
    Meeting.objects.select_for_update().filter(pk=m.pk).first()         # два «＋» поспіль — без дублів і з порядком
    ref = _clean(ref, 60)
    if ref:
        same = MeetingAgendaItem.objects.filter(meeting=m, ref=ref).first()
        if same:
            return same
    if MeetingAgendaItem.objects.filter(meeting=m).count() >= ITEMS_MAX:
        raise _err(f"В агенді вже {ITEMS_MAX} пунктів — забагато для однієї наради")
    pj = getattr(project, "pk", project)
    if pj and pj not in {p.pk for p in _projects(user, m)}:
        pj = None
    order = (MeetingAgendaItem.objects.filter(meeting=m).aggregate(o=Max("order"))["o"] or 0) + 10
    item = MeetingAgendaItem.objects.create(meeting=m, order=order, text=text, why=_clean(why, 200), ref=ref,
                                            url=str(url or "")[:300] if str(url or "").startswith("/") else "",
                                            project_id=pj or None)
    if ref.startswith("carry:"):
        src = _carry_source(user, m, ref[6:])
        if src is not None:
            MeetingAgendaItem.objects.filter(pk=src.pk).update(status=MeetingAgendaItem.MOVED, moved_to=item,
                                                               updated_at=timezone.now())
    return item


def _carry_source(user, m, pk):
    if not str(pk).isdigit():
        return None
    return (MeetingAgendaItem.objects.filter(pk=int(pk), meeting__owner=m.owner_id)
            .exclude(meeting=m).first())


def edit(user, m, pk, *, text=None, note=None) -> MeetingAgendaItem:
    item = _item(user, m, pk)
    fields = []
    if text is not None:
        text = _clean(text, TEXT_MAX)
        if not text:
            raise _err("Напишіть, що обговорити")
        item.text = text
        fields.append("text")
    if note is not None:
        item.note = _clean(note, NOTE_MAX)
        fields.append("note")
    if fields:
        item.save(update_fields=fields + ["updated_at"])
    return item


def set_status(user, m, pk, status) -> MeetingAgendaItem:
    item = _item(user, m, pk)
    if status not in (MeetingAgendaItem.OPEN, MeetingAgendaItem.DONE, MeetingAgendaItem.SKIPPED):
        raise _err("Невідомий стан пункту")
    if item.status == MeetingAgendaItem.MOVED:
        raise _err("Пункт перенесено на наступну нараду — відмічайте його там")
    item.status = status
    item.save(update_fields=["status", "updated_at"])
    return item


@transaction.atomic
def move(user, m, pk, delta) -> None:
    item = _item(user, m, pk)
    rows = items(m)
    i = next((k for k, r in enumerate(rows) if r.pk == item.pk), None)
    j = (i or 0) + (1 if int(delta or 0) > 0 else -1)
    if i is None or not 0 <= j < len(rows):
        return
    rows[i], rows[j] = rows[j], rows[i]
    for k, r in enumerate(rows):
        if r.order != (k + 1) * 10:
            MeetingAgendaItem.objects.filter(pk=r.pk).update(order=(k + 1) * 10)


@transaction.atomic
def remove(user, m, pk) -> None:
    item = _item(user, m, pk)
    if item.ref.startswith("carry:"):          # узяли з минулої наради — там він знову «обговорити»
        (MeetingAgendaItem.objects.filter(moved_to=item, meeting__owner=m.owner_id)
         .update(status=MeetingAgendaItem.OPEN, moved_to=None, updated_at=timezone.now()))
    item.delete()


def copy_text(m, rows=None) -> str:
    """Агенда текстом — для запрошення, листа чи чату учасникам."""
    rows = [i for i in (items(m) if rows is None else rows) if i.status != MeetingAgendaItem.MOVED]
    head = f"Агенда: {m.title}"
    lines = [head, ""] + [f"{n}. {i.text}" + (f" — {i.note}" if i.note else "") for n, i in enumerate(rows, 1)]
    return "\n".join(lines).strip()


# ── 🧭 Підказати з проєкту ──────────────────────────────────────────────────

def suggest(user, m, today=None) -> list:
    """[{ref, text, why, url, project, group}] — з проєктів наради, яких ще немає в агенді (без AI)."""
    _owner(user, m)
    today = today or timezone.localdate()
    have = set(MeetingAgendaItem.objects.filter(meeting=m).exclude(ref="").values_list("ref", flat=True))
    out, seen = [], set(have)

    def put(group, ref, text, why, url="", project=None):
        text = _clean(text, TEXT_MAX)
        if not text or ref in seen or len(out) >= SUGGEST_MAX:
            return
        seen.add(ref)
        out.append({"group": group, "ref": ref, "text": text, "why": _clean(why, 200), "url": url or "",
                    "project": getattr(project, "pk", None)})

    projects = _projects(user, m)
    _carry(m, projects, put)
    for p in projects:
        _from_project(user, p, today, put, label=len(projects) > 1)
    order = {g: k for k, (g, _) in enumerate(GROUPS)}
    return sorted(out, key=lambda r: order.get(r["group"], 99))


def grouped(rows) -> list:
    return [{"key": g, "title": title, "rows": [r for r in rows if r["group"] == g]}
            for g, title in GROUPS if any(r["group"] == g for r in rows)]


def _carry(m, projects, put):
    """⏭ Не обговорене на попередніх нарадах тих самих проєктів (лише нарад цієї людини)."""
    pks = [p.pk for p in projects]
    if not pks:
        return
    from .models import MeetingProject
    earlier = (Meeting.objects.filter(owner_id=m.owner_id, created_at__lt=m.created_at)
               .filter(Q(project_id__in=pks) | Q(pk__in=MeetingProject.objects.filter(project_id__in=pks)
                                                  .values("meeting_id"))))
    rows = (MeetingAgendaItem.objects.filter(meeting__in=earlier, status=MeetingAgendaItem.OPEN)
            .select_related("meeting").order_by("-meeting__created_at", "order", "pk")[:SUGGEST_MAX])
    for r in rows:
        when = timezone.localtime(r.meeting.started_at or r.meeting.created_at).strftime("%d.%m")
        put("carry", f"carry:{r.pk}", r.text, f"не обговорили на нараді {when}",
            url=f"/admin/meetings/meeting/{r.meeting_id}/page/#agenda", project=r.project_id)


def _from_project(user, p, today, put, label=False):
    from projects import overview
    from projects.models import ProjectRisk, ProjectStep
    tag = f" · {p.code}" if label else ""
    page = f"/admin/projects/project/{p.pk}/change/"
    try:
        facts = overview.collect(p, today=today)
    except Exception:  # noqa: BLE001 — без «Що зараз» підказки з ризиків і плану однаково є
        facts = {"attention": []}
    risk_titles = []
    for a in overview.visible(facts.get("attention", []), user):
        if a.get("icon") in SKIP_ICONS or not a.get("text") or a["text"].startswith(SKIP_TEXTS):
            continue
        risk_titles.append(a["text"])
        put("attn", f"attn:{p.pk}:{_hash(a['text'])}", f"{a.get('icon', '')} {a['text']}".strip(),
            ("терміново" if a.get("level") == "high" else "потребує уваги") + tag, url=a.get("url") or page, project=p)
    for r in (ProjectRisk.objects.filter(project=p, status__in=[ProjectRisk.OPEN, ProjectRisk.WATCH])
              .order_by("-probability", "-impact", "pk")):
        if r.level == "green" or any(r.title in t for t in risk_titles):
            continue
        put("risk", f"risk:{r.pk}", f"⚠️ {r.title}" + (f" — заходи: {r.mitigation}" if r.mitigation else ""),
            ("ризик 🔴" if r.level == "red" else "ризик 🟡") + tag, url=f"{page}#pj-risks", project=p)
    horizon = today + timedelta(days=STEP_DAYS)
    steps = (ProjectStep.objects.filter(project=p).exclude(eff_status__in=["done", "skipped"])
             .exclude(status__in=[ProjectStep.DONE, ProjectStep.SKIPPED]).order_by("f_end", "pk"))
    for s in steps:
        end = s.f_end or s.end
        start = s.f_start or s.start or end
        if s.kind == ProjectStep.MILESTONE and end and end <= horizon:
            put("step", f"step:{s.pk}", f"◆ {s.title} — до {_fmt(end)}", "віха плану" + tag, url=f"{page}#pj-plan",
                project=p)
        elif s.side in (ProjectStep.CUSTOMER, ProjectStep.SUPPLIER) and start and start <= horizon:
            who = "від клієнта" if s.side == ProjectStep.CUSTOMER else "від постачальника"
            put("step", f"step:{s.pk}", f"{s.title}" + (f" — до {_fmt(end)}" if end else ""),
                f"чекаємо {who}" + tag, url=f"{page}#pj-plan", project=p)
    ov = getattr(p, "overview", None) if hasattr(p, "overview") else None
    try:
        data = ov.data if ov else {}
    except Exception:  # noqa: BLE001
        data = {}
    for q in (data or {}).get("open_questions", [])[:10]:
        text = (q or {}).get("text")
        if text:
            put("q", f"q:{p.pk}:{_hash(text)}", f"❓ {text}", "відкрите питання (AI-огляд)" + tag, url=f"{page}#overview",
                project=p)
    for pr in (data or {}).get("promises", [])[:10]:
        if (pr or {}).get("status") in ("open", "overdue") and pr.get("text"):
            due = f" — до {pr['due']}" if pr.get("due") else ""
            put("q", f"q:{p.pk}:{_hash(pr['text'])}", f"🤝 {pr['text']}{due}",
                ("обіцянку прострочено" if pr.get("status") == "overdue" else "обіцянка (AI-огляд)") + tag,
                url=f"{page}#overview", project=p)


def take(user, m, refs, today=None) -> int:
    """Узяти підказки (refs; «*» — усі, що зараз підказуються). → скільки додано."""
    rows = suggest(user, m, today=today)
    want = set(refs or [])
    added = 0
    for r in rows:
        if "*" in want or r["ref"] in want:
            add(user, m, r["text"], why=r["why"], ref=r["ref"], url=r["url"], project=r["project"])
            added += 1
    if not added:
        raise _err("Цієї підказки вже немає — оновіть список")
    return added


def upcoming(project, user, facts, today=None) -> list:
    """🤝 Найближчі зустрічі проєкту з календаря людини (overview.collect → terms): [{title, day, url, agenda}] —
    «📋 Агенда»: уже підготовлена нарада з цією подією → її сторінка, інакше — підготовка наради з подією."""
    from projects import terms, timeline
    today = today or timezone.localdate()
    uid = getattr(user, "pk", None)
    out = []
    for t in facts.get("terms", []) or []:
        ev = t.event
        if not t.meeting or t.state not in (terms.SOON, terms.PLANNED) or getattr(ev, "user_id", None) != uid:
            continue
        day = timezone.localtime(ev.start_at).date() if ev.start_at else None
        if not day or not today <= day <= today + timedelta(days=STEP_DAYS):
            continue
        m = Meeting.objects.filter(owner_id=uid, calendar_event_id=ev.pk).order_by("-created_at").first()
        if m is not None:
            done, total = counts([m.pk]).get(m.pk, (0, 0))
            out.append({"title": ev.title, "day": day, "url": f"/admin/meetings/meeting/{m.pk}/page/#agenda",
                        "agenda": total, "done": done, "prepared": True})
        else:
            url = timeline.new_meeting_url(user, project=project, event=ev.pk)
            if url:
                out.append({"title": ev.title, "day": day, "url": url, "agenda": 0, "done": 0, "prepared": False})
    return sorted(out, key=lambda r: r["day"])
