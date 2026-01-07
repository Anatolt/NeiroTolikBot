from telegram import Update
from telegram.ext import ContextTypes

from services.memory import is_admin


def is_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    return is_admin(chat_id, user_id) or context.user_data.get("is_admin", False)
