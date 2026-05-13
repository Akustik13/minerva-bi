"""
Management command: clear old per-customer AI conversations.

Usage:
    python manage.py clear_old_conversations [--days 30]
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Clear old per-customer AI conversation history (crm_customer_* chat IDs)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=30,
            help='Delete conversations older than N days (default: 30)')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be deleted without actually deleting')

    def handle(self, *args, **options):
        days    = options['days']
        dry_run = options['dry_run']
        cutoff  = timezone.now() - timezone.timedelta(days=days)

        try:
            from ai_assistant.models import AIConversation
        except ImportError:
            self.stderr.write('ai_assistant app not available')
            return

        qs = AIConversation.objects.filter(
            telegram_chat_id__startswith='crm_customer_',
            updated_at__lt=cutoff,
        )
        count = qs.count()

        if dry_run:
            self.stdout.write(
                f'[dry-run] Would delete {count} conversation(s) older than {days} days '
                f'(before {cutoff:%Y-%m-%d})')
            return

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted} per-customer AI conversation(s) older than {days} days.'))
