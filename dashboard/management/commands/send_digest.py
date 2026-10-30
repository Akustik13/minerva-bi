"""
Management command: send periodic digest report.

Usage:
  python manage.py send_digest                    # daily digest: respect schedule, send if due
  python manage.py send_digest --period all       # daily + weekly + monthly, each only if due (cron)
  python manage.py send_digest --period weekly    # one period
  python manage.py send_digest --force            # send immediately regardless of schedule

Розклад сервера (cron_runner.sh) запускає `send_digest --period all --verbosity 0` щохвилини: кожен звіт
сам перевіряє свій день і «Час відправки» з «Налаштувань сповіщень» і надсилається не більше разу на день
(*_last_sent). З --verbosity 0 у журнал пишуться лише надіслані звіти й помилки.
Не вдалося надіслати — повтор не раніше ніж за 30 хв і не більше 3 разів на день (dashboard/schedule_guard.py).
"""
from django.core.management.base import BaseCommand

PERIODS = ("daily", "weekly", "monthly")
LABELS = {"daily": "Щоденний", "weekly": "Тижневий", "monthly": "Місячний"}


class Command(BaseCommand):
    help = "Send periodic digest report (pending shipments, overdue, new orders, stock)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Send immediately, ignoring schedule and digest_enabled flag",
        )
        parser.add_argument(
            "--period", choices=PERIODS + ("all",), default="daily",
            help="Which digest: daily (default), weekly, monthly or all (each only when due)",
        )

    def handle(self, *args, **options):
        from dashboard import schedule_guard
        from dashboard.digest import send_digest

        periods = PERIODS if options["period"] == "all" else (options["period"],)
        quiet = options.get("verbosity", 1) < 1
        force = options["force"]
        for period in periods:
            label = LABELS[period]
            guard = f"digest-{period}"
            if not force and not schedule_guard.may_try(guard):
                if not quiet:
                    self.stdout.write(f"⏭ {label} звіт: після невдалої спроби — пауза (повтор пізніше)")
                continue
            result = send_digest(force=force, period=period)

            if result.get("sent"):
                schedule_guard.clear(guard)
                email_ok = result.get("email", {}).get("sent", False)
                tg_ok    = result.get("telegram", {}).get("sent", False)
                parts    = []
                if email_ok:
                    parts.append("Email ✓")
                if tg_ok:
                    parts.append("Telegram ✓")
                for ch, info in result.items():
                    if isinstance(info, dict) and info.get("error"):
                        parts.append(f"{ch}: ПОМИЛКА — {info['error']}")
                self.stdout.write(self.style.SUCCESS(f"✅ {label} звіт надіслано: {', '.join(parts)}"))
                continue

            errors = schedule_guard.channel_errors(result)
            if errors:   # спробували, але жоден канал не надіслав — це видно завжди
                if not force:
                    schedule_guard.note_failure(guard)
                self.stdout.write(self.style.WARNING(f"⚠️ {label} звіт не надіслано: {'; '.join(errors)}"))
                continue
            if not quiet:
                reason = result.get("reason") or result.get("error") or "невідома причина"
                self.stdout.write(self.style.WARNING(f"⏭ {label} звіт не надіслано: {reason}"))
