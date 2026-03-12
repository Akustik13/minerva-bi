"""
crm/management/commands/sync_crm.py

Синхронізує замовлення з порожнім customer_key → CRM.

Запуск:
  python manage.py sync_crm              # тільки без customer_key
  python manage.py sync_crm --all        # всі замовлення
  python manage.py sync_crm --dry-run    # перегляд без змін
  python manage.py sync_crm --source digikey
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Синхронізує замовлення з CRM (створює/оновлює Customer)"

    def add_arguments(self, parser):
        parser.add_argument("--all",     action="store_true", help="Обробити всі замовлення, не тільки без customer_key")
        parser.add_argument("--dry-run", action="store_true", help="Показати що буде зроблено без збереження")
        parser.add_argument("--source",  type=str, default="", help="Фільтр по джерелу (digikey, manual, ...)")

    def handle(self, *args, **options):
        from sales.models import SalesOrder
        from crm.models import Customer

        dry_run    = options["dry_run"]
        process_all = options["all"]
        source     = options["source"]

        qs = SalesOrder.objects.all()
        if source:
            qs = qs.filter(source=source)
        if not process_all:
            qs = qs.filter(customer_key="")

        total = qs.count()
        self.stdout.write(
            f"{'[DRY-RUN] ' if dry_run else ''}"
            f"Замовлень до обробки: {total}"
            + (f" (source={source})" if source else "")
            + (" [всі]" if process_all else " [без customer_key]")
        )

        stats = {
            "created":  0,
            "updated":  0,
            "linked":   0,
            "skipped":  0,
            "errors":   [],
        }

        for order in qs.iterator():
            if not order.email and not order.client:
                stats["skipped"] += 1
                continue

            try:
                key = Customer.generate_key(
                    order.email or order.client,
                    order.client or order.email,
                )

                # Розділяємо: client = компанія, contact_name = контактна особа
                contact = (order.contact_name or "").strip()
                client  = (order.client or "").strip()
                if contact:
                    customer_name    = contact
                    customer_company = client
                else:
                    customer_name    = client or (order.email.split("@")[0] if order.email else "")
                    customer_company = ""

                if dry_run:
                    exists = Customer.objects.filter(external_key=key).exists()
                    action = "existing" if exists else "NEW"
                    self.stdout.write(
                        f"  [{action}] {order.source}:{order.order_number} "
                        f"→ {customer_name} / {customer_company} ({order.email})"
                    )
                    if order.customer_key != key:
                        stats["linked"] += 1
                    if not exists:
                        stats["created"] += 1
                    continue

                customer, created = Customer.objects.get_or_create(
                    external_key=key,
                    defaults={
                        "name":        customer_name,
                        "company":     customer_company,
                        "email":       order.email or "",
                        "phone":       order.phone or "",
                        "country":     order.addr_country or "",
                        "addr_street": order.addr_street or "",
                        "addr_city":   order.addr_city or "",
                        "addr_zip":    order.addr_zip or "",
                        "source":      order.source,
                    },
                )

                if created:
                    stats["created"] += 1
                else:
                    # Оновлюємо порожні поля якщо з'явились нові дані
                    upd = {}
                    if not customer.company and customer_company:
                        upd["company"] = customer_company
                    if not customer.phone and order.phone:
                        upd["phone"] = order.phone
                    if not customer.addr_city and order.addr_city:
                        upd["addr_city"]   = order.addr_city
                        upd["addr_street"] = order.addr_street
                        upd["addr_zip"]    = order.addr_zip
                        upd["country"]     = order.addr_country
                    if upd:
                        Customer.objects.filter(pk=customer.pk).update(**upd)
                        stats["updated"] += 1

                if order.customer_key != key:
                    SalesOrder.objects.filter(pk=order.pk).update(customer_key=key)
                    stats["linked"] += 1

            except Exception as e:
                msg = f"{order.source}:{order.order_number} — {e}"
                stats["errors"].append(msg)
                self.stderr.write(f"  ERROR: {msg}")

        prefix = "[DRY-RUN] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"\n{prefix}Готово:\n"
            f"  Клієнтів створено:       {stats['created']}\n"
            f"  Клієнтів оновлено:       {stats['updated']}\n"
            f"  Замовлень прив'язано:    {stats['linked']}\n"
            f"  Пропущено (нема даних):  {stats['skipped']}\n"
            f"  Помилок:                 {len(stats['errors'])}"
        ))
        if stats["errors"]:
            self.stderr.write("\nПомилки:")
            for e in stats["errors"]:
                self.stderr.write(f"  • {e}")
