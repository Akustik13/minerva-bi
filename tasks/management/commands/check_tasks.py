from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Sum


class Command(BaseCommand):
    help = 'Перевіряє критичний склад і прострочені дедлайни → створює Tasks'

    def handle(self, *args, **options):
        self._check_stock()
        self._check_deadlines()
        self.stdout.write(self.style.SUCCESS('check_tasks: готово'))

    def _check_stock(self):
        from inventory.models import Product, InventoryTransaction
        from tasks.models import Task

        created = 0
        products = Product.objects.filter(reorder_point__gt=0)
        for product in products:
            stock_result = InventoryTransaction.objects.filter(
                product=product
            ).aggregate(total=Sum('qty'))
            stock = float(stock_result['total'] or 0)

            if stock < product.reorder_point:
                _, was_created = Task.objects.get_or_create(
                    product=product,
                    task_type=Task.TaskType.STOCK_ALERT,
                    status=Task.Status.PENDING,
                    defaults=dict(
                        title=f'📦 Критичний залишок: {product.sku}',
                        description=(
                            f'Товар: {product.name}\n'
                            f'SKU: {product.sku}\n'
                            f'Залишок: {stock:.0f} | Мінімум: {product.reorder_point}'
                        ),
                        priority=Task.Priority.HIGH,
                        notify_email=True,
                    ),
                )
                if was_created:
                    created += 1
            else:
                # Склад поповнено — закрити відкриту задачу
                Task.objects.filter(
                    product=product,
                    task_type=Task.TaskType.STOCK_ALERT,
                    status=Task.Status.PENDING,
                ).update(status=Task.Status.DONE)

        self.stdout.write(f'  Склад: нових задач {created}')

    def _check_deadlines(self):
        from sales.models import SalesOrder
        from tasks.models import Task

        today = timezone.now().date()
        overdue = SalesOrder.objects.filter(
            affects_stock=True,
            shipping_deadline__lt=today,
            shipped_at__isnull=True,
        )

        created = 0
        for order in overdue:
            _, was_created = Task.objects.get_or_create(
                order=order,
                task_type=Task.TaskType.DEADLINE_ALERT,
                status=Task.Status.PENDING,
                defaults=dict(
                    title=f'⚠️ Прострочено: {order.order_number}',
                    description=(
                        f'Дедлайн: {order.shipping_deadline}\n'
                        f'Клієнт: {order.client or order.customer_key}'
                    ),
                    due_date=order.shipping_deadline,
                    priority=Task.Priority.HIGH,
                    notify_email=True,
                ),
            )
            if was_created:
                created += 1

        self.stdout.write(f'  Дедлайни: нових задач {created} (всього прострочених: {overdue.count()})')
