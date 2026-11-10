"""«🧭 Пульт проєкту» вгорі картки проєкту: колір і строк, що зараз, ризики, важливі моменти, пропозиції прив'язок,
шкала подій. Факти збираються один раз (overview.collect), стан записується в кеш списку (state.save), шкала —
timeline.render. Пошта й календар особисті: тема листа чи назва події — лише власнику (overview.visible, timeline).

Маршрути — projects/views.py: cockpit (GET, перегляд), highlight / highlight_delete / link_decide / scan (POST, зміна);
JS пульта — admin/projects/project/change_form.html (замінює #pj-cockpit без перезавантаження форми).
"""
import json

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from . import overview, state, timeline
from .models import ProjectHighlight, ProjectLink

MOMENTS = 6
RECENT_MAILS = 25
SUGGESTIONS = 20
RANGE_LABELS = [("all", "усе"), ("3", "3 міс"), ("6", "6 міс"), ("12", "12 міс")]


def _due(project, today):
    if not project.due_date:
        return None
    left = (project.due_date - today).days
    if not project.is_open:
        text = ""
    elif left < 0:
        text = f"минув {-left} дн. тому"
    elif left == 0:
        text = "сьогодні"
    else:
        text = f"через {left} дн."
    return {"date": project.due_date, "text": text, "late": left < 0 and project.is_open}


def _own(obj, uid) -> bool:
    return bool(obj is not None and obj.account_id and obj.account.user_id == uid)


def link_label(link, uid) -> tuple:
    """(підпис, адреса) зв'язку для людини: тема чужої гілки чи листа — без тексту й посилання."""
    if link.thread_id:
        own = _own(link.thread, uid)
        return (f"✉️ {link.thread.subject or '(без теми)'}" if own else "✉️ гілка з іншої скриньки",
                f"/email/thread/{link.thread_id}/" if own else "")
    if link.message_id:
        own = _own(link.message, uid)
        return (f"📨 {link.message.subject or '(без теми)'}" if own else "📨 лист з іншої скриньки",
                f"/email/message/{link.message_id}/" if own else "")
    if link.event_id:
        own = link.event.user_id == uid
        return (f"📅 {link.event.title}" if own else "📅 подія з іншого календаря", "/calendar/" if own else "")
    urls = {"order_id": "/admin/sales/salesorder/{}/change/",
            "purchase_order_id": "/admin/inventory/purchaseorder/{}/change/",
            "jlc_order_id": "/admin/jlcpcb/jlcorder/{}/change/",
            "shipment_id": "/admin/shipping/shipment/{}/change/"}
    for field, url in urls.items():
        if getattr(link, field):
            return link.target_label, url.format(getattr(link, field))
    return "—", ""


def _suggestions(project, uid) -> list:
    out = []
    for link in (ProjectLink.objects.filter(project=project, status=ProjectLink.SUGGESTED)
                 .select_related("thread__account", "message__account", "order", "purchase_order__supplier",
                                 "jlc_order", "shipment")
                 .order_by("-created_at")[:SUGGESTIONS]):
        label, url = link_label(link, uid)
        mail = link.thread_id or link.message_id
        out.append({"id": link.pk, "label": label, "url": url, "reason": link.reason,
                    "can_decide": not mail or bool(url)})        # чужу гілку вирішує власник скриньки (у пошті)
    return out


def _moments(facts, uid) -> list:
    out = []
    for h in reversed(facts["highlights"]):          # нові зверху
        icon, _, name = h.get_kind_display().partition(" ")
        out.append({"id": h.pk, "date": h.date, "icon": icon, "kind": name, "kind_key": h.kind, "title": h.title,
                    "quote": h.quote, "url": f"/email/message/{h.message_id}/" if _own(h.message, uid) else ""})
    return out


