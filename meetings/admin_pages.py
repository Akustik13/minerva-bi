"""🎙 Наради — що показують сторінки (meetings/admin.py лише приймає запити й викликає ці функції та
meetings/services.py).

- підготовка («🎙 Записати нараду», new/ і continue/): стан форми з prefill або з POST, варіанти проєкту й клієнта;
- сторінка наради (<pk>/page/): шапка, картка запису за станом, «🧾 Протокол згоди», «🧠 Ключові моменти» (✓ / ✕),
  «📝 Розшифровка», «📚 База знань»;
- тексти ходу запису для опитування (<pk>/status/).

Приватність: усе тут — лише для власника наради (перевіряє services.get_own у кожному запиті). Назва, розшифровка,
цитати, імена й адреси в шаблонах — лише в елементах translate="no".
"""
from datetime import timedelta
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from . import consent, knowledge, services
from .models import LANG_CHOICES, RETENTION_CHOICES, Meeting, MeetingItem, MeetingTranscript

PROJECT_OPTIONS_MAX = 300
STAFF_MAX = 300
SILENT_WARN = timedelta(minutes=60)       # RAG-ПК давно не повідомляв про хід запису
SOURCE_LABELS = {"invite": "із запрошення", "mail": "з листа", "project": "учасник проєкту",
                 "customer": "клієнт", "manual": "додано вручну"}
CONSENT_LANG_LABELS = {"de": "Deutsch", "en": "English", "uk": "Українська"}
STATUS_TONE = {Meeting.READY: "idle", Meeting.RECORDING: "rec", Meeting.PROCESSING: "warn",
               Meeting.REVIEW: "ok", Meeting.FAILED: "err"}
TARGET_TEXT = {
    "moment": ("📌 Важливий момент", "на шкалі проєкту (бачать усі в проєкті)"),
    "risk": ("⚠️ Ризик", "у реєстр ризиків проєкту"),
    "step": ("📅 Крок плану", "з виконавцем і строком, з’явиться в календарі виконавця"),
    "deadline": ("⏰ Дедлайн у моєму календарі", "бачите лише ви"),
}
TARGET_DONE = {"moment": "📌 момент", "risk": "⚠️ ризик", "step": "📅 крок плану", "deadline": "⏰ дедлайн"}
SHARED_WARNING = ("🔒 Пункт і цитату побачать усі, хто бачить проєкт. Назву пункту можуть отримати "
                  "AI-помічники Minerva.")
DEADLINE_WARNING = ("🔒 Дедлайн бачите лише ви; назва піде й у ваші підписки календаря (Google / Outlook), "
                    "якщо вони є.")
NO_HEADSET_NOTE = "Запис без гарнітури: слова співрозмовників могли потрапити й у «Я»."
PHONE_NOTE = ("Записувати можна лише з комп’ютера в офісі (Chrome або Edge). На телефоні — перегляд, ✓ / ✕ і "
              "база знань.")
CLOSE_NOTE = "Сторінку можна закрити — коли буде готово, прийде лист і повідомлення в Telegram."
MOMENTS_WAIT = "🧠 Шукаю ключові моменти — з’являться тут самі, зазвичай за 5–20 хв."
NEVER_SEEN = ("RAG-ПК ще не підключено до Minerva. Попросіть адміністратора: «⚙️ Налаштування нарад» → "
              "«🔑 Створити ключ для RAG-ПК (наради)».")
REDO_CONFIRM = ("Попередній запис цієї наради буде видалено з RAG-ПК. Записувати заново варто, лише якщо нарада ще "
                "триває або буде повторена.")
KB_TEXT = ("Розшифровка стане вашим приватним документом бази знань (бачите лише ви), прив’язаним до проєкту й "
           "клієнта. Її знайде пошук у базі знань — у пошті, картках і помічнику — лише для вас; хмарні "
           "AI-помічники Minerva її не отримують.")
