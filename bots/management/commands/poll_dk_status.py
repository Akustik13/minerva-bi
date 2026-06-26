"""
Management command: poll_dk_status

Checks staged DigiKey listings and promotes them to 'published' if DigiKey has approved.

Usage:
  python manage.py poll_dk_status
  python manage.py poll_dk_status --force

Cron (Synology Docker):
  */60 * * * * docker-compose exec -T web python manage.py poll_dk_status
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Перевіряє staged лістинги DigiKey і переводить затверджені у published"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ігнорувати poll_enabled та інтервал і запустити примусово",
        )

    def handle(self, *args, **options):
        from bots.models import DigiKeyConfig, DigiKeyListing
        from bots.services.dk_marketplace import check_staged_listing, DKMarketplaceError

        force = options["force"]

        cfg = DigiKeyConfig.objects.filter(pk=1).first()
        if not cfg:
            self.stderr.write("DigiKeyConfig не налаштовано.")
            return

        if not force:
            if not cfg.poll_enabled:
                self.stdout.write("Авто-перевірка вимкнена (poll_enabled=False). Використай --force для примусового запуску.")
                return

            if cfg.last_polled_at:
                elapsed_minutes = (timezone.now() - cfg.last_polled_at).total_seconds() / 60
                if elapsed_minutes < cfg.poll_interval_minutes:
                    remaining = int(cfg.poll_interval_minutes - elapsed_minutes)
                    self.stdout.write(f"Ще зарано — до наступної перевірки {remaining} хв.")
                    return

        staged_qs = DigiKeyListing.objects.filter(
            sync_status=DigiKeyListing.SYNC_STAGED
        ).select_related("product")

        count = staged_qs.count()
        if not count:
            self.stdout.write("Немає лістингів зі статусом 'staged'.")
            cfg.last_polled_at = timezone.now()
            cfg.save(update_fields=["last_polled_at"])
            return

        self.stdout.write(f"Перевіряємо {count} staged лістингів…")

        promoted = still_pending = errors = 0
        for listing in staged_qs:
            sku = listing.product.sku if listing.product else f"pk={listing.pk}"
            try:
                result = check_staged_listing(listing)
                if result == "published":
                    promoted += 1
                    self.stdout.write(self.style.SUCCESS(f"  ✅ {sku} → published (offer: {listing.dk_offer_id})"))
                else:
                    still_pending += 1
                    self.stdout.write(f"  ⏳ {sku} — ще очікує DigiKey")
            except DKMarketplaceError as exc:
                errors += 1
                self.stderr.write(f"  ❌ {sku}: {exc}")
            except Exception as exc:
                errors += 1
                self.stderr.write(f"  ❌ {sku} (unexpected): {exc}")

        cfg.last_polled_at = timezone.now()
        cfg.save(update_fields=["last_polled_at"])

        self.stdout.write(
            f"\nГотово: затверджено {promoted}, ще очікує {still_pending}, помилок {errors}."
        )
