"""
Management command: sync_digikey_orders

Запуск:
  python manage.py sync_digikey_orders
  python manage.py sync_digikey_orders --dry-run

Cron (Synology Docker):
  */30 * * * * docker-compose exec -T web python manage.py sync_digikey_orders
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Синхронізує замовлення з DigiKey Marketplace API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Показати що буде зроблено без збереження в БД",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ігнорувати sync_enabled і запустити примусово",
        )

    def handle(self, *args, **options):
        from bots.models import DigiKeyConfig
        from bots.services.digikey import sync_orders, DigiKeyAPIError

        dry_run = options["dry_run"]
        force   = options["force"]

        config = DigiKeyConfig.get()

        if not config.client_id or not config.client_secret:
            self.stderr.write("❌ DigiKey Client ID / Secret не налаштовані.")
            return

        if not config.sync_enabled and not force:
            self.stdout.write("⏸ Синхронізація вимкнена (sync_enabled=False). Використайте --force для ручного запуску.")
            return

        # Перевіряємо інтервал (якщо не --force)
        if not force and config.last_synced_at and config.sync_interval_minutes:
            from django.utils import timezone
            elapsed = (timezone.now() - config.last_synced_at).total_seconds() / 60
            if elapsed < config.sync_interval_minutes:
                remaining = int(config.sync_interval_minutes - elapsed)
                self.stdout.write(
                    f"⏸ Ще рано (остання синхронізація {int(elapsed)} хв тому, "
                    f"інтервал {config.sync_interval_minutes} хв). "
                    f"Наступна через ~{remaining} хв."
                )
                return

        if dry_run:
            self.stdout.write("🔍 DRY-RUN: дані не будуть збережені в БД.")
            # Just test connection
            from bots.services.digikey import test_connection
            result = test_connection(config)
            self.stdout.write(result["message"])
            return

        self.stdout.write(f"🔄 Запуск DigiKey синхронізації [{timezone.now().strftime('%Y-%m-%d %H:%M')}]…")

        try:
            if config.marketplace_refresh_token or config.marketplace_access_token:
                from bots.services.digikey import sync_marketplace_orders
                self.stdout.write("  -> Marketplace API (3-legged OAuth)")
                stats = sync_marketplace_orders(config)
            else:
                self.stdout.write("  -> OrderStatus API (2-legged, legacy)")
                stats = sync_orders(config)
        except DigiKeyAPIError as e:
            self.stderr.write(f"❌ DigiKey API error: {e}")
            return
        except Exception as e:
            self.stderr.write(f"❌ Unexpected error: {e}")
            raise

        self.stdout.write(
            f"✅ Готово:\n"
            f"   Замовлень створено:  {stats['created']}\n"
            f"   Замовлень оновлено:  {stats['updated']}\n"
            f"   Замовлень без змін:  {stats['skipped']}\n"
            f"   Рядків створено:     {stats['lines_created']}\n"
            f"   Рядків пропущено:    {stats['lines_skipped']}"
        )

        if stats["unmatched_skus"]:
            self.stdout.write(
                "\n⚠️  Товари не знайдено в базі (потрібно додати вручну):"
            )
            for sku in stats["unmatched_skus"]:
                self.stdout.write(f"   • {sku}")

        if stats["errors"]:
            self.stderr.write("\n❌ Помилки:")
            for err in stats["errors"]:
                self.stderr.write(f"   • {err}")

        # Сповіщення якщо були зміни
        if stats["created"] or stats["updated"] or stats["errors"]:
            try:
                from dashboard.notifications import notify_sync_result
                notify_sync_result("DigiKey синхронізація", {
                    "created": stats["created"],
                    "updated": stats["updated"],
                    "errors":  stats["errors"],
                    "changes": stats.get("changes", []),
                })
            except Exception:
                pass