NO_CHANGE_TEXT = ("Немає права змінювати наради — лише перегляд. Попросіть адміністратора дати право «редагування» "
                  "в розділі «🎙 Наради».")


# ── дрібниці ─────────────────────────────────────────────────────────────────

def can_change(user) -> bool:
    """✓ / ✕, «✏️ Змінити», база знань, «🔄 Спробувати ще раз» — право змінювати наради (роль Minerva)."""
    return bool(user is not None and user.has_perm("meetings.change_meeting"))


def url(name, *args, anchor="", query=""):
    try:
        return reverse(name, args=args) + query + anchor
    except NoReverseMatch:
        return ""


def clock(seconds) -> str:
    return knowledge.clock(seconds)


def duration_text(seconds) -> str:
    """1 год 12 хв / 12 хв / 45 с."""
    if seconds is None:
        return ""
    s = max(int(round(seconds)), 0)
    hours, minutes = s // 3600, s % 3600 // 60
    if hours:
        return f"{hours} год {minutes} хв" if minutes else f"{hours} год"
    if minutes:
        return f"{minutes} хв"
    return f"{s} с"


def ttl_text(seconds) -> str:
    s = int(seconds or 120)
    return f"{s // 60} хв" if s % 60 == 0 else f"{s} с"


def bench_host(settings=None) -> str:
    base = services.bench_url(settings)
    try:
        return urlparse(base).hostname or base
    except ValueError:
        return base


def local(dt):
    return timezone.localtime(dt) if dt else None


def plural(n, one, few, many) -> str:
    return consent._plural_uk(int(n), one, few, many)


# ── RAG-ПК на зв'язку ────────────────────────────────────────────────────────

def health(user=None, settings=None, now=None) -> dict:
    """Рядок стану RAG-ПК для картки запису й налаштувань: {level ok|warn|never, text, block, busy_text}.
    block — RAG-ПК ще ні разу не виходив на зв'язок (кнопка запису неактивна). «Інший запис» не показується, якщо
    зараз записує сама людина (це її запис)."""
    s = settings or services.settings_obj()
    h = services.bench_health(s, now)
    out = {"level": "never", "text": NEVER_SEEN, "block": True, "busy_text": ""}
    if h["never"]:
        return out
    seen = local(h["seen_at"])
    if h["ok"]:
        minutes = h["minutes"]
        ago = "щойно" if minutes < 1 else f"{minutes} хв тому"
        out.update(level="ok", text=f"🟢 RAG-ПК на зв'язку: {ago}", block=False)
    else:
        out.update(level="warn", text=f"⚠️ RAG-ПК не виходив на зв'язок з {seen:%d.%m.%Y %H:%M} — запис може не "
                                      f"відкритися; якщо так і буде, повідомте адміністратора.", block=False)
    own_live = user is not None and services.own(user).filter(status=Meeting.RECORDING).exists()
    if h["busy"] and not own_live:
        out["busy_text"] = ("⏳ На RAG-ПК зараз іде інший запис — одночасно лише один. Спробуйте, коли він "
                            "закінчиться.")
    return out


def bench_text(settings=None, now=None) -> str:
    """Для «⚙️ Налаштування нарад»: на зв'язку, версія, черга, помилка."""
    s = settings or services.settings_obj()
    h = services.bench_health(s, now)
    if h["never"]:
        return "RAG-ПК ще ні разу не виходив на зв'язок (немає ключа або адреси Minerva на RAG-ПК)."
    info = h["info"] or {}
    parts = [f"востаннє {local(h['seen_at']):%d.%m.%Y %H:%M}" + (" 🟢" if h["ok"] else " ⚠️ давно")]
    if info.get("version"):
        parts.append(f"версія {info['version']}")
    if info.get("live_busy"):
        parts.append("зараз іде запис")
    for key, label in (("queue", "у черзі"), ("outbox", "чекає передачі"), ("failed", "не передано")):
        if info.get(key):
            parts.append(f"{label}: {info[key]}")
    if info.get("retention_days"):
        parts.append(f"записи на RAG-ПК — до {info['retention_days']} днів")
    if info.get("last_error"):
        parts.append(f"остання помилка: {info['last_error']}")
    return " · ".join(parts)


