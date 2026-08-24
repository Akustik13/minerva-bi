"""
python manage.py sync_jlc_orders [--force]

Syncs JLCPCB order statuses from the API (when credentials configured).
Also runs auto-matching of unmatched orders against the product catalog.
Can be called manually or via cron.
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
        parser.add_argument('--order-id', type=str,
                            help='Sync a single order by its JLC order ID')

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
                order.product         = product
                order.mapping_status  = JLCOrder.MappingStatus.MATCHED
                order.auto_matched_sku = product.sku
                order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                matched_count += 1
                self.stdout.write(f'  ✅ Matched {order.jlc_order_id} → {product.sku} ({match_type})')

        if matched_count:
            self.stdout.write(self.style.SUCCESS(f'Auto-matched {matched_count} orders'))

        if options['match_only']:
            return

        # ── Check if API sync should run ──────────────────────────────────────
        if not cfg.sync_enabled and not options['force']:
            self.stdout.write('Sync disabled in JLCConfig. Use --force to override.')
            return

        if not cfg.api_key:
            self.stdout.write(self.style.WARNING(
                'JLCPCB API key not configured. Enter credentials in JLCConfig admin.'
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

        # ── Run API sync ──────────────────────────────────────────────────────
        client = JLCAPIClient(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            use_sandbox=cfg.use_sandbox,
        )

        try:
            if options['order_id']:
                raw_orders = [client.get_order(options['order_id'])]
            else:
                raw_orders = client.get_orders()
        except JLCAPIError as e:
            self.stdout.write(self.style.ERROR(f'JLCPCB API error: {e}'))
            return

        updated = 0
        for raw in raw_orders:
            order_id   = raw.get('orderId') or raw.get('id', '')
            jlc_status = raw.get('status', '')
            new_status = map_jlc_status(jlc_status)

            order, created = JLCOrder.objects.get_or_create(
                jlc_order_id=order_id,
                defaults={
                    'jlc_status':   jlc_status,
                    'local_status': new_status,
                    'description':  raw.get('name', ''),
                    'quantity':     raw.get('quantity', 1),
                    'raw_data':     raw,
                },
            )

            if created:
                self.stdout.write(f'  + Created JLCOrder {order_id}')
                # Try auto-match for new order
                product, match_type = find_product_for_jlc_name(order.description)
                if product:
                    order.product        = product
                    order.mapping_status = JLCOrder.MappingStatus.MATCHED
                    order.auto_matched_sku = product.sku
                    order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
            else:
                old_status = order.local_status
                if status_can_advance(old_status, new_status):
                    order.jlc_status   = jlc_status
                    order.local_status = new_status
                    order.raw_data     = raw

                    # Update tracking if available
                    if raw.get('trackingNumber'):
                        order.tracking_number  = raw['trackingNumber']
                        order.tracking_carrier = raw.get('carrier', '')
                    if new_status == 'shipped' and not order.shipped_date:
                        from django.utils.timezone import now
                        order.shipped_date = now().date()
                    if new_status == 'delivered' and not order.delivered_date:
                        from django.utils.timezone import now
                        order.delivered_date = now().date()

                    order.save()
                    updated += 1
                    self.stdout.write(f'  ↑ {order_id}: {old_status} → {new_status}')

                    # Notify
                    if new_status != order.last_notified_status:
                        notify_jlc_status_change(order, old_status, new_status)

                    # Auto-receive
                    if (new_status == 'delivered' and cfg.auto_receive_on_delivered
                            and order.product_id and float(order.received_qty) < order.quantity):
                        receive_into_inventory(order)

        cfg.last_synced_at = timezone.now()
        cfg.save(update_fields=['last_synced_at'])
        self.stdout.write(self.style.SUCCESS(f'Sync complete. Updated: {updated}'))
