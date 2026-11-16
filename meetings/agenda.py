"""📋 Агенда наради — що обговорити, щоб не забути (MeetingAgendaItem). Лише власник наради, як і сама нарада.

- пункти: додати, змінити текст і «що вирішили», ☑ обговорили / ⏭ не потрібно, порядок, прибрати;
- «🧭 Підказати з проєкту» (`suggest`, без AI) — з кожного проєкту наради, який людина бачить:
  ⏭ не обговорене на попередніх нарадах цього проєкту (її власних), 🎯 «Що зараз» пульта (overview.collect →
  visible: тема чужого листа чи подія чужого календаря — без тексту), ⚠️ відкриті ризики 🔴 / 🟡, 📅 найближчі віхи й
  кроки клієнта чи постачальника, ❓ відкриті питання й обіцянки з AI-огляду проєкту; уже додане (ref) — не
  пропонується;
- підпункти (parent, один рівень: 1.1, 1.2): ☑ пункту відмічає його відкриті підпункти, усі підпункти ☑ / ⏭ —
  пункт ☑, підпункт знову «обговорити» — пункт теж; порядок — серед сусідів; ✕ пункту — разом з підпунктами;
- узяли пункт з минулої наради (carry:<pk>) — там він стає «перенесено» (moved_to), його не закриті підпункти
  переходять разом з ним; прибрали — там знову «обговорити»;
- `copy_text` — агенда текстом (підпункти з відступом) для запрошення чи листа учасникам;
- 📎 файли (MeetingAgendaFile: скріншот з буфера, перетягнуті, з ПК) — до агенди або до пункту: `add_file` (тип за
  розширенням `FILE_EXT`, картинки й PDF — за першими байтами, ≤ `FILE_MAX_MB`, ≤ `FILES_MAX`), `remove_file`,
  `file_for` (лише власник; сторінка наради віддає його, /media/ теку meetings/ нікому не віддає);
- 🌐 переклад під пунктами (`Meeting.agenda_lang`, `set_tr_lang`): пишете своєю мовою — під кожним пунктом і «що
  вирішили» переклад (`MeetingAgendaItem.translations` {мова: {text, text_src, text_by, note, …}}, назва наради —
  `Meeting.agenda_title_tr`); `*_src` — відбиток оригіналу: змінили текст — переклад застарів і перекладається знову;
  `translate_missing` — один запит AI на все, чого ще немає (`_pieces`: лише тексти з літерами; українською — лише
  тексти без кирилиці), людина може виправити переклад у ✏️ (`edit` → `*_by` human); `tr_info` — що перекладено якою мовою;
- `send` — «✉️ Надіслати агенду»: чернетка у власній скриньці (учасники наради + дописані адреси, тема й привітання
  мовою листа); готовий переклад цією мовою береться як є, решту за бажанням перекладає AI (`translate` →
  core/ai_texts, Claude Haiku 5.5, бюджет AI) і запам'ятовує — людина перевіряє й надсилає сама.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import Meeting, MeetingAgendaFile, MeetingAgendaItem

TEXT_MAX = 300
NOTE_MAX = 300
ITEMS_MAX = 60
SUGGEST_MAX = 40
STEP_DAYS = 14                     # віхи й кроки клієнта / постачальника — на стільки днів уперед
SKIP_ICONS = {"🤝", "➡️", "💤"}    # «Скоро зустріч» (це ж і є нарада), «не задано наступну дію», «тиша» — не теми
SKIP_TEXTS = ("Тижневого статусу",)   # нагадування для Minerva («📝 Статус»), а не тема наради
SEND_MAX = 30                      # адрес в одному листі
FILE_MAX_MB = 15
FILES_MAX = 20                     # файлів на нараду
MAIL_MAX_MB = 20                   # усіх файлів у листі (поштові сервери далі відмовляють)
FILE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
            ".odt", ".ods", ".txt", ".csv", ".zip", ".step", ".stp", ".dxf", ".dwg", ".eml", ".msg"}
SNIFFED = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
           ".webp": "image/webp", ".pdf": "application/pdf"}
AI_MODEL = "claude-haiku-5-5"
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


_CYR = re.compile(r"[А-Яа-яЁёЄєІіЇїҐґ]")
_LAT = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")


def _needs(text, lang) -> bool:
    """Чи перекладати текст мовою lang: лише з літерами; українською — коли кирилиці немає зовсім (українське речення
    з назвою чи кодом латиницею — уже українською); німецьку від англійської так не відрізнити — латиницю AI
    перекладає, а вже потрібною мовою лишає як є."""
    cyr, lat = len(_CYR.findall(text or "")), len(_LAT.findall(text or ""))
    if not cyr and not lat:
        return False
    return not cyr if lang == "uk" else True


def _differs(tr, text) -> bool:
    return bool(tr) and _clean(tr, 2000).lower() != _clean(text, 2000).lower()


def _tr_fields(r, lang):
    """Переклад пункту для сторінки: tr_text / tr_note — показати (порожньо — немає або такий самий, як оригінал),
    *_raw — як збережено (поля ✏️), tr_stale — оригінал змінили після перекладу, tr_wait — ще не перекладено."""
    tr = ((r.translations or {}).get(lang) or {}) if lang else {}
    r.tr_text_raw = tr.get("text") or ""
    r.tr_note_raw = (tr.get("note") or "") if r.note else ""
    r.tr_text = r.tr_text_raw if _differs(r.tr_text_raw, r.text) else ""
    r.tr_note = r.tr_note_raw if _differs(r.tr_note_raw, r.note) else ""
    r.tr_stale = bool(lang) and bool((r.tr_text_raw and tr.get("text_src") != _hash(r.text))
                                     or (r.tr_note_raw and tr.get("note_src") != _hash(r.note)))
    r.tr_wait = bool(lang) and not r.frozen and bool(
        (_needs(r.text, lang) and not r.tr_text_raw) or (r.note and _needs(r.note, lang) and not r.tr_note_raw))
    r.tr_human = (tr.get("text_by") == "human") or (bool(r.tr_note_raw) and tr.get("note_by") == "human")


def files(m) -> list:
    return list(MeetingAgendaFile.objects.filter(meeting=m).order_by("created_at", "pk"))


def items(m) -> list:
    """Пункти по порядку, за кожним — його підпункти: level 0 / 1, num «1» / «1.1», first / last — серед сусідів,
    kid_count — скільки підпунктів, group_top / group_end — пункт групи й останній рядок групи (поле «＋ Підпункт»),
    frozen — пункт перенесено (і його підпункти лише для читання)."""
    rows = list(MeetingAgendaItem.objects.filter(meeting=m).select_related("project").order_by("order", "pk"))
    by_item = {}
    for f in files(m):
        if f.item_id:
            by_item.setdefault(f.item_id, []).append(f)
    for r in rows:
        r.file_list = by_item.get(r.pk, [])
    tops = [r for r in rows if not r.parent_id]
    kids = {}
    for r in rows:
        if r.parent_id:
            kids.setdefault(r.parent_id, []).append(r)
    out = []
    for n, t in enumerate(tops, 1):
        sub = kids.get(t.pk, [])
        frozen = t.status == MeetingAgendaItem.MOVED            # перенесено — разом з підпунктами лише для читання
        t.level, t.num, t.first, t.last, t.kid_count = 0, str(n), n == 1, n == len(tops), len(sub)
        t.group_top, t.group_end, t.frozen = t.pk, not sub, frozen
        out.append(t)
        for k, c in enumerate(sub, 1):
            c.level, c.num, c.first, c.last, c.kid_count = 1, f"{n}.{k}", k == 1, k == len(sub), 0
            c.group_top, c.group_end, c.frozen = t.pk, k == len(sub), frozen
            out.append(c)
    lang = m.agenda_lang if m.agenda_lang in LANGS else ""
    for r in out:
        _tr_fields(r, lang)
    return out


def progress(m, rows=None) -> dict:
    """Скільки пунктів (без підпунктів і перенесених) обговорили."""
    rows = items(m) if rows is None else rows
    live = [i for i in rows if not i.parent_id and i.status != MeetingAgendaItem.MOVED]
    return {"total": len(live), "done": sum(1 for i in live if i.status == MeetingAgendaItem.DONE),
            "open": sum(1 for i in live if i.status == MeetingAgendaItem.OPEN)}


def counts(meeting_ids) -> dict:
    """{pk наради: (обговорили, усього)} — для «🎙 Мої наради» в пульті проєкту (один запит)."""
    from django.db.models import Count
    out = {}
    rows = (MeetingAgendaItem.objects.filter(meeting_id__in=list(meeting_ids), parent__isnull=True)
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
def add(user, m, text, *, why="", ref="", url="", project=None, parent=None) -> MeetingAgendaItem:
    _owner(user, m)
    text = _clean(text, TEXT_MAX)
    if not text:
        raise _err("Напишіть, що обговорити")
    Meeting.objects.select_for_update().filter(pk=m.pk).first()         # два «＋» поспіль — без дублів і з порядком
    top = None
    if parent not in (None, "", 0, "0"):
        top = _item(user, m, parent)
        if top.parent_id:
            raise _err("Підпункт додається до пункту агенди, не до іншого підпункту")
        if top.status == MeetingAgendaItem.MOVED:
            raise _err("Пункт перенесено на наступну нараду — відмічайте його там")
        ref = ""
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
    order = (MeetingAgendaItem.objects.filter(meeting=m, parent=top).aggregate(o=Max("order"))["o"] or 0) + 10
    item = MeetingAgendaItem.objects.create(meeting=m, order=order, text=text, why=_clean(why, 200), ref=ref,
                                            url=str(url or "")[:300] if str(url or "").startswith("/") else "",
                                            project_id=pj or (top.project_id if top else None), parent=top)
    if top is not None:
        _sync_parent(top)                                     # новий підпункт — пункт знову «обговорити»
    if ref.startswith("carry:"):
        src = _carry_source(user, m, ref[6:])
        if src is not None:
            if src.translations and src.text == item.text:            # 🌐 переклад — разом з пунктом
                item.translations = src.translations
                item.save(update_fields=["translations"])
            for k, kid in enumerate(src.kids.filter(status=MeetingAgendaItem.OPEN).order_by("order", "pk"), 1):
                MeetingAgendaItem.objects.create(meeting=m, order=k * 10, text=kid.text, why=kid.why, url=kid.url,
                                                 project_id=kid.project_id, parent=item,
                                                 translations=kid.translations or {})
            MeetingAgendaItem.objects.filter(pk=src.pk).update(status=MeetingAgendaItem.MOVED, moved_to=item,
                                                               updated_at=timezone.now())
    return item


def _carry_source(user, m, pk):
    if not str(pk).isdigit():
        return None
    return (MeetingAgendaItem.objects.filter(pk=int(pk), meeting__owner=m.owner_id, parent__isnull=True)
            .exclude(meeting=m).first())


def _sync_parent(top):
    """Пункт з підпунктами: усі підпункти ☑ / ⏭ (хоч один ☑) — пункт ☑; підпункт знову відкритий — пункт теж."""
    kids = list(MeetingAgendaItem.objects.filter(parent=top).exclude(status=MeetingAgendaItem.MOVED)
                .values_list("status", flat=True))
    top.refresh_from_db(fields=["status"])
    if not kids or top.status == MeetingAgendaItem.MOVED:
        return
    closed = all(s in (MeetingAgendaItem.DONE, MeetingAgendaItem.SKIPPED) for s in kids) and MeetingAgendaItem.DONE in kids
    new = MeetingAgendaItem.DONE if closed else (MeetingAgendaItem.OPEN if top.status == MeetingAgendaItem.DONE
                                                 else top.status)
    if new != top.status:
        MeetingAgendaItem.objects.filter(pk=top.pk).update(status=new, updated_at=timezone.now())


def edit(user, m, pk, *, text=None, note=None, tr_text=None, tr_note=None) -> MeetingAgendaItem:
    """Текст і «що вирішили»; tr_text / tr_note — переклад мовою агенди, виправлений людиною (не змінили — лишається,
    а якщо змінили оригінал, стає застарілим і перекладається знову; порожньо — прибрати)."""
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
    lang = m.agenda_lang if m.agenda_lang in LANGS else ""
    if lang and (tr_text is not None or tr_note is not None):
        tr = dict(item.translations or {})
        cur, changed = dict(tr.get(lang) or {}), False
        for kind, value, src in (("text", tr_text, item.text), ("note", tr_note, item.note)):
            if value is None:
                continue
            value = _clean(value, TEXT_MAX)
            if value == _clean(cur.get(kind) or "", TEXT_MAX):
                continue                                          # переклад не чіпали
            changed = True
            if value and src:
                cur.update({kind: value, f"{kind}_src": _hash(src), f"{kind}_by": "human"})
            else:
                for k in (kind, f"{kind}_src", f"{kind}_by"):
                    cur.pop(k, None)
        if changed:
            tr[lang] = cur
            item.translations = tr
            fields.append("translations")
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
    if not item.parent_id and status == MeetingAgendaItem.DONE:
        MeetingAgendaItem.objects.filter(parent=item, status=MeetingAgendaItem.OPEN).update(
            status=MeetingAgendaItem.DONE, updated_at=timezone.now())
    elif item.parent_id:
        _sync_parent(item.parent)
    return item


@transaction.atomic
def move(user, m, pk, delta) -> None:
    item = _item(user, m, pk)
    rows = [r for r in items(m) if r.parent_id == item.parent_id]            # пункти — серед пунктів, підпункти — у пункті
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
    top = item.parent
    item.delete()                                # з підпунктами (CASCADE)
    if top is not None:
        _sync_parent(top)


def copy_text(m, rows=None, lang="") -> str:
    """Агенда текстом — для запрошення, листа чи чату учасникам; lang — збереженим перекладом (чого немає —
    оригінал)."""
    rows = items(m) if rows is None else rows
    if lang in LANGS:
        title, texts, notes = _translated(m, rows, lang)
        head = MAIL[lang]["subject"].format(title=title)
        return "\n".join([head, ""] + agenda_lines(rows, texts, notes)).strip()
    head = f"Агенда: {m.title}"
    return "\n".join([head, ""] + agenda_lines(rows)).strip()


def _live(rows) -> list:
    """Без перенесених пунктів і їхніх підпунктів."""
    gone = {r.pk for r in rows if not r.parent_id and r.status == MeetingAgendaItem.MOVED}
    return [r for r in rows if r.pk not in gone and r.parent_id not in gone]


def agenda_lines(rows, texts=None, notes=None, indent="   ", with_files=True) -> list:
    """«1. …», «   1.1. …» (+ « — що вирішили»); texts / notes — {pk: переклад}; indent — відступ підпункту (у листі —
    нерозривні пробіли: редактор листа звичайні на початку рядка прибирає)."""
    out = []
    for r in _live(rows):
        text = (texts or {}).get(r.pk) or r.text
        note = (notes or {}).get(r.pk) or r.note
        names = ", ".join(f.name for f in getattr(r, "file_list", [])) if with_files else ""
        out.append((indent if r.level else "") + f"{r.num}. {text}" + (f" — {note}" if note else "")
                   + (f" (📎 {names})" if names else ""))
    return out


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
    from django.db.models import Count
    rows = (MeetingAgendaItem.objects.filter(meeting__in=earlier, status=MeetingAgendaItem.OPEN, parent__isnull=True)
            .select_related("meeting")
            .annotate(open_kids=Count("kids", filter=Q(kids__status=MeetingAgendaItem.OPEN)))
            .order_by("-meeting__created_at", "order", "pk")[:SUGGEST_MAX])
    for r in rows:
        when = timezone.localtime(r.meeting.started_at or r.meeting.created_at).strftime("%d.%m")
        why = f"не обговорили на нараді {when}" + (f" · підпунктів: {r.open_kids}" if r.open_kids else "")
        put("carry", f"carry:{r.pk}", r.text, why,
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


# ── ✉️ Надіслати агенду (з перекладом AI за бажанням) ─────────────────────────

LANGS = {"uk": "Українська", "de": "Deutsch", "en": "English"}
MAIL = {
    "uk": {"subject": "Агенда: {title}", "hello": "Добрий день!",
           "intro": "Нижче — агенда нашої наради «{title}»{when}.", "when": " ({date})",
           "outro": "Доповнення — у відповіді на цей лист."},
    "de": {"subject": "Tagesordnung: {title}", "hello": "Guten Tag,",
           "intro": "anbei die Tagesordnung für unsere Besprechung „{title}“{when}.", "when": " am {date}",
           "outro": "Ergänzungen gern vorab per Antwort."},
    "en": {"subject": "Agenda: {title}", "hello": "Hello,",
           "intro": "please find below the agenda for our meeting “{title}”{when}.", "when": " on {date}",
           "outro": "Additions are welcome — just reply to this e-mail."},
}
def people(m, user=None) -> list:
    """Учасники наради з адресою (без своїх адрес): [{name, email}]."""
    own = set()
    if user is not None:
        from .services import _own_addresses
        try:
            own = {a.lower() for a in _own_addresses(user)}
        except Exception:  # noqa: BLE001
            own = set()
    out, seen = [], set()
    for p in m.participants or []:
        email = str((p or {}).get("email") or "").strip()
        if email and email.lower() not in seen and email.lower() not in own:
            seen.add(email.lower())
            out.append({"name": str(p.get("name") or ""), "email": email})
    return out


def clean_emails(values) -> list:
    """Адреси з галочок і поля «ще адреси» (кома, крапка з комою, пробіл, рядки) — лише справжні, без повторів."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    raw = []
    for v in values if isinstance(values, (list, tuple)) else [values]:
        raw += re.split(r"[\s,;]+", str(v or ""))
    out, seen = [], set()
    for e in raw:
        e = e.strip().strip("<>")
        if not e or e.lower() in seen:
            continue
        try:
            validate_email(e)
        except ValidationError:
            raise _err(f"Адреса «{e[:80]}» неправильна") from None
        seen.add(e.lower())
        out.append(e)
    if len(out) > SEND_MAX:
        raise _err(f"Забагато адрес — не більше {SEND_MAX} в одному листі")
    return out


