"""JSON для пошти: проєкти гілки, «📁 До проєкту», створення з листування, ✓/✗ пропозицій;
пульт проєкту (projects/cockpit.py): шкала, 📌 важливі моменти, пропозиції прив'язок, «🔎 Знайти пов'язане»."""
import json
from datetime import date

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .models import Project, ProjectHighlight, ProjectLink


def _thread_for(request, thread_pk):
    """Гілка лише зі скриньок користувача (суперкористувач — будь-яка)."""
    from email_assistant.models import EmailThread
    qs = EmailThread.objects.all() if request.user.is_superuser else EmailThread.objects.filter(
        account__user=request.user)
    return get_object_or_404(qs, pk=thread_pk)


def _project_dict(p):
    return {"id": p.pk, "code": p.code, "name": p.name, "status": p.get_status_display(),
            "customer": p.customer.name if p.customer_id else "",
            "url": f"/admin/projects/project/{p.pk}/change/", "mail_url": f"/email/?project={p.pk}"}


def thread_state(thread) -> dict:
    info = services.thread_projects(thread)
    return {
        "thread": thread.pk,
        "confirmed": [{"link": l.pk, "project": _project_dict(l.project), "reason": l.reason}
                      for l in info["confirmed"]],
        "suggested": [{"link": l.pk, "project": _project_dict(l.project), "reason": l.reason}
                      for l in info["suggested"]],
    }


def _body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


@staff_member_required
def thread_view(request, thread_pk):
    thread = _thread_for(request, thread_pk)
    state = thread_state(thread)
    linked = {c["project"]["id"] for c in state["confirmed"]}
    state["candidates"] = [_project_dict(p) for p in services.candidate_projects(thread) if p.pk not in linked]
    state["name_suggestion"] = services.suggest_name([thread])
    return JsonResponse(state)


@staff_member_required
@require_POST
def link_view(request, thread_pk):
    thread = _thread_for(request, thread_pk)
    project = get_object_or_404(Project, pk=_body(request).get("project_id") or 0)
    services.link_thread(project, thread, user=request.user, reason="додано з пошти")
    return JsonResponse({"ok": True, **thread_state(thread)})


@staff_member_required
@require_POST
def create_view(request, thread_pk):
    thread = _thread_for(request, thread_pk)
    project = services.create_project(name=_body(request).get("name", ""), user=request.user, threads=[thread])
    return JsonResponse({"ok": True, "project": _project_dict(project), **thread_state(thread)})


def _link_for(request, link_pk):
    link = get_object_or_404(ProjectLink.objects.select_related("project", "thread"), pk=link_pk)
    if link.thread_id:
        _thread_for(request, link.thread_id)   # перевірка доступу до скриньки
    elif not request.user.is_superuser:
        from django.http import Http404
        raise Http404
    return link


@staff_member_required
@require_POST
def decide_view(request, link_pk):
    link = _link_for(request, link_pk)
    services.decide(link, bool(_body(request).get("accept")), user=request.user)
    return JsonResponse({"ok": True, **(thread_state(link.thread) if link.thread_id else {})})


@staff_member_required
@require_POST
def remove_view(request, link_pk):
    link = _link_for(request, link_pk)
    thread = link.thread
    services.unlink(link)
    return JsonResponse({"ok": True, **(thread_state(thread) if thread else {})})


# ── Огляд проєкту ────────────────────────────────────────────────────────────

def _project_for(request, pk, perm):
    """Огляд — як сторінка проєкту: переглядати з правом view, складати (пише в БД, витрачає AI-запит) — change."""
    from django.http import Http404
    if not request.user.has_perm(f"projects.{perm}_project"):
        raise Http404
    return get_object_or_404(Project, pk=pk)


def _project_for_change(request, pk):
    return _project_for(request, pk, "change")


@staff_member_required
@require_POST
def rename_view(request, pk):
    """✏️ Перейменувати проєкт / 🎨 значок (правий клік у пошті): {name} і / або {icon} — право change;
    код P-… не змінюється."""
    project = _project_for_change(request, pk)
    body, fields = _body(request), ["updated_at"]
    if "name" in body:
        name = str(body.get("name") or "").strip()[:200]
        if not name:
            return JsonResponse({"ok": False, "error": "Вкажіть назву"}, status=400)
        project.name = name
        fields.append("name")
    if "icon" in body:
        project.icon = str(body.get("icon") or "").strip()[:8]
        fields.append("icon")
    project.save(update_fields=fields)
    return JsonResponse({"ok": True, "project": _project_dict(project)})