# ── хід запису (картка й опитування) ────────────────────────────────────────

def notifications_on(settings=None) -> bool:
    s = settings or services.settings_obj()
    return bool(s.notify_email or s.notify_telegram)


def progress_lines(m, now=None, settings=None) -> list:
    """[{text, tone}] — що зараз з записом (для recording / processing; інші стани — порожньо). Подробиця від RAG-ПК
    (bench_detail: «Спроба 1 з 3 не вдалася…», «🔄 Ще одна спроба…») — окремим рядком на будь-якому етапі; «Сторінку
    можна закрити…» — лише коли повідомлення (лист / Telegram) увімкнено в «⚙️ Налаштування нарад»."""
    now = now or timezone.now()
    lines = []
    if m.status == Meeting.RECORDING:
        start = local(m.started_at)
        since = f" (з {start:%H:%M})" if start else ""
        took = duration_text((now - m.started_at).total_seconds()) if m.started_at else ""
        lines.append({"text": f"🔴 Йде запис{' · ' + took if took else ''}{since}. Зупиняють у вікні запису — "
                              f"«⏹ Зупинити».", "tone": "rec"})
        if m.bench_detail:
            lines.append({"text": m.bench_detail, "tone": "muted"})
    elif m.status == Meeting.PROCESSING:
        state = m.bench_state
        if m.has_transcript and not m.moments_at:
            lines.append({"text": MOMENTS_WAIT, "tone": "warn"})
        elif state == "recorded":
            lines.append({"text": "⏳ Запис збережено на RAG-ПК, чекає черги на розшифровку…", "tone": "warn"})
        elif state == "transcribing":
            pct = f" {m.bench_progress} %" if m.bench_progress is not None else ""
            lines.append({"text": f"⏳ Розшифровка…{pct}", "tone": "warn"})
        elif state in ("transcribed", "finding_moments"):
            lines.append({"text": "🧠 Шукаю ключові моменти…", "tone": "warn"})
        elif m.bench_detail:
            lines.append({"text": m.bench_detail, "tone": "warn"})
        else:
            lines.append({"text": "⏳ Обробка на RAG-ПК…", "tone": "warn"})
        if m.bench_detail and m.bench_detail not in [x["text"] for x in lines]:
            lines.append({"text": m.bench_detail, "tone": "muted"})
    else:
        return lines
    last = m.bench_updated_at or m.ticket_redeemed_at
    if last and last < now - SILENT_WARN:
        lines.append({"text": f"⚠️ RAG-ПК давно не повідомляв про хід (останнє — {local(last):%H:%M}). Запис "
                              f"лишається на RAG-ПК; якщо так буде й завтра — повідомте адміністратора.",
                      "tone": "err"})
    if notifications_on(settings):
        lines.append({"text": CLOSE_NOTE, "tone": "muted"})
    return lines


def status_json(m) -> dict:
    """Відповідь опитування: services.poll_state + готові рядки ходу запису (без розшифровки й назви)."""
    out = services.poll_state(m)
    out["lines"] = progress_lines(m)
    return out


# ── варіанти проєкту й клієнта (підготовка й «✏️ Змінити») ──────────────────

def project_options(user, selected=None, extra=()) -> list:
    """Проєкти, які людина бачить: вибраний, підказані (подія), відкриті (≤ 300). [{pk, code, name, customer}]."""
    if not user.has_perm("projects.view_project"):
        return []
    from projects.models import Project
    sel = services._pk(selected)
    seen, out = set(), []

    def add(p, chosen=False):
        if p is None or p.pk in seen:
            return
        seen.add(p.pk)
        out.append({"pk": p.pk, "code": p.code, "name": p.name, "customer": p.customer_id or "",
                    "selected": chosen or p.pk == sel})
    if sel:
        add(Project.objects.filter(pk=sel).first(), True)
    for p in extra or ():
        add(p)
    for p in Project.objects.filter(status__in=Project.OPEN_STATUSES).order_by("-pk")[:PROJECT_OPTIONS_MAX]:
        add(p)
    return out


