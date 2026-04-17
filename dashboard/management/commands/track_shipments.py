"""
dashboard/management/commands/track_shipments.py

Автоматичне оновлення трекінгу відправлень.
Підтримує Jumingo, DHL, DHL Tracking Unified, UPS, FedEx через fallback-ланцюг
правил TrackingRule (налаштовується в адмінці → Налаштування доставки).

Використання:
  python manage.py track_shipments              # пропускає якщо ще не час
  python manage.py track_shipments --force      # ігнорує інтервал
  python manage.py track_shipments --dry-run    # лише перелік, без змін

Cron (кожні 5 хв, команда сама регулює інтервал):
  */5 * * * * docker-compose exec -T web python manage.py track_shipments
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Оновлює трекінг активних відправлень (Jumingo / DHL / UPS / FedEx)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Показати без збереження")
        parser.add_argument("--force", action="store_true",
                            help="Ігнорувати інтервал і запустити примусово")
        parser.add_argument("--notify-always", action="store_true",
                            help="Надіслати сповіщення навіть якщо змін не було")

    def handle(self, *args, **options):
        from shipping.models import Shipment, ShippingSettings
        from shipping.services.tracking_engine import track_with_fallback

        dry_run       = options["dry_run"]
        force         = options["force"]
        notify_always = options["notify_always"]

        cfg = ShippingSettings.get()

        # ── Перевірка чи увімкнено (--force обходить) ────────────────────────
        if not cfg.auto_tracking_enabled and not force:
            self.stdout.write(
                "⏸ Автоматичний трекінг вимкнено. "
                "Використайте --force для ручного запуску."
            )
            return

        # ── Антиспам-інтервал ─────────────────────────────────────────────────
        if not force and cfg.last_tracking_run:
            elapsed = (timezone.now() - cfg.last_tracking_run).total_seconds() / 60
            if elapsed < cfg.tracking_interval_minutes:
                remaining = cfg.tracking_interval_minutes - elapsed
                self.stdout.write(
                    f"⏳ Ще зарано: наступне оновлення через {remaining:.0f} хв "
                    f"(інтервал: {cfg.tracking_interval_minutes} хв)"
                )
                return

        # ── Вибірка активних відправлень ──────────────────────────────────────
        active_statuses = [
            Shipment.Status.SUBMITTED,
            Shipment.Status.LABEL_READY,
            Shipment.Status.IN_TRANSIT,
        ]
        qs = (
            Shipment.objects
            .filter(status__in=active_statuses)
            .select_related("carrier", "order")
        )

        total = qs.count()
        self.stdout.write(
            f"[track_shipments] {total} активних відправлень"
            + (" [dry-run]" if dry_run else "")
        )

        updated = 0
        errors  = 0
        changes = []

        def _track_one(shipment):
            nonlocal updated, errors

            carrier_label = (
                f"{shipment.carrier.carrier_type}({shipment.carrier.name})"
                if shipment.carrier else "?"
            )
            label = f"#{shipment.pk} {carrier_label}"
            old_status = shipment.status

            try:
                changed, log_entries = track_with_fallback(shipment, dry_run=dry_run)

                # Друкуємо результат кожної спроби в dry-run
                if dry_run:
                    for entry in log_entries:
                        if entry.get("error"):
                            self.stdout.write(
                                f"  {label} [{entry['tracker']}] ❌ {entry['error']}"
                            )
                        else:
                            self.stdout.write(
                                f"  {label} [{entry['tracker']}] "
                                f"→ {entry.get('normalized_class', entry.get('status', '—'))}"
                            )
                    return

                # Визначаємо чи були реальні зміни
                successful = [e for e in log_entries if not e.get("error")]
                if not successful:
                    err_msg = "; ".join(
                        f"{e['tracker']}: {e['error']}" for e in log_entries
                    )
                    errors += 1
                    self.stdout.write(
                        self.style.ERROR(f"  ❌ {label} — {err_msg}")
                    )
                    return

                if changed:
                    updated += 1
                    tracker_used = successful[0]["tracker"]
                    changes.append({
                        "order":      shipment.order.order_number if shipment.order else f"#{shipment.pk}",
                        "client":     (shipment.order.client or shipment.order.email or "—") if shipment.order else "—",
                        "old_status": old_status,
                        "new_status": shipment.status,
                        "tracking":   shipment.tracking_number or "",
                    })
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✅ {label} [{tracker_used}]"
                        f" → {shipment.get_status_display()}"
                        + (f" TN:{shipment.tracking_number}" if shipment.tracking_number else "")
                    ))
                else:
                    tracker_used = successful[0]["tracker"]
                    self.stdout.write(f"  {label} [{tracker_used}] — без змін")

            except Exception as exc:
                errors += 1
                logger.exception("track_shipments error %s: %s", label, exc)
                self.stdout.write(self.style.ERROR(f"  ❌ {label} — {exc}"))

        # ── Головний цикл ─────────────────────────────────────────────────────
        for shipment in qs:
            _track_one(shipment)

        if dry_run:
            return

        # ── Перевірка прострочених ETA (незалежно від API) ────────────────────
        self._check_overdue_eta(changes)

        # ── Оновлюємо час останнього запуску ─────────────────────────────────
        ShippingSettings.objects.filter(pk=1).update(last_tracking_run=timezone.now())
        self.stdout.write(self.style.SUCCESS(
            f"[track_shipments] Готово: оновлено {updated}/{total}, помилок {errors}"
        ))

        if updated or (notify_always and errors):
            try:
                from dashboard.notifications import notify_sync_result
                notify_sync_result("Авто-трекінг відправлень", {
                    "created": 0,
                    "updated": updated,
                    "errors":  errors,
                    "changes": changes,
                }, force_notify=notify_always)
            except Exception:
                pass

    def _check_overdue_eta(self, changes: list):
        """
        Позначає посилки як затримані якщо eta_to < сьогодні і статус IN_TRANSIT.
        """
        from shipping.models import Shipment
        from dashboard.notifications import _send_telegram, _get_ns

        today = timezone.now().date()
        overdue_qs = Shipment.objects.filter(
            status=Shipment.Status.IN_TRANSIT,
            carrier_delayed=False,
            eta_to__lt=today,
            eta_to__isnull=False,
        ).select_related("carrier", "order")

        if not overdue_qs.exists():
            return

        ns = _get_ns()
        tg_enabled = (
            ns and ns.telegram_enabled
            and ns.telegram_bot_token and ns.telegram_chat_id
        )

        for shipment in overdue_qs:
            shipment.carrier_delayed = True
            shipment.save(update_fields=["carrier_delayed"])

            order_num = shipment.order.order_number if shipment.order else f"#{shipment.pk}"
            client    = (shipment.order.client or "") if shipment.order else ""
            tn        = shipment.tracking_number or shipment.carrier_shipment_id or "—"
            days_late = (today - shipment.eta_to).days

            self.stdout.write(self.style.WARNING(
                f"  ⚠️ Затримка #{shipment.pk} {order_num} — ETA {shipment.eta_to} (+{days_late} дн.)"
            ))

            changes.append({
                "order":      order_num,
                "client":     client,
                "old_status": "in_transit",
                "new_status": "in_transit",
                "extra":      f"⚠️ Затримка +{days_late} дн.",
            })

            if tg_enabled:
                try:
                    eta_str = shipment.eta_to.strftime("%d.%m.%Y")
                    msg = (
                        f"🚨 <b>Посилка затримується</b>\n"
                        f"Замовлення: <b>{order_num}</b>"
                        + (f" | {client}" if client else "") +
                        f"\nТрекінг: <code>{tn}</code>\n"
                        f"Очікувалось до: <b>{eta_str}</b> (+{days_late} дн.)\n"
                        f"Перевір статус у перевізника"
                    )
                    _send_telegram(ns, msg)
                except Exception:
                    pass
