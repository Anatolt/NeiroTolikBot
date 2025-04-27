import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from config import BOT_CONFIG
from services.memory import start_new_dialog, clear_memory
from services.generation import client, init_client

logger = logging.getLogger(__name__)

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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help - справка по командам."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    
    text = (
        f"Привет, {user_mention}\\! Вот список доступных команд:\n\n"
        f"📝 /new \\- Начать новый диалог \\(сохраняет историю для будущего использования\\)\n"
        f"🧹 /clear \\- Полностью очистить память бота\n"
        f"❓ /help \\- Показать эту справку\n"
        f"🤖 /models \\- Показать список доступных моделей\n\n"
        f"Также вы можете:\n"
        f"• Задавать вопросы боту\n"
        f"• Просить нарисовать картинки\n"
        f"• Указывать модель для ответа \\(например, 'chatgpt расскажи о погоде'\\)\n"
        f"• Написать 'модели' или 'models' для просмотра списка моделей"
    )
    
    await update.message.reply_markdown_v2(text=text)

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /models - показывает список доступных моделей."""
    try:
        client = init_client()
        logger.info("Fetching models list from OpenRouter")
        response = await client.models.list()
        
        # Проверяем структуру ответа
        if not response:
            logger.error("Empty response from OpenRouter API")
            await update.message.reply_text("Не удалось получить список моделей. Пустой ответ от API.")
            return
            
        # Безопасное извлечение данных
        models_data = []
        if hasattr(response, 'data'):
            models_data = response.data
        elif isinstance(response, list):
            models_data = response
        else:
            logger.error(f"Unexpected response format from OpenRouter: {response}")
            await update.message.reply_text("Не удалось получить список моделей. Неожиданный формат ответа от API.")
            return
        
        # Группируем модели по категориям
        free_models = []
        paid_models = []
        large_context_models = []
        specialized_models = []
        
        for model in models_data:
            # Преобразуем модель в словарь, если она является объектом
            model_data = model if isinstance(model, dict) else model.model_dump()
            
            # Извлекаем данные
            model_id = model_data.get('id', 'Unknown')
            context_length = model_data.get('context_length', 0)
            
            # Форматируем контекст в КБ
            context_kb = context_length / 1024 if context_length else 0
            context_str = f"{context_kb:.0f}K" if context_kb > 0 else 'N/A'
            
            # Создаем строку с информацией о модели
            model_info = f"• {model_id} ({context_str})"
            
            # Определяем категорию модели
            if ':free' in model_id:
                free_models.append(model_info)
            elif context_length >= 100000:  # Модели с контекстом >= 100K
                large_context_models.append(model_info)
            elif any(tag in model_id.lower() for tag in ['instruct', 'coding', 'research', 'solidity']):
                specialized_models.append(model_info)
            else:
                paid_models.append(model_info)
        
        # Формируем сообщение
        message = "🤖 Доступные модели:\n\n"
        
        if free_models:
            message += "БЕСПЛАТНЫЕ МОДЕЛИ:\n"
            message += "\n".join(free_models) + "\n\n"
            
        if large_context_models:
            message += "МОДЕЛИ С БОЛЬШИМ КОНТЕКСТОМ (>100K):\n"
            message += "\n".join(large_context_models) + "\n\n"
            
        if specialized_models:
            message += "СПЕЦИАЛИЗИРОВАННЫЕ МОДЕЛИ:\n"
            message += "\n".join(specialized_models) + "\n\n"
            
        if paid_models:
            message += "ПЛАТНЫЕ МОДЕЛИ:\n"
            message += "\n".join(paid_models) + "\n\n"
        
        # Разбиваем сообщение на части, если оно слишком длинное
        max_length = 3000
        message_parts = [message[i:i+max_length] for i in range(0, len(message), max_length)]
        
        for part in message_parts:
            await update.message.reply_text(part)
            
        logger.info("Models list sent successfully")
    except Exception as e:
        logger.error(f"Error fetching models list: {str(e)}")
        await update.message.reply_text("Не удалось получить список моделей. Пожалуйста, попробуйте позже.") 