"""📌 Панель праворуч у списку проєктів (core/side_panel.py): клік по рядку — стан, «потребує уваги», план і фази,
пов'язане, ⚡ впливи інших проєктів, важливі моменти — без переходу в картку. Швидкі зміни — статус, строк клієнту,
наступна дія (core/panel_edit.py: той самий шлях, що «Зберегти» в картці).

Правила: стан і план — portfolio.load (кеш стану й плану, без нових розрахунків, крім застарілого стану); «потребує
уваги» — state.items_for (тема чужого листа — без тексту); посилання на інші записи — sp.admin_link (лише якщо людина
їх відкриє); пошти тут немає (теми листів — лише власнику, вони в пульті картки). Дані записів — у translate="no".
"""
from __future__ import annotations

from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from core import side_panel as sp

MODEL = "projects.project"
STATUS_TONES = {"lead": "new", "active": "ok", "on_hold": "warn", "done": "done", "cancelled": "off"}
HEALTH_TONES = {"bad": "err", "warn": "warn", "ok": "ok"}
STEPS_SHOWN = 5
LINKS_SHOWN = 4
MOMENTS_SHOWN = 5


def _row(p, request):
    """Рядок портфеля (стан, фаза, наступне) — раз на запит."""
    from . import portfolio
    return sp.memo(p, "projects:row",
                   lambda: (portfolio.load([p], request.user, timezone.localdate()) or [None])[0])


def _url(name, *args, query="") -> str:
    try:
        return reverse(name, args=list(args)) + query
    except NoReverseMatch:
        return ""


def _line(main, side="", note=""):
    def h(v):
        return v if hasattr(v, "__html__") else sp.data(v)
    return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span><span>{}</span></div>'
                       '{}</div>', h(main), h(side),
                       format_html('<div class="mvp-note">{}</div>', h(note)) if note else "")


# ── назва й підзаголовок ───────────────────────────────────────────────────────

def title(p):
    return format_html("<b>{}</b> {}", sp.data(p.code), sp.data(f"{p.icon} {p.name}".strip()))


def label(p) -> str:
    return f"{p.code} · {p.name}"


def subtitle(p, request) -> str:
    from .portfolio import customer_name
    owner = (p.owner.get_full_name() or p.owner.get_username()) if p.owner_id else ""
    return " · ".join(x for x in (customer_name(p.customer), p.get_status_display(), owner) if x)


def _status_choices(p, request):
    from .models import Project
    return list(Project.STATUS_CHOICES)          # назви статусів уже зі значками («🟢 Активний»)


# ── 🧭 Стан ────────────────────────────────────────────────────────────────────

def state_html(p, request):
    r = _row(p, request)
    if r is None:
        return ""
    from .state import HEALTH
    icon, name = HEALTH.get(r.health, ("⚪", "Ще не пораховано"))
    due = ""
    if p.due_date:
        late = bool(r.forecast and r.forecast > p.due_date)
        due = format_html("{}{}", sp.date(p.due_date), format_html(' <span class="mvp-err">{}</span>',
                                                                   f"прогноз {r.forecast:%d.%m.%Y}") if late else "")
    nxt = ""
    if r.next:
        nxt = sp.join([sp.data(r.next["title"]), f"до {r.next['end']:%d.%m}" if r.next.get("end") else "",
                       "прострочено" if r.next.get("late") else ("критичний шлях" if r.next.get("critical") else "")])
    st = r.status
    status = sp.join([st.get_health_display(), sp.date(st.created_at), sp.data(st.text[:160])]) if st else ""
    return sp.kv([
        ("Стан", sp.join([f"{icon} {name}", r.reason]), HEALTH_TONES.get(r.health, "")),
        ("Тижневий статус", status),
        ("Строк клієнту", due),
        ("Прогноз", sp.date(r.forecast) if r.forecast and not p.due_date else ""),
        ("Фаза", r.phase_text),
        ("Наступний крок", nxt),
        ("Чекаємо на", r.waiting),
        ("Наступна дія", sp.data(p.next_action) if p.next_action else ""),
        ("Старт", sp.date(p.start_date)),
    ])


# ── ⚠️ Ризики ──────────────────────────────────────────────────────────────────

def risks_html(p, request):
    from .models import ProjectRisk
    from .risk_rules import rule_name
    items = sorted(ProjectRisk.objects.filter(project=p).exclude(status=ProjectRisk.CLOSED).select_related("owner"),
                   key=lambda r: (-r.score, r.pk))
    lines = []
    for r in items[:8]:
        owner = (r.owner.get_full_name() or r.owner.get_username()) if r.owner_id else ""
        rule = rule_name(r.rule_key)
        lines.append(_line(r.title, sp.badge(f"бал {r.score}", {"red": "err", "yellow": "warn", "green": "ok"}[r.level]),
                           sp.join([f"{r.get_source_display()}" + (f" «{rule}»" if rule else ""), owner,
                                    sp.data(r.mitigation[:120]) if r.mitigation else ""])))
    lines.append(sp.more(_url("admin:projects_project_change", p.pk, query="#pj-risks"), "⚠️ Матриця й реєстр →"))
    return mark_safe("".join(str(x) for x in lines)) if items else ""