def translate(user, texts, lang, hint="") -> list:
    """Тексти агенди мовою lang (core/ai_texts: Claude Haiku 5.5, бюджет AI). Не вийшло — MeetingError."""
    from core import ai_texts
    try:
        return ai_texts.translate_list(user, texts, lang, AI_MODEL, about="These are items of a meeting agenda.")
    except ai_texts.TranslateError as e:
        raise _err(f"{e}{hint}") from None


# ── 🌐 Переклад під пунктами ──────────────────────────────────────────────────

def _pieces(m, rows, lang) -> list:
    """Що ще не перекладено мовою lang (немає перекладу або оригінал змінили після нього): [(вид, пункт | None,
    текст)] — назва наради, тексти й «що вирішили» пунктів (без перенесених)."""
    if lang not in LANGS:
        return []
    out = []
    t = (m.agenda_title_tr or {}).get(lang) or {}
    if _needs(m.title, lang) and t.get("src") != _hash(m.title):
        out.append(("title", None, m.title))
    for r in _live(rows):
        tr = (r.translations or {}).get(lang) or {}
        if _needs(r.text, lang) and tr.get("text_src") != _hash(r.text):
            out.append(("text", r, r.text))
        if r.note and _needs(r.note, lang) and tr.get("note_src") != _hash(r.note):
            out.append(("note", r, r.note))
    return out


