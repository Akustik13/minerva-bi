"""
strategy/services/engine.py

Core workflow engine for the CRM Workflow Builder.
"""
from __future__ import annotations
from django.utils import timezone


def recommend_template_behavior(customer) -> str | None:
    """
    Returns the recommended StrategyTemplate.behavior_type for a customer
    based on their RFM scores, or None if no recommendation.

    Rules (in priority order):
      R > 3 and F == 1          → onboarding   (recent first-time buyer)
      R < 2 and lost/inactive   → reactivation  (At Risk / Hibernating)
      Promising / growing       → nurturing     (Potential / Regular / New)
      Champion / Loyal          → retention     (VIP)
    """
    try:
        rfm = customer.rfm_score()
    except Exception:
        return None

    r, f    = rfm["R"], rfm["F"]
    segment = rfm["segment"]   # e.g. "🏆 Champions", "💤 Hibernating"

    if r > 3 and f == 1:
        return "onboarding"
    if r < 2 and ("At Risk" in segment or "Hibernating" in segment):
        return "reactivation"
    if "Potential" in segment or "Regular" in segment or "New" in segment:
        return "nurturing"
    if "Champion" in segment or "Loyal" in segment:
        return "retention"
    return None


def start_strategy(customer, template) -> "CustomerStrategy":
    """
    Create a new CustomerStrategy for a customer based on a template.
    Generates the first CustomerStep from the template's first step.
    Returns the created CustomerStrategy.
    """
    from strategy.models import CustomerStrategy, CustomerStep

    strategy = CustomerStrategy.objects.create(
        customer=customer,
        template=template,
        name=str(template),
        status=CustomerStrategy.Status.ACTIVE,
        started_at=timezone.now(),
    )

    first_template_step = template.steps.order_by("order").first()
    if first_template_step:
        delay_delta = timezone.timedelta(days=first_template_step.delay_days)
        step = CustomerStep.objects.create(
            strategy=strategy,
            template_step=first_template_step,
            step_type=first_template_step.step_type,
            title=first_template_step.title,
            description=first_template_step.description,
            scheduled_at=timezone.now() + delay_delta,
        )
        strategy.current_step = step
        strategy.next_action_at = step.scheduled_at
        strategy.save(update_fields=["current_step", "next_action_at"])

    return strategy


def get_next_step(current_step: "CustomerStep", outcome: str) -> "TemplateStep | None":
    """
    Given a completed CustomerStep and its outcome, return the next TemplateStep.
    For decision steps: done_pos → next_step_yes; done_neg/no_response → next_step_no.
    For linear steps: returns the template step with order = current.order + 1.
    """
    ts = current_step.template_step
    if ts is None:
        return None

    if ts.step_type == "decision":
        if outcome == "done_pos":
            return ts.next_step_yes
        else:
            return ts.next_step_no

    # Linear: next step by order within same template
    return (
        ts.template.steps
        .filter(order__gt=ts.order)
        .order_by("order")
        .first()
    )


def advance_step(
    customer_step: "CustomerStep",
    outcome: str,
    notes: str,
    user,
) -> "CustomerStep | None":
    """
    Mark the current step as completed, determine the next TemplateStep,
    create the next CustomerStep, update CustomerStrategy.current_step
    and next_action_at.

    Returns the newly created CustomerStep, or None if the strategy is finished.
    """
    from strategy.models import CustomerStrategy, CustomerStep

    strategy = customer_step.strategy

    # Don't double-advance if already moved past this step
    if strategy.current_step_id != customer_step.pk:
        return None

    next_ts = get_next_step(customer_step, outcome)

    if next_ts is None:
        # Strategy completed
        strategy.status = CustomerStrategy.Status.DONE
        strategy.current_step = None
        strategy.next_action_at = None
        strategy.save(update_fields=["status", "current_step", "next_action_at"])
        return None

    delay_delta = timezone.timedelta(days=next_ts.delay_days)
    next_step = CustomerStep.objects.create(
        strategy=strategy,
        template_step=next_ts,
        step_type=next_ts.step_type,
        title=next_ts.title,
        description=next_ts.description,
        scheduled_at=timezone.now() + delay_delta,
    )

    strategy.current_step = next_step
    strategy.next_action_at = next_step.scheduled_at
    strategy.save(update_fields=["current_step", "next_action_at"])

    return next_step
