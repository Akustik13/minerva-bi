"""
crm/management/commands/clean_customer_addresses.py

Виявляє та виправляє Customer-записи з явно невірними адресними полями:
  1. addr_city починається з "Email:" або "Phone:" → очищаємо
  2. addr_street збігається з company або name (ім'я/назва компанії потрапила у вулицю) → очищаємо
  3. Після очистки — підтягуємо правильні дані з пов'язаних SalesOrder (якщо є)

Використання:
    python manage.py clean_customer_addresses [--dry-run] [--source digikey]
"""
from django.core.management.base import BaseCommand


def _looks_like_bad_city(city: str) -> bool:
    low = city.lower().strip()
    return (
        low.startswith("email:") or
        low.startswith("phone:") or
        "@" in city            # email адреса в полі міста
    )


def _looks_like_bad_street(street: str, company: str, name: str) -> bool:
    """True якщо вулиця = назва компанії або ім'я (не реальна адреса)."""
    s = street.strip().lower()
    if company and s == company.strip().lower():
        return True
    if name and s == name.strip().lower():
        return True
    return False


class Command(BaseCommand):
    help = "Очистити невірні адресні поля Customer (email у місті, назва компанії у вулиці)"

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Показати що буде зроблено, без змін в БД')
        parser.add_argument('--source',
                            help='Обробити тільки клієнтів з певного джерела (напр. digikey)')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        source  = options.get('source')

        from crm.models import Customer
        from sales.models import SalesOrder

        self.stdout.write(self.style.WARNING('=== clean_customer_addresses ==='))
        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN]'))

        qs = Customer.objects.all()
        if source:
            qs = qs.filter(source=source)

        fixed_city   = 0
        fixed_street = 0
        refilled     = 0

        for customer in qs:
            update_fields = []

            # 1) Невірне місто
            if customer.addr_city and _looks_like_bad_city(customer.addr_city):
                self.stdout.write(
                    f'  pk={customer.pk} "{customer.name}" | '
                    f'addr_city "{customer.addr_city}" → ""'
                )
                if not dry_run:
                    customer.addr_city = ""
                update_fields.append("addr_city")
                fixed_city += 1

            # 2) Вулиця = назва компанії або ім'я
            if customer.addr_street and _looks_like_bad_street(
                customer.addr_street, customer.company, customer.name
            ):
                self.stdout.write(
                    f'  pk={customer.pk} "{customer.name}" | '
                    f'addr_street "{customer.addr_street}" → ""'
                )
                if not dry_run:
                    customer.addr_street = ""
                update_fields.append("addr_street")
                fixed_street += 1

            if update_fields and not dry_run:
                customer.save(update_fields=update_fields)

            # 3) Підтягнути адресу з SalesOrder якщо поля порожні
            if not customer.addr_street:
                order = (
                    SalesOrder.objects
                    .filter(customer_key=customer.external_key)
                    .exclude(addr_street="")
                    .exclude(addr_street__startswith="Email:")
                    .order_by("-order_date")
                    .first()
                )
                if order:
                    self.stdout.write(
                        f'  pk={customer.pk} "{customer.name}" | '
                        f'refill from order #{order.order_number}: '
                        f'"{order.addr_street}", "{order.addr_city}"'
                    )
                    if not dry_run:
                        customer.addr_street = order.addr_street or ""
                        customer.addr_city   = order.addr_city   or ""
                        customer.addr_zip    = order.addr_zip    or ""
                        customer.addr_state  = (order.addr_state or "")[:2]
                        customer.save(update_fields=[
                            "addr_street", "addr_city", "addr_zip", "addr_state"
                        ])
                    refilled += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Готово. Виправлено міст: {fixed_city} | "
            f"Виправлено вулиць: {fixed_street} | "
            f"Заповнено з замовлень: {refilled}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Це був dry-run. Запусти без --dry-run для реальних змін.'
            ))