@staff_member_required
@require_http_methods(["GET", "POST"])
def overview_view(request, pk):
    """GET — стан огляду (панель у пошті); POST — скласти AI-огляд заново."""
    from . import overview
    if request.method == "POST":
        project = _project_for_change(request, pk)
        result, error = overview.generate(project, user=request.user)
        if result is None:      # як AI-функції пошти: збій AI — не помилка сервера
            return JsonResponse({"ok": False, "error": error})
    else:
        project = _project_for(request, pk, "view")
    return JsonResponse(overview.state(project, request.user))


@staff_member_required
@require_POST
def overview_next_action_view(request, pk):
    from . import overview
    project = _project_for_change(request, pk)
    text = overview.apply_next_action(project)
    return JsonResponse({"ok": bool(text), "next_action": text})


# ── Пульт проєкту ────────────────────────────────────────────────────────────

def _cockpit(request, project, notice="", extra=None):
    """Свіжий HTML пульта (JS замінює #pj-cockpit, форма картки не перезавантажується)."""
    from . import cockpit
    if request.method == "POST":
        range_key = str(_body(request).get("range") or request.GET.get("range") or "all")
    else:
        range_key = request.GET.get("range") or "all"
    return JsonResponse({"ok": True, "html": cockpit.render(project, request.user, range_key, notice), **(extra or {})})


@staff_member_required
@require_GET
def cockpit_view(request, pk):
    return _cockpit(request, _project_for(request, pk, "view"))


def _own_message(request, pk):
    """Лист лише з власних скриньок людини (і для суперкористувача — як «📬 Листи зі скриньки»)."""
    from email_assistant.models import EmailMessage
    return EmailMessage.objects.filter(pk=pk, account__user=request.user, is_deleted=False).first()


@staff_member_required
@require_POST
def highlight_view(request, pk):
    """📌 Важливий момент: {title, date YYYY-MM-DD, kind, quote, message, cockpit, range} — право змінювати проєкт.
    Лист — лише з власної скриньки. cockpit: true — у відповіді свіжий пульт (html)."""
    project = _project_for_change(request, pk)
    body = _body(request)
    message = None
    if body.get("message"):
        message = _own_message(request, body["message"]) if str(body["message"]).isdigit() else None
        if message is None:
            return JsonResponse({"ok": False, "error": "Лист не знайдено у ваших скриньках"}, status=400)
    try:
        when = date.fromisoformat(str(body.get("date"))[:10]) if body.get("date") else None
    except ValueError:
        when = None
    try:
        h = services.add_highlight(project, title=body.get("title"), date=when, kind=str(body.get("kind") or ""),
                                   quote=body.get("quote") or "", message=message, user=request.user)
    except services.HighlightError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    info = {"highlight": {"id": h.pk, "date": h.date.strftime("%d.%m.%Y"), "title": h.title},
            "project": {"id": project.pk, "code": project.code, "name": project.name}}
    if body.get("cockpit"):
        return _cockpit(request, project, f"📌 Додано: {h.title}", extra=info)
    return JsonResponse({"ok": True, **info})


@staff_member_required
@require_POST
def highlight_delete_view(request, hid):
    h = get_object_or_404(ProjectHighlight.objects.select_related("project"), pk=hid)
    project = _project_for_change(request, h.project_id)
    h.delete()
    return _cockpit(request, project, "📌 Момент прибрано")


@staff_member_required
@require_POST
def link_decide_view(request, pk, link_pk):
    """✓/✗ пропозиції прив'язки з пульта. Гілку чи лист вирішує лише власник скриньки (як у пошті)."""
    project = _project_for_change(request, pk)
    link = get_object_or_404(ProjectLink.objects.select_related("thread", "message"), pk=link_pk, project=project)
    mail = link.thread or (link.message.thread if link.message_id else None)
    if mail is not None and mail.account.user_id != request.user.pk:
        return JsonResponse({"ok": False, "error": "Цю гілку вирішує власник скриньки — у пошті"}, status=403)
    accept = bool(_body(request).get("accept"))
    services.decide(link, accept, user=request.user)
    return _cockpit(request, project, "🔗 Прив'язано" if accept else "🔗 Більше не пропонуватиметься")


@staff_member_required
@require_POST
def scan_view(request, pk):
    """«🔎 Знайти пов'язане»: закупівлі й JLC-замовлення з товарами проєкту → пропозиції ✓/✗."""
    project = _project_for_change(request, pk)
    if not project.products.exists():
        notice = "Додайте «Товари проєкту» в полях проєкту — за ними Minerva шукає закупівлі й JLC-замовлення"
    else:
        n = services.scan(project)
        notice = (f"🔎 Нових пропозицій: {n}" if n else
                  "🔎 Нового не знайдено: закупівлі й JLC-замовлення з товарами проєкту вже запропоновані або їх немає")
    return _cockpit(request, project, notice)


# ── 📅 План (projects/plan.py, schedule.py) ──────────────────────────────────

