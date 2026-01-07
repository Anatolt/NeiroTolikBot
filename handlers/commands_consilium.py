import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from config import BOT_CONFIG
from services.analytics import log_text_usage
from services.consilium import (
    parse_consilium_request,
    select_default_consilium_models,
    generate_consilium_responses,
    format_consilium_results,
)
from services.memory import add_message

logger = logging.getLogger(__name__)


async def consilium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /consilium - одновременный запрос к нескольким моделям."""
    message = update.message
    if not message or not message.text:
        return

    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    command_text = message.text[10:].strip() if message.text.startswith("/consilium") else message.text.strip()

    if not command_text:
        help_text = (
            "🏥 Консилиум моделей\n\n"
            "Получите ответы от нескольких моделей одновременно.\n\n"
            "Использование:\n"
            "• /consilium: ваш вопрос — автоматический выбор 3 моделей\n"
            "• /consilium через chatgpt, claude, deepseek: ваш вопрос — указанные модели\n"
            "• консилиум: ваш вопрос — через текст\n"
            "• консилиум через chatgpt, claude: ваш вопрос — через текст с моделями\n\n"
            "Примеры:\n"
            "• /consilium: какая погода в Москве?\n"
            "• /consilium через chatgpt, claude: объясни квантовую физику"
        )
        await message.reply_text(help_text)
        return

    full_text = f"консилиум {command_text}"

    models, prompt, has_colon = parse_consilium_request(full_text)
    if not has_colon:
        await message.reply_text(
            "❗ Для консилиума нужен двоеточие после списка моделей.\n"
            "Пример: /consilium gpt, claude: ваш вопрос"
        )
        return

    if not prompt:
        await message.reply_text("❌ Не указан вопрос для консилиума. Используйте: /consilium модели: ваш вопрос")
        return

    if not models:
        models = await select_default_consilium_models()
        if not models:
            await message.reply_text("❌ Не удалось выбрать модели для консилиума. Попробуйте указать модели явно.")
            return

    pending = context.user_data.get("pending_consilium_requests", {})
    key = f"{chat_id}:{user_id}"
    pending[key] = {"prompt": prompt, "models": models}
    context.user_data["pending_consilium_requests"] = pending

    models_list = ", ".join(models)
    await message.reply_text(
        "🏥 Консилиум готов к запуску.\n"
        f"Модели: {models_list}\n"
        f"Вопрос: {prompt}\n"
        "Нужен ответ? /yes"
    )


async def execute_consilium_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, models: list[str]
) -> None:
    message = update.message
    if not message:
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    status_message = await message.reply_text(f"🏥 Генерирую ответы от {len(models)} моделей...")

    if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
        add_message(chat_id, user_id, "user", models[0], prompt)

    start_time = time.time()
    results = await generate_consilium_responses(prompt, models, chat_id, user_id)
    execution_time = time.time() - start_time
    formatted_messages = format_consilium_results(results, execution_time)

    try:
        await status_message.delete()
    except Exception as e:
        logger.warning("Could not delete status message: %s", e)

    if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
        for result in results:
            if result.get("success") and result.get("response"):
                add_message(chat_id, user_id, "assistant", result.get("model"), result.get("response"))

    for result in results:
        if result.get("success") and result.get("response"):
            log_text_usage(
                platform="telegram",
                chat_id=str(chat_id),
                user_id=str(user_id),
                model_id=result.get("model"),
                prompt=prompt,
                response=result.get("response"),
            )

    max_length = 4000
    for msg in formatted_messages:
        if len(msg) > max_length:
            parts = []
            current_part = ""
            lines = msg.split("\n")

            for line in lines:
                if len(current_part) + len(line) + 1 > max_length:
                    if current_part:
                        parts.append(current_part)
                    current_part = line + "\n"
                else:
                    current_part += line + "\n"

            if current_part:
                parts.append(current_part)

            for i, part in enumerate(parts):
                if i == 0:
                    await message.reply_text(part)
                else:
                    await message.reply_text(
                        f"*(продолжение {i+1}/{len(parts)})*\n\n{part}", parse_mode="Markdown"
                    )
        else:
            await message.reply_text(msg)
