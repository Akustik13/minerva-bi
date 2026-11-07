"""Відновити трек-номери посилок, яким «📦 Відстежувати» записало номер іншої посилки того самого замовлення.

    python manage.py repair_parcel_tracking            # лише показати
    python manage.py repair_parcel_tracking --apply    # виправити

Справжній номер — те, що перевізник повернув при оформленні: carrier_shipment_id (UPS ShipmentIdentificationNumber /
FedEx master) або номер у назві файлу етикетки ups_<pk>_<номер>.pdf. Логіка — shipping/services/parcel_repair.py.
"""
from django.core.management.base import BaseCommand

from shipping.services import parcel_repair


class Command(BaseCommand):
    help = "Відновити трек-номери посилок з однаковим номером в одному замовленні"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="записати виправлення (інакше — лише показати)")

    def handle(self, *args, **opts):
        plan = parcel_repair.plan()
        if not plan:
            self.stdout.write("Посилок з чужим трек-номером не знайдено.")
            return
        for row in plan:
            fix = f"→ {row['new']} ({row['why']})" if row["new"] else "→ справжній номер невідомий, впишіть вручну"
            self.stdout.write(f"Замовлення {row['order']} · відправлення #{row['pk']}: {row['old']} {fix}")
        if opts["apply"]:
            done = parcel_repair.apply(plan)
            self.stdout.write(self.style.SUCCESS(f"Виправлено: {done}"))
        else:
            self.stdout.write("Лише перегляд. Щоб виправити — додайте --apply")