def _plan_action(request, pk, fn):
    """Дія плану, ризику чи статусу: право змінювати проєкт; помилка даних (PlanError, RiskError, ValueError) →
    400 з поясненням; успіх → свіжий пульт."""
    project = _project_for_change(request, pk)
    try:
        notice = fn(project, _body(request))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return _cockpit(request, project, notice)


# ── ⚠️ Ризики й 📝 тижневий статус (projects/risks.py, risk_rules.py, health.py) ─────────────

@staff_member_required
@require_POST
def risk_view(request, pk):
    """Створити / змінити ризик: {id, title, description, category, probability, impact, owner, mitigation, status,
    review_on, steps, message, quote} — message лише з власної скриньки (ризик з листа)."""
    from . import risks

    def run(project, body):
        message = None
        if str(body.get("message") or "").isdigit() and not body.get("id"):
            message = _own_message(request, int(body["message"]))
            if message is None:
                raise risks.RiskError("Лист не знайдено у ваших скриньках")
        r = risks.save(project, body, user=request.user, message=message)
        return f"⚠️ Ризик збережено: {r.title}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def risk_delete_view(request, pk, rid):
    from . import risks
    return _plan_action(request, pk, lambda project, body: "⚠️ Ризик видалено" if risks.delete(project, rid)
                        else "Ризик уже видалено")


@staff_member_required
@require_POST
def status_view(request, pk):
    """📝 Тижневий статус: {health on_track | at_risk | off_track, text, ai} — зі знімком плану й ризиків."""
    from . import health

    def run(project, body):
        u = health.save(project, request.user, body.get("health"), body.get("text"), ai=bool(body.get("ai")))
        return f"📝 Статус збережено: {u.get_health_display()}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def status_draft_view(request, pk):
    """🧠 AI-чернетка тижневого статусу (Claude Haiku 5.5, бюджет AI) → {ok, text, error} — нічого не зберігає."""
    from . import health
    project = _project_for_change(request, pk)
    text, error = health.ai_draft(project, request.user)
    return JsonResponse({"ok": bool(text), "text": text, "error": error})


@staff_member_required
@require_POST
def plan_template_view(request, pk):
    """«🧩 Скласти план з шаблону»: {template, start YYYY-MM-DD}."""
    from . import plan
    from .models import ProjectTemplate

    def run(project, body):
        tpl = ProjectTemplate.objects.filter(pk=body.get("template") or 0, is_active=True).first() \
            if str(body.get("template") or "").isdigit() else None
        if tpl is None:
            raise plan.PlanError("Виберіть шаблон")
        n = plan.apply_template(project, tpl, plan._date(body.get("start")), user=request.user)
        return f"🧩 План з шаблону «{tpl.name}»: кроків {n}. Перевірте дати й зафіксуйте базовий план"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_step_view(request, pk):
    from . import plan

    def run(project, body):
        step = plan.save_step(project, body, user=request.user)
        return f"📋 Збережено: {step.title}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_step_delete_view(request, pk, sid):
    from . import plan
    return _plan_action(request, pk, lambda project, body: "📋 Крок прибрано" if plan.delete_step(project, sid)
                        else "Крок уже прибрано")


@staff_member_required
@require_POST
def plan_phase_view(request, pk):
    from . import plan

    def run(project, body):
        phase = plan.save_phase(project, body)
        return f"📋 Фаза збережена: {phase.name}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_phase_delete_view(request, pk, fid):
    from . import plan
    return _plan_action(request, pk, lambda project, body: "📋 Фазу прибрано, її кроки лишились без фази"
                        if plan.delete_phase(project, fid) else "Фазу вже прибрано")


@staff_member_required
@require_POST
def plan_baseline_view(request, pk):
    from . import plan
    return _plan_action(request, pk, lambda project, body:
                        f"📌 Базовий план зафіксовано: кроків {plan.baseline(project, request.user)}")


@staff_member_required
@require_POST
def plan_save_template_view(request, pk):
    from . import plan

    def run(project, body):
        tpl = plan.save_as_template(project, body.get("name"), user=request.user)
        return f"💾 Шаблон «{tpl.name}» збережено — він з'явиться у виборі шаблонів"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_clear_view(request, pk):
    from . import plan

    def run(project, body):
        plan.clear(project)
        return "🗑 План очищено"
    return _plan_action(request, pk, run)


# ── 🧠 AI-помічник проєкту (projects/ai_scan.py, ai_plan.py, forgotten.py, delegate.py) ───────────────────────────
# Усе — з правом змінювати проєкт; пошта — лише власні скриньки людини (перевіряють самі модулі).

