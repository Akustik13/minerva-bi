from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import WebhookDelivery
from api.webhooks import send_due


class Command(BaseCommand):
    help = "Повторна доставка вебхуків, час яких настав (запускається з cron_runner.sh)"

    def add_arguments(self, parser):
        parser.add_argument("--keep-days", type=int, default=30,
                            help="Видаляти успішні доставки, старші за N днів (0 — не видаляти)")

    def handle(self, *args, keep_days=30, **opts):
        sent, failed = send_due()
        if sent or failed:
            self.stdout.write(f"webhooks: delivered={sent} failed={failed}")
        if keep_days:
            WebhookDelivery.objects.filter(
                status=WebhookDelivery.SUCCESS,
                created_at__lt=timezone.now() - timedelta(days=keep_days),
            ).delete()
