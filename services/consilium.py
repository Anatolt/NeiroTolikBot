import logging
import asyncio
import re
from typing import List, Dict, Optional
from config import BOT_CONFIG
from services.generation import generate_text, _resolve_user_model_keyword, fetch_models_data, _is_free_pricing

logger = logging.getLogger(__name__)

async def parse_models_from_message(text: str) -> List[str]:
    """
    Парсит список моделей из сообщения пользователя.
    
    Примеры:
    - "консилиум через chatgpt, claude, deepseek: вопрос" -> ["openai/gpt-4-turbo", "anthropic/claude-3-haiku", "deepseek/deepseek-r1-distill-qwen-14b"]
    - "консилиум chatgpt claude" -> ["openai/gpt-4-turbo", "anthropic/claude-3-haiku"]
    - "консилиум: вопрос" -> [] (автоматический выбор)
    """
    text_lower = text.lower().strip()
    
    # Ищем паттерн "консилиум через ..." или "консилиум ..." или "/consilium ..."
    # Убираем "консилиум" или "/consilium" из начала
    if text_lower.startswith("консилиум"):
        remaining = text[9:].strip()  # Убираем "консилиум" (9 символов)
    elif text_lower.startswith("/consilium"):
        remaining = text[10:].strip()  # Убираем "/consilium" (10 символов)
    else:
        return []
    
    # Если есть "через", берем текст после него
    if remaining.lower().startswith("через"):
        remaining = remaining[5:].strip()  # Убираем "через"
    
    # Если после "через" ничего нет или сразу идет двоеточие, возвращаем пустой список
    if not remaining or remaining.startswith(":"):
        return []
    
    # Извлекаем список моделей до двоеточия (если есть)
    if ":" in remaining:
        models_part = remaining.split(":", 1)[0].strip()
    else:
        # Если двоеточия нет, пытаемся найти модели в начале
        # Берем первые слова до пробела или запятой
        models_part = remaining
    
    # Разбиваем на модели по запятой или пробелу
    models_raw = re.split(r'[,;]\s*|\s+', models_part)
    models_raw = [m.strip() for m in models_raw if m.strip()]
    
    # Разрешаем каждую модель через _resolve_user_model_keyword
    resolved_models = []
    for model_keyword in models_raw:
        resolved = _resolve_user_model_keyword(model_keyword)
        if resolved:
            resolved_models.append(resolved)
        else:
            # Если не удалось разрешить, пробуем использовать как есть
            logger.warning(f"Could not resolve model keyword: {model_keyword}")
            # Можно добавить проверку, что это валидный ID модели
    
    return resolved_models


async def select_default_consilium_models() -> List[str]:
    """
    Выбирает 3 разные бесплатные модели по умолчанию для консилиума.
    Если бесплатных моделей недостаточно, использует фолбеки.
    """
    selected_models = []
    seen = set()
    excluded = set(BOT_CONFIG.get("EXCLUDED_MODELS", []))
    
    # Получаем список моделей из каталога или API
    models_data = BOT_CONFIG.get("MODEL_CATALOG") or []
    
    # Если каталог пуст, пытаемся получить модели из API
    if not models_data:
        try:
            models_data = await fetch_models_data()
            if models_data:
                # Фильтруем исключенные модели
                models_data = [m for m in models_data if m.get("id") not in excluded]
        except Exception as e:
            logger.warning(f"Failed to fetch models data: {e}")
            models_data = []
    
    # Фильтруем бесплатные модели
    free_models = []
    for model in models_data:
        model_id = model.get("id", "")
        if model_id in excluded:
            continue
        
        # Проверяем, является ли модель бесплатной
        pricing = model.get("pricing", {}) if isinstance(model.get("pricing"), dict) else {}
        prompt_price = pricing.get("prompt")
        is_free = ":free" in model_id or _is_free_pricing(prompt_price)
        
        if is_free:
            free_models.append(model)
    
    # Сортируем бесплатные модели по длине контекста (по убыванию)
    free_models.sort(key=lambda m: m.get("context_length", 0) or 0, reverse=True)
    
    # Выбираем 3 разные бесплатные модели
    for model in free_models:
        model_id = model.get("id", "")
        if model_id and model_id not in seen:
            selected_models.append(model_id)
            seen.add(model_id)
            if len(selected_models) >= 3:
                break
    
    # Если бесплатных моделей недостаточно, добавляем фолбеки
    if len(selected_models) < 3:
        fallback_models = BOT_CONFIG.get("FALLBACK_MODELS", [])
        for model in fallback_models:
            if len(selected_models) >= 3:
                break
            if model and model not in seen and model not in excluded:
                selected_models.append(model)
                seen.add(model)
    
    # Если все еще недостаточно, добавляем другие бесплатные модели из MODELS
    if len(selected_models) < 3:
        for key, model_id in BOT_CONFIG.get("MODELS", {}).items():
            if len(selected_models) >= 3:
                break
            if model_id and model_id not in seen and model_id not in excluded:
                # Проверяем, является ли модель бесплатной
                if ":free" in model_id:
                    selected_models.append(model_id)
                    seen.add(model_id)
    
    # Если все еще недостаточно, добавляем любые модели из MODELS (не только бесплатные)
    if len(selected_models) < 3:
        for key, model_id in BOT_CONFIG.get("MODELS", {}).items():
            if len(selected_models) >= 3:
                break
            if model_id and model_id not in seen and model_id not in excluded:
                selected_models.append(model_id)
                seen.add(model_id)
    
    return selected_models[:3]  # Возвращаем максимум 3 модели