def customer_options(user, selected=None, projects=()) -> list:
    """Клієнти для вибору: вибраний і клієнти перелічених проєктів (JS ставить клієнта проєкту при зміні проєкту).
    [{pk, label, selected}]."""
    if not user.has_perm("crm.view_customer"):
        return []
    from crm.models import Customer
    sel = services._pk(selected)
    ids = [sel] if sel else []
    ids += [p["customer"] for p in projects if p.get("customer")]
    found = {c.pk: c for c in Customer.objects.filter(pk__in=set(ids))}
    out, seen = [], set()
    for pk in ids:
        c = found.get(pk)
        if c is None or pk in seen:
            continue
        seen.add(pk)
        label = (c.company or c.name or c.email or f"#{c.pk}").strip()
        if c.company and c.name:
            label = f"{c.company} · {c.name}"
        out.append({"pk": c.pk, "label": label, "selected": pk == sel})
    return out


def participants_from_post(post) -> list:
    """Хто на нараді з форми: підказані рядки (p_name / p_email / p_source, галочка p_on = номер рядка) і
    «＋ Ще хтось» (x_name / x_email — завжди відмічені). Невідмічені не зберігаються (services.clean_participants)."""
    names, emails, sources = post.getlist("p_name"), post.getlist("p_email"), post.getlist("p_source")
    on = set(post.getlist("p_on"))
    people = []
    for i, name in enumerate(names):
        people.append({"name": name, "email": emails[i] if i < len(emails) else "",
                       "source": sources[i] if i < len(sources) else "manual", "checked": str(i) in on})
    for name, email in zip(post.getlist("x_name"), post.getlist("x_email")):
        if (name or "").strip() or (email or "").strip():
            people.append({"name": name, "email": email, "source": "manual", "checked": True})
    return people


def _pk_or_none(value):
    return getattr(value, "pk", value) if value not in (None, "") else None


def state_from_prefill(data) -> dict:
    return {
        "title": data.get("title") or "", "project": _pk_or_none(data.get("project")),
        "customer": _pk_or_none(data.get("customer")), "event": data.get("calendar_event"),
        "continues": data.get("continues"), "lang_l": data.get("lang_l") or "", "lang_r": data.get("lang_r") or "",
        "retention": data.get("retention") or "", "consent_lang": data.get("consent_lang") or consent.DEFAULT_LANG,
        "participants": [dict(p, source_label=SOURCE_LABELS.get(p.get("source"), "")) for p in
                         data.get("participants") or []],
        "consent_note": data.get("consent_note") or "", "candidates": data.get("projects") or [],
    }


def data_from_post(post) -> dict:
    """POST форми підготовки → data для services.create."""
    return {
        "title": post.get("title", ""), "project": post.get("project") or None,
        "customer": post.get("customer") or None, "calendar_event": post.get("event") or None,
        "continues": post.get("continues") or None, "lang_l": post.get("lang_l", ""),
        "lang_r": post.get("lang_r", ""), "retention": post.get("retention", ""),
        "consent_lang": post.get("consent_lang", ""), "participants": participants_from_post(post),
        "consent_note": post.get("consent_note", ""),
    }


def state_from_post(user, post) -> dict:
    """Повторний показ форми після помилки — з тим, що людина ввела."""
    from calendar_app.models import CalendarEvent
    data = data_from_post(post)
    ev = CalendarEvent.objects.filter(pk=services._pk(data["calendar_event"]) or 0, user=user).first()
    base = services.own(user).filter(pk=services._pk(data["continues"]) or 0).first()
    people = [dict(p, source_label=SOURCE_LABELS.get(p.get("source"), "")) for p in data["participants"]]
    return {**data, "project": services._pk(data["project"]), "customer": services._pk(data["customer"]),
            "event": ev, "continues": base, "participants": people, "candidates": []}


