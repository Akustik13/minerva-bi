import json
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_POST
from django.utils import timezone

from .models import CustomerStrategy, CustomerStep, StepLog


@staff_member_required
def canvas_view(request, pk):
    strategy = get_object_or_404(CustomerStrategy.objects.select_related(
        "customer", "template", "current_step"
    ), pk=pk)

    steps = strategy.steps.select_related("template_step").order_by("scheduled_at")

    context = {
        "title": f"Canvas — {strategy}",
        "strategy": strategy,
        "steps": steps,
        "outcome_choices": CustomerStep.Outcome.choices,
        # Admin context for sidebar
        "has_permission": True,
    }
    return TemplateResponse(request, "strategy/canvas.html", context)


@staff_member_required
def canvas_data_view(request, pk):
    strategy = get_object_or_404(CustomerStrategy, pk=pk)
    steps = list(strategy.steps.select_related("template_step").order_by("scheduled_at"))

    nodes = []
    for s in steps:
        ts = s.template_step
        nodes.append({
            "id": s.pk,
            "title": s.title,
            "step_type": s.step_type,
            "outcome": s.outcome,
            "scheduled_at": s.scheduled_at.isoformat() if s.scheduled_at else None,
            "x": ts.canvas_x if ts else 0,
            "y": ts.canvas_y if ts else 0,
            "next_yes_id": ts.next_step_yes_id if ts else None,
            "next_no_id": ts.next_step_no_id if ts else None,
        })

    return JsonResponse({"nodes": nodes, "strategy_status": strategy.status})


@require_POST
@staff_member_required
def log_step_view(request, pk):
    strategy = get_object_or_404(CustomerStrategy, pk=pk)
    step_id  = request.POST.get("step_id")
    outcome  = request.POST.get("outcome", "done_pos")
    notes    = request.POST.get("notes", "")

    step = get_object_or_404(CustomerStep, pk=step_id, strategy=strategy)

    StepLog.objects.create(
        step=step,
        logged_by=request.user,
        outcome=outcome,
        notes=notes,
        logged_at=timezone.now(),
    )

    return redirect("strategy:canvas", pk=pk)
