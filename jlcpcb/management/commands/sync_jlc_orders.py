"""
python manage.py sync_jlc_orders [--force] [--match-only] [--batch-num W202501...]

Refreshes JLCPCB order statuses from the API.
Because JLCPCB has no "list all orders" endpoint for PCB orders, this command
iterates over existing JLCOrder records and refreshes each one by batch number.
New orders must be added manually (or via the admin) with their batch number.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Sync JLCPCB order statuses and run product auto-matching'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Run even if sync interval has not elapsed')
        parser.add_argument('--match-only', action='store_true',
                            help='Only run product matching, skip API sync')
        parser.add_argument('--batch-num', type=str,
                            help='Sync a single order by its JLCPCB batch number (e.g. W2025040800001)')

    def handle(self, *args, **options):
        from jlcpcb.models import JLCConfig, JLCOrder
        from jlcpcb.services.api import (
            JLCAPIClient, JLCAPIError,
            find_product_for_jlc_name, map_jlc_status, status_can_advance,
            receive_into_inventory,
        )
        from jlcpcb.notifications import notify_jlc_status_change

        cfg = JLCConfig.get()

        # ── Auto-match unmatched orders ───────────────────────────────────────
        unmatched = JLCOrder.objects.filter(mapping_status=JLCOrder.MappingStatus.UNMATCHED)
        matched_count = 0
        for order in unmatched:
            product, match_type = find_product_for_jlc_name(order.description or order.jlc_order_id)
            if product:
                order.product          = product
                order.mapping_status   = JLCOrder.MappingStatus.MATCHED
                order.auto_matched_sku = product.sku
                order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                matched_count += 1
                self.stdout.write(f'  Matched {order.jlc_order_id} → {product.sku} ({match_type})')

        if matched_count:
            self.stdout.write(self.style.SUCCESS(f'Auto-matched {matched_count} orders'))

        if options['match_only']:
            return

        # ── Check if API sync should run ──────────────────────────────────────
        if not cfg.sync_enabled and not options['force']:
            self.stdout.write('Sync disabled in JLCConfig. Use --force to override.')
            return

        if not cfg.access_key:
            self.stdout.write(self.style.WARNING(
                'JLCPCB Access Key not configured. Enter credentials in JLCConfig admin.'
            ))
            return

        if not options['force'] and cfg.last_synced_at:
            elapsed_hours = (timezone.now() - cfg.last_synced_at).total_seconds() / 3600
            if elapsed_hours < cfg.sync_interval_hours:
                self.stdout.write(
                    f'Sync interval not elapsed ({elapsed_hours:.1f}h / {cfg.sync_interval_hours}h). '
                    'Use --force to skip.'
                )
                return

        # ── Determine which orders to sync ────────────────────────────────────
        client = JLCAPIClient.from_config()

        if options['batch_num']:
            batch_num = options['batch_num']
            orders_qs = JLCOrder.objects.filter(
                jlc_order_number=batch_num
            ) or JLCOrder.objects.filter(jlc_order_id=batch_num)
            if not orders_qs.exists():
                # Create a new order record for this batch number
                order = JLCOrder.objects.create(
                    jlc_order_id=batch_num,
                    jlc_order_number=batch_num,
                )
                orders_to_sync = [order]
                self.stdout.write(f'  + Created JLCOrder for batch {batch_num}')
            else:
                orders_to_sync = list(orders_qs)
        else:
            # Sync all active orders (not delivered/cancelled)
            orders_to_sync = list(
                JLCOrder.objects.exclude(
                    local_status__in=[JLCOrder.LocalStatus.DELIVERED, JLCOrder.LocalStatus.CANCELLED]
                )
            )

        if not orders_to_sync:
            self.stdout.write('No active orders to sync. Add orders via admin or --batch-num.')
            cfg.last_synced_at = timezone.now()
            cfg.save(update_fields=['last_synced_at'])
            return

        # ── Sync each order by batch number ───────────────────────────────────
        updated = 0
        errors  = 0

        for order in orders_to_sync:
            batch = order.jlc_order_number or order.jlc_order_id
            if not batch:
                self.stdout.write(self.style.WARNING(
                    f'  Skipping order pk={order.pk}: no batch number set'
                ))
                continue

            try:
                raw = client.get_pcb_order(batch)
            except JLCAPIError as e:
                self.stdout.write(self.style.ERROR(f'  Error fetching {batch}: {e}'))
                errors += 1
                continue

            jlc_status = (
                raw.get('status') or raw.get('orderStatus') or raw.get('pcbStatus', '')
            )
            new_status = map_jlc_status(jlc_status)
            old_status = order.local_status

            # Update description / gerber name if blank
            if not order.description:
                order.description = raw.get('name') or raw.get('gerberName') or ''

            # Auto-match product if unmatched and description is now set
            if (order.mapping_status == JLCOrder.MappingStatus.UNMATCHED
                    and order.description):
                product, match_type = find_product_for_jlc_name(order.description)
                if product:
                    order.product          = product
                    order.mapping_status   = JLCOrder.MappingStatus.MATCHED
                    order.auto_matched_sku = product.sku
                    self.stdout.write(f'  Matched {batch} → {product.sku} ({match_type})')

            if status_can_advance(old_status, new_status):
                order.jlc_status   = jlc_status
                order.local_status = new_status
                order.raw_data     = raw

                tracking = raw.get('trackingNumber') or raw.get('expressNo', '')
                if tracking:
                    order.tracking_number  = tracking
                    order.tracking_carrier = raw.get('carrier') or raw.get('expressCompany', '')

                if new_status == 'shipped' and not order.shipped_date:
                    order.shipped_date = timezone.now().date()
                if new_status == 'delivered' and not order.delivered_date:
                    order.delivered_date = timezone.now().date()

                order.save()
                updated += 1
                self.stdout.write(f'  {batch}: {old_status} → {new_status}')

                notify_jlc_status_change(order, old_status, new_status)

                if (new_status == 'delivered'
                        and cfg.auto_receive_on_delivered
                        and order.product_id
                        and float(order.received_qty) < order.quantity):
                    receive_into_inventory(order)
            else:
                order.raw_data = raw
                order.save(update_fields=['raw_data', 'updated_at'])
                self.stdout.write(f'  {batch}: no change ({old_status})')

        cfg.last_synced_at = timezone.now()
        cfg.save(update_fields=['last_synced_at'])
        self.stdout.write(self.style.SUCCESS(
            f'Sync complete. Updated: {updated}, Errors: {errors}'
        ))