@transaction.atomic
def _store(m, lang, pieces, done, by="ai") -> None:
    """Запам'ятати переклад: кожному тексту — відбиток оригіналу, з якого перекладено (змінили потім — застарів)."""
    rows = {}
    for (kind, r, src), value in zip(pieces, done):
        if kind == "title":
            mm = Meeting.objects.select_for_update().filter(pk=m.pk).first()
            if mm is not None:
                d = dict(mm.agenda_title_tr or {})
                d[lang] = {"text": _clean(value, 200), "src": _hash(src)}
                Meeting.objects.filter(pk=m.pk).update(agenda_title_tr=d)
                m.agenda_title_tr = d
        else:
            rows.setdefault(r.pk, []).append((kind, src, value))
    for pk, parts in rows.items():
        it = MeetingAgendaItem.objects.select_for_update().filter(pk=pk, meeting=m).first()
        if it is None:                                   # прибрали, поки перекладали
            continue
        tr = dict(it.translations or {})
        cur = dict(tr.get(lang) or {})
        for kind, src, value in parts:
            cur.update({kind: _clean(value, TEXT_MAX), f"{kind}_src": _hash(src), f"{kind}_by": by})
        tr[lang] = cur
        MeetingAgendaItem.objects.filter(pk=pk).update(translations=tr)