async def generate_single_model_response(
    prompt: str,
    model: str,
    chat_id: Optional[str],
    user_id: Optional[str],
    timeout: int = 60
) -> Dict:
    """
    Генерирует ответ от одной модели с таймаутом.
    Возвращает словарь с результатом или ошибкой.
    """
    try:
        # Добавляем инструкции о краткости и без markdown
        enhanced_prompt = prompt + "\n\nВАЖНО: Отвечай кратко (2-4 предложения, максимум 100-150 слов). Не используй markdown разметку (**, ###, ``` и т.д.) - пиши простым текстом. Отвечай по существу вопроса."
        response, used_model = await asyncio.wait_for(
            generate_text(enhanced_prompt, model, chat_id, user_id),
            timeout=timeout
        )
        return {
            "model": used_model,
            "response": response,
            "success": True,
            "error": None
        }
    except asyncio.TimeoutError:
        logger.error(f"Timeout generating response from model {model}")
        return {
            "model": model,
            "response": None,
            "success": False,
            "error": "Превышено время ожидания ответа"
        }
    except Exception as e:
        logger.error(f"Error generating response from model {model}: {str(e)}")
        return {
            "model": model,
            "response": None,
            "success": False,
            "error": str(e)[:100]  # Ограничиваем длину ошибки
        }


async def generate_consilium_responses(
    prompt: str,
    models: List[str],
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None
) -> List[Dict]:
    """
    Параллельно генерирует ответы от нескольких моделей.
    
    Args:
        prompt: Текст запроса пользователя
        models: Список моделей для запроса
        chat_id: ID чата (опционально)
        user_id: ID пользователя (опционально)
    
    Returns:
        Список словарей с результатами для каждой модели
    """
    if not models:
        logger.warning("No models provided for consilium")
        return []
    
    timeout = BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("TIMEOUT_PER_MODEL", 60)
    
    # Создаем задачи для параллельного выполнения
    tasks = [
        generate_single_model_response(prompt, model, chat_id, user_id, timeout)
        for model in models
    ]
    
    # Выполняем все задачи параллельно
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Обрабатываем результаты и исключения
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Exception in consilium task for model {models[i]}: {str(result)}")
            processed_results.append({
                "model": models[i],
                "response": None,
                "success": False,
                "error": f"Исключение: {str(result)[:100]}"
            })
        else:
            processed_results.append(result)
    
    return processed_results


