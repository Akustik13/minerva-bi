"""Створити простий шаблон Angebot без бланка: offers/templates_docx/offer_template.docx.

    python manage.py build_offer_template

Зазвичай Angebot будується на бланку Commercial Invoice (offers/template_builder.py, кеш у MEDIA_ROOT);
цей шаблон — запасний, якщо бланка немає. Власний шаблон завантажується в «Налаштуваннях Angebote»;
змінні — див. offers/document.py build_context.
"""
from django.core.management.base import BaseCommand

from offers.document import DEFAULT_TEMPLATE
from offers.template_builder import build


class Command(BaseCommand):
    help = "Створити простий шаблон DOCX для Angebote (без бланка)"

    def handle(self, *args, **opts):
        DEFAULT_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
        build(DEFAULT_TEMPLATE)
        self.stdout.write(f"Шаблон: {DEFAULT_TEMPLATE}")
