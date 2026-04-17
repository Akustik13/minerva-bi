"""
shipping/management/commands/setup_tracking_rules.py

Створює стандартні правила трекінгу (idempotent — безпечно запускати повторно).

Використання:
  python manage.py setup_tracking_rules
"""
from django.core.management.base import BaseCommand


DEFAULTS = [
    # (carrier_type, priority, tracker)
    ("jumingo", 1, "jumingo"),
    ("dhl",     1, "dhl_track"),
    ("dhl",     2, "dhl"),
    ("dhl",     3, "jumingo"),
    ("ups",     1, "ups"),
    ("ups",     2, "jumingo"),
    ("fedex",   1, "fedex"),
    ("fedex",   2, "jumingo"),
    ("other",   1, "jumingo"),
]


class Command(BaseCommand):
    help = "Створює стандартні правила трекінгу для кожного типу перевізника"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Перезаписати існуючі правила",
        )

    def handle(self, *args, **options):
        from shipping.models import TrackingRule

        force = options["force"]
        created = updated = skipped = 0

        for carrier_type, priority, tracker in DEFAULTS:
            obj, was_created = TrackingRule.objects.get_or_create(
                carrier_type=carrier_type,
                priority=priority,
                defaults={"tracker": tracker, "enabled": True},
            )
            if was_created:
                created += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✅ Створено: {carrier_type} p{priority} → {tracker}"
                    )
                )
            elif force and obj.tracker != tracker:
                obj.tracker = tracker
                obj.enabled = True
                obj.save(update_fields=["tracker", "enabled"])
                updated += 1
                self.stdout.write(
                    f"  🔄 Оновлено: {carrier_type} p{priority} → {tracker}"
                )
            else:
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\n[setup_tracking_rules] Готово: "
                f"створено {created}, оновлено {updated}, пропущено {skipped}"
            )
        )