def _translated(m, rows, lang):
    """Збережений свіжий переклад мовою lang: (назва, {pk: текст}, {pk: що вирішили}); чого немає — оригінал."""
    t = (m.agenda_title_tr or {}).get(lang) or {}
    title = t["text"] if t.get("text") and t.get("src") == _hash(m.title) else m.title
    texts, notes = {}, {}
    for r in rows:
        tr = (r.translations or {}).get(lang) or {}
        if tr.get("text") and tr.get("text_src") == _hash(r.text):
            texts[r.pk] = tr["text"]
        if r.note and tr.get("note") and tr.get("note_src") == _hash(r.note):
            notes[r.pk] = tr["note"]
    return title, texts, notes


def set_tr_lang(user, m, lang) -> None:
    """🌐 Мова перекладу під пунктами ('' — без перекладу). Уже перекладене іншою мовою лишається (для листа)."""
    _owner(user, m)
    lang = str(lang or "")
    if lang and lang not in LANGS:
        raise _err("Невідома мова перекладу")
    Meeting.objects.filter(pk=m.pk).update(agenda_lang=lang)
    m.agenda_lang = lang


def translate_missing(user, m, lang=None) -> int:
    """🌐 Перекласти все, чого ще немає мовою lang (за замовчуванням — під пунктами), одним запитом AI → скільки
    текстів перекладено."""
    _owner(user, m)
    lang = lang or m.agenda_lang
    if lang not in LANGS:
        raise _err("Виберіть мову перекладу")
    pieces = _pieces(m, items(m), lang)
    if not pieces:
        return 0
    _store(m, lang, pieces, translate(user, [p[2] for p in pieces], lang))
    return len(pieces)