@staff_member_required
@require_POST
def ai_scan_view(request, pk):
    """🧠 Знайти моменти й ризики в листах: наступна порція власних непрочитаних листів проєкту → свіжий пульт."""
    from . import ai_scan
    project = _project_for_change(request, pk)
    res = ai_scan.scan(project, request.user)
    if res["error"]:
        return JsonResponse({"ok": False, "error": res["error"]}, status=400)
    if not res["read"]:
        notice = "🧠 Нових листів немає — усі ваші листи проєкту вже прочитано"
    else:
        notice = f"🧠 Прочитано листів: {res['read']} · нових пропозицій: {res['found']}"
        if res["left"]:
            notice += f" · ще не прочитано: {res['left']}"
    return _cockpit(request, project, notice)


@staff_member_required
@require_POST
def ai_suggestion_view(request, pk, sid):
    """✓ / ✗ пропозиції AI: {accept: true|false} — лише та людина, з чиїх листів пропозиція."""
    from . import ai_scan

    def run(project, body):
        if body.get("accept"):
            return ai_scan.accept(project, request.user, sid)
        return ai_scan.reject(project, request.user, sid)
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def ai_plan_view(request, pk):
    """🧠 Чернетка плану з листування → JSON для вікна перегляду (нічого не пише в план)."""
    from . import ai_plan
    project = _project_for_change(request, pk)
    try:
        draft = ai_plan.draft(project, request.user)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "draft": draft})


@staff_member_required
@require_POST
def ai_plan_apply_view(request, pk):
    """✓ Застосувати чернетку: {skip: [номери кроків], start} → план → свіжий пульт."""
    from . import ai_plan

    def run(project, body):
        skip = body.get("skip") if isinstance(body.get("skip"), list) else []
        return f"🧠 План з листування складено: кроків {ai_plan.apply(project, request.user, skip, body.get('start'))}"
    return _plan_action(request, pk, run)


def _rag_state(project, user, job) -> dict:
    from . import forgotten
    from rag_assistant.models import RetrievalJob
    if job is None:
        return {"rag": False, "job": "", "done": True, "rag_rows": [], "message": ""}
    done = job.is_terminal
    rows = forgotten.rag_rows(project, user, job) if job.status == RetrievalJob.COMPLETED else []
    return {"rag": True, "job": "" if done else str(job.pk), "done": done, "rag_rows": rows,
            "message": "" if job.status == RetrievalJob.COMPLETED else (job.message or job.get_status_display()),
            "poll_ms": max(800, min(5000, int(job.poll_after_ms or 1500)))}


@staff_member_required
@require_POST
def forgotten_view(request, pk):
    """🔎 Забуті листи: одразу — знайдене без AI; база знань — завдання RAG, браузер опитує forgotten_poll."""
    from . import forgotten
    project = _project_for_change(request, pk)
    rows = forgotten.local(project, request.user)
    return JsonResponse({"ok": True, "rows": rows, **_rag_state(project, request.user,
                                                                 forgotten.rag_start(project, request.user))})


@staff_member_required
def forgotten_poll_view(request, pk, job_id):
    from rag_assistant import retrieval
    from rag_assistant.models import RetrievalJob
    project = _project_for_change(request, pk)
    job = get_object_or_404(RetrievalJob, pk=job_id, user=request.user, purpose="project_forgotten")
    retrieval.poll(job)
    return JsonResponse({"ok": True, **_rag_state(project, request.user, job)})


@staff_member_required
@require_POST
def forgotten_decide_view(request, pk):
    """✓ / ✗ забутої гілки: {thread, accept} — лише гілки власних скриньок."""
    from . import forgotten
    project = _project_for_change(request, pk)
    body = _body(request)
    try:
        notice = forgotten.decide(project, request.user, int(body.get("thread") or 0), bool(body.get("accept")))
    except (TypeError, ValueError) as e:
        return JsonResponse({"ok": False, "error": str(e) or "Гілку не знайдено"}, status=400)
    return JsonResponse({"ok": True, "notice": notice})


@staff_member_required
def step_agent_view(request, pk, sid):
    """🤖 Доручити крок агенту: GET — варіанти доручення й завдання кроку; POST {instruction, mail_to, model, budget}
    — завдання агенту від імені людини, прив'язане до кроку, запуск у фоні → свіжий пульт і адреса перевірки."""
    from . import cockpit, delegate
    from .models import ProjectStep
    project = _project_for_change(request, pk)
    step = get_object_or_404(ProjectStep.objects.select_related("project__customer", "phase"), pk=sid, project=project)
    if request.method == "GET":
        return JsonResponse(delegate.editor(step, request.user))
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST"}, status=405)
    try:
        task = delegate.create(step, request.user, _body(request))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "url": f"/admin/agent/agenttask/{task.pk}/review/",
                         "html": cockpit.render(project, request.user, str(_body(request).get("range") or "all"),
                                                f"🤖 Завдання агенту #{task.pk}: «{step.title}» — агент працює")})
