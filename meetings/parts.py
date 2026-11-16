"""🎙 Нарада й проєкти: одна нарада може стосуватися кількох проєктів.

- «📁 Проєкти наради» (MeetingProject): головний — Meeting.project (завжди серед них; сигнал post_save наради,
  ensure_main_link), решта — додані людиною. У кожному з цих проєктів нараду бачить її власник («🎙 Мої наради» і
  доріжка «🎙 Наради» на шкалі, projects/timeline.own_meetings).
- 🏷 Частини (MeetingPart): позначка в записі «звідси — про проєкт …» діє до наступної позначки або до кінця;
  без проєкту — «загальне». До першої позначки — головний проєкт. Частини розшифровки в базі знань ідуть у проєкт
  своєї частини (meetings/knowledge.build), посилання з проєкту — на початок його частини.
- Проєкт пункту (MeetingItem.project): вибраний людиною у «✓ Прийняти», інакше проєкт частини, де пункт прозвучав,
  інакше головний (item_project) — туди й іде ✓ (📌 момент, ⚠️ ризик, 📅 крок).
- 💡 Підказки (suggest, без AI): у розшифровці згадано слово з назви, артикул або клієнта лише одного з проєктів
  наради — «схоже, звідси про …»; нічого не зберігають, людина ставить позначку сама.

Усе — лише власнику наради (services.get_own / _check_owner) з правом змінювати наради; проєкт — лише той, який
людина бачить (services._project).
"""
import re

from django.db import transaction
from django.utils import timezone

from .models import Meeting, MeetingItem, MeetingPart, MeetingProject

COLORS = 6                      # класи mt-pj-c0…c5 (meetings.css); «загальне» — mt-pj-gen
PARTS_MAX = 100
NOTE_MAX = 200
SUGGEST_MAX = 12
SUGGEST_GAP = 60                # секунд між підказками й від наявних позначок
WORD_MIN = 4
STOP_WORDS = {
    "проєкт", "проект", "нарада", "project", "projekt", "meeting", "angebot", "anfrage", "offer", "order", "auftrag",
    "the", "and", "und", "für", "fuer", "with", "mit", "для", "про", "новий", "нова", "нове", "test", "тест",
    "gmbh", "ltd", "llc", "inc", "ag", "kg", "co", "тов",
}
WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _err(text):
    from .services import MeetingError
    return MeetingError(text)


def _owner(user, m):
    from .services import _check_owner
    _check_owner(user, m)


# ── проєкти наради ───────────────────────────────────────────────────────────

def links(m) -> list:
    """Проєкти наради: головний першим, далі за часом додавання. [Project]"""
    out = [link.project for link in MeetingProject.objects.filter(meeting=m).select_related("project", "project__customer")]
    if m.project_id:
        main = next((p for p in out if p.pk == m.project_id), None)
        if main is None:
            main = m.project
        out = [main] + [p for p in out if p.pk != main.pk]
    return out


def colors(projects) -> dict:
    """{pk проєкту: клас кольору} — однаковий у шапці, частинах, розшифровці й пунктах."""
    return {p.pk: f"mt-pj-c{i % COLORS}" for i, p in enumerate(projects)}


def ensure_main_link(m):
    """Головний проєкт — завжди серед проєктів наради (create, edit, quick_start… — сигнал post_save)."""
    if m.project_id:
        MeetingProject.objects.get_or_create(meeting_id=m.pk, project_id=m.project_id)


def add_project(user, m, project_pk, main=False):
    """«＋ Проєкт»: ще один проєкт наради (першим — і головним). → Project."""
    from .services import _project
    _owner(user, m)
    project = _project(user, project_pk)
    if project is None:
        raise _err("Виберіть проєкт")
    with transaction.atomic():
        MeetingProject.objects.get_or_create(meeting=m, project=project)
        if main or not m.project_id:
            _set_main(m, project)
    return project


def set_main(user, m, project_pk):
    """⭐ Головний проєкт — з нього шаблон назви, клієнт за замовчуванням і проєкт частин до першої позначки."""
    _owner(user, m)
    link = MeetingProject.objects.filter(meeting=m, project_id=_pk(project_pk)).select_related("project").first()
    if link is None:
        raise _err("Цього проєкту немає серед проєктів наради")
    _set_main(m, link.project)
    return link.project


def _set_main(m, project):
    m.project = project
    fields = ["project", "updated_at"]
    if not m.customer_id and project.customer_id:
        m.customer_id = project.customer_id
        fields.append("customer")
    m.save(update_fields=fields)


def remove_project(user, m, project_pk):
    """✕ Прибрати проєкт з наради: його 🏷 позначки зникають (ці місця — до попередньої позначки), пункти, що чекають
    ✓ / ✕, знову беруть проєкт за частиною. Прийняте в проєкт лишається там. Головний — наступний або без проєкту."""
    _owner(user, m)
    pk = _pk(project_pk)
    with transaction.atomic():
        deleted, _ = MeetingProject.objects.filter(meeting=m, project_id=pk).delete()
        if not deleted and m.project_id != pk:
            raise _err("Цього проєкту немає серед проєктів наради")
        MeetingPart.objects.filter(meeting=m, project_id=pk).delete()
        MeetingItem.objects.filter(meeting=m, project_id=pk, status=MeetingItem.PENDING).update(project=None)
        if m.project_id == pk:
            rest = MeetingProject.objects.filter(meeting=m).select_related("project").order_by("added_at", "pk").first()
            m.project = rest.project if rest else None
            m.save(update_fields=["project", "updated_at"])


