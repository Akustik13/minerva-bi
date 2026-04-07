import logging
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run Minerva Telegram bot (polling mode)'

    def handle(self, *args, **options):
        self.stdout.write('Starting Minerva Telegram bot…')
        try:
            from ai_assistant.telegram_bot.bot import run_bot
            run_bot()
        except KeyboardInterrupt:
            self.stdout.write('\nBot stopped.')
        except Exception as e:
            logger.exception("Bot crashed")
            raise
