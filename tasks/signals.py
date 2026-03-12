from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


@receiver(post_save, sender='crm.CustomerNote')
def note_reminder_to_task(sender, instance, **kwargs):
    """CustomerNote з типом REMINDER → авто-Task."""
    if instance.note_type != 'reminder':
        return
    from tasks.models import Task
    Task.objects.update_or_create(
        note=instance,
        task_type=Task.TaskType.NOTE_REMINDER,
        defaults=dict(
            title=instance.subject or f'Нагадування: {instance.customer.name}',
            description=instance.body,
            customer=instance.customer,
            due_date=instance.due_date,
            status=Task.Status.PENDING,
            priority=Task.Priority.MEDIUM,
        ),
    )


@receiver(post_save, sender='sales.SalesOrder')
def deadline_to_task(sender, instance, **kwargs):
    """SalesOrder: прострочений дедлайн і не відправлено → авто-Task."""
    from tasks.models import Task
    today = timezone.now().date()

    if (instance.shipping_deadline
            and instance.shipping_deadline < today
            and not instance.shipped_at):
        Task.objects.update_or_create(
            order=instance,
            task_type=Task.TaskType.DEADLINE_ALERT,
            defaults=dict(
                title=f'⚠️ Прострочено: {instance.order_number}',
                description=(
                    f'Дедлайн: {instance.shipping_deadline}\n'
                    f'Клієнт: {instance.client or instance.customer_key}'
                ),
                due_date=instance.shipping_deadline,
                priority=Task.Priority.HIGH,
                status=Task.Status.PENDING,
                notify_email=True,
            ),
        )
    elif instance.shipped_at:
        # Замовлення відправлено → закрити задачу якщо є
        Task.objects.filter(
            order=instance,
            task_type=Task.TaskType.DEADLINE_ALERT,
            status=Task.Status.PENDING,
        ).update(status=Task.Status.DONE)
