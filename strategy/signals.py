"""
strategy/signals.py

post_save on StepLog → advance strategy to next step when outcome is positive.
post_save on CustomerStrategy → recalculate RFM when strategy is done.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="strategy.StepLog")
def on_step_log_saved(sender, instance, created, **kwargs):
    if not created:
        return

    step = instance.step
    strategy = step.strategy

    if instance.outcome in ("done_pos", "done_neg", "skipped", "no_response"):
        # Mark the CustomerStep as completed
        from django.utils import timezone as tz
        step.outcome = instance.outcome
        step.outcome_notes = instance.notes
        step.completed_at = instance.logged_at or tz.now()
        step.save(update_fields=["outcome", "outcome_notes", "completed_at"])

        # Advance strategy
        from strategy.services.engine import advance_step
        advance_step(step, instance.outcome, instance.notes, instance.logged_by)


@receiver(post_save, sender="strategy.CustomerStrategy")
def on_strategy_done(sender, instance, **kwargs):
    """Trigger RFM recalculation when a strategy is completed.
    The Customer.rfm_score() method computes scores on-the-fly;
    if a stored-score model is added in the future, call it here.
    """
    if instance.status == "done":
        try:
            # Refresh the customer's computed RFM (no-op for now — computed live)
            instance.customer.rfm_score()
        except Exception:
            pass
