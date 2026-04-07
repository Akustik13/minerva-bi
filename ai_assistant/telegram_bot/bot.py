"""
Telegram bot via python-telegram-bot (PTB v21+).
Run via: python manage.py run_telegram_bot
"""
import logging
import django
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai_assistant.permissions import get_profile_for_telegram
    tg_user = update.effective_user
    profile = get_profile_for_telegram(tg_user.id)

    if not profile:
        await update.message.reply_text(
            "👋 Вітаю! Я Minerva AI — асистент системи.\n\n"
            "Твій Telegram ще не прив'язаний до жодного акаунту.\n"
            f"Telegram ID: `{tg_user.id}`\n\n"
            "Попроси адміністратора додати цей ID до твого профілю.",
            parse_mode='Markdown',
        )
        return

    await update.message.reply_text(
        f"🏛️ Слава, {profile.user.get_full_name() or profile.user.username}!\n"
        "Я Minerva — твій AI-помічник. Чим можу допомогти?\n\n"
        "Команди: /reset — нова розмова, /help — довідка"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai_assistant.permissions import get_profile_for_telegram
    from ai_assistant.service import reset_conversation
    profile = get_profile_for_telegram(update.effective_user.id)
    reset_conversation(
        profile,
        channel='telegram_private',
        telegram_chat_id=str(update.effective_chat.id),
    )
    await update.message.reply_text('🔄 Розмову скинуто. Починаємо з чистого аркуша.')


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏛️ *Minerva AI — довідка*\n\n"
        "Просто напиши питання про бізнес.\n\n"
        "*Команди:*\n"
        "/start — привітання\n"
        "/reset — почати нову розмову\n"
        "/help — ця довідка",
        parse_mode='Markdown',
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai_assistant.permissions import get_profile_for_telegram
    from ai_assistant.service import chat

    tg_user = update.effective_user
    profile = get_profile_for_telegram(tg_user.id)

    if not profile:
        await update.message.reply_text(
            "⚠️ Твій Telegram не прив'язаний до акаунту Minerva.\n"
            f"ID: `{tg_user.id}` — передай адміністратору.",
            parse_mode='Markdown',
        )
        return

    if not profile.ai_enabled:
        await update.message.reply_text('🔒 AI-асистент для тебе вимкнений.')
        return

    # Typing indicator
    await update.message.reply_chat_action('typing')

    try:
        reply = chat(
            user_text=update.message.text,
            profile=profile,
            channel='telegram_private',
            telegram_chat_id=str(update.effective_chat.id),
        )
    except Exception as e:
        logger.exception("chat() error")
        reply = 'Щось пішло не так. Спробуй пізніше. 🏛️'

    # Telegram message limit
    if len(reply) > 4096:
        reply = reply[:4090] + '…'

    await update.message.reply_text(reply)


def run_bot():
    from strategy.models import AISettings
    token = AISettings.get().telegram_bot_token
    if not token:
        logger.error("Telegram bot token not configured in AISettings.")
        return

    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('reset', cmd_reset))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Minerva Telegram bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