def tr_info(m, rows) -> dict:
    """Для сторінки: мова під пунктами й скільки ще не перекладено; для листа — кожна мова: missing (скільки текстів
    перекладе AI), ready (усе вже перекладено — AI не потрібен); send_lang — мова листа за замовчуванням (мова під
    пунктами, інакше та, якою все вже перекладено); suggest — підказати переклад (мова співрозмовників / згоди)."""
    lang = m.agenda_lang if m.agenda_lang in LANGS else ""
    live = _live(rows)
    send = []
    for code, label in LANGS.items():
        missing = len(_pieces(m, rows, code))
        have = any(((r.translations or {}).get(code) or {}).get("text") for r in live)
        send.append({"code": code, "label": label, "missing": missing, "have": have, "ready": have and not missing})
    by = {s["code"]: s for s in send}
    suggest = ""
    if not lang and live:
        suggest = next((c for c in (m.lang_r, m.consent_lang) if c in LANGS and by[c]["missing"]), "")
    send_lang = lang or next((s["code"] for s in send if s["ready"]), "") or (
        m.consent_lang if m.consent_lang in LANGS else "de")
    return {"lang": lang, "label": LANGS.get(lang, ""), "short": lang.upper(),
            "missing": by[lang]["missing"] if lang else 0, "suggest": suggest, "suggest_label": LANGS.get(suggest, ""),
            "send": send, "send_lang": send_lang,
            "choices": [("", "без перекладу")] + [(c, LANGS[c]) for c in ("en", "de", "uk")]}


