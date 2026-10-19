"""
relink_email_crm — одноразовий ремонт уже синхронізованих листів.

  python manage.py relink_email_crm --dry-run     # лише показати, що зміниться
  python manage.py relink_email_crm               # застосувати
  python manage.py relink_email_crm --days 90 --account 1

Що робить:
1. Листи лише з text/html, збережені як текст (сирий HTML у скриньці) → body_html + текст.
2. Кожен лист проходить email_assistant.crm_link: клієнт серед To/Cc без власних адрес,
   напрям за відправником (копія власного листа у Вхідних = 📤), номер замовлення.
   Пропущені раніше листи (напр. надіслані з веб-пошти, відкриті через папку сервера)
   з'являються в хронології клієнта.
3. Старі події того ж листа (до появи Message-ID) прив'язуються до листа; зайві копії
   (той самий лист у Вхідних і Надісланих або подія не в того клієнта) видаляються.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max
from django.utils import timezone


class Command(BaseCommand):
    help = 'Перепривʼязати синхронізовані листи до CRM-клієнтів і замовлень, виправити HTML-листи'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Показати без збереження')
        parser.add_argument('--days', type=int, default=None, help='Лише листи за N днів (за замовч. усі)')
        parser.add_argument('--account', type=int, default=None, help='Лише цей EmailAccount pk')

    def handle(self, *args, **options):
        from crm.models import CustomerTimeline
        from email_assistant.crm_link import link_message_to_crm, own_addresses
        from email_assistant.models import EmailMessage

        dry_run = options['dry_run']
        qs = (EmailMessage.objects
              .filter(is_deleted=False, is_spam=False)
              .exclude(folder__in=[EmailMessage.FOLDER_SPAM, EmailMessage.FOLDER_DRAFT])
              .select_related('account', 'account__user')
              .order_by('sent_at', 'pk'))
        if options['days']:
            qs = qs.filter(sent_at__gte=timezone.now() - timedelta(days=options['days']))
        if options['account']:
            qs = qs.filter(account_id=options['account'])

        own = own_addresses()
        self.stdout.write(f'Власні адреси: {", ".join(sorted(own[0])) or "—"}')
        self.stdout.write(f'Власні домени: {", ".join(sorted(own[1])) or "—"}')

        stats = dict(html=0, created=0, linked=0, removed=0, unmatched=0)
        last_pk_before = CustomerTimeline.objects.aggregate(m=Max('pk'))['m'] or 0

        with transaction.atomic():
            for em in qs.iterator(chunk_size=200):
                if em.fix_html_body():
                    stats['html'] += 1

                legacy = list(self._legacy_events(em))
                event = link_message_to_crm(em, own=own)
                if event is None:
                    stats['unmatched'] += 1
                    continue
                if event.pk > last_pk_before:
                    stats['created'] += 1
                    self.stdout.write(f'  + {event.customer} | {event.get_event_type_display()} | {em.subject[:70]}')
                else:
                    stats['linked'] += 1
                for old in legacy:
                    if old.pk != event.pk:
                        stats['removed'] += 1
                        self.stdout.write(f'  − дубль у {old.customer}: {old.title[:70]}')
                        old.delete()

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(
            f'{"[DRY-RUN] " if dry_run else ""}'
            f'HTML виправлено: {stats["html"]}, нових подій у CRM: {stats["created"]}, '
            f'оновлено/привʼязано: {stats["linked"]}, видалено дублів: {stats["removed"]}, '
            f'без клієнта: {stats["unmatched"]}'
        )

    @staticmethod
    def _legacy_events(em):
        """Події старої синхронізації для цього листа: той самий юзер, UID, тема, тип за папкою."""
        from crm.models import CustomerTimeline
        from email_assistant.models import EmailMessage

        account = em.account
        standard = {(account.imap_folder_inbox or '').lower(), (account.imap_folder_sent or '').lower()}
        if not em.imap_uid or (em.imap_folder_name or '').lower() not in standard:
            return CustomerTimeline.objects.none()
        return CustomerTimeline.objects.filter(
            user=account.user,
            email_message__isnull=True,
            message_id='',
            related_email_id=em.imap_uid,
            title=(em.subject or '(без теми)')[:300],
            event_type='email_out' if em.folder == EmailMessage.FOLDER_SENT else 'email_in',
        )