def _pk(value):
    from .services import _pk as pk
    return pk(value)


# ── 🏷 частини ───────────────────────────────────────────────────────────────

def parts_of(m) -> list:
    return list(MeetingPart.objects.filter(meeting=m).select_related("project").order_by("start_s", "pk"))


def set_part(user, m, second, project_pk=None, note=""):
    """🏷 «Звідси — про проєкт …» (порожньо — «загальне»). Проєкт, якого ще немає серед проєктів наради, додається.
    Та сама секунда — позначка замінюється. → MeetingPart."""
    from .services import _float, _project
    _owner(user, m)
    second = _float(str(second).strip().replace(",", ".") if isinstance(second, str) else second)
    if second is None or second < 0:
        raise _err("Не вказано місце в записі")
    if m.duration_s and second > m.duration_s + 5:
        raise _err("Це місце поза записом")
    second = round(second, 1)
    project = _project(user, project_pk) if _pk(project_pk) else None
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    with transaction.atomic():
        existing = MeetingPart.objects.filter(meeting=m, start_s=second).first()
        if existing is None and MeetingPart.objects.filter(meeting=m).count() >= PARTS_MAX:
            raise _err(f"Позначок уже {PARTS_MAX} — приберіть зайві")
        if project is not None:
            MeetingProject.objects.get_or_create(meeting=m, project=project)
            if not m.project_id:
                _set_main(m, project)
        part = existing or MeetingPart(meeting=m, start_s=second)
        part.project, part.note = project, note
        part.save()
    return part


def remove_part(user, m, part_pk):
    _owner(user, m)
    deleted, _ = MeetingPart.objects.filter(meeting=m, pk=_pk(part_pk)).delete()
    if not deleted:
        raise _err("Позначку не знайдено")


def project_at(parts, second, main=None):
    """Проєкт місця запису: остання позначка до second (її проєкт або None — «загальне»), без позначок — main.
    → (project | None, part | None)."""
    found = None
    for part in parts:
        if part.start_s <= (second or 0) + 0.05:
            found = part
        else:
            break
    if found is None:
        return main, None
    return found.project, found


def item_project(m, item, parts=None, main=None):
    """Куди піде ✓ пункту: вибраний людиною → проєкт частини, де прозвучав → головний (і для «загальне»)."""
    if item.project_id:
        return item.project
    main = main if main is not None else (m.project if m.project_id else None)
    if item.start_s is not None:
        project, _part = project_at(parts if parts is not None else parts_of(m), item.start_s, main)
        if project is not None:
            return project
    return main


def ranges(m, parts=None, main=None) -> list:
    """Частини запису для смуги над розшифровкою: [{start, end (None — до кінця), project, part, general}]."""
    parts = parts if parts is not None else parts_of(m)
    main = main if main is not None else (m.project if m.project_id else None)
    out, cur = [], {"start": 0.0, "project": main, "part": None, "general": main is None}
    for part in parts:
        if part.start_s <= 0.05:
            cur.update(project=part.project, part=part, general=part.project_id is None)
            continue
        out.append(dict(cur, end=part.start_s))
        cur = {"start": part.start_s, "project": part.project, "part": part, "general": part.project_id is None}
    out.append(dict(cur, end=None))
    return [r for r in out if r["end"] is None or r["end"] > r["start"]]


def project_seconds(m, project_pk, parts=None) -> list:
    """Де в записі мова про цей проєкт: [(початок, кінець | None)] — для посилань з проєкту."""
    return [(r["start"], r["end"]) for r in ranges(m, parts) if r["project"] is not None and r["project"].pk == project_pk]


def groups(segments, parts, main) -> list:
    """Розшифровка шматками за проєктом частини (поспіль): [(project | None, [сегменти])] — для бази знань.
    «Загальне» — головний проєкт (нарада загалом належить йому)."""
    out = []
    for seg in segments or []:
        project, _part = project_at(parts, float(seg.get("start") or 0), main)
        project = project or main
        if out and (out[-1][0] or None) == (project or None):
            out[-1][1].append(seg)
        else:
            out.append((project, [seg]))
    return out


# ── 💡 підказки (без AI) ────────────────────────────────────────────────────

def _words(text) -> set:
    return {w.lower().strip("-") for w in WORD_RE.findall(text or "") if len(w.strip("-")) >= WORD_MIN}


SIGNAL_KINDS = {"name": "слово з назви проєкту", "customer": "клієнт", "sku": "артикул", "code": "код проєкту"}