def _handled(facts, uid) -> list:
    """«✓ Позначено вручну»: власні листи, на які відповіли поза поштою (телефоном, на зустрічі…) — з «↶»."""
    out = []
    for h in facts.get("handled") or []:
        m = h["msg"]
        if _own(m, uid):
            mark = h["mark"]
            out.append({"pk": m.pk, "subject": m.subject or "(без теми)", "dir": h["dir"],
                        "label": overview.HANDLED_LABELS.get(mark.how, mark.how), "date": mark.created_at,
                        "note": mark.note, "url": f"/email/message/{m.pk}/",
                        "who": (m.from_name or m.from_email) if h["dir"] == "in" else ", ".join((m.to_emails or [])[:1])})
    return out


def _acts(attention, can_change) -> list:
    """✓ біля пункту «Що зараз» / «Ризики» (overview.collect → act): JSON для меню — лише з правом змінювати."""
    out = []
    for a in attention:
        if a.get("act") and can_change:
            a = {**a, "act_json": json.dumps(a["act"], ensure_ascii=False)}
        out.append(a)
    return out


def _mails(facts, uid) -> list:
    """Власні листи проєкту для вибору в «📌 Важливий момент» (нові зверху)."""
    out = []
    for x in reversed(facts["messages"]):
        m = x["msg"]
        if _own(m, uid):
            when = timezone.localtime(m.sent_at).strftime("%d.%m") if m.sent_at else ""
            out.append({"pk": m.pk, "label": f"{when} · {'📥' if x['dir'] == 'in' else '📤'} {m.subject or '(без теми)'}"[:120]})
            if len(out) >= RECENT_MAILS:
                break
    return out


def _strip(plan) -> list:
    """Смуга фаз (stage-gate): назва, стан, дати, кроки, зсув, умова переходу."""
    out = []
    for pc in plan.phases:
        ph = pc.phase
        gate = pc.gate_step
        out.append({
            "name": (f"{ph.icon} {ph.name}".strip() if ph else "Без фази"), "status": pc.status,
            "dates": f"{pc.start:%d.%m}–{pc.end:%d.%m}" if pc.start and pc.end else "",
            "progress": f"{pc.done} з {len(pc.steps)}" if pc.steps else "",
            "slip": pc.slip if pc.slip and pc.slip > 0 and pc.status != "done" else 0,
            "gate": ph.gate if ph else "", "gate_done": bool(gate and gate.status == "done"),
            "id": ph.pk if ph else "",
        })
    return out


def _plan_context(project, facts, today, can_change=False) -> dict:
    """Вкладка «📅 План»: смуга фаз, Гант, дані вікна кроку (лише тому, хто може змінювати проєкт)."""
    from . import gantt, plan as plan_mod
    p = facts.get("plan")
    if p is None or not p.has_plan:
        return {"has_plan": False, "templates": [
            {"id": t.pk, "name": t.name, "counts": plan_mod.template_counts(t), "description": t.description}
            for t in plan_mod.templates()] if can_change else []}
    return {"has_plan": True, "strip": _strip(p), "gantt": gantt.render(project, p, today, editable=can_change),
            "gantt_brief": gantt.render(project, p, today, editable=can_change, brief=True),
            "classic": gantt.classic_data(project, p, today),
            "end": p.end, "base_end": p.base_end,
            "late": bool(project.due_date and p.end and p.end > project.due_date),
            "baseline_at": project.baseline_at, "data": plan_mod.editor_data(project, p) if can_change else None,
            "steps": len(p.steps), "critical": sum(1 for c in p.steps if c.critical and c.status not in
                                                   ("done", "skipped"))}


