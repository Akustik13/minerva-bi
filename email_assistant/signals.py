"""email_assistant/signals.py — cross-app triggers for email draft generation."""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger('email_assistant')


@receiver(post_save, sender='sales.SalesOrder')
def order_email_draft(sender, instance, created, **kwargs):
    """On new SalesOrder: generate an email draft to the customer if opt-in."""
    if not created:
        return

    try:
        from email_assistant.models import EmailAccount, EmailDraft, EmailSettings

        # Find account: prefer one belonging to order creator
        order_user = getattr(instance, 'user', None)
        account = None
        if order_user:
            account = EmailAccount.objects.filter(user=order_user, is_active=True).first()
        if not account:
            account = EmailAccount.objects.filter(is_active=True).first()
        if not account:
            return

        # Check if feature is enabled for that account's user
        es = EmailSettings.get_for_user(account.user)
        if not es.order_trigger_enabled:
            return

        # Find customer — SalesOrder.crm_customer is a property (external_key or email lookup)
        customer = None
        try:
            customer = instance.crm_customer  # property defined on SalesOrder
        except Exception:
            pass

        if not customer or not getattr(customer, 'email', None):
            return

        try:
            profile = account.user.profile
        except Exception:
            profile = None

        from email_assistant import ai_helper
        body = ai_helper.generate_order_draft(instance, customer, account, profile)
        if not body:
            return

        order_num = getattr(instance, 'order_number', None) or instance.pk
        subject = f'Замовлення #{order_num}'

        EmailDraft.objects.create(
            account=account,
            subject=subject,
            to_emails=[customer.email],
            body=body,
        )
        logger.info('Order draft created for order pk=%s customer=%s', instance.pk, customer.email)

    except Exception as exc:
        logger.error('order_email_draft signal error: %s', exc)
