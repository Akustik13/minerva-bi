"""Проєкти й події інших модулів:
- новий лист → підказка, до якого проєкту він може належати (після прив'язки до CRM у email_assistant);
- нова закупівля, нова позиція закупівлі, JLC-замовлення → пропозиції прив'язки за товарами проєкту (services);
- зміни прив'язок, важливих моментів, проєкту, листів, Angebote, замовлень, закупівель, JLC, відправлень, запрошень на зустрічі →
  кеш стану проєкту застарів (projects/state.py: список і фільтр «За терміновістю»).
Збій тут ніколи не ламає збереження самого об'єкта.
"""
import logging

from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from email_assistant.models import EmailMessage

from .models import Project, ProjectHighlight, ProjectLink, ProjectPhase, ProjectStep

logger = logging.getLogger(__name__)


def _safe(what, fn, *args):
    try:
        with transaction.atomic():
            return fn(*args)
    except Exception as e:  # noqa: BLE001 — підказка чи кеш не повинні зламати збереження
        logger.warning("projects %s: %s", what, e)
        return None


def _dirty(cond):
    """Кеш стану проєктів, пов'язаних умовою над ProjectLink, — застарів."""
    from . import state
    ids = set(ProjectLink.objects.filter(cond).values_list("project_id", flat=True))
    if ids:
        state.mark_dirty(ids)


@receiver(post_save, sender=EmailMessage)
def suggest_projects_for_new_mail(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return
    from projects.services import suggest_for_message
    _safe(f"suggestion for message {instance.pk}", suggest_for_message, instance)
    if instance.thread_id:
        _safe("dirty (mail)", _dirty, Q(thread_id=instance.thread_id) | Q(message_id=instance.pk))


@receiver(post_save, sender=ProjectLink)
@receiver(post_delete, sender=ProjectLink)
@receiver(post_save, sender=ProjectHighlight)
@receiver(post_delete, sender=ProjectHighlight)
def project_part_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    from . import state
    _safe("dirty (link)", state.mark_dirty, [instance.project_id])


@receiver(post_save, sender=Project)
def project_changed(sender, instance, created, **kwargs):
    if created or kwargs.get("raw"):
        return
    from . import state
    _safe("dirty (project)", state.mark_dirty, [instance.pk])


@receiver(post_save, sender="offers.Offer")
def offer_changed(sender, instance, **kwargs):
    if kwargs.get("raw") or not instance.project_id:
        return
    from . import state
    _safe("dirty (offer)", state.mark_dirty, [instance.project_id])


@receiver(post_save, sender="sales.SalesOrder")
def order_changed(sender, instance, **kwargs):
    if not kwargs.get("raw"):
        _safe("dirty (order)", _dirty, Q(order_id=instance.pk))


@receiver(post_save, sender="inventory.PurchaseOrder")
def purchase_changed(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    from projects.services import suggest_for_purchase
    if created:
        _safe(f"suggestion for {instance}", suggest_for_purchase, instance)
    _safe("dirty (purchase)", _dirty, Q(purchase_order_id=instance.pk))


@receiver(post_save, sender="inventory.PurchaseOrderLine")
def purchase_line_changed(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not created or not instance.product_id:
        return
    from projects.services import suggest_for_purchase
    _safe(f"suggestion for PO {instance.purchase_order_id}", suggest_for_purchase, instance.purchase_order)


@receiver(post_save, sender="jlcpcb.JLCOrder")
def jlc_changed(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    from projects.services import suggest_for_jlc
    if created or instance.product_id:
        _safe(f"suggestion for JLC {instance.pk}", suggest_for_jlc, instance)
    cond = Q(jlc_order_id=instance.pk)
    if instance.purchase_order_id:
        cond |= Q(purchase_order_id=instance.purchase_order_id)
    _safe("dirty (jlc)", _dirty, cond)


@receiver(post_save, sender="shipping.Shipment")
def shipment_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    cond = Q(shipment_id=instance.pk)
    for field in ("order_id", "purchase_order_id", "jlc_order_id"):
        if getattr(instance, field, None):
            cond |= Q(**{field: getattr(instance, field)})
    _safe("dirty (shipment)", _dirty, cond)


@receiver(post_save, sender="calendar_app.MeetingInvite")
def invite_changed(sender, instance, **kwargs):
    """📨 Запрошення прийшло, змінилось чи на нього відповіли — «потребує уваги» проєктів його листа, гілки, події
    й клієнта (projects/invites.py)."""
    if kwargs.get("raw"):
        return

    def run():
        from . import state
        cond = Q(message_id=instance.email_message_id) if instance.email_message_id else Q(pk__in=[])
        thread_id = (EmailMessage.objects.filter(pk=instance.email_message_id).values_list("thread_id", flat=True)
                     .first() if instance.email_message_id else None)
        if thread_id:
            cond |= Q(thread_id=thread_id)
        if instance.event_id:
            cond |= Q(event_id=instance.event_id)
        _dirty(cond)
        if instance.crm_customer_id:
            state.mark_dirty(list(Project.objects.filter(customer_id=instance.crm_customer_id,
                                                         status__in=Project.OPEN_STATUSES).values_list("pk", flat=True)))
    _safe("dirty (invite)", run)


# 🕘 Історія змін плану (projects/plan_history.py): збій запису історії не заважає зберегти крок чи фазу
def _history(what, fn, *args):
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        logger.warning("projects plan history (%s): %s", what, e)


@receiver(pre_save, sender=ProjectStep, dispatch_uid="plan_history_step_pre")
def plan_step_before(sender, instance, update_fields=None, **kwargs):
    if kwargs.get("raw"):
        return
    from . import plan_history
    _history("step pre", plan_history.step_pre_save, instance, update_fields)


@receiver(post_save, sender=ProjectStep, dispatch_uid="plan_history_step_post")
def plan_step_saved(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    from . import plan_history
    _history("step", plan_history.step_post_save, instance, created)


@receiver(post_delete, sender=ProjectStep, dispatch_uid="plan_history_step_delete")
def plan_step_deleted(sender, instance, **kwargs):
    from . import plan_history
    _history("step delete", plan_history.step_post_delete, instance)


@receiver(pre_save, sender=ProjectPhase, dispatch_uid="plan_history_phase_pre")
def plan_phase_before(sender, instance, update_fields=None, **kwargs):
    if kwargs.get("raw"):
        return
    from . import plan_history
    _history("phase pre", plan_history.phase_pre_save, instance, update_fields)


@receiver(post_save, sender=ProjectPhase, dispatch_uid="plan_history_phase_post")
def plan_phase_saved(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    from . import plan_history
    _history("phase", plan_history.phase_post_save, instance, created)


@receiver(post_delete, sender=ProjectPhase, dispatch_uid="plan_history_phase_delete")
def plan_phase_deleted(sender, instance, **kwargs):
    from . import plan_history
    _history("phase delete", plan_history.phase_post_delete, instance)


@receiver(pre_delete, sender=Project, dispatch_uid="plan_history_project_deleting")
def plan_project_deleting(sender, instance, **kwargs):
    from . import plan_history
    plan_history.project_deleting(instance.pk)


@receiver(post_delete, sender=Project, dispatch_uid="plan_history_project_deleted")
def plan_project_deleted(sender, instance, **kwargs):
    from . import plan_history
    plan_history.project_deleting(instance.pk, on=False)