def _risks_context(project, can_change) -> dict:
    """Вкладка «⚠️ Ризики»: матриця 3 × 3 і реєстр (відкриті й ті, за якими стежимо — за балом; закриті окремо);
    дані вікна ризику — лише тому, хто може змінювати проєкт."""
    from django.contrib.auth import get_user_model
    from . import risk_rules
    from .models import ProjectRisk, ProjectStep
    risks = list(ProjectRisk.objects.filter(project=project).select_related("owner").prefetch_related("steps"))
    live = sorted([r for r in risks if r.is_open], key=lambda r: (-r.score, r.status != ProjectRisk.OPEN, r.pk))
    closed = sorted([r for r in risks if not r.is_open], key=lambda r: r.closed_at or r.updated_at, reverse=True)
    rows = [{"n": n, "risk": r, "rule": risk_rules.rule_name(r.rule_key), "steps": [s.title for s in r.steps.all()]}
            for n, r in enumerate(live, 1)]
    data = None
    if can_change:
        users = get_user_model().objects.filter(is_active=True, is_staff=True).order_by("first_name", "username")
        data = {
            "risks": [{"id": r.pk, "title": r.title, "description": r.description, "category": r.category,
                       "probability": r.probability, "impact": r.impact, "owner": r.owner_id or "",
                       "mitigation": r.mitigation, "status": r.status,
                       "review_on": r.review_on.isoformat() if r.review_on else "", "source": r.source,
                       "evidence": r.evidence, "rule": risk_rules.rule_name(r.rule_key),
                       "steps": [s.pk for s in r.steps.all()]} for r in risks],
            "users": [{"id": u.pk, "label": u.get_full_name() or u.username} for u in users[:200]],
            "steps": [{"id": s.pk, "label": s.title} for s in ProjectStep.objects.filter(project=project)
                      .order_by("phase__order", "order", "pk")],
            "categories": ProjectRisk.CATEGORY_CHOICES, "statuses": ProjectRisk.STATUS_CHOICES,
            "probabilities": ProjectRisk.PROBABILITY_CHOICES, "impacts": ProjectRisk.IMPACT_CHOICES,
            "owner": project.owner_id or "", "review": risk_rules.review_default().isoformat(),
        }
    return {"rows": rows, "closed": closed[:20], "closed_total": len(closed), "matrix": risk_rules.matrix(live),
            "counts": {k: sum(1 for r in live if r.level == k) for k in ("red", "yellow", "green")},
            "total": len(live), "data": data}


def _status_context(project, user, facts, st, today, can_change) -> dict:
    """Вкладка «📝 Статус»: останній статус людини, історія кольорів, форма з чернеткою без AI."""
    from . import health
    from .models import ProjectUpdate
    last = health.latest(project)
    draft = ""
    if can_change and project.is_open:
        try:
            draft = health.draft_text(health.facts_for(project, user, today, facts))
        except Exception:  # noqa: BLE001 — форма без чернетки краще, ніж пульт без вкладки
            draft = ""
    return {"last": last, "age": health.age(last, today), "history": health.history(project),
            "suggest": health.suggest(st.health), "draft": draft, "choices": ProjectUpdate.HEALTH_CHOICES,
            "dots": health.DOTS, "last_dot": health.DOTS.get(last.health, "") if last else ""}


def _ai_context(project, user, can_change) -> dict:
    """«🧠 AI-помічник»: пропозиції з листів цієї людини (лише її), скільки її листів ще не прочитано, чи є база
    знань і право доручати агенту. Без права змінювати проєкт — блоку немає."""
    from . import ai_scan, delegate, forgotten
    from .models import ProjectSuggestion
    if not can_change or user is None:
        return {"on": False}
    scan = ai_scan.context(project, user, can_change)
    items = []
    for s in scan["items"]:
        risk = s.kind == ProjectSuggestion.RISK
        icon, _, label = (s.get_category_display() if risk else s.get_moment_kind_display()).partition(" ")
        items.append({"id": s.pk, "kind": s.kind, "icon": icon, "label": label, "title": s.title, "quote": s.quote,
                      "date": s.date, "risk": risk, "score": s.score, "level": s.level, "mitigation": s.mitigation,
                      "url": f"/email/message/{s.message_id}/" if s.message_id else ""})
    return {"on": True, "items": items, "since": project.ai_mail_since,
            "moments": sum(1 for i in items if not i["risk"]),
            "risks": sum(1 for i in items if i["risk"]), "unread": scan["unread"], "own": scan["own"],
            "rag": forgotten.rag_ready(user), "agent": delegate.can(user)}


