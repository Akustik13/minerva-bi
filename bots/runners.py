"""
bots/runners.py — Логіка запуску ботів (ЧИСТА ВЕРСІЯ)
"""
from django.utils import timezone
from .models import Bot, BotLog


def run_digikey_bot(bot: Bot) -> dict:
    """
    Запуск DigiKey бота — синхронізація замовлень через DigiKey Orders API.

    Returns:
        dict з результатами: {'success': bool, 'message': str, 'stats': dict}
    """
    from bots.models import DigiKeyConfig
    from bots.services.digikey import sync_orders, DigiKeyAPIError

    config = DigiKeyConfig.get()

    if not config.client_id or not config.client_secret:
        return {
            'success': False,
            'message': "DigiKey Client ID / Secret не налаштовані в DigiKey Конфігурації.",
            'stats': {},
        }

    # Оновлюємо статус бота
    bot.status = Bot.Status.RUNNING
    bot.save(update_fields=['status'])

    log = BotLog.objects.create(
        bot=bot,
        level=BotLog.LogLevel.INFO,
        message="🔵 Запуск DigiKey синхронізації замовлень…"
    )
    start_time = timezone.now()

    try:
        stats = sync_orders(config)

        log.finished_at     = timezone.now()
        log.duration        = (log.finished_at - start_time).seconds
        log.level           = BotLog.LogLevel.SUCCESS if not stats["errors"] else BotLog.LogLevel.WARNING
        log.message         = (
            f"✅ DigiKey синхронізація: +{stats['created']} замовлень, "
            f"+{stats['lines_created']} рядків"
        )
        log.items_processed = stats["created"] + stats["updated"] + stats["skipped"]
        log.items_created   = stats["created"]
        log.items_updated   = stats["updated"]
        log.items_failed    = len(stats["errors"])
        log.details         = stats
        log.save()

        bot.status            = Bot.Status.ACTIVE
        bot.last_run_at       = start_time
        bot.last_run_status   = 'success'
        bot.last_run_duration = log.duration
        bot.total_runs       += 1
        bot.success_runs     += 1
        bot.next_run_at       = bot.calculate_next_run()
        bot.save()

        return {
            'success': True,
            'message': (
                f"Створено {stats['created']} замовлень, "
                f"оновлено {stats['updated']}, "
                f"рядків {stats['lines_created']}"
            ),
            'stats': stats,
        }

    except DigiKeyAPIError as e:
        msg = f"DigiKey API помилка: {e}"
        _fail_bot(bot, log, start_time, msg, str(e))
        return {'success': False, 'message': msg, 'stats': {}}

    except Exception as e:
        msg = f"Помилка: {type(e).__name__}: {e}"
        _fail_bot(bot, log, start_time, msg, str(e))
        return {'success': False, 'message': msg, 'stats': {}}


def _fail_bot(bot, log, start_time, msg, error_detail):
    log.finished_at = timezone.now()
    log.duration    = (log.finished_at - start_time).seconds
    log.level       = BotLog.LogLevel.ERROR
    log.message     = f"❌ {msg}"
    log.details     = {'error': error_detail}
    log.save()

    bot.status            = Bot.Status.ERROR
    bot.last_run_at       = start_time
    bot.last_run_status   = 'error'
    bot.last_run_duration = log.duration
    bot.total_runs       += 1
    bot.error_runs       += 1
    bot.save()
