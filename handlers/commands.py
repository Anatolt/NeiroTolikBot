import logging
import time
from telegram import Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from config import BOT_CONFIG
from services.memory import start_new_dialog, clear_memory, add_message
from services.generation import CATEGORY_TITLES, build_models_messages
from services.consilium import (
    parse_models_from_message,
    select_default_consilium_models,
    generate_consilium_responses,
    format_consilium_results,
    extract_prompt_from_consilium_message,
)

logger = logging.getLogger(__name__)

MODELS_HINT_TEXT = (
    "🤖 Списки моделей по категориям:\n"
    "• /models_free — бесплатные\n"
    "• /models_paid — платные\n"
    "• /models_large_context — с большим контекстом\n"
    "• /models_specialized — специализированные\n"
    "• /models_all — полный список (может быть длинным)\n\n"
    "Можно также написать: 'покажи бесплатные модели', 'покажи платные модели' и т.д."
)

ADMIN_SESSIONS: set[tuple[str, str]] = set()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    default_model_escaped = escape_markdown_v2(BOT_CONFIG["DEFAULT_MODEL"])

    text = (
        f"Привет, {user_mention}\\! Я бот\\-помощник\\.\n\n"
        f"📝 Спроси меня что\\-нибудь, и я отвечу с помощью `{default_model_escaped}`\\.\n"
        f"🎨 Попроси нарисовать картинку \\(например, 'нарисуй закат над морем'\\)\\.\n"
        f"🤖 Хочешь ответ от другой модели? Укажи ее в конце запроса \\(например, '\\.\\.\\. через deepseek', '\\.\\.\\. via claude'\\) или в начале \\(например, 'chatgpt какой сегодня день?'\\)\\.\n"
        f"   Сейчас поддерживаются: deepseek, chatgpt, claude\\.\n\n"
        f"🔄 Используй /new для начала нового диалога \\(сохраняет историю\\)\\.\n"
        f"🧹 Используй /clear для полной очистки памяти\\.\n"
        f"❓ Используй /help для получения справки\\."
    )

    await update.message.reply_markdown_v2(text=text)

async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /new - начало нового диалога."""
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    
    # Начинаем новый диалог, сохраняя историю для будущей суммаризации
    session_id = start_new_dialog(chat_id, user_id)
    
    user_mention = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        f"Привет, {user_mention}\\! Начинаю новый диалог\\.\n"
        f"История нашего общения сохранена и может быть использована в будущем\\."
    )

async def clear_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /clear - полная очистка памяти."""
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    
    # Полностью очищаем память
    clear_memory(chat_id, user_id)
    
    user_mention = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        f"{user_mention}, память полностью очищена\\.\n"
        f"Начинаю диалог с чистого листа\\."
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает пароль и включает режим админа."""
    if not BOT_CONFIG.get("ADMIN_PASS"):
        await update.message.reply_text("Пароль администратора не задан.")
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    if (chat_id, user_id) in ADMIN_SESSIONS or context.user_data.get("is_admin"):
        await update.message.reply_text(
            f"Уже в режиме админа. Бот запущен: {BOT_CONFIG.get('BOOT_TIME')}"
        )
        return

    context.user_data["awaiting_admin_pass"] = True
    await update.message.reply_text("Введите пароль администратора:")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help - справка по командам."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    
    text = (
        f"Привет, {user_mention}\\! Вот список доступных команд:\n\n"
        f"📝 /new \\- Начать новый диалог \\(сохраняет историю для будущего использования\\)\n"
        f"🧹 /clear \\- Полностью очистить память бота\n"
        f"❓ /help \\- Показать эту справку\n"
        f"🤖 /models \\- Подсказка по спискам моделей\n"
        f"   /models_free, /models_paid, /models_large_context, /models_specialized\n"
        f"   /models_all — полный список моделей\n"
        f"🏥 /consilium \\- Получить ответы от нескольких моделей одновременно\n\n"
        f"Также вы можете:\n"
        f"• Задавать вопросы боту\n"
        f"• Просить нарисовать картинки\n"
        f"• Указывать модель для ответа \\(например, 'chatgpt расскажи о погоде'\\)\n"
        f"• Использовать консилиум: 'консилиум: ваш вопрос' или 'консилиум через chatgpt, claude: вопрос'\n"
        f"• Написать 'модели' или 'models' для просмотра списка моделей"
    )
    
    await update.message.reply_markdown_v2(text=text)

async def _send_models(update: Update, order: list[str], header: str, max_items: int | None = 20) -> None:
    """Получает модели и отправляет пользователю списком."""

    messages = await build_models_messages(order, header=header, max_items_per_category=max_items)

    if not messages:
        await update.message.reply_text("Не удалось получить список моделей. Пожалуйста, попробуйте позже.")
        return

    for part in messages:
        await update.message.reply_text(part)


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /models - показывает подсказку по спискам моделей."""
    await update.message.reply_text(MODELS_HINT_TEXT)


