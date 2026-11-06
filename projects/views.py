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
