"""
python manage.py sync_jlc_orders [--force] [--match-only] [--batch-num W202501...]
                                  [--days N] [--skip-dhl]

Syncs JLCPCB PCB order statuses via the Open API, then checks DHL
tracking for shipped orders to detect real delivery.

Discovery:  POST /overseas/openapi/pcb/pageBatchInfoByOrderType
Status:     POST /overseas/openapi/pcb/order/detail
            Reads data.orderItem[0].pcbItem.orderStatus (integer 0-5).
DHL phase:  GET  api-eu.dhl.com/track/shipments
            For shipped JLC orders with tracking_number → detect 'delivered'.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Sync JLCPCB order statuses, auto-match products, and check DHL delivery'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Run even if sync interval has not elapsed')
        parser.add_argument('--match-only', action='store_true',
                            help='Only run product matching, skip API sync')
        parser.add_argument('--batch-num', type=str,
                            help='Sync a single order by JLCPCB batch number (e.g. W2025040800001)')
        parser.add_argument('--days', type=int, default=None,
                            help='How many days back to look for new orders (default: from JLCConfig.sync_days_back)')
        parser.add_argument('--skip-dhl', action='store_true',
                            help='Skip DHL tracking check phase')

    def handle(self, *args, **options):
        from datetime import timedelta, datetime
        from jlcpcb.models import JLCConfig, JLCOrder
        from jlcpcb.services.api import (
            JLCAPIClient, JLCAPIError,
            find_product_for_jlc_name, map_jlc_status, status_can_advance,
            receive_into_inventory, extract_pcb_item,
        )
        from jlcpcb.notifications import notify_jlc_status_change

        cfg = JLCConfig.get()

        # ── Phase 0: Auto-match unmatched orders ──────────────────────────────
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
            self._check_dhl(cfg, options)
            return

        # ── Check if JLCPCB API sync should run ───────────────────────────────
        if not cfg.sync_enabled and not options['force']:
            self.stdout.write('Sync disabled in JLCConfig. Use --force to override.')
            self._check_dhl(cfg, options)
            return

        if not cfg.access_key:
            self.stdout.write(self.style.WARNING(
                'JLCPCB Access Key not configured. Enter credentials in JLCConfig admin.'
            ))
            self._check_dhl(cfg, options)
            return

        if not options['force'] and cfg.last_synced_at:
            elapsed_hours = (timezone.now() - cfg.last_synced_at).total_seconds() / 3600
            if elapsed_hours < cfg.sync_interval_hours:
                self.stdout.write(
                    f'JLCPCB sync interval not elapsed '
                    f'({elapsed_hours:.1f}h / {cfg.sync_interval_hours}h). '
                    'Use --force to skip.'
                )
                self._check_dhl(cfg, options)
                return

        client = JLCAPIClient.from_config()

        # ── Determine which batch numbers to sync ─────────────────────────────
        if options['batch_num']:
            batch_nums_to_sync = [options['batch_num']]
            self.stdout.write(f'Syncing single batch: {options["batch_num"]}')
        else:
            days    = options['days'] or cfg.sync_days_back or 30
            fmt     = '%Y-%m-%d %H:%M:%S'
            date_to = timezone.now()

            # Discovery window: from last_synced_at (incremental) or full days (first/manual)
            if options['days'] or not cfg.last_synced_at:
                # Explicit --days or first-ever sync → full window
                date_from   = date_to - timedelta(days=days)
                window_desc = f'last {days} days'
            else:
                # Incremental: look back only to previous sync (+ 1h overlap)
                date_from   = cfg.last_synced_at - timedelta(hours=1)
                window_desc = f'since last sync ({date_from.strftime("%d.%m %H:%M")})'

            self.stdout.write(
                f'Fetching PCB batch list {window_desc} '
                f'({date_from.strftime(fmt)} → {date_to.strftime(fmt)})...'
            )
            try:
                batch_nums_to_sync = client.get_all_pcb_batch_numbers(
                    date_from.strftime(fmt), date_to.strftime(fmt)
                )
            except JLCAPIError as e:
                self.stdout.write(self.style.ERROR(f'Failed to fetch batch list: {e}'))
                batch_nums_to_sync = []

            # Always include existing active orders (status refresh regardless of age)
            existing_active = list(
                JLCOrder.objects.exclude(
                    local_status__in=[JLCOrder.LocalStatus.DELIVERED, JLCOrder.LocalStatus.CANCELLED]
                ).values_list('jlc_order_number', flat=True)
            )
            for b in existing_active:
                if b and b not in batch_nums_to_sync:
                    batch_nums_to_sync.append(b)

            self.stdout.write(f'Found {len(batch_nums_to_sync)} batch(es) to process.')

        if not batch_nums_to_sync:
            self.stdout.write('No orders to sync.')
            cfg.last_synced_at = timezone.now()
            cfg.save(update_fields=['last_synced_at'])
            self._check_dhl(cfg, options)
            return

        # ── Phase 1: Sync each JLCPCB batch ──────────────────────────────────
        created = 0
        updated = 0
        errors  = 0

        for batch in batch_nums_to_sync:
            if not batch:
                continue
            try:
                raw = client.get_pcb_order(batch)
            except JLCAPIError as e:
                self.stdout.write(self.style.ERROR(f'  Error fetching {batch}: {e}'))
                errors += 1
                continue

            pcb = extract_pcb_item(raw)
            if not pcb:
                self.stdout.write(f'  {batch}: no pcbItem in response, skip')
                continue

            status_int      = pcb.get('orderStatus')
            new_status      = map_jlc_status(status_int)
            file_name       = pcb.get('fileName', '')
            quantity        = pcb.get('count', 1) or 1
            delivery_time   = pcb.get('deliveryTime')
            price           = pcb.get('price')
            shipping_method = raw.get('shippingMethod', '')

            # Parse order_date from pcbItem.orderDate (actual JLCPCB order time)
            order_date = None
            raw_order_date = pcb.get('orderDate')
            if raw_order_date:
                try:
                    order_date = datetime.strptime(
                        raw_order_date[:10], '%Y-%m-%d'
                    ).date()
                except (ValueError, TypeError):
                    pass

            # Parse expected_date from deliveryTime field
            expected_date = None
            if delivery_time:
                try:
                    expected_date = datetime.strptime(
                        delivery_time[:10], '%Y-%m-%d'
                    ).date()
                except (ValueError, TypeError):
                    pass

            order, is_created = JLCOrder.objects.get_or_create(
                jlc_order_number=batch,
                defaults={
                    'jlc_order_id':    batch,
                    'local_status':    new_status,
                    'jlc_status':      str(status_int) if status_int is not None else '',
                    'description':     file_name,
                    'quantity':        quantity,
                    'total_price':     price,
                    'order_date':      order_date or timezone.now().date(),
                    'expected_date':   expected_date,
                    'tracking_carrier': shipping_method,
                    'raw_data':        raw,
                },
            )

            if is_created:
                created += 1
                eta = f' ETA {expected_date}' if expected_date else ''
                self.stdout.write(f'  + {batch} ({file_name[:40]}) — {new_status}{eta}')
                if file_name:
                    product, match_type = find_product_for_jlc_name(file_name)
                    if product:
                        order.product          = product
                        order.mapping_status   = JLCOrder.MappingStatus.MATCHED
                        order.auto_matched_sku = product.sku
                        order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                        self.stdout.write(f'    → Matched {product.sku} ({match_type})')
                # Notify for newly discovered orders whose status was not yet sent
                if new_status not in ('cancelled',) and not order.last_notified_status:
                    notify_jlc_status_change(order, '', new_status)
            else:
                old_status = order.local_status

                # Manually-delivered orders are final — never let API roll them back.
                # Only update raw API data for audit, skip all status logic.
                if old_status == JLCOrder.LocalStatus.DELIVERED:
                    order.raw_data   = raw
                    order.jlc_status = str(status_int) if status_int is not None else ''
                    order.save(update_fields=['raw_data', 'jlc_status', 'updated_at'])
                    self.stdout.write(f'  {batch}: delivered (locked) — API says {new_status}, skip')
                    continue

                # Update fields if now available / fix wrong defaults
                update_fields = ['raw_data', 'jlc_status', 'description', 'updated_at']
                if order_date and order.order_date != order_date:
                    order.order_date = order_date
                    update_fields.append('order_date')
                if expected_date and not order.expected_date:
                    order.expected_date = expected_date
                    update_fields.append('expected_date')
                if shipping_method and not order.tracking_carrier:
                    order.tracking_carrier = shipping_method
                    update_fields.append('tracking_carrier')

                # Update description if blank and try auto-match
                if not order.description and file_name:
                    order.description = file_name
                    if order.mapping_status == JLCOrder.MappingStatus.UNMATCHED:
                        product, match_type = find_product_for_jlc_name(file_name)
                        if product:
                            order.product          = product
                            order.mapping_status   = JLCOrder.MappingStatus.MATCHED
                            order.auto_matched_sku = product.sku
                            update_fields += ['product', 'mapping_status', 'auto_matched_sku']
                            self.stdout.write(f'    → Matched {product.sku} ({match_type})')

                order.raw_data   = raw
                order.jlc_status = str(status_int) if status_int is not None else ''

                if status_can_advance(old_status, new_status):
                    # Status advanced → save + notify
                    order.local_status = new_status
                    if new_status == 'shipped' and not order.shipped_date:
                        order.shipped_date = timezone.now().date()
                    order.save()
                    updated += 1
                    eta = f' (ETA {expected_date})' if expected_date else ''
                    self.stdout.write(f'  {batch}: {old_status} → {new_status}{eta}')
                    notify_jlc_status_change(order, old_status, new_status)

                    if (new_status == 'delivered'
                            and cfg.auto_receive_on_delivered
                            and order.product_id
                            and float(order.received_qty) < order.quantity):
                        receive_into_inventory(order)
                else:
                    order.save(update_fields=update_fields)
                    # Status unchanged but never notified → send now
                    if (new_status not in ('cancelled',)
                            and order.last_notified_status != new_status):
                        self.stdout.write(f'  {batch}: sending missed notification ({new_status})')
                        notify_jlc_status_change(order, new_status, new_status)
                    else:
                        self.stdout.write(f'  {batch}: no change ({old_status})')

        cfg.last_synced_at = timezone.now()
        cfg.save(update_fields=['last_synced_at'])
        self.stdout.write(self.style.SUCCESS(
            f'JLCPCB sync complete. New: {created}, Updated: {updated}, Errors: {errors}'
        ))

        # ── Phase 2: DHL tracking for shipped orders ──────────────────────────
        self._check_dhl(cfg, options)

    # ─────────────────────────────────────────────────────────────────────────

    def _check_dhl(self, cfg, options):
        """
        Phase 2a — DHL tracking: for shipped orders with tracking_number, check DHL API.
        Phase 2b — Overdue detection: shipped orders where expected_date < today-3d
                   and no tracking → auto-mark delivered.
        """
        if options.get('skip_dhl'):
            return

        from datetime import timedelta
        from jlcpcb.models import JLCOrder
        from jlcpcb.services.api import receive_into_inventory
        from jlcpcb.notifications import notify_jlc_status_change

        def _do_deliver(order, source: str):
            old = order.local_status
            order.local_status   = JLCOrder.LocalStatus.DELIVERED
            order.delivered_date = timezone.now().date()
            order.save(update_fields=['local_status', 'delivered_date', 'updated_at'])
            self.stdout.write(self.style.SUCCESS(
                f'    ✅ {order.jlc_order_id}: shipped → delivered ({source})'
            ))
            notify_jlc_status_change(order, old, 'delivered')
            if (cfg.auto_receive_on_delivered and order.product_id
                    and float(order.received_qty) < order.quantity):
                tx = receive_into_inventory(order)
                if tx:
                    self.stdout.write(f'    📦 Auto-received {tx.qty} pcs → inventory')

        shipped = JLCOrder.objects.filter(local_status=JLCOrder.LocalStatus.SHIPPED)

        # ── 2a: DHL tracking ──────────────────────────────────────────────────
        dhl_api_key = self._get_dhl_api_key()
        shipped_dhl = shipped.exclude(tracking_number='').exclude(tracking_number__isnull=True)

        if dhl_api_key and shipped_dhl.exists():
            self.stdout.write(f'  DHL Tracking: checking {shipped_dhl.count()} order(s)...')
            from shipping.services.dhl_track import track as dhl_track
            for order in shipped_dhl:
                result = dhl_track(order.tracking_number, dhl_api_key)
                if result.get('error'):
                    self.stdout.write(f'    {order.jlc_order_id}: {result["error"]}')
                    continue
                dhl_status = result.get('status_code', '')
                self.stdout.write(
                    f'    {order.jlc_order_id} [{order.tracking_number}]: '
                    f'{result.get("status_icon","")} {result.get("status_label", dhl_status)}'
                )
                if dhl_status == 'delivered':
                    _do_deliver(order, 'DHL confirmed')
        elif not dhl_api_key:
            self.stdout.write('  DHL Tracking: no api key — skip (set track_api_key on DHL Carrier)')

        # ── 2b: Overdue detection (no tracking, expected_date + 3 days < today) ─
        cutoff = timezone.now().date() - timedelta(days=3)
        overdue = shipped.filter(
            expected_date__isnull=False,
            expected_date__lt=cutoff,
        ).filter(
            tracking_number__isnull=True
        ) | shipped.filter(
            expected_date__isnull=False,
            expected_date__lt=cutoff,
            tracking_number='',
        )
        overdue = overdue.distinct()
        if overdue.exists():
            self.stdout.write(f'  Overdue (no tracking): {overdue.count()} order(s) past ETA+3d')
            for order in overdue:
                self.stdout.write(f'    {order.jlc_order_id}: ETA was {order.expected_date} — marking delivered')
                _do_deliver(order, f'ETA {order.expected_date} passed')

    def _get_dhl_api_key(self) -> str:
        """Get DHL Unified Tracking API key from Carrier config."""
        try:
            from shipping.models import Carrier
            carrier = Carrier.objects.filter(
                carrier_type='dhl',
                is_active=True,
            ).exclude(track_api_key='').exclude(track_api_key__isnull=True).first()
            return carrier.track_api_key if carrier else ''
        except Exception:
            return ''
