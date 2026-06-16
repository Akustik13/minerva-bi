"""
Management command: sync_digikey_orders

Запуск:
  python manage.py sync_digikey_orders
  python manage.py sync_digikey_orders --dry-run
  python manage.py sync_digikey_orders --force

Cron (Synology Docker):
  */30 * * * * docker-compose exec -T web python manage.py sync_digikey_orders
"""
import time
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
        from bots.services.digikey import DigiKeyAPIError
        from bots.services.api_health import notify_connection_failure, notify_reauth_needed

        dry_run = options["dry_run"]
        force   = options["force"]

        config = DigiKeyConfig.get()

        if not config.client_id or not config.client_secret:
            self.stderr.write("❌ DigiKey Client ID / Secret не налаштовані.")
            return

        if not config.sync_enabled and not force:
            self.stdout.write(
                "⏸ Синхронізація вимкнена (sync_enabled=False). "
                "Використайте --force для ручного запуску."
            )
            return

        # Self-throttle: check interval from DB
        if not force and config.last_synced_at and config.sync_interval_minutes:
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
            from bots.services.digikey import test_connection
            result = test_connection(config)
            self.stdout.write(result["message"])
            return

        use_marketplace = bool(
            config.marketplace_refresh_token or config.marketplace_access_token
        )
        retry_count = max(1, min(10, config.api_retry_count or 3))
        retry_delay = max(1, config.api_retry_delay or 10)

        self.stdout.write(
            f"🔄 Запуск DigiKey синхронізації [{timezone.now().strftime('%Y-%m-%d %H:%M')}]…"
        )
        if use_marketplace:
            self.stdout.write(f"  -> Marketplace API (3-legged OAuth), спроб: {retry_count}")
        else:
            self.stdout.write(f"  -> OrderStatus API (2-legged, legacy), спроб: {retry_count}")

        stats = None
        for attempt in range(1, retry_count + 1):
            try:
                if use_marketplace:
                    from bots.services.digikey import sync_marketplace_orders
                    stats = sync_marketplace_orders(config)
                else:
                    from bots.services.digikey import sync_orders
                    stats = sync_orders(config)
                break  # success

            except DigiKeyAPIError as e:
                err_str = str(e)
                # 401 / token errors: no retry, notify reauth needed
                auth_keywords = ('401', 'Unauthorized', 'Token refresh error',
                                 'не авторизовано', 'invalid_grant')
                if any(kw.lower() in err_str.lower() for kw in auth_keywords):
                    self.stderr.write(f"❌ OAuth помилка: {err_str}")
                    notify_reauth_needed(config, err_str)
                    return
                self.stderr.write(
                    f"⚠️  DigiKey API error (спроба {attempt}/{retry_count}): {err_str}"
                )
                if attempt < retry_count:
                    self.stdout.write(f"   Повтор через {retry_delay}с…")
                    time.sleep(retry_delay)
                else:
                    self.stderr.write(f"❌ Всі {retry_count} спроб невдалі.")
                    notify_connection_failure(
                        config, err_str, retry_count, "sync_digikey_orders"
                    )
                    return

            except Exception as e:
                import requests as _req
                err_str = str(e)
                is_network = isinstance(e, (
                    _req.exceptions.ConnectTimeout,
                    _req.exceptions.ConnectionError,
                    _req.exceptions.Timeout,
                ))
                self.stderr.write(
                    f"⚠️  Помилка (спроба {attempt}/{retry_count}): {err_str}"
                )
                if is_network and attempt < retry_count:
                    self.stdout.write(f"   Мережева помилка — повтор через {retry_delay}с…")
                    time.sleep(retry_delay)
                else:
                    notify_connection_failure(
                        config, err_str, attempt, "sync_digikey_orders"
                    )
                    if not is_network:
                        raise
                    return

        if stats is None:
            return

        self.stdout.write(
            f"✅ Готово:\n"
            f"   Замовлень створено:  {stats['created']}\n"
            f"   Замовлень оновлено:  {stats['updated']}\n"
            f"   Замовлень без змін:  {stats['skipped']}\n"
            f"   Рядків створено:     {stats['lines_created']}\n"
            f"   Рядків пропущено:    {stats['lines_skipped']}"
        )

        if stats["unmatched_skus"]:
            self.stdout.write("\n⚠️  Товари не знайдено в базі (потрібно додати вручну):")
            for sku in stats["unmatched_skus"]:
                self.stdout.write(f"   • {sku}")

        if stats["errors"]:
            self.stderr.write("\n❌ Помилки:")
            for err in stats["errors"]:
                self.stderr.write(f"   • {err}")

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
