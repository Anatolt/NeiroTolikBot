from telegram import Update
from telegram.ext import ContextTypes

from config import BOT_CONFIG
from services.memory import get_routing_mode, set_routing_mode


def _format_routing_mode_label(mode: str) -> str:
    return "алгоритмический" if mode == "rules" else "LLM"


async def routing_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает алгоритмический роутер для пользователя."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_routing_mode(chat_id, user_id, "rules")
    await update.message.reply_text(
        "🔀 Включён алгоритмический роутинг. Чтобы вернуться к LLM, используйте /rout_llm или напишите 'роутинг ллм'."
    )


async def routing_llm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает LLM роутер для пользователя."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_routing_mode(chat_id, user_id, "llm")
    await update.message.reply_text(
        "🔀 Включён LLM роутинг. Чтобы вернуться к алгоритмам, используйте /rout_algo или напишите 'роутинг алгоритмами'."
    )


async def routing_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текущий режим роутинга для пользователя."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    current_mode = get_routing_mode(chat_id, user_id) or BOT_CONFIG.get("ROUTING_MODE", "rules")
    await update.message.reply_text(
        "🔎 Текущий режим роутинга: "
        f"{_format_routing_mode_label(current_mode)}.\n"
        "Переключение: /rout_algo (алгоритмы), /rout_llm (LLM)."
    )
