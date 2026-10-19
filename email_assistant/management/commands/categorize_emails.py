"""
categorize_emails — проставити категорії листам (клієнти, постачальники, сервіси, розсилки…).

  python manage.py categorize_emails          # лише листи без категорії
  python manage.py categorize_emails --all    # перерахувати всі (напр. після додавання клієнтів/постачальників)

Крон (sync_email) і так дозаповнює категорії порціями по 1000; команда — щоб одразу.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Автоматичні категорії листів для фільтра в скриньці'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Перерахувати всі листи')
        parser.add_argument('--account', type=int, default=None, help='Лише цей EmailAccount pk')

    def handle(self, *args, **options):
        from django.db.models import Count
        from email_assistant.categorize import categorize_pending
        from email_assistant.models import EmailAccount, EmailMessage

        account = EmailAccount.objects.get(pk=options['account']) if options['account'] else None
        done = categorize_pending(account, limit=None, force=options['all'])
        self.stdout.write(f'Оброблено листів: {done}')

        qs = EmailMessage.objects.all() if account is None else EmailMessage.objects.filter(account=account)
        labels = dict(EmailMessage.CATEGORY_CHOICES)
        for row in qs.values('category').annotate(n=Count('id')).order_by('-n'):
            self.stdout.write(f'  {labels.get(row["category"], "— без категорії")}: {row["n"]}')
