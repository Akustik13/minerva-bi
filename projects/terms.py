"""📅 Терміни й зустрічі проєкту — події календаря з листів проєкту: що з них справді прострочено.

classify(events, messages) → [Term]:
- зустріч — тип «🤝 Зустріч» або в назві «Telko», «Call», «Meeting», «Besprechung», «розмова», «дзвінок», «зустріч»…:
  минула означає «відбулась», а не «прострочено»;
- термін: ✓ позначила людина — виконано; після листа, з якого термін, ми відповіли в тій самій гілці не раніше ніж
  за MET_WINDOW_DAYS до терміну — «ймовірно виконано» (раніша відповідь — лише «дякуємо, зробимо»); після терміну інша сторона написала в гілці — «листування продовжилось, перевірте»; інакше —
  прострочено;
- відпустка, свято, відсутність у назві («Початок відпустки», «Urlaub», «Betriebsferien») — минула лише «минуло»;
- однакові події (та сама людина, назва й день — AI знайшов ту саму зустріч у кількох листах) — одна, ×N.
Ним користуються шкала (timeline) і «потребує уваги» (overview.collect). Рішення людини — у календарі
(✓ виконано, тип події); шкала дає це зробити кліком на ромб.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

MEETING_RE = re.compile(
    r"(?<!\w)(telko|telefon\w*|video(?:call|konferenz|chat|anruf|meeting)\w*|call|calls|meeting|meet|"
    r"\w*besprechung\w*|\w*gespr[äa]ch\w*|\w*konferenz\w*|teams|zoom|"
    r"webex|anruf\w*|termin vor ort|розмов\w*|дзвін\w*|зустріч\w*|нарад\w*|конференц\w*|відеодзвін\w*|созвон\w*|"
    r"відео(?:нарад|зустріч|дзвін|конференц|зв.язок)\w*|"
    r"звонок\w*|встреч\w*|совещан\w*)(?!\w)", re.I)
# Лише в назві події (в описі «visit our website», «Besuch der Messe» — не зустріч)
VISIT_RE = re.compile(r"(?<!\w)(візит\w*|besuch\w*|visit|vor[ -]ort\w*|приїзд\w*|приїде\w*)(?!\w)", re.I)
# Відпустка, свято, відсутність — минула така подія не «прострочено»
ABSENCE_RE = re.compile(
    r"(?<!\w)(відпустк\w*|відпочин\w*|свят[оа]|відсутн\w*|urlaub\w*|\w*ferien|feiertag\w*|"
    r"abwesen\w*|vacation|holiday\w*|day off|out of office|ooo)(?!\w)", re.I)
SOON_DAYS = 3
MET_WINDOW_DAYS = 7      # наша відповідь раніше за термін − 7 днів — лише «дякуємо, зробимо», не виконання

HELD, DONE, MET, MOVED, OVERDUE, SOON, PLANNED, PASSED = (
    "held", "done", "met", "moved", "overdue", "soon", "planned", "passed")
LABELS = {HELD: "відбулась", DONE: "виконано", MET: "ймовірно виконано", MOVED: "листування продовжилось — перевірте",
          OVERDUE: "прострочено", SOON: "скоро", PLANNED: "заплановано", PASSED: "минуло"}
CLS = {HELD: "ok", DONE: "ok", MET: "ok", MOVED: "warn", OVERDUE: "bad", SOON: "warn", PLANNED: "open", PASSED: "ok"}


@dataclass
class Term:
    event: object
    meeting: bool
    state: str
    note: str = ""
    count: int = 1
    evidence: object = None             # лист, на якому ґрунтується «ймовірно виконано» / «продовжилось»
    duplicates: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.state]

    @property
    def cls(self) -> str:
        return CLS[self.state]

    @property
    def icon(self) -> str:
        return "🤝" if self.meeting else "⏰"


def is_meeting(event) -> bool:
    if getattr(event, "type_by_user", False):        # людина вибрала тип у меню події — не вгадуємо
        return getattr(event, "event_type", "") == "meeting"
    if getattr(event, "event_type", "") == "meeting":
        return True
    if VISIT_RE.search(event.title or ""):
        return True
    if getattr(event, "event_type", "") == "deadline" and not MEETING_RE.search(event.title or ""):
        return False
    return bool(MEETING_RE.search(f"{event.title or ''} {event.description or ''}"[:600]))


def is_absence(event) -> bool:
    """Відпустка, свято, відсутність — подія для відома, а не справа (людина вибрала тип — не вгадуємо)."""
    if getattr(event, "type_by_user", False):
        return False
    return bool(ABSENCE_RE.search(event.title or ""))


def _day(value):
    if value is None:
        return None
    return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()


def _norm(text) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").lower()).split())


def classify(events, messages, now=None) -> list:
    """Події календаря (один раз, без повторів) зі станом. messages — листи проєкту [{msg, dir}] (overview.collect)."""
    now = now or timezone.now()
    today = timezone.localdate(now) if timezone.is_aware(now) else now.date()
    by_pk = {x["msg"].pk: x for x in messages}
    threads = defaultdict(list)
    for x in messages:
        m = x["msg"]
        threads[m.thread_id or f"m{m.pk}"].append(x)
    out, seen = [], {}
    for e in sorted(events, key=lambda ev: (ev.start_at, ev.pk)):
        key = (e.user_id, _norm(e.title), _day(e.start_at))
        if key in seen:
            seen[key].count += 1
            seen[key].duplicates.append(e)
            continue
        meeting = is_meeting(e)
        t = Term(event=e, meeting=meeting, state=PLANNED)
        seen[key] = t
        out.append(t)
        past = (e.end_at or e.start_at) < now if meeting else e.start_at < now
        if e.is_done:
            t.state = HELD if meeting else DONE
            continue
        if not past:
            t.state = SOON if (_day(e.start_at) - today).days <= SOON_DAYS else PLANNED
            continue
        if meeting:
            t.state = HELD
            continue
        if is_absence(e):
            t.state = PASSED
            continue
        src = by_pk.get(e.email_message_id)
        later = []
        if src is not None and src["msg"].sent_at:
            tid = src["msg"].thread_id or f"m{src['msg'].pk}"
            later = sorted((y for y in threads[tid] if y["msg"].sent_at and y["msg"].sent_at > src["msg"].sent_at),
                           key=lambda y: y["msg"].sent_at)
        ours = [y for y in later if y["dir"] == "out" and y["msg"].sent_at >= e.start_at - timedelta(days=MET_WINDOW_DAYS)]
        theirs = [y for y in later if y["dir"] == "in" and y["msg"].sent_at >= e.start_at]
        if ours:
            t.state, t.evidence = MET, ours[0]["msg"]
            t.note = f"після листа з терміном ми відповіли {_day(ours[0]['msg'].sent_at):%d.%m.%Y}"
        elif theirs:
            t.state, t.evidence = MOVED, theirs[0]["msg"]
            t.note = f"після терміну інша сторона написала {_day(theirs[0]['msg'].sent_at):%d.%m.%Y}"
        else:
            t.state = OVERDUE
    return out