def prepare_context(user, state, settings=None) -> dict:
    s = settings or services.settings_obj()
    projects = project_options(user, state.get("project"), state.get("candidates"))
    texts = consent.texts(s)
    return {
        "state": state, "projects": projects, "customers": customer_options(user, state.get("customer"), projects),
        "can_projects": user.has_perm("projects.view_project"), "can_customers": user.has_perm("crm.view_customer"),
        "lang_choices": LANG_CHOICES, "retention_choices": RETENTION_CHOICES,
        "consent_langs": [{"code": code, "label": CONSENT_LANG_LABELS[code], "ask": texts[code]["ask"],
                           "after": texts[code]["after"]} for code in consent.LANGS],
        "invites": [{"code": code, "label": CONSENT_LANG_LABELS[code], "text": text}
                    for code, text in consent.invitation_notes(None, s).items()],
        "privacy_url": s.privacy_url, "health": health(user, s),
    }


# ── сторінка наради ─────────────────────────────────────────────────────────

def staff_options() -> list:
    User = get_user_model()
    out = []
    for u in User.objects.filter(is_active=True, is_staff=True).order_by("first_name", "last_name", "username")[
            :STAFF_MAX]:
        out.append({"pk": u.pk, "name": services.user_name(u)})
    return out


def result_link(item) -> dict:
    """Куди додано прийнятий пункт: {label, url} (url '' — запис уже видалено)."""
    label = TARGET_DONE.get(item.target, "пункт")
    target = ""
    if item.target == "moment" and item.highlight_id and item.highlight is not None:
        target = url("admin:projects_project_change", item.highlight.project_id)
    elif item.target == "risk" and item.risk_id and item.risk is not None:
        target = url("admin:projects_project_change", item.risk.project_id, anchor="#pj-risks")
    elif item.target == "step" and item.step_id and item.step is not None:
        target = url("admin:projects_project_change", item.step.project_id, anchor="#pj-plan")
    elif item.target == "deadline" and item.event_id and item.event is not None:
        start = local(item.event.start_at)
        target = url("calendar_app:calendar", query=f"?year={start.year}&month={start.month}") if start else ""
    return {"label": label, "url": target}


def item_context(user, m, item, base=None, staff=None) -> dict:
    """Дані одного пункту для _item.html: куди можна додати (з причинами), що вибрано, підказки дати й виконавця."""
    from projects.models import ProjectHighlight, ProjectRisk, ProjectStep
    base = base or services.targets(user, m)
    reasons = base["targets"]
    default = services.DEFAULT_TARGET.get(item.kind, "moment")
    if reasons.get(default):
        default = "deadline"
    targets = [{"key": key, "label": TARGET_TEXT[key][0], "meaning": TARGET_TEXT[key][1],
                "reason": reasons.get(key, ""), "checked": key == default} for key in ("moment", "risk", "step",
                                                                                        "deadline")]
    day = services.parse_day(item.due_text) if item.due_text else None
    labels = services.speaker_labels(m)
    side = services._step_side(m, item, {})
    return {
        "side_default": side, "can_change": can_change(user), "no_change_text": NO_CHANGE_TEXT,
        "item": item, "m": m, "targets": targets, "default": default,
        "time": clock(item.start_s) if item.start_s is not None else "",
        "second": int(item.start_s) if item.start_s is not None else None,
        "speaker": labels.get(item.ch, "") if item.ch else "",
        "moment_kinds": [{"code": code, "label": label,
                          "selected": code == services.DEFAULT_MOMENT_KIND.get(item.kind, ProjectHighlight.OTHER)}
                         for code, label in ProjectHighlight.KIND_CHOICES],
        "categories": ProjectRisk.CATEGORY_CHOICES,
        "sides": [{"code": code, "label": label, "selected": code == side} for code, label in ProjectStep.SIDE_CHOICES],
        "me": user.pk, "staff": staff if staff is not None else staff_options(),
        "date": day.isoformat() if day else "", "result": result_link(item) if item.status == MeetingItem.ACCEPTED
        else None, "shared_warning": SHARED_WARNING, "deadline_warning": DEADLINE_WARNING,
        "item_url": url("admin:meetings_meeting_item", m.pk, item.pk),
    }