def context(project, user, range_key="all", notice="") -> dict:
    today = timezone.localdate()
    if range_key not in timeline.RANGES:
        range_key = "all"
    facts = overview.collect(project, today)
    st = state.save(project, facts)
    uid = getattr(user, "pk", None)
    attention = overview.visible(facts["attention"], user)
    icon, label = state.HEALTH[st.health]
    pk = project.pk
    can_change = bool(user and user.has_perm("projects.change_project"))
    attention = _acts(attention, can_change)
    plan = _plan_context(project, facts, today, can_change)
    ai = _ai_context(project, user, can_change)
    if plan.get("data") is not None:
        plan["data"]["agent"] = bool(ai.get("agent"))
    evs = timeline.events(project, facts, user, today)          # одні події — для SVG, браузера й телефона
    return {
        "project": project, "health": {"key": st.health, "icon": icon, "label": label},
        "due": _due(project, today), "start": project.start_date, "today": today,
        "next_action": project.next_action,
        "todo": [a for a in attention if a.get("kind") == "todo"],
        "risks": [a for a in attention if a.get("kind") != "todo"],
        "suggestions": _suggestions(project, uid),
        "handled": _handled(facts, uid),
        "moments": _moments(facts, uid)[:MOMENTS], "moments_total": len(facts["highlights"]),
        "kinds": ProjectHighlight.KIND_CHOICES, "mails": _mails(facts, uid),
        "counts": facts["counts"], "more": facts["more"],
        "timeline": timeline.render(project, facts, user, months=timeline.RANGES[range_key], today=today, evs=evs),
        "timeline_data": timeline.payload(project, facts, user, today, evs=evs),
        "timeline_list": timeline.mobile_items(project, facts, user, today, evs=evs),
        "range": range_key, "ranges": RANGE_LABELS,
        "can_change": can_change,
        "has_products": project.products.exists(),
        "notice": notice,
        "plan": plan,
        "ai": ai,
        "riskreg": _risks_context(project, can_change),
        "status": _status_context(project, user, facts, st, today, can_change),
        "urls": {"cockpit": reverse("projects:cockpit", args=[pk]), "highlight": reverse("projects:highlight", args=[pk]),
                 "mail_handled": reverse("projects:mail_handled", args=[pk]),
                 "risk": reverse("projects:risk", args=[pk]), "status_save": reverse("projects:status", args=[pk]),
                 "status_draft": reverse("projects:status_draft", args=[pk]),
                 "scan": reverse("projects:scan", args=[pk]), "mail": f"/email/?project={pk}",
                 "plan_template": reverse("projects:plan_template", args=[pk]),
                 "plan_step": reverse("projects:plan_step", args=[pk]),
                 "plan_phase": reverse("projects:plan_phase", args=[pk]),
                 "plan_baseline": reverse("projects:plan_baseline", args=[pk]),
                 "plan_save_template": reverse("projects:plan_save_template", args=[pk]),
                 "plan_clear": reverse("projects:plan_clear", args=[pk]),
                 "plan_base": f"/projects/api/project/{pk}/plan/",
                 "ai_scan": reverse("projects:ai_scan", args=[pk]), "ai_plan": reverse("projects:ai_plan", args=[pk]),
                 "ai_plan_apply": reverse("projects:ai_plan_apply", args=[pk]),
                 "ai_settings": reverse("projects:ai_settings", args=[pk]),
                 "ai_base": f"/projects/api/project/{pk}/ai/suggestion/",
                 "forgotten": reverse("projects:forgotten", args=[pk]),
                 "forgotten_decide": reverse("projects:forgotten_decide", args=[pk]),
                 "events": reverse("projects:project_events", args=[pk]),
                 "workspace": reverse("admin:projects_project_workspace", args=[pk]),
                 "event_link": reverse("projects:project_event_link", args=[pk])},
    }


def render(project, user, range_key="all", notice="") -> str:
    return render_to_string("admin/projects/project/_cockpit.html", context(project, user, range_key, notice))
