import re
from telegram import Update, BotCommand
from telegram.ext import Application, ContextTypes
import logging
from services.memory import get_all_admins
from config import BOT_CONFIG

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

async def notify_admins_on_startup(application: Application) -> None:
    """Отправка уведомлений админам о перезапуске бота."""
    try:
        admins = get_all_admins()
        if not admins:
            logger.info("Нет админов для уведомления о перезапуске")
            return
            
        boot_time = BOT_CONFIG.get('BOOT_TIME', 'неизвестно')
        message_text = f"Вы админ, поэтому сообщаю, что я перезагрузился. {boot_time}"
        
        for admin in admins:
            chat_id = int(admin['chat_id'])
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=message_text
                )
                logger.info(f"Уведомление отправлено админу: chat_id={chat_id}, user_id={admin['user_id']}")
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление админу {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомлений админам: {e}") 