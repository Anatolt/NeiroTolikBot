import re
from telegram import Update, BotCommand
from telegram.ext import Application, ContextTypes
import logging

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Экранирование специальных символов для MarkdownV2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def post_init(application: Application) -> None:
    """Инициализация команд бота после запуска."""
    await application.bot.set_my_commands([
        BotCommand("start", "Начать диалог"),
    ])
    logger.info("Bot commands set.") 