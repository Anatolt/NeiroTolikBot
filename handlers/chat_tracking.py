import logging
from telegram import Update
from telegram.ext import ContextTypes
from services.memory import upsert_telegram_chat

logger = logging.getLogger(__name__)


async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохраняет информацию о чате Telegram для последующего выбора."""
    chat = update.effective_chat
    if not chat:
        return

    title = getattr(chat, "title", None) or getattr(chat, "username", None)
    if not title and update.effective_user:
        title = update.effective_user.full_name

    try:
        upsert_telegram_chat(str(chat.id), title, str(chat.type) if chat.type else None)
    except Exception as exc:
        logger.debug("Failed to track chat %s: %s", chat.id, exc)
