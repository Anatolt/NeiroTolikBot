import os
import re
from telegram import Update, BotCommand
from telegram.ext import Application, ContextTypes
import logging
from pathlib import Path
from services.memory import get_all_admins
from config import BOT_CONFIG

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Экранирование специальных символов для MarkdownV2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def resolve_system_prompt(base_dir: Path) -> str:
    """
    Возвращает системный промпт, поддерживая загрузку из файла.

    Приоритет:
    1) Прямое значение CUSTOM_SYSTEM_PROMPT.
       Если оно записано как "$(cat file.txt)", то прочитаем указанный файл.
    2) Файл в CUSTOM_SYSTEM_PROMPT_FILE (если задан).
    3) Файл neiro-tolik-promt.txt рядом с приложением (если существует).
    4) Базовый промпт.
    """
    base_dir = Path(base_dir)

    def read_prompt_file(path_str: str | None) -> str | None:
        if not path_str:
            return None
        path = Path(path_str)
        if not path.is_absolute():
            path = base_dir / path
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.info(f"Prompt file not found: {path}")
        except Exception as exc:  # pragma: no cover - защитный блок
            logger.warning(f"Failed to read prompt file {path}: {exc}")
        return None

    env_prompt = os.getenv("CUSTOM_SYSTEM_PROMPT")
    prompt_file = os.getenv("CUSTOM_SYSTEM_PROMPT_FILE")

    if env_prompt:
        # Поддержка записи вида $(cat my_prompt.txt)
        shell_like = env_prompt.strip()
        if shell_like.startswith("$(") and shell_like.endswith(")"):
            inner = shell_like[2:-1].strip()
            if inner.startswith("cat "):
                cat_target = inner[4:].strip()
                file_prompt = read_prompt_file(cat_target)
                if file_prompt:
                    return file_prompt
        return env_prompt

    file_prompt = read_prompt_file(prompt_file) or read_prompt_file("neiro-tolik-promt.txt")
    if file_prompt:
        return file_prompt

    return "You are a helpful assistant."

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
