"""
dashboard/management/commands/track_shipments.py

Автоматичне оновлення трекінгу відправлень (Jumingo + DHL).
Поважає інтервал з ShippingSettings — безпечно запускати через cron кожні 5 хв.

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
    help = "Оновлює трекінг активних відправлень через Jumingo та DHL API"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Показати без збереження")
        parser.add_argument("--force", action="store_true",
                            help="Ігнорувати інтервал і запустити примусово")
        parser.add_argument("--notify-always", action="store_true",
                            help="Надіслати сповіщення навіть якщо змін не було")

    def handle(self, *args, **options):
        from shipping.models import Shipment, ShippingSettings
        from shipping.services.registry import get_service
        from shipping.admin import _apply_tracking_update

        dry_run       = options["dry_run"]
        force         = options["force"]
        notify_always = options["notify_always"]

        cfg = ShippingSettings.get()

        # ── Перевірка чи увімкнено (--force обходить) ────────────────────────
        if not cfg.auto_tracking_enabled and not force:
            self.stdout.write("⏸ Автоматичний трекінг вимкнено. Використайте --force для ручного запуску.")
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
        qs = Shipment.objects.filter(
            status__in=active_statuses,
        ).select_related("carrier", "order")

        # Розбиваємо по типу перевізника
        jumingo_qs = qs.filter(carrier__carrier_type="jumingo",
                               carrier_shipment_id__gt="")
        dhl_qs     = qs.filter(carrier__carrier_type="dhl",
                               tracking_number__gt="")

        total = jumingo_qs.count() + dhl_qs.count()
        self.stdout.write(
            f"[track_shipments] {total} активних відправлень "
            f"(Jumingo: {jumingo_qs.count()}, DHL: {dhl_qs.count()})"
            + (" [dry-run]" if dry_run else "")
        )

        updated = 0
        errors  = 0
        changes = []   # деталі для нотифікації

        def _track_one(shipment, get_data_fn, label):
            nonlocal updated, errors
            old_status = shipment.status
            try:
                data = get_data_fn()
                if not data:
                    self.stdout.write(f"  {label} — немає відповіді")
                    return
                if dry_run:
                    self.stdout.write(f"  {label}: {data.get('status','—')}")
                    return
                if _apply_tracking_update(shipment, data):
                    updated += 1
                    changes.append({
                        "order":      shipment.order.order_number if shipment.order else f"#{shipment.pk}",
                        "client":     (shipment.order.client or shipment.order.email or "—") if shipment.order else "—",
                        "old_status": old_status,
                        "new_status": shipment.status,
                        "tracking":   shipment.tracking_number or "",
                    })
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✅ {label} → {shipment.get_status_display()}"
                        + (f" TN:{shipment.tracking_number}" if shipment.tracking_number else "")
                    ))
                else:
                    self.stdout.write(f"  {label} — без змін")
            except Exception as exc:
                errors += 1
                logger.exception("track_shipments error %s: %s", label, exc)
                self.stdout.write(self.style.ERROR(f"  ❌ {label} — {exc}"))

        # ── Jumingo ───────────────────────────────────────────────────────────
        for shipment in jumingo_qs:
            service = get_service(shipment.carrier)
            _track_one(shipment,
                       lambda s=shipment: service.track(s.carrier_shipment_id),
                       f"#{shipment.pk} Jumingo({shipment.carrier_shipment_id})")

        # ── DHL ───────────────────────────────────────────────────────────────
        from shipping.services.dhl import get_tracking as dhl_get_tracking
        for shipment in dhl_qs:
            _track_one(shipment,
                       lambda s=shipment: dhl_get_tracking(s.carrier, s.tracking_number),
                       f"#{shipment.pk} DHL({shipment.tracking_number})")

    def _check_overdue_eta(self, changes: list):
        """
        Позначає посилки як затримані якщо eta_to < сьогодні і статус IN_TRANSIT.
        Перевізник міг не надіслати явного сигналу затримки — виявляємо самостійно.
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

            # Telegram-сповіщення
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

        # ── Перевірка прострочених ETA (незалежно від API) ───────────────────
        if not dry_run:
            self._check_overdue_eta(changes)

        # ── Оновлюємо час останнього запуску ─────────────────────────────────
        if not dry_run:
            ShippingSettings.objects.filter(pk=1).update(last_tracking_run=timezone.now())
            self.stdout.write(self.style.SUCCESS(
                f"[track_shipments] Готово: оновлено {updated}/{total}, помилок {errors}"
            ))
            # Сповіщення якщо були зміни
            # errors без updated — не сповіщаємо (API errors ≠ зміни замовлень)
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
