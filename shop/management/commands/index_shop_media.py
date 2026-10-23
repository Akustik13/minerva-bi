"""Фото й даташити для сайту (shop/media.py). --if-due — cron щохвилини: не частіше ніж раз на 5 хв і лише коли
ввімкнено на сторінці «🖼 Фото й даташити», перевіряє нові, змінені в картці й давно не перевірені посилання.
Без параметрів — нові й змінені зараз; --all — усе заново; --sku — окремі товари."""
from django.core.management.base import BaseCommand

from inventory.models import Product
from shop import media


class Command(BaseCommand):
    help = "Перевіряє фото й даташити товарів магазину і зберігає копії для сайту"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="Автоматичний прохід (cron)")
        parser.add_argument("--all", action="store_true", help="Перевірити все заново")
        parser.add_argument("--sku", nargs="+", help="Лише ці товари")

    def handle(self, *args, **options):
        if options["if_due"]:
            state = media.auto()
        elif options["sku"]:
            state = media.run("all", products=Product.objects.filter(sku__in=options["sku"]), by="manage.py")
        else:
            state = media.run("all" if options["all"] else "changed", by="manage.py")
        if state:
            c = state["counts"]
            self.stdout.write(f"index_shop_media: перевірено {state['total']} · ✅ {c['ok']} · ⚠ файл {c['fallback']} · "
                              f"— немає {c['none']} · ❌ {c['error']} · змінилось {c['changed']}")