def signals(projects) -> dict:
    """{pk проєкту: {слово: вид}} (вид — SIGNAL_KINDS) — слова з назви, клієнт, коди й артикули товарів проєкту,
    яких немає в інших проєктах наради (спільне не розрізняє)."""
    raw = {}
    for p in projects:
        words = {}
        for w in _words(p.name):
            if w not in STOP_WORDS and not w.isdigit():
                words[w] = "name"
        cust = getattr(p, "customer", None)
        if cust is not None:
            for w in _words(cust.company or cust.name):
                if w not in STOP_WORDS and not w.isdigit():
                    words.setdefault(w, "customer")
        try:
            skus = list(p.products.values_list("sku", flat=True)[:50])
        except Exception:  # noqa: BLE001 — без товарів підказки все одно є
            skus = []
        for sku in skus:
            sku = (sku or "").strip().lower()
            if len(sku) >= WORD_MIN:
                words.setdefault(sku, "sku")
                head = re.match(r"[a-z]{2,}\d{4,}", sku)
                if head and head.group(0) != sku:
                    words.setdefault(head.group(0), "sku")
        words.setdefault(p.code.lower(), "code")
        raw[p.pk] = words
    out = {}
    for pk, words in raw.items():
        others = set().union(*[set(w) for k, w in raw.items() if k != pk]) if len(raw) > 1 else set()
        out[pk] = {w: why for w, why in words.items() if w not in others}
    return out


def suggest(m, segments, projects=None, parts=None) -> list:
    """💡 «Схоже, звідси про проєкт …»: рядок розшифровки згадує лише один проєкт наради, а за поточними позначками
    тут інший. [{second, time, project, word, why (вид слова), text}] ≤ SUGGEST_MAX; лише коли проєктів ≥ 2."""
    projects = projects if projects is not None else links(m)
    if len(projects) < 2 or not segments:
        return []
    parts = parts if parts is not None else parts_of(m)
    main = m.project if m.project_id else None
    sig = signals(projects)
    by_pk = {p.pk: p for p in projects}
    marked = [p.start_s for p in parts]
    out, believed, believed_in, last = [], None, None, None
    for seg in segments:
        text = " ".join(str(seg.get("text") or "").split())
        words = _words(text)
        hits = {pk: sorted(w for w in sig[pk] if w in words) for pk in sig}
        hits = {pk: found[0] for pk, found in hits.items() if found}
        if len(hits) != 1:
            continue
        (pk, word), = hits.items()
        second = float(seg.get("start") or 0)
        here, part = project_at(parts, second, main)
        if part is not believed_in:              # нова позначка людини — далі рахуємо від неї
            believed, believed_in = None, part
        now = believed if believed is not None else (here.pk if here is not None else None)
        if pk == now:
            believed = pk
            continue
        if any(abs(second - s) < SUGGEST_GAP for s in marked) or (last is not None and second - last < SUGGEST_GAP):
            continue
        out.append({"second": round(second, 1), "time": _clock(second), "project": by_pk[pk], "word": word,
                    "why": SIGNAL_KINDS[sig[pk][word]], "text": text[:160]})
        believed, last = pk, second
        if len(out) >= SUGGEST_MAX:
            break
    return out


def _clock(seconds) -> str:
    from .admin_pages import clock
    return clock(seconds)


def where(m, project_pk) -> tuple:
    """Для наради в проєкті (projects/timeline.own_meetings): (текст, секунда) — «🏷 частина 00:05:12–00:23:40» /
    «🏷 частин про цей проєкт: 2» і де почати; уся нарада про цей проєкт (без позначок) — ('', None); проєкт наради
    без жодної своєї частини — («🏷 згадано», None)."""
    if not MeetingPart.objects.filter(meeting_id=m.pk).exists():
        return "", None
    spans = project_seconds(m, project_pk)
    if not spans:
        return "🏷 у цьому проєкті без позначених частин", None
    first = int(spans[0][0])
    if len(spans) == 1:
        start, end = spans[0]
        if start <= 0.05 and end is None:
            return "", None
        return f"🏷 частина {_clock(start)}–{_clock(end) if end is not None else 'кінець'}", first
    return f"🏷 частин про цей проєкт: {len(spans)}", first


def pending_for(m, project_pk, total=None):
    """Скільки пунктів чекають ✓ / ✕ саме в цьому проєкті (нарада про кілька проєктів — за item_project);
    нарада про один проєкт без позначок — усі (total, якщо вже пораховано)."""
    several = MeetingProject.objects.filter(meeting_id=m.pk).exclude(project_id=project_pk).exists()
    if not several and not MeetingPart.objects.filter(meeting_id=m.pk).exists():
        return total if total is not None else MeetingItem.objects.filter(meeting_id=m.pk,
                                                                          status=MeetingItem.PENDING).count()
    marks = parts_of(m)
    main = m.project if m.project_id else None
    items = MeetingItem.objects.filter(meeting_id=m.pk, status=MeetingItem.PENDING).select_related("project")
    count = 0
    for item in items:
        project = item_project(m, item, marks, main)
        if project is not None and project.pk == project_pk:
            count += 1
    return count


def touch(m):
    Meeting.objects.filter(pk=m.pk).update(updated_at=timezone.now())
