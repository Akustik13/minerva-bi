"""JSON для пошти: проєкти гілки, «📁 До проєкту», створення з листування, ✓/✗ пропозицій."""
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from . import services
from .models import Project, ProjectLink


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
