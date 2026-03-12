"""
python manage.py update_rfm

Оновлює RFM оцінки для всіх клієнтів.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Оновити RFM оцінки для всіх клієнтів"

    def handle(self, *args, **options):
        from crm.models import Customer

        customers = Customer.objects.all()
        total = customers.count()
        self.stdout.write(f"Оновлення RFM для {total} клієнтів...")

        updated = 0
        for customer in customers.iterator():
            customer.rfm_score()  # викличе метод який оновить БД
            updated += 1
            if updated % 50 == 0:
                self.stdout.write(f"  Оброблено: {updated}/{total}")

        self.stdout.write(self.style.SUCCESS(
            f"✅ Готово! Оновлено {updated} клієнтів."
        ))