def risks_badge(p, request):
    from .risk_rules import counts
    c = counts([p.pk]).get(p.pk, {})
    n = sum(c.values())
    return sp.Badge(n, "err" if c.get("red") else "warn", f"ризиків: {n}") if n else None


def state_summary(p, request) -> str:
    r = _row(p, request)
    return " · ".join(x for x in (r.phase_text, r.reason) if x) if r else ""


# ── ⚠️ Потребує уваги ──────────────────────────────────────────────────────────

def attention_html(p, request):
    r = _row(p, request)
    if r is None or not r.items:
        return ""
    lines = []
    for a in r.items:
        text = sp.link(a["url"], a["text"]) if a.get("url") else a["text"]
        lines.append(format_html('<div class="mvp-line {}">{} {}</div>', "mvp-err" if a["level"] == "high" else "",
                                 a["icon"], text))
    return mark_safe("".join(lines))


def attention_badge(p, request):
    r = _row(p, request)
    if r is None or not r.items:
        return None
    high = sum(1 for a in r.items if a["level"] == "high")
    return sp.Badge(len(r.items), "err" if high else "warn", f"важливого: {len(r.items)}")


# ── 📅 План ────────────────────────────────────────────────────────────────────

def plan_html(p, request):
    from .models import ProjectPhase, ProjectStep
    r = _row(p, request)
    if r is None or not r.has_plan:
        return ""
    mark = {ProjectPhase.DONE: "✓", ProjectPhase.ACTIVE: "●"}
    phases = [_line(f"{mark.get(ph['status'], '○')} {ph['icon']} {ph['name']}".replace("  ", " "),
                    f"{ph['start']:%d.%m}–{ph['end']:%d.%m}" if ph["start"] and ph["end"] else "",
                    sp.join([f"кроків {ph['done']} з {ph['steps']}",
                             "затягується" if ph["current"] and r.stuck else ""]))
              for ph in r.phases]
    closed = (ProjectStep.DONE, ProjectStep.SKIPPED)
    steps = [s for s in ProjectStep.objects.filter(project=p).select_related("assignee")
             if (s.eff_status or s.status) not in closed and (s.f_end or s.end)]
    steps.sort(key=lambda s: (s.f_end or s.end, s.order))
    upcoming = [_line(("◆ " if s.kind == ProjectStep.MILESTONE else "") + s.title,
                      f"{(s.f_end or s.end):%d.%m}",
                      sp.join([s.get_side_display(),
                               (s.assignee.get_full_name() or s.assignee.get_username()) if s.assignee_id else "",
                               "критичний шлях" if s.critical else "",
                               f"+{s.slip_days} дн." if (s.slip_days or 0) > 0 else ""]))
                for s in steps[:STEPS_SHOWN]]
    parts = [format_html('<p class="mvp-note">{}</p>', "Фази"), mark_safe("".join(phases))] if phases else []
    if upcoming:
        parts += [format_html('<p class="mvp-note">{}</p>', "Найближчі кроки"), mark_safe("".join(upcoming))]
    parts.append(sp.more(_url("admin:projects_project_change", p.pk, query="#pj-plan"), "📅 План проєкту →"))
    return mark_safe("".join(str(x) for x in parts))


def plan_summary(p, request) -> str:
    r = _row(p, request)
    if r is None or not r.has_plan:
        return ""
    return f"кроків {r.done} з {r.steps}" + (f" · прогноз {r.forecast:%d.%m}" if r.forecast else "")


# ── 🔗 Пов'язане ───────────────────────────────────────────────────────────────

def links_html(p, request):
    from .models import ProjectLink
    links = list(ProjectLink.objects.filter(project=p, status=ProjectLink.CONFIRMED, thread__isnull=True,
                                            message__isnull=True)
                 .select_related("order", "purchase_order__supplier", "jlc_order", "shipment"))
    groups = (("🛒 Замовлення", "order"), ("🏭 Закупівлі", "purchase_order"), ("🔧 JLC", "jlc_order"),
              ("🚚 Відправлення", "shipment"))
    out = []
    for title_, attr in groups:
        objs = [getattr(link, attr) for link in links if getattr(link, f"{attr}_id")]
        if not objs:
            continue
        shown = format_html_join(", ", "{}", ((sp.admin_link(request, o),) for o in objs[:LINKS_SHOWN]))
        more = f" … +{len(objs) - LINKS_SHOWN}" if len(objs) > LINKS_SHOWN else ""
        out.append(_line(title_, str(len(objs)), format_html("{}{}", shown, more)))
    offers = list(getattr(p, "offers").order_by("-created_at")[:LINKS_SHOWN]) if hasattr(p, "offers") else []
    if offers:
        out.append(_line("📝 Angebote", str(p.offers.count()),
                         format_html_join(", ", "{}", ((sp.admin_link(request, o, f"{o.display_number} · "
                                                                                   f"{o.get_status_display()}"),)
                                                      for o in offers))))
    return mark_safe("".join(str(x) for x in out))


