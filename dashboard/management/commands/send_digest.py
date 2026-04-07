"""
Management command: send periodic digest report.

Usage:
  python manage.py send_digest           # respect schedule, send if due
  python manage.py send_digest --force   # send immediately regardless of schedule

Cron example (daily at 08:00 server time):
  0 8 * * * docker-compose exec -T web python manage.py send_digest
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send periodic digest report (pending shipments, overdue, new orders, stock)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Send immediately, ignoring schedule and digest_enabled flag",
        )

    def handle(self, *args, **options):
        from dashboard.digest import send_digest
        result = send_digest(force=options["force"])

        if result.get("sent"):
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
            self.stdout.write(self.style.SUCCESS(f"✅ Digest надіслано: {', '.join(parts)}"))
        else:
            reason = result.get("reason") or result.get("error") or "невідома причина"
            self.stdout.write(self.style.WARNING(f"⏭ Не надіслано: {reason}"))
