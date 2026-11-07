"""«🧭 Пульт проєкту» вгорі картки проєкту: колір і строк, що зараз, ризики, важливі моменти, пропозиції прив'язок,
шкала подій. Факти збираються один раз (overview.collect), стан записується в кеш списку (state.save), шкала —
timeline.render. Пошта й календар особисті: тема листа чи назва події — лише власнику (overview.visible, timeline).

Маршрути — projects/views.py: cockpit (GET, перегляд), highlight / highlight_delete / link_decide / scan (POST, зміна);
JS пульта — admin/projects/project/change_form.html (замінює #pj-cockpit без перезавантаження форми).
"""
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
            "end": p.end, "base_end": p.base_end,
            "late": bool(project.due_date and p.end and p.end > project.due_date),
            "baseline_at": project.baseline_at, "data": plan_mod.editor_data(project, p) if can_change else None,
            "steps": len(p.steps), "critical": sum(1 for c in p.steps if c.critical and c.status not in
                                                   ("done", "skipped"))}


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
    return {
        "project": project, "health": {"key": st.health, "icon": icon, "label": label},
        "due": _due(project, today), "start": project.start_date, "today": today,
        "next_action": project.next_action,
        "todo": [a for a in attention if a.get("kind") == "todo"],
        "risks": [a for a in attention if a.get("kind") != "todo"],
        "suggestions": _suggestions(project, uid),
        "moments": _moments(facts, uid)[:MOMENTS], "moments_total": len(facts["highlights"]),
        "kinds": ProjectHighlight.KIND_CHOICES, "mails": _mails(facts, uid),
        "counts": facts["counts"], "more": facts["more"],
        "timeline": timeline.render(project, facts, user, months=timeline.RANGES[range_key], today=today),
        "range": range_key, "ranges": RANGE_LABELS,
        "can_change": can_change,
        "has_products": project.products.exists(),
        "notice": notice,
        "plan": _plan_context(project, facts, today, can_change),
        "urls": {"cockpit": reverse("projects:cockpit", args=[pk]), "highlight": reverse("projects:highlight", args=[pk]),
                 "scan": reverse("projects:scan", args=[pk]), "mail": f"/email/?project={pk}",
                 "plan_template": reverse("projects:plan_template", args=[pk]),
                 "plan_step": reverse("projects:plan_step", args=[pk]),
                 "plan_phase": reverse("projects:plan_phase", args=[pk]),
                 "plan_baseline": reverse("projects:plan_baseline", args=[pk]),
                 "plan_save_template": reverse("projects:plan_save_template", args=[pk]),
                 "plan_clear": reverse("projects:plan_clear", args=[pk]),
                 "plan_base": f"/projects/api/project/{pk}/plan/"},
    }


def render(project, user, range_key="all", notice="") -> str:
    return render_to_string("admin/projects/project/_cockpit.html", context(project, user, range_key, notice))
