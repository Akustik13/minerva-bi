from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Перевіряє критичний склад і прострочені дедлайни → створює Tasks'

    def handle(self, *args, **options):
        self._check_stock()
        self._check_deadlines()
        self.stdout.write(self.style.SUCCESS('check_tasks: готово'))

    def _check_stock(self):
        """Критичний склад — як «⚠️ Критичні» в огляді складу: «Доступно» (на складі − резерв і ще не списані
        замовлення, inventory/services/stock.annotate_stock) не більше «Точки дозамовлення»."""
        from inventory.models import Product
        from inventory.services.stock import annotate_stock
        from tasks.models import Task

        created = 0
        products = annotate_stock(Product.objects.filter(reorder_point__gt=0))
        for product in products:
            stock = float(product._available)

            if stock <= product.reorder_point:
                _, was_created = Task.objects.get_or_create(
                    product=product,
                    task_type=Task.TaskType.STOCK_ALERT,
                    status=Task.Status.PENDING,
                    defaults=dict(
                        title=f'📦 Критичний залишок: {product.sku}',
                        description=(
                            f'Товар: {product.name}\n'
                            f'SKU: {product.sku}\n'
                            f'Доступно: {stock:.0f} | Точка дозамовлення: {product.reorder_point}'
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
        from sales.revenue import to_ship
        from tasks.models import Task

        today = timezone.now().date()
        # лише ті, що ще чекають відправки (без доставлених / відправлених / скасованих / Angebote)
        overdue = to_ship().filter(shipping_deadline__lt=today)
        # нагадування про замовлення, яке вже відправили, доставили чи скасували, — виконано само
        closed = (Task.objects.filter(task_type=Task.TaskType.DEADLINE_ALERT,
                                      status__in=[Task.Status.PENDING, Task.Status.IN_PROGRESS], order__isnull=False)
                  .exclude(order__in=to_ship()).update(status=Task.Status.DONE))
        if closed:
            self.stdout.write(f'  Дедлайни: закрито нагадувань про вже відправлені / закриті замовлення: {closed}')

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
