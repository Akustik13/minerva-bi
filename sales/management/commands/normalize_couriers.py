"""
python manage.py normalize_couriers [--dry-run]

Normalises all existing shipping_courier values in SalesOrder.
Run once after deploying the normalize_courier() fix.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Normalize shipping_courier values in all SalesOrders"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show changes without saving")

    def handle(self, *args, **options):
        from sales.models import SalesOrder
        from sales.utils import normalize_courier

        changed = []
        for order in SalesOrder.objects.exclude(shipping_courier=""):
            normalised = normalize_courier(order.shipping_courier)
            if normalised != order.shipping_courier:
                changed.append((order.pk, order.shipping_courier, normalised))

        if not changed:
            self.stdout.write(self.style.SUCCESS("✅ All couriers already normalised."))
            return

        self.stdout.write(f"Found {len(changed)} orders to update:")
        for pk, old, new in changed:
            self.stdout.write(f"  #{pk}: '{old}' → '{new}'")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("[DRY RUN] Nothing saved."))
            return

        for pk, old, new in changed:
            SalesOrder.objects.filter(pk=pk).update(shipping_courier=new)

        self.stdout.write(self.style.SUCCESS(
            f"✅ Updated {len(changed)} orders."))
