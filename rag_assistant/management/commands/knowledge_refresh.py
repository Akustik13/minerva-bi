"""Звірка стрічки бази знань: усі допущені листи + усі передані документи, очищення журналу (30 днів) і знімків.

    python manage.py knowledge_refresh            # зараз
    python manage.py knowledge_refresh --if-due   # з cron: не частіше ніж раз на добу і лише якщо база знань використовується
"""
from django.core.management.base import BaseCommand

from rag_assistant import knowledge


class Command(BaseCommand):
    help = "Звірити стрічку бази знань для RAG з поточним станом пошти"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише якщо минула доба від останньої звірки")

    def handle(self, *args, **opts):
        if opts["if_due"]:
            if knowledge.refresh_if_due():
                self.stdout.write("knowledge_refresh: виконано")
            return
        result = knowledge.refresh_all()
        self.stdout.write(f"knowledge_refresh: подій {result['events']}, очищено до seq {result['pruned_seq'] or '—'}")