def render_item(user, m, item, base=None, staff=None) -> str:
    return render_to_string("admin/meetings/meeting/_item.html", item_context(user, m, item, base, staff))


def log_lines(m) -> list:
    """consent_log людською мовою: [{at, text}]."""
    out = []
    for e in m.consent_log or []:
        at = services._parse_dt(e.get("at"))
        when = f"{local(at):%d.%m.%Y %H:%M}" if at else ""
        kind = e.get("kind")
        if kind == "recording":
            lang = CONSENT_LANG_LABELS.get(e.get("lang"), e.get("lang") or "")
            text = f"🎙 Спроба {e.get('attempt') or 1}: запис оголошено, усі погодились — підтверджено у вікні запису" \
                   + (f" (текст: {lang})" if lang else "")
        elif kind == "discarded":
            text = "⛔ Запис скасовано на RAG-ПК (учасник не погодився) — запис видалено"
        elif kind == "join":
            text = "🙋 Новий учасник погодився"
        elif kind == "confirmed":
            text = "✅ Згоду підтверджено вголос"
        elif kind == "edit":
            field = {"participants": "хто на нараді", "consent_note": "примітка"}.get(e.get("field"), "дані")
            text = f"✏️ Змінено: {field}"
        else:
            continue
        out.append({"at": when, "text": text})
    return out


def transcript_rows(m, tr) -> list:
    """Рядки розшифровки й позначки під час запису (📌 людини, службові, згода) — за часом."""
    labels = services.speaker_labels(m)
    rows = []
    for s in (tr.segments if tr else None) or []:
        rows.append({"type": "seg", "n": s.get("n"), "t": float(s.get("start") or 0), "time": clock(s.get("start")),
                     "second": int(s.get("start") or 0), "who": labels.get(s.get("ch"), labels["M"]),
                     "ch": s.get("ch") or "M", "text": s.get("text") or ""})
    for mk in m.marks or []:
        kind = mk.get("kind")
        label = mk.get("label") or ""
        if kind == "user":
            text = label or services.MARK_DEFAULT
            text, cls = (text if text.startswith("📌") else f"📌 {text}"), "mark"
        elif kind in services.CONSENT_MARKS:
            text, cls = services.CONSENT_MARKS[kind], "consent"
        else:
            if not label:
                continue
            text, cls = f"· {label}", "auto"
        t = float(mk.get("t") or 0)
        rows.append({"type": cls, "t": t, "time": clock(t), "second": int(t), "text": text})
    rows.sort(key=lambda r: (r["t"], 0 if r["type"] == "seg" else 1))
    return rows


def excerpt_rows(m) -> list:
    labels = services.speaker_labels(m)
    return [{"time": clock(e.get("start")), "second": int(e.get("start") or 0),
             "who": labels.get(e.get("ch"), labels["M"]), "text": e.get("text") or ""}
            for e in m.consent_excerpt or []]


def accepted_rows(m) -> list:
    return [{"item": i, "result": result_link(i)} for i in services.accepted_items(m)]


