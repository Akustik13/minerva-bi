import json
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_POST, require_http_methods
from django.utils import timezone

from .models import CustomerStrategy, CustomerStep, StepLog


@staff_member_required
def canvas_view(request, pk):
    strategy = get_object_or_404(
        CustomerStrategy.objects.select_related("customer", "template", "current_step"),
        pk=pk,
    )
    steps = list(strategy.steps.select_related("template_step").order_by("scheduled_at"))

    context = {
        "title": f"Canvas — {strategy}",
        "strategy": strategy,
        "steps": steps,
        "outcome_choices": CustomerStep.Outcome.choices,
        "has_permission": True,
        "strategy_pk": pk,
    }
    return TemplateResponse(request, "strategy/canvas.html", context)


@staff_member_required
def canvas_data(request, pk):
    """JSON: full strategy + steps data for the canvas."""
    strategy = get_object_or_404(
        CustomerStrategy.objects.select_related("customer", "template"),
        pk=pk,
    )
    steps = list(strategy.steps.select_related("template_step").order_by("scheduled_at"))

    rfm = strategy.customer.rfm_score()

    # Build TemplateStep order→CustomerStep map for next_*_id resolution
    # next_yes_id / next_no_id are TemplateStep PKs; we need CustomerStep PKs
    ts_to_cs = {}
    for s in steps:
        if s.template_step_id:
            ts_to_cs[s.template_step_id] = s.pk

    nodes = []
    for s in steps:
        ts = s.template_step
        sched = s.scheduled_at.strftime("%d.%m.%Y") if s.scheduled_at else None
        nodes.append({
            "id": s.pk,
            "title": s.title,
            "step_type": s.step_type,
            "outcome": s.outcome,
            "outcome_notes": s.outcome_notes,
            "scheduled_at": sched,
            "canvas_x": ts.canvas_x if ts else 0.0,
            "canvas_y": ts.canvas_y if ts else 0.0,
            "next_yes_id": ts_to_cs.get(ts.next_step_yes_id) if ts and ts.next_step_yes_id else None,
            "next_no_id":  ts_to_cs.get(ts.next_step_no_id)  if ts and ts.next_step_no_id  else None,
            "is_current": (strategy.current_step_id == s.pk),
        })

    return JsonResponse({
        "strategy": {
            "id": strategy.pk,
            "name": strategy.name or str(strategy.template),
            "status": strategy.status,
            "customer_name": strategy.customer.company or strategy.customer.name,
            "rfm_r": rfm["R"], "rfm_f": rfm["F"], "rfm_m": rfm["M"],
            "segment": rfm["segment"],
        },
        "steps": nodes,
    })


@require_POST
@staff_member_required
def step_complete(request, pk):
    """POST JSON → log step outcome, advance strategy, return next_step_id."""
    step = get_object_or_404(CustomerStep.objects.select_related("strategy"), pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    outcome = body.get("outcome", "done_pos")
    notes   = body.get("notes", "")

    VALID = {c[0] for c in CustomerStep.Outcome.choices} - {"pending"}
    if outcome not in VALID:
        return JsonResponse({"success": False, "error": "Invalid outcome"}, status=400)

    log = StepLog.objects.create(
        step=step,
        logged_by=request.user,
        outcome=outcome,
        notes=notes,
        logged_at=timezone.now(),
    )
    # Signal in signals.py fires on StepLog save → marks step + advances strategy.
    # Reload step to get updated state.
    step.refresh_from_db()
    strategy = step.strategy
    strategy.refresh_from_db()

    return JsonResponse({
        "success": True,
        "next_step_id": strategy.current_step_id,
        "strategy_status": strategy.status,
    })


@require_http_methods(["PATCH"])
@staff_member_required
def step_position(request, pk):
    """PATCH JSON {x, y} → save canvas_x/canvas_y on TemplateStep."""
    step = get_object_or_404(CustomerStep.objects.select_related("template_step"), pk=pk)

    try:
        body = json.loads(request.body)
        x, y = float(body["x"]), float(body["y"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid body"}, status=400)

    if step.template_step:
        step.template_step.canvas_x = x
        step.template_step.canvas_y = y
        step.template_step.save(update_fields=["canvas_x", "canvas_y"])

    return JsonResponse({"success": True})