async def models_free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает бесплатные модели."""
    await _send_models(update, ["free"], CATEGORY_TITLES["free"], max_items=20)


async def models_paid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает платные модели."""
    await _send_models(update, ["paid"], CATEGORY_TITLES["paid"], max_items=20)


async def models_large_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели с большим контекстом."""
    await _send_models(update, ["large_context"], CATEGORY_TITLES["large_context"], max_items=20)


async def models_specialized_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает специализированные модели."""
    await _send_models(update, ["specialized"], CATEGORY_TITLES["specialized"], max_items=20)


async def models_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает полный список моделей по категориям."""
    await _send_models(update, ["free", "large_context", "specialized", "paid"], MODELS_HINT_TEXT, max_items=None)


async def consilium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /consilium - одновременный запрос к нескольким моделям."""
    message = update.message
    if not message or not message.text:
        return
    
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    
    # Извлекаем текст команды (убираем "/consilium")
    command_text = message.text[10:].strip() if message.text.startswith("/consilium") else message.text.strip()
    
    # Если команда без аргументов, показываем справку
    if not command_text:
        help_text = (
            "🏥 Консилиум моделей\n\n"
            "Получите ответы от нескольких моделей одновременно.\n\n"
            "Использование:\n"
            "• /consilium ваш вопрос — автоматический выбор 3 моделей\n"
            "• /consilium через chatgpt, claude, deepseek: ваш вопрос — указанные модели\n"
            "• консилиум: ваш вопрос — через текст\n"
            "• консилиум через chatgpt, claude: ваш вопрос — через текст с моделями\n\n"
            "Примеры:\n"
            "• /consilium какая погода в Москве?\n"
            "• /consilium через chatgpt, claude: объясни квантовую физику"
        )
        await message.reply_text(help_text)
        return
    
    # Формируем полный текст для парсинга
    full_text = f"консилиум {command_text}"
    
    # Парсим модели из сообщения
    models = await parse_models_from_message(full_text)
    
    # Если модели не указаны, выбираем по умолчанию
    if not models:
        models = select_default_consilium_models()
        if not models:
            await message.reply_text("❌ Не удалось выбрать модели для консилиума. Попробуйте указать модели явно.")
            return
    
    # Извлекаем промпт
    prompt = extract_prompt_from_consilium_message(full_text)
    
    if not prompt:
        await message.reply_text("❌ Не указан вопрос для консилиума. Используйте: /consilium ваш вопрос")
        return
    
    # Отправляем сообщение о начале генерации
    status_message = await message.reply_text(f"🏥 Генерирую ответы от {len(models)} моделей...")
    
    # Добавляем запрос в историю (один раз)
    if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
        add_message(chat_id, user_id, "user", models[0], prompt)
    
    # Засекаем время
    start_time = time.time()
    
    # Генерируем ответы параллельно
    results = await generate_consilium_responses(prompt, models, chat_id, user_id)
    
    # Вычисляем время выполнения
    execution_time = time.time() - start_time
    
    # Форматируем результаты (теперь возвращает список сообщений)
    formatted_messages = format_consilium_results(results, execution_time)
    
    # Удаляем сообщение о статусе
    try:
        await status_message.delete()
    except Exception as e:
        logger.warning(f"Could not delete status message: {e}")
    
    # Сохраняем ответы в историю (если включено)
    if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
        for result in results:
            if result.get("success") and result.get("response"):
                add_message(chat_id, user_id, "assistant", result.get("model"), result.get("response"))
    
    # Отправляем каждое сообщение отдельно
    max_length = 4000
    for msg in formatted_messages:
        # Если сообщение слишком длинное, разбиваем его
        if len(msg) > max_length:
            # Разбиваем на части
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
            
            # Отправляем части
            for i, part in enumerate(parts):
                if i == 0:
                    await message.reply_text(part)
                else:
                    await message.reply_text(f"*(продолжение {i+1}/{len(parts)})*\n\n{part}", parse_mode="Markdown")
        else:
            await message.reply_text(msg)
