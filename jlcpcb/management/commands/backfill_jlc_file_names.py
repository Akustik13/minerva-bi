"""
python manage.py backfill_jlc_file_names

One-time backfill: populate file_names from raw_data for all existing JLCOrders.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Backfill file_names field from raw_data for all existing JLC orders'

    def handle(self, *args, **options):
        from jlcpcb.models import JLCOrder
        updated = 0
        total = JLCOrder.objects.count()
        self.stdout.write(f'Processing {total} orders...')
        for order in JLCOrder.objects.all():
            names = '\n'.join(
                item.get('pcbItem', {}).get('fileName', '')
                for item in order.raw_data.get('orderItem', [])
                if item.get('pcbItem', {}).get('fileName')
            )
            if names != order.file_names:
                order.file_names = names
                order.save(update_fields=['file_names'])
                updated += 1
        self.stdout.write(self.style.SUCCESS(f'Done. Updated {updated} of {total} orders.'))
