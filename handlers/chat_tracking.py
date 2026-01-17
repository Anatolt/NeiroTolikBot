import logging
from telegram import Update
from telegram.ext import ContextTypes
from services.memory import upsert_telegram_chat, upsert_user_profile

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

    user = update.effective_user
    if user:
        user_name = user.username or user.full_name
        try:
            upsert_user_profile("telegram", str(chat.id), str(user.id), user_name)
        except Exception as exc:
            logger.debug("Failed to track user profile %s: %s", user.id, exc)
