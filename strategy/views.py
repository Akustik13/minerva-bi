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

    # Auto-initialize: if an active strategy was created via admin (no start_strategy()
    # call), create the first CustomerStep so list + canvas are immediately actionable.
    if not steps and strategy.status == CustomerStrategy.Status.ACTIVE:
        first_ts = strategy.template.steps.order_by("order").first()
        if first_ts:
            first_step = CustomerStep.objects.create(
                strategy=strategy,
                template_step=first_ts,
                step_type=first_ts.step_type,
                title=first_ts.title,
                description=first_ts.description,
                scheduled_at=timezone.now() + timezone.timedelta(days=first_ts.delay_days),
            )
            strategy.current_step = first_step
            strategy.next_action_at = first_step.scheduled_at
            strategy.save(update_fields=["current_step", "next_action_at"])
            steps = [first_step]

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
    """JSON: full strategy + steps data for the canvas.

    Returns ALL TemplateStep nodes — activated ones as real nodes,
    un-activated ones as ghost nodes (dimmed, dashed border).
    """
    strategy = get_object_or_404(
        CustomerStrategy.objects.select_related("customer", "template"),
        pk=pk,
    )

    rfm = strategy.customer.rfm_score()

    # All template steps define the full plan (ordered by template order)
    template_steps = list(
        strategy.template.steps.order_by("order")
    )

    # Map template_step_id → CustomerStep (only for activated steps)
    customer_steps = list(strategy.steps.select_related("template_step").all())
    cs_by_ts_id = {}
    for s in customer_steps:
        if s.template_step_id:
            cs_by_ts_id[s.template_step_id] = s

    # template_step.pk → node id:
    #   real CustomerStep  →  CustomerStep.pk  (positive int)
    #   ghost (not yet)    → -TemplateStep.pk  (negative int)
    ts_to_node = {}
    for ts in template_steps:
        cs = cs_by_ts_id.get(ts.pk)
        ts_to_node[ts.pk] = cs.pk if cs else -(ts.pk)

    nodes = []
    for ts in template_steps:
        cs = cs_by_ts_id.get(ts.pk)
        is_ghost = cs is None

        if is_ghost:
            node_id      = -(ts.pk)
            title        = ts.title
            outcome      = "pending"
            outcome_notes = ""
            scheduled_at = None
            is_current   = False
        else:
            node_id       = cs.pk
            title         = cs.title or ts.title
            scheduled_at  = cs.scheduled_at.strftime("%d.%m.%Y") if cs.scheduled_at else None
            outcome       = cs.outcome
            outcome_notes = cs.outcome_notes or ""
            is_current    = (strategy.current_step_id == cs.pk)

        nodes.append({
            "id":           node_id,
            "title":        title,
            "step_type":    ts.step_type,
            "outcome":      outcome,
            "outcome_notes": outcome_notes,
            "scheduled_at": scheduled_at,
            "canvas_x":     ts.canvas_x,
            "canvas_y":     ts.canvas_y,
            "next_yes_id":  ts_to_node.get(ts.next_step_yes_id) if ts.next_step_yes_id else None,
            "next_no_id":   ts_to_node.get(ts.next_step_no_id)  if ts.next_step_no_id  else None,
            "is_current":   is_current,
            "is_ghost":     is_ghost,
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
