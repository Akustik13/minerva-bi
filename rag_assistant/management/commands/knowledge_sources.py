"""«📂 Джерела знань»: перевірити папки й сайти (rag_assistant/knowledge_sources.py).

    python manage.py knowledge_sources            # усі увімкнені зараз
    python manage.py knowledge_sources --id 3     # одне джерело
    python manage.py knowledge_sources --if-due   # з cron: лише ті, що давно не перевіряли (rescan_hours)
"""
from django.core.management.base import BaseCommand

from rag_assistant import knowledge_sources
from rag_assistant.models import KnowledgeSource


class Command(BaseCommand):
    help = "Перевірити джерела знань (папки й сайти) і передати зміни в базу знань"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише джерела, які давно не перевіряли")
        parser.add_argument("--id", type=int, help="лише це джерело")

    def handle(self, *args, **opts):
        if opts["if_due"]:
            done = knowledge_sources.scan_due()
            if done:
                self.stdout.write(f"knowledge_sources: перевірено {len(done)}")
            return
        qs = KnowledgeSource.objects.filter(enabled=True)
        if opts["id"]:
            qs = KnowledgeSource.objects.filter(pk=opts["id"])
        for source in qs.order_by("pk"):
            result = knowledge_sources.scan(source)
            self.stdout.write(f"{source.name}: {result}")
