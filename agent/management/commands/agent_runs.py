"""cron: 🤖 агент — доручення для пошти (agent/mail.py) і завдання з черги (agent/tasks.py). Нічого немає — нічого."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '🤖 Агент Minerva: нові листи за дорученнями й завдання з черги'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None, help='Листів за прохід (за замовч. PER_LOOP)')

    def handle(self, *args, **opts):
        from agent.models import AgentRule, AgentTask
        if AgentRule.objects.filter(enabled=True).exists():
            from agent.mail import PER_LOOP, run_due
            out = run_due(opts['limit'] or PER_LOOP)
            if any(out.values()):
                self.stdout.write(f"agent mail: у черзі +{out['queued']}, оброблено {out['done']}, "
                                  f"перервано {out['stale']}")
        if AgentTask.objects.filter(status__in=(AgentTask.QUEUED, AgentTask.RUNNING)).exists():
            from agent import tasks
            out = tasks.run_due()
            if any(out.values()):
                self.stdout.write(f"agent tasks: знову в черзі {out['requeued']}, працював {out['ran']}")
