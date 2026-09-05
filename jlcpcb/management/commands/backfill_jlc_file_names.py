"""
python manage.py backfill_jlc_file_names

Backfills file_names field AND creates JLCOrderLine records
from existing raw_data for all JLCOrders (no API calls needed).
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Backfill file_names + create JLCOrderLine from raw_data for existing orders'

    def handle(self, *args, **options):
        from jlcpcb.models import JLCOrder
        from jlcpcb.services.api import find_product_for_jlc_name, map_jlc_status
        from jlcpcb.management.commands.sync_jlc_orders import _sync_order_lines

        total    = JLCOrder.objects.count()
        updated  = 0
        lines_created = 0

        self.stdout.write(f'Processing {total} orders...')

        for order in JLCOrder.objects.all():
            # Backfill file_names
            names = '\n'.join(
                item.get('pcbItem', {}).get('fileName', '')
                for item in order.raw_data.get('orderItem', [])
                if item.get('pcbItem', {}).get('fileName')
            )
            if names != order.file_names:
                order.file_names = names
                order.save(update_fields=['file_names'])
                updated += 1

            # Backfill JLCOrderLine
            before = order.lines.count()
            _sync_order_lines(order, order.raw_data, find_product_for_jlc_name, map_jlc_status)
            lines_created += order.lines.count() - before

        self.stdout.write(self.style.SUCCESS(
            f'Done. file_names updated: {updated}. Lines created: {lines_created}.'
        ))
