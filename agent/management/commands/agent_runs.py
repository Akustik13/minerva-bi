"""cron: доручення агента — нові листи скриньок → відповіді за дорученнями (agent/mail.py). Без доручень — нічого."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '🤖 Агент Minerva: обробити нові листи за дорученнями'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None, help='Листів за прохід (за замовч. PER_LOOP)')

    def handle(self, *args, **opts):
        from agent.mail import PER_LOOP, run_due
        from agent.models import AgentRule
        if not AgentRule.objects.filter(enabled=True).exists():
            return
        out = run_due(opts['limit'] or PER_LOOP)
        if any(out.values()):
            self.stdout.write(f"agent: у черзі +{out['queued']}, оброблено {out['done']}, перервано {out['stale']}")