def _remove_markdown(text: str) -> str:
    """
    Удаляет markdown разметку из текста.
    
    Args:
        text: Текст с markdown разметкой
    
    Returns:
        Текст без markdown разметки
    """
    if not text:
        return text
    
    # Удаляем заголовки (###, ##, #)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    
    # Удаляем жирный текст (**текст**, __текст__)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    
    # Удаляем курсив (*текст*, _текст_)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'\1', text)
    text = re.sub(r'(?<!_)_([^_]+)_(?!_)', r'\1', text)
    
    # Удаляем код блоки (```код```)
    text = re.sub(r'```[\s\S]*?```', '', text)
    
    # Удаляем инлайн код (`код`)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    
    # Удаляем горизонтальные линии (---, ***)
    text = re.sub(r'^[-*]{3,}$', '', text, flags=re.MULTILINE)
    
    # Удаляем ссылки [текст](url)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    
    # Удаляем лишние пробелы и переносы строк
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    
    return text


def format_consilium_results(results: List[Dict], execution_time: float = None) -> List[str]:
    """
    Форматирует результаты консилиума для отправки пользователю.
    
    Args:
        results: Список результатов от моделей
        execution_time: Время выполнения в секундах (опционально)
    
    Returns:
        Список сообщений для отправки (первое - заголовок, остальные - ответы моделей)
    """
    if not results:
        return ["❌ Не удалось получить ответы от моделей."]
    
    messages = []
    
    # Первое сообщение - заголовок с временем выполнения
    header = "🏥 Консилиум моделей"
    if execution_time is not None and BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SHOW_TIMING", True):
        header += f"\n⏱ Время выполнения: {execution_time:.1f} сек"
    messages.append(header)
    
    # Каждый ответ модели - отдельное сообщение
    for result in results:
        model = result.get("model", "unknown")
        success = result.get("success", False)
        
        if success:
            response = result.get("response", "")
            if response:
                # Удаляем markdown и форматируем
                clean_response = _remove_markdown(response)
                messages.append(f"🤖 {model}:\n\n{clean_response}")
            else:
                messages.append(f"🤖 {model}:\n\n⚠️ Получен пустой ответ")
        else:
            error = result.get("error", "Неизвестная ошибка")
            messages.append(f"🤖 {model}:\n\n❌ Ошибка: {error}")
    
    return messages


def extract_prompt_from_consilium_message(text: str) -> str:
    """
    Извлекает промпт из сообщения с консилиумом.
    
    Примеры:
    - "консилиум: какая погода?" -> "какая погода?"
    - "консилиум через chatgpt, claude: объясни квантовую физику" -> "объясни квантовую физику"
    - "консилиум chatgpt claude какая погода" -> "какая погода"
    """
    text_lower = text.lower().strip()
    
    if not text_lower.startswith("консилиум") and not text_lower.startswith("/consilium"):
        return text
    
    # Убираем "консилиум" или "/consilium" из начала
    if text_lower.startswith("консилиум"):
        remaining = text[9:].strip()  # Убираем "консилиум" (9 символов)
    else:
        remaining = text[10:].strip()  # Убираем "/consilium" (10 символов)
    
    # Если есть "через", убираем его
    if remaining.lower().startswith("через"):
        remaining = remaining[5:].strip()
    
    # Если есть двоеточие, берем текст после него
    if ":" in remaining:
        return remaining.split(":", 1)[1].strip()
    
    # Если нет двоеточия, пытаемся найти промпт после списка моделей
    # Это сложнее, так как нужно определить, где заканчиваются модели
    # Для простоты, если нет двоеточия, возвращаем весь текст после "консилиум"
    # Пользователь должен использовать двоеточие для явного указания промпта
    
    # Если в тексте есть известные модели, пытаемся найти промпт после них
    models_keywords = list(BOT_CONFIG.get("MODELS", {}).keys())
    words = remaining.split()
    
    # Ищем последнее вхождение ключевого слова модели
    last_model_index = -1
    for i, word in enumerate(words):
        if word.lower() in [kw.lower() for kw in models_keywords]:
            last_model_index = i
    
    # Если нашли модели, берем текст после них
    if last_model_index >= 0:
        prompt_words = words[last_model_index + 1:]
        return " ".join(prompt_words).strip()
    
    # Если не нашли модели или промпт, возвращаем весь текст
    return remaining
