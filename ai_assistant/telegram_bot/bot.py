"""
Telegram bot via python-telegram-bot (PTB v21+).
Run via: python manage.py run_telegram_bot

All Django ORM/service calls are wrapped with sync_to_async because
PTB v21+ uses asyncio and Django ORM is synchronous.
"""
import logging
from asgiref.sync import sync_to_async
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai_assistant.permissions import get_profile_for_telegram
    tg_user = update.effective_user
    profile = await sync_to_async(get_profile_for_telegram)(tg_user.id)

    if not profile:
        await update.message.reply_text(
            "👋 Вітаю! Я Minerva AI — асистент системи.\n\n"
            "Твій Telegram ще не прив'язаний до жодного акаунту.\n"
            f"Telegram ID: `{tg_user.id}`\n\n"
            "Попроси адміністратора додати цей ID до твого профілю.",
            parse_mode='Markdown',
        )
        return

    name = await sync_to_async(lambda: profile.user.get_full_name() or profile.user.username)()
    await update.message.reply_text(
        f"🏛️ Слава, {name}!\n"
        "Я Minerva — твій AI-помічник. Чим можу допомогти?\n\n"
        "Команди: /reset — нова розмова, /help — довідка"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai_assistant.permissions import get_profile_for_telegram
    from ai_assistant.service import reset_conversation
    profile = await sync_to_async(get_profile_for_telegram)(update.effective_user.id)
    chat_type = update.effective_chat.type
    channel = 'telegram_group' if chat_type in ('group', 'supergroup') else 'telegram_private'
    await sync_to_async(reset_conversation)(
        profile,
        channel=channel,
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

    if not update.message or not update.message.text:
        return

    tg_user = update.effective_user
    chat_type = update.effective_chat.type  # 'private' | 'group' | 'supergroup'
    is_group = chat_type in ('group', 'supergroup')

    if is_group:
        bot_username = context.bot.username
        text = update.message.text
        is_mentioned = f'@{bot_username}' in text
        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            update.message.reply_to_message.from_user.username == bot_username
        )
        if not is_mentioned and not is_reply_to_bot:
            return
        text = text.replace(f'@{bot_username}', '').strip()
        if not text:
            return
    else:
        text = update.message.text

    profile = await sync_to_async(get_profile_for_telegram)(tg_user.id)

    if not profile:
        await update.message.reply_text(
            "⚠️ Твій Telegram не прив'язаний до акаунту Minerva.\n"
            f"ID: `{tg_user.id}` — передай адміністратору.",
            parse_mode='Markdown',
        )
        return

    ai_enabled = await sync_to_async(lambda: profile.ai_enabled)()
    if not ai_enabled:
        await update.message.reply_text('🔒 AI-асистент для тебе вимкнений.')
        return

    await update.message.reply_chat_action('typing')

    channel = 'telegram_group' if is_group else 'telegram_private'
    try:
        reply = await sync_to_async(chat)(
            user_text=text,
            profile=profile,
            channel=channel,
            telegram_chat_id=str(update.effective_chat.id),
        )
    except Exception:
        logger.exception("chat() error")
        reply = 'Щось пішло не так. Спробуй пізніше. 🏛️'

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