def links_badge(p, request):
    from .models import ProjectLink
    n = ProjectLink.objects.filter(project=p, status=ProjectLink.CONFIRMED, thread__isnull=True,
                                   message__isnull=True).count()
    return sp.Badge(n, "info", f"пов'язаного: {n}") if n else None


# ── ⚡ Впливи ──────────────────────────────────────────────────────────────────

def _impacts(p):
    from . import impact
    return sp.memo(p, "projects:impacts", lambda: impact.compute(timezone.localdate(), focus=p))


def impacts_html(p, request):
    items = _impacts(p)
    if not items:
        return ""
    lines = [format_html('<div class="mvp-line {}">{} <b translate="no">{}</b> — {}</div>',
                         "mvp-err" if i.level in ("high", "medium") else "", i.icon, i.title, i.text) for i in items]
    lines.append(sp.more(_url("admin:projects_project_portfolio", query="#impacts"), "📊 Портфель і всі впливи →"))
    return mark_safe("".join(str(x) for x in lines))


def impacts_badge(p, request):
    items = _impacts(p)
    bad = sum(1 for i in items if i.level in ("high", "medium"))
    return sp.Badge(len(items), "warn" if bad else "info", f"впливів: {len(items)}") if items else None


# ── 📌 Важливі моменти ─────────────────────────────────────────────────────────

def moments_html(p, request):
    from .models import ProjectHighlight
    items = list(ProjectHighlight.objects.filter(project=p).order_by("-date", "-pk")[:MOMENTS_SHOWN])
    return mark_safe("".join(str(_line(f"{h.get_kind_display()}", sp.date(h.date), h.title)) for h in items))


BLOCKS = [
    sp.Block("state", "Стан", state_html, icon="🧭", summary=state_summary, empty="Стан ще не пораховано."),
    sp.Block("attention", "Потребує уваги", attention_html, icon="⚠️", short="Увага", badge=attention_badge,
             empty="✅ Нічого не чекає: строки, листи, Angebote й закупівлі в порядку."),
    sp.Block("risks", "Ризики", risks_html, icon="🎯", badge=risks_badge,
             empty="Відкритих ризиків немає — правила відкриють їх самі."),
    sp.Block("plan", "План і фази", plan_html, icon="📅", short="План", summary=plan_summary,
             empty="Плану ще немає — «📅 План» у пульті картки проєкту."),
    sp.Block("links", "Пов’язане", links_html, icon="🔗", badge=links_badge,
             empty="Замовлень, закупівель, JLC і Angebote в проєкті ще немає."),
    sp.Block("impacts", "Впливи інших проєктів", impacts_html, icon="⚡", short="Впливи", badge=impacts_badge,
             lazy=True, empty="Інші проєкти на цей не впливають."),
    sp.Block("moments", "Важливі моменти", moments_html, icon="📌", short="Моменти", default=False,
             empty="Важливих моментів ще немає."),
]

ACTIONS = [
    sp.Action("cockpit", "🧭 Пульт проєкту",
              lambda p, r: _url("admin:projects_project_change", p.pk, query="#pj-cockpit"), hotkey="KeyO"),
    sp.Action("plan_open", "📅 План", lambda p, r: _url("admin:projects_project_change", p.pk, query="#pj-plan")),
    sp.Action("mail", "✉️ Листи проєкту", lambda p, r: f"/email/?project={p.pk}", perm="module:email_assistant",
              new_tab=True),
    sp.Action("portfolio", "📊 Портфель", lambda p, r: _url("admin:projects_project_portfolio"), default=False),
]

EDITS = [
    sp.Edit("status", "status", "Статус", "choice", place="head", choices=_status_choices, undo=True,
            hotkey="KeyS", tones=STATUS_TONES,
            confirm={"done": "Позначити проєкт завершеним?", "cancelled": "Скасувати проєкт?"}),
    sp.Edit("due", "due_date", "Строк клієнту", "date", place="block", undo=True),
    sp.Edit("next_action", "next_action", "Наступна дія", "text", place="block", undo=True, maxlength=255),
]

PANEL = sp.register(MODEL, title=title, label=label, subtitle=subtitle, blocks=BLOCKS, actions=ACTIONS, edits=EDITS,
                    hide_cols=("threads_col", "updated_at"))
