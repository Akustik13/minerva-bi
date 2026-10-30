"""email_assistant/signals.py — cross-app triggers for email events."""
import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger('email_assistant')


@receiver(post_save, sender='sales.SalesOrder')
def order_email_draft(sender, instance, created, **kwargs):
    """On new SalesOrder: generate an email draft to the customer if opt-in."""
    if not created:
        return

    try:
        from email_assistant.models import EmailAccount, EmailDraft, EmailSettings

        order_user = getattr(instance, 'user', None)
        account = None
        if order_user:
            account = EmailAccount.objects.filter(user=order_user, is_active=True).first()
        if not account:
            account = EmailAccount.objects.filter(is_active=True).first()
        if not account:
            return

        es = EmailSettings.get_for_user(account.user)
        if not es.order_trigger_enabled:
            return

        customer = None
        try:
            customer = instance.crm_customer
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
            source=EmailDraft.SOURCE_ORDER,
        )
        logger.info('Order draft created for order pk=%s customer=%s', instance.pk, customer.email)

    except Exception as exc:
        logger.error('order_email_draft signal error: %s', exc)


@receiver(post_delete, sender='email_assistant.EmailDraft')
@receiver(post_delete, sender='email_assistant.ScheduledEmail')
def remove_stored_attachments(sender, instance, **kwargs):
    """Чернетку / запланований лист видалено (надіслано, «🗑», адмінка, скринька) — їхні файли з MEDIA теж."""
    from email_assistant.attachments import remove_stored
    remove_stored(instance.attachments or [])


@receiver(post_save, sender='email_assistant.EmailMessage')
def link_email_to_crm(sender, instance, created, **kwargs):
    """Кожен новий лист (будь-яка синхронізація, імпорт, відправка) → хронологія CRM-клієнта."""
    if not created:
        return
    from email_assistant.crm_link import link_message_to_crm
    link_message_to_crm(instance)
    _set_category(instance)
    if not instance.is_spam:
        from email_assistant.work_categories import assign
        assign(instance)   # своя категорія (DigiKey, магазин…) — після CRM: замовлення з листа вже знайдено


def _set_category(em):
    """Категорія для фільтра скриньки (клієнти, постачальники, сервіси…); після прив'язки до CRM."""
    try:
        from email_assistant.categorize import categorize
        from email_assistant.models import EmailMessage
        em.category = categorize(em, list_unsubscribe=getattr(em, '_list_unsubscribe', False))
        EmailMessage.objects.filter(pk=em.pk).update(category=em.category)
    except Exception as exc:
        logger.error('categorize email pk=%s: %s', em.pk, exc)


@receiver(post_save, sender='email_assistant.EmailMessage')
def on_email_received(sender, instance, created, **kwargs):
    """When a new inbox email arrives, try to auto-advance the customer's active strategy."""
    if not created or instance.folder != 'inbox':
        return
    # Лише справжні Вхідні: синхронізація інших папок підтягує й старі листи
    inbox = (instance.account.imap_folder_inbox or 'INBOX').lower()
    if instance.imap_folder_name and instance.imap_folder_name.lower() != inbox:
        return
    try:
        _try_advance_strategy(instance)
    except Exception as exc:
        logger.error('on_email_received strategy advance error: %s', exc)


def _classify_sentiment(text: str) -> str:
    """Keyword-based sentiment classification for strategy branching."""
    t = text.lower()
    pos = ['yes', 'ok', 'agree', 'great', 'sure', 'interested', 'зголошуюсь',
           'погоджуюсь', 'добре', 'так', 'цікаво', 'чудово', 'готовий', 'дякую', 'дякуємо']
    neg = ['no', 'not interested', 'cancel', 'stop', 'unsubscribe', 'ні', 'відмовляюсь',
           'не цікаво', 'скасуй', 'відпишіть', 'більше не надсилайте']
    p = sum(1 for k in pos if k in t)
    n = sum(1 for k in neg if k in t)
    if p > n:
        return 'positive'
    if n > p:
        return 'negative'
    return 'neutral'


def _try_advance_strategy(msg):
    """Find CRM customer by sender email and advance their active strategy if current step is 'email'."""
    from crm.models import Customer
    from strategy.models import CustomerStrategy

    email_addr = (msg.from_email or '').lower().strip()
    if not email_addr:
        return

    customer = Customer.objects.filter(email__iexact=email_addr).first()
    if not customer:
        return

    strategy = (CustomerStrategy.objects
                .filter(customer=customer, status=CustomerStrategy.Status.ACTIVE)
                .select_related('current_step')
                .first())
    if not strategy or not strategy.current_step:
        return

    current_step = strategy.current_step
    if current_step.step_type != 'email':
        return
    if current_step.outcome != 'pending':
        return

    text = (msg.body_text or '')[:500]
    sentiment = _classify_sentiment(text)
    outcome = 'done_pos' if sentiment in ('positive', 'neutral') else 'done_neg'

    current_step.outcome = outcome
    current_step.save(update_fields=['outcome'])

    try:
        from crm.models import CustomerTimeline
        CustomerTimeline.objects.create(
            customer=customer,
            event_type='email_in',
            title=f'Відповідь на крок стратегії: {current_step.title}',
            body=text[:200],
        )
    except Exception:
        pass

    from strategy.services.engine import advance_step
    advance_step(current_step, outcome, '', None)
    logger.info('Strategy %s advanced for customer %s after email reply (outcome=%s)',
                strategy.pk, customer.name, outcome)
