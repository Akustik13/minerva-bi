"""
Management command: pull_dk_listings

Pulls fresh data (prices, titles, attributes) from DigiKey for all active listings.

Usage:
  python manage.py pull_dk_listings
  python manage.py pull_dk_listings --force
  python manage.py pull_dk_listings --limit 10

Cron (Synology Docker):
  0 */12 * * * docker-compose exec -T web python manage.py pull_dk_listings
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Стягує дані лістингів (ціни, назви, атрибути) з DigiKey"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ігнорувати pull_enabled та інтервал і запустити примусово",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Максимальна кількість лістингів (0 = всі)",
        )
        parser.add_argument(
            "--sku",
            type=str,
            default="",
            help="Обробити тільки конкретний SKU",
        )

    def handle(self, *args, **options):
        from bots.models import DigiKeyConfig, DigiKeyListing
        from bots.services.dk_marketplace import pull_product_fields, DKMarketplaceError

        force = options["force"]
        limit = options["limit"]
        sku   = options["sku"].strip()

        config = DigiKeyConfig.get()

        if not config.client_id or not config.client_secret:
            self.stderr.write("❌ DigiKey Client ID / Secret не налаштовані.")
            return

        if not config.pull_enabled and not force:
            self.stdout.write(
                "⏸ Авто-стягування вимкнено (pull_enabled=False). "
                "Використайте --force для ручного запуску."
            )
            return

        # Check interval
        if not force and config.last_pulled_at and config.pull_interval_hours:
            elapsed_h = (timezone.now() - config.last_pulled_at).total_seconds() / 3600
            if elapsed_h < config.pull_interval_hours:
                remaining = int(config.pull_interval_hours - elapsed_h)
                self.stdout.write(
                    f"⏸ Ще рано (остання синхронізація {elapsed_h:.1f} год тому, "
                    f"інтервал {config.pull_interval_hours} год). "
                    f"Наступна приблизно через {remaining} год."
                )
                return

        qs = DigiKeyListing.objects.select_related('product').exclude(
            sync_status=DigiKeyListing.SYNC_STAGED
        )
        if sku:
            qs = qs.filter(product__sku=sku)
        if limit:
            qs = qs[:limit]

        total = qs.count() if not limit else min(qs.count(), limit)
        self.stdout.write(
            f"⬇️  Починаємо стягування для {total} лістингів "
            f"[{timezone.now().strftime('%Y-%m-%d %H:%M')}]…"
        )

        ok = err = changed_total = 0
        for listing in qs:
            try:
                result = pull_product_fields(listing)
                changed = result.get('changed', [])
                ok += 1
                changed_total += len(changed)
                if changed:
                    self.stdout.write(
                        f"  ✅ {listing.product.sku} — оновлено: {', '.join(changed)}"
                    )
                else:
                    self.stdout.write(f"  — {listing.product.sku} без змін")
            except Exception as exc:
                err += 1
                self.stderr.write(f"  ❌ {listing.product.sku}: {exc}")

        # Update last_pulled_at
        if ok:
            config.last_pulled_at = timezone.now()
            config.save(update_fields=['last_pulled_at'])

        self.stdout.write(
            f"\n✅ Готово: {ok} лістингів оброблено, "
            f"{changed_total} полів оновлено, {err} помилок."
        )