# ── 📎 Файли агенди ────────────────────────────────────────────────────────────

def add_file(user, m, upload, item=None) -> MeetingAgendaFile:
    """Файл з форми (multipart) → до агенди або до пункту item. Тип — за розширенням; картинка чи PDF — і за вмістом."""
    import os

    from email_assistant.imap_client import sniff_content_type
    _owner(user, m)
    if upload is None:
        raise _err("Виберіть файл")
    name = os.path.basename(str(upload.name or "").replace("\\", "/")).strip()[:200] or "file"
    ext = os.path.splitext(name)[1].lower()
    if ext not in FILE_EXT:
        raise _err(f"Такий файл до агенди не додається ({ext or 'без розширення'}) — картинки, PDF, документи Office, "
                   f"текст, CSV, ZIP, креслення")
    if upload.size > FILE_MAX_MB * 1024 * 1024:
        raise _err(f"Файл завеликий — не більше {FILE_MAX_MB} МБ")
    if MeetingAgendaFile.objects.filter(meeting=m).count() >= FILES_MAX:
        raise _err(f"До агенди вже додано {FILES_MAX} файлів")
    target = None
    if item not in (None, "", 0, "0"):
        target = _item(user, m, item)
    head = upload.read(2048)
    upload.seek(0)
    ctype = SNIFFED.get(ext, "")
    if ctype and sniff_content_type(head) != ctype:
        raise _err("Файл не той, за кого себе видає (вміст не збігається з розширенням)")
    import mimetypes
    ctype = ctype or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return MeetingAgendaFile.objects.create(meeting=m, item=target, file=upload, name=name, size=upload.size,
                                            content_type=ctype[:100])


