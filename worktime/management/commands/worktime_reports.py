"""Звіти по працівниках askDANTE поштою за розкладом (✉️ Звіти поштою).

    python manage.py worktime_reports --if-due   # з cron: ті, чий час настав
    python manage.py worktime_reports            # усі увімкнені — зараз
"""
from django.core.management.base import BaseCommand

from worktime import reports
from worktime.models import DanteSettings, WorkReport


class Command(BaseCommand):
    help = "Надіслати звіти робочого часу askDANTE поштою"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише звіти, чий час настав")

    def handle(self, *args, **opts):
        st = DanteSettings.get()
        if not st.enabled or not st.configured:
            return
        for rep in WorkReport.objects.filter(enabled=True):
            if opts["if_due"] and not reports.due(rep):
                continue
            self.stdout.write(f"worktime_reports: {rep}: {reports.send(rep)}")
