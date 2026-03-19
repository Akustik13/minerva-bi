"""
python manage.py check_revenue_data

Shows a summary of where revenue data IS and IS NOT stored per year,
to help diagnose why trends page shows €0 for some years.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Diagnose revenue data availability per year"

    def handle(self, *args, **options):
        from sales.models import SalesOrder, SalesOrderLine
        from django.db.models import Sum, Count, Q

        self.stdout.write("\n📊 Revenue data diagnosis per year\n")
        self.stdout.write("=" * 60)

        for year in range(2023, 2027):
            qs = SalesOrder.objects.filter(affects_stock=True, order_date__year=year)
            total_orders = qs.count()
            if total_orders == 0:
                continue

            orders_with_total = qs.exclude(total_price__isnull=True).count()
            order_total_sum   = qs.aggregate(s=Sum('total_price'))['s'] or 0

            lines = SalesOrderLine.objects.filter(order__in=qs)
            total_lines          = lines.count()
            lines_with_tp        = lines.exclude(total_price__isnull=True).count()
            lines_with_up        = lines.exclude(unit_price__isnull=True).count()
            line_tp_sum          = lines.aggregate(s=Sum('total_price'))['s'] or 0
            line_up_sum          = lines.filter(unit_price__isnull=False).aggregate(
                                       s=Sum('unit_price'))['s'] or 0

            self.stdout.write(f"\n  {year}:")
            self.stdout.write(f"    Orders: {total_orders} total, {orders_with_total} with total_price, sum={order_total_sum}")
            self.stdout.write(f"    Lines:  {total_lines} total, {lines_with_tp} with total_price (sum={line_tp_sum}), {lines_with_up} with unit_price")

            if line_tp_sum > 0:
                self.stdout.write(self.style.SUCCESS(f"    → Revenue via SalesOrderLine.total_price: €{line_tp_sum}"))
            elif order_total_sum > 0:
                self.stdout.write(self.style.WARNING(f"    → Revenue only in SalesOrder.total_price: €{order_total_sum}"))
            else:
                self.stdout.write(self.style.ERROR(f"    → NO revenue data in DB for {year}"))

        self.stdout.write("\n")