def file_for(user, m, pk) -> MeetingAgendaFile:
    _owner(user, m)
    f = MeetingAgendaFile.objects.filter(meeting=m, pk=pk).first() if str(pk or "").isdigit() else None
    if f is None:
        raise _err("Файл не знайдено — оновіть сторінку")
    return f


def remove_file(user, m, pk) -> None:
    file_for(user, m, pk).delete()                 # з диска — сигнал post_delete


def mail_files(m) -> list:
    """Файли агенди для листа: [(назва, bytes, тип)]; разом більше MAIL_MAX_MB — помилка."""
    out, total = [], 0
    for f in files(m):
        try:
            with f.file.open("rb") as fh:
                data = fh.read()
        except OSError:
            raise _err(f"Файл «{f.name}» не знайдено на сервері — прибрати його з агенди й додати знову") from None
        total += len(data)
        out.append((f.name, data, f.content_type))
    if total > MAIL_MAX_MB * 1024 * 1024:
        raise _err(f"Файли разом завеликі для листа (понад {MAIL_MAX_MB} МБ) — зніміть «📎 Додати файли» або "
                   f"приберіть частину файлів")
    return out


def _when(m, lang) -> str:
    ev = m.calendar_event if m.calendar_event_id else None
    start = getattr(ev, "start_at", None)
    if not start:
        return ""
    day = timezone.localtime(start).date()
    return MAIL[lang]["when"].format(date=day.isoformat() if lang == "en" else day.strftime("%d.%m.%Y"))


def compose(user, m, lang, ai=False, attach=True) -> dict:
    """Тема й текст листа з агендою мовою lang: готовий переклад цією мовою (🌐 під пунктами чи з попереднього листа)
    береться як є; ai — решту (пункти, назву, «що вирішили») перекладає AI і запам'ятовує."""
    if lang not in MAIL:
        raise _err("Виберіть мову листа")
    rows = _live(items(m))
    if not rows:
        raise _err("Агенда порожня — спершу додайте пункти")
    if ai:
        pieces = _pieces(m, rows, lang)
        if pieces:
            _store(m, lang, pieces, translate(user, [p[2] for p in pieces], lang,
                                              hint=" — або надішліть агенду без перекладу"))
            rows = _live(items(m))
    title, texts, notes = _translated(m, rows, lang)
    t = MAIL[lang]
    lines = [t["hello"], "", t["intro"].format(title=title, when=_when(m, lang)), ""]
    lines += agenda_lines(rows, texts, notes, indent="\u00a0" * 4, with_files=attach)
    general = [f.name for f in files(m) if not f.item_id] if attach else []
    if general:                                     # 📎 файли до всієї агенди
        lines += ["", "📎 " + ", ".join(general)]
    lines += ["", t["outro"]]
    return {"subject": t["subject"].format(title=title)[:500], "body": "\n".join(lines)}


def send(user, m, to, lang, ai=False, attach=True) -> str:
    """Чернетка листа з агендою у власній скриньці (attach — і 📎 файли агенди вкладеннями) → адреса форми листа
    (надсилає людина)."""
    from email_assistant import attachments
    from email_assistant.models import EmailAccount, EmailDraft
    _owner(user, m)
    account = EmailAccount.objects.filter(user=user, is_active=True).order_by("-is_primary").first()
    if account is None:
        raise _err("У Minerva немає вашої поштової скриньки — скопіюйте агенду текстом («📋 Копіювати текстом»)")
    emails = clean_emails(to)
    stored = [attachments.store_file(n, d, t) for n, d, t in mail_files(m)] if attach else []
    mail = compose(user, m, lang, ai=ai, attach=attach)
    draft = EmailDraft.objects.create(account=account, to_emails=emails, subject=mail["subject"], body=mail["body"],
                                      attachments=stored, source=EmailDraft.SOURCE_MANUAL)
    return f"/email/compose/?draft={draft.pk}"