def page_context(user, m, settings=None) -> dict:
    """Усе для page.html (лише власнику — перевірено до виклику)."""
    s = settings or services.settings_obj()
    tr = MeetingTranscript.objects.filter(meeting=m).first() if m.has_transcript else None
    items = list(m.items.select_related("highlight", "risk", "step", "event").order_by("start_s", "pk"))
    base = services.targets(user, m)
    staff = staff_options() if items else []
    rendered = [{"item": i, "html": render_item(user, m, i, base, staff)} for i in items]
    pending = [r for r in rendered if r["item"].status == MeetingItem.PENDING]
    accepted = [r for r in rendered if r["item"].status == MeetingItem.ACCEPTED]
    rejected = [r for r in rendered if r["item"].status == MeetingItem.REJECTED]
    can_projects = user.has_perm("projects.view_project")
    can_customers = user.has_perm("crm.view_customer")
    projects = project_options(user, m.project_id) if can_projects else []
    customers = customer_options(user, m.customer_id, projects) if can_customers else []
    texts = consent.texts(s)
    capture = dict(Meeting.CAPTURE_CHOICES).get(m.capture_mode, "") if m.capture_mode else ""
    silent_names = {"L": "🔇 мікрофон мовчав", "R": "🔇 звуку наради не було"}
    return {
        "m": m, "tone": STATUS_TONE.get(m.status, "idle"), "start": local(m.start),
        "duration": duration_text(m.duration_s), "retention_label": "лише текст" if m.retention == "text_only"
        else "аудіо + текст", "capture": capture,
        "no_headset": m.no_headset and m.capture_mode in ("", Meeting.BOTH),
        "no_headset_note": NO_HEADSET_NOTE, "silent": [silent_names[ch] for ch in m.silent or [] if ch in silent_names],
        "project_url": url("admin:projects_project_change", m.project_id) if m.project_id and can_projects else "",
        "customer_url": url("admin:crm_customer_change", m.customer_id) if m.customer_id and can_customers else "",
        "event": m.calendar_event if m.calendar_event_id else None,
        "lines": progress_lines(m, settings=s), "health": health(user, s), "host": bench_host(s),
        "can_change": can_change(user), "no_change_text": NO_CHANGE_TEXT,
        "ttl": ttl_text(s.ticket_ttl_seconds), "check_url": services.bench_url(s) + "/#check",
        "can_record": services.can_record(user), "refusal": services.record_refusal(user),
        "can_retry": services.can_retry(m) and can_change(user), "redo_confirm": REDO_CONFIRM,
        "phone_note": PHONE_NOTE,
        "can_continue": m.status in (Meeting.RECORDING, Meeting.PROCESSING, Meeting.REVIEW),
        "pending": pending, "accepted": accepted, "rejected": rejected, "items_total": len(items),
        "summary": tr.summary if tr else "", "moments_wait": m.status == Meeting.PROCESSING and m.has_transcript
        and not m.moments_at, "moments_wait_text": MOMENTS_WAIT,
        "no_project_hint": not m.project_id, "transcript": transcript_rows(m, tr) if tr else [],
        "transcript_model": tr.model if tr else "",
        "kb": knowledge.parts_state(m, user) if m.approved_at else None, "kb_text": KB_TEXT,
        "consent_lang_label": CONSENT_LANG_LABELS.get(m.consent_lang, ""), "log": log_lines(m),
        "participants": [dict(p, source_label=SOURCE_LABELS.get(p.get("source"), "")) for p in m.participants or []],
        "excerpt": excerpt_rows(m), "accepted_rows": accepted_rows(m),
        "projects": projects, "customers": customers, "can_projects": can_projects, "can_customers": can_customers,
        "lang_choices": LANG_CHOICES, "retention_choices": RETENTION_CHOICES,
        "consent_langs": [{"code": code, "label": CONSENT_LANG_LABELS[code], "ask": texts[code]["ask"],
                           "after": texts[code]["after"], "selected": code == consent.lang_of(m.consent_lang)}
                          for code in consent.LANGS],
        "invites": [{"code": code, "label": CONSENT_LANG_LABELS[code], "text": text,
                     "selected": code == consent.lang_of(m.consent_lang)}
                    for code, text in consent.invitation_notes(m, s).items()],
        "privacy_url": s.privacy_url,
        "config": {"status_url": url("admin:meetings_meeting_status", m.pk), "status": m.status,
                   "transcript": m.has_transcript, "moments": bool(m.moments_at),
                   "poll": m.status in (Meeting.READY, Meeting.RECORDING, Meeting.PROCESSING)},
    }
