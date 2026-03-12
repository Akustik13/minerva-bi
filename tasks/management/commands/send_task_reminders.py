from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings


class Command(BaseCommand):
    help = 'Надсилає email нагадування для задач з notify_email=True і due_date <= сьогодні'

    def handle(self, *args, **options):
        from tasks.models import Task

        today = timezone.now().date()
        pending = Task.objects.filter(
            notify_email=True,
            notified_at__isnull=True,
            due_date__lte=today,
        ).exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])

        if not pending.exists():
            self.stdout.write('send_task_reminders: немає задач для нагадування')
            return

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'minerva@localhost')
        admin_email = getattr(settings, 'ADMINS', [])
        recipients = [e for _, e in admin_email] if admin_email else []

        if not recipients:
            self.stdout.write(self.style.WARNING(
                'send_task_reminders: ADMINS не налаштовано в settings.py — '
                'email не надіслано. Додайте: ADMINS = [("Name", "email@example.com")]'
            ))
            # Все одно позначаємо як "оброблено" щоб не спамити в логах
            pending.update(notified_at=timezone.now())
            return

        lines = []
        for task in pending:
            line = f'[{task.get_priority_display()}] {task.title}'
            if task.due_date:
                line += f' — дедлайн: {task.due_date}'
            if task.order:
                line += f' (замовлення: {task.order.order_number})'
            if task.customer:
                line += f' (клієнт: {task.customer.name})'
            lines.append(line)

        body = 'Minerva — нагадування про задачі:\n\n' + '\n'.join(lines)
        body += f'\n\nВсього: {len(lines)} задач\nДата: {today}'

        try:
            send_mail(
                subject=f'Minerva: {len(lines)} задач потребують уваги',
                message=body,
                from_email=from_email,
                recipient_list=recipients,
                fail_silently=False,
            )
            pending.update(notified_at=timezone.now())
            self.stdout.write(self.style.SUCCESS(
                f'send_task_reminders: надіслано {len(lines)} нагадувань → {recipients}'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'send_task_reminders: помилка email — {e}'))
