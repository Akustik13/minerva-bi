"""
crm/management/commands/merge_duplicate_customers.py

Знаходить Customer-записи з однаковою назвою компанії (company) і об'єднує їх
в один запис (primary).

Для кожної групи:
  - обирає primary (найбільше пов'язаних SalesOrder → customer_key)
  - зливає дублікатів у primary — crm/merge.py: замовлення й УСІ зв'язки з Customer (нотатки, хронологія,
    стратегії, задачі, рахунки, проєкти, пошта, Angebote, база знань, календар, askDANTE…), файли картки
  - переводить primary і всі його замовлення на новий B2B-ключ (SHA256('b2b:<company>'))
  - видаляє дублікати

Використання:
    python manage.py merge_duplicate_customers [--dry-run] [--company "NAME"]
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q


class Command(BaseCommand):
    help = "Об'єднати дублікатів клієнтів за назвою компанії"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показати що буде зроблено, без змін в БД',
        )
        parser.add_argument(
            '--company',
            help='Обробити тільки конкретну компанію (точна назва)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        only_company = options.get('company')

        from crm.models import Customer

        self.stdout.write(
            self.style.WARNING('=== merge_duplicate_customers ===')
        )
        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN — змін не буде]'))

        # Знайти company-імена, що зустрічаються більше 1 разу
        qs = (
            Customer.objects
            .exclude(company='')
            .exclude(company__isnull=True)
            .values('company')
            .annotate(cnt=Count('pk'))
            .filter(cnt__gt=1)
            .order_by('company')
        )

        if only_company:
            qs = qs.filter(company__iexact=only_company)

        groups = list(qs)
        if not groups:
            self.stdout.write(self.style.SUCCESS('Дублікатів не знайдено.'))
            return

        self.stdout.write(f'Знайдено груп з дублікатами: {len(groups)}')
        total_merged = 0
        total_deleted = 0

        for grp in groups:
            company_name = grp['company']
            count        = grp['cnt']
            merged, deleted = self._merge_group(
                company_name, dry_run, Customer
            )
            total_merged  += merged
            total_deleted += deleted

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Готово. Об\'єднано груп: {total_merged} | Видалено записів: {total_deleted}'
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Це був dry-run. Запусти без --dry-run для реальних змін.'
            ))

    # ------------------------------------------------------------------
    def _merge_group(self, company_name: str, dry_run: bool, Customer) -> tuple:
        """
        Об'єднує всі Customer-записи для company_name.
        Повертає (1, n_deleted) або (0, 0).
        """
        from sales.models import SalesOrder

        customers = list(
            Customer.objects
            .filter(company__iexact=company_name)
            .prefetch_related()
        )
        if len(customers) <= 1:
            return 0, 0

        # Обчислюємо новий B2B-ключ
        new_key = Customer.generate_key('b2b', company_name)

        # Визначаємо primary: той що вже має new_key, або той з найбільшою
        # кількістю замовлень (customer_key)
        primary = next(
            (c for c in customers if c.external_key == new_key), None
        )
        if primary is None:
            # рахуємо замовлення по старих ключах
            old_keys = [c.external_key for c in customers]
            key_counts = {
                row['customer_key']: row['cnt']
                for row in SalesOrder.objects
                .filter(customer_key__in=old_keys)
                .values('customer_key')
                .annotate(cnt=Count('pk'))
            }
            primary = max(customers, key=lambda c: key_counts.get(c.external_key, 0))

        duplicates = [c for c in customers if c.pk != primary.pk]

        self.stdout.write(
            f'\n  Company: "{company_name}" | {len(customers)} записів'
        )
        self.stdout.write(
            f'  Primary pk={primary.pk} key={primary.external_key[:12]}…'
        )
        for d in duplicates:
            self.stdout.write(
                f'  Дублікат pk={d.pk} name="{d.name}" key={d.external_key[:12]}…'
            )

        if dry_run:
            return 1, len(duplicates)

        from crm.merge import merge_customers, summary

        with transaction.atomic():
            # --- Усі зв'язки дублікатів (замовлення, нотатки, проєкти, пошта, задачі, рахунки…) → primary ---
            stats = merge_customers(primary, duplicates)

            # --- primary переходить на B2B-ключ разом з усіма своїми замовленнями ---
            if primary.external_key != new_key:
                updated = SalesOrder.objects.filter(
                    customer_key=primary.external_key
                ).update(customer_key=new_key)
                primary.external_key = new_key
                primary.save(update_fields=['external_key'])
                self.stdout.write(
                    f'  SalesOrder.customer_key → B2B-ключ: {updated} записів'
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f'  ✓ Видалено {stats["deleted"]} дублікатів, primary pk={primary.pk} · {summary(stats)}'
                )
            )

        return 1, len(duplicates)
