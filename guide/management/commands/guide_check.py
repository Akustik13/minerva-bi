"""📚 Гід: перевірити шапки, посилання й адреси сторінок docs/guide/ (guide/check.py).

    python manage.py guide_check                 # увесь гід
    python manage.py guide_check --section mail  # один розділ
"""
from django.core.management.base import BaseCommand, CommandError

from guide import check, loader


class Command(BaseCommand):
    help = "Перевірити сторінки гіда по Minerva (docs/guide/)"

    def add_arguments(self, parser):
        parser.add_argument("--section", default="", help="лише цей розділ (назва теки)")

    def handle(self, *args, **opts):
        errors = check.check(opts["section"])
        pages = [p for p in loader.pages() if not opts["section"] or p.section == opts["section"]]
        for e in errors:
            self.stdout.write(f"✗ {e}")
        if errors:
            raise CommandError(f"Помилок: {len(errors)} (сторінок перевірено: {len(pages)})")
        self.stdout.write(f"✓ Сторінок: {len(pages)} — посилання й шапки в порядку")
