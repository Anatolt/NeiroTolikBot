import logging
import asyncio
import re
from typing import List, Dict, Optional
from config import BOT_CONFIG
from services.generation import (
    generate_text,
    _resolve_user_model_keyword,
    fetch_models_data,
    _is_free_pricing,
    _prepare_messages,
)

logger = logging.getLogger(__name__)
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def _is_model_token(token: str) -> bool:
    return bool(_MODEL_TOKEN_RE.match(token))


def _split_models_and_prompt(remaining: str) -> tuple[List[str], str]:
    models_part, prompt = remaining.split(":", 1)
    prompt = prompt.strip()
    tokens = re.split(r"[,;]\s*|\s+", models_part.strip())
    models = [t for t in (token.strip() for token in tokens) if t and _is_model_token(t)]
    return models, prompt


def _extract_consilium_remaining(text: str) -> str | None:
    text_lower = text.lower().strip()
    if text_lower.startswith("консилиум"):
        remaining = text[9:].strip()
    elif text_lower.startswith("/consilium"):
        remaining = text[10:].strip()
    else:
        return None

    if remaining.lower().startswith("через"):
        remaining = remaining[5:].strip()

    return remaining


def parse_consilium_request(text: str) -> tuple[List[str], str, bool]:
    remaining = _extract_consilium_remaining(text)
    if remaining is None:
        return [], "", False
    
    if ":" not in remaining:
        # Если двоеточия нет, считаем все оставшееся промптом, а модели выберутся автоматически
        return [], remaining.strip(), True

    models_raw, prompt = _split_models_and_prompt(remaining)

    resolved_models = []
    for model_keyword in models_raw:
        resolved = _resolve_user_model_keyword(model_keyword)
        if resolved:
            resolved_models.append(resolved)
        else:
            # Если не удалось разрешить, используем как есть, чтобы призвать все перечисленные модели.
            logger.warning(f"Could not resolve model keyword, using as-is: {model_keyword}")
            resolved_models.append(model_keyword)

    return resolved_models, prompt, True

async def parse_models_from_message(text: str) -> List[str]:
    """
    Парсит список моделей из сообщения пользователя.
    
    Примеры:
    - "консилиум через chatgpt, claude, deepseek: вопрос" -> ["openai/gpt-4-turbo", "anthropic/claude-3-haiku", "deepseek/deepseek-r1-distill-qwen-14b"]
    - "консилиум chatgpt claude" -> ["openai/gpt-4-turbo", "anthropic/claude-3-haiku"]
    - "консилиум: вопрос" -> [] (автоматический выбор)
    """
    models, _prompt, has_colon = parse_consilium_request(text)
    if not has_colon:
        return []
    return models


async def select_default_consilium_models() -> List[str]:
    """
    Выбирает 3 разные бесплатные модели по умолчанию для консилиума.
    Если бесплатных моделей недостаточно, использует фолбеки.
    """
    selected_models: list[str] = []
    seen = set()
    excluded = set(BOT_CONFIG.get("EXCLUDED_MODELS", []))

    priority_order = BOT_CONFIG.get("PREFERRED_MODEL_ORDER", [])
    for alias in priority_order:
        resolved = _resolve_user_model_keyword(alias)
        if resolved and resolved not in seen and resolved not in excluded:
            selected_models.append(resolved)
            seen.add(resolved)
        if len(selected_models) >= 3:
            break

    # Если по приоритету не набрали достаточно, используем старую стратегию бесплатных моделей
    if len(selected_models) < 3:
        models_data = BOT_CONFIG.get("MODEL_CATALOG") or []

        if not models_data:
            try:
                models_data = await fetch_models_data()
                if models_data:
                    models_data = [m for m in models_data if m.get("id") not in excluded]
            except Exception as e:
                logger.warning(f"Failed to fetch models data: {e}")
                models_data = []

        free_models = []
        for model in models_data:
            model_id = model.get("id", "")
            if model_id in excluded:
                continue

            pricing = model.get("pricing", {}) if isinstance(model.get("pricing"), dict) else {}
            prompt_price = pricing.get("prompt")
            is_free = ":free" in model_id or _is_free_pricing(prompt_price)

            if is_free:
                free_models.append(model)

        free_models.sort(key=lambda m: m.get("context_length", 0) or 0, reverse=True)

        for model in free_models:
            model_id = model.get("id", "")
            if model_id and model_id not in seen:
                selected_models.append(model_id)
                seen.add(model_id)
                if len(selected_models) >= 3:
                    break

    # Если все еще недостаточно, добавляем фолбеки
    if len(selected_models) < 3:
        for model in BOT_CONFIG.get("FALLBACK_MODELS", []):
            if len(selected_models) >= 3:
                break
            if model and model not in seen and model not in excluded:
                selected_models.append(model)
                seen.add(model)

    # Если все еще недостаточно, добавляем любые бесплатные модели из MODELS
    if len(selected_models) < 3:
        for model_id in BOT_CONFIG.get("MODELS", {}).values():
            if len(selected_models) >= 3:
                break
            if model_id and model_id not in seen and model_id not in excluded and ":free" in model_id:
                selected_models.append(model_id)
                seen.add(model_id)

    # Если все еще недостаточно, добавляем любые модели из MODELS (не только бесплатные)
    if len(selected_models) < 3:
        for model_id in BOT_CONFIG.get("MODELS", {}).values():
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
    platform: Optional[str] = None,
    timeout: int = 60
) -> Dict:
    """
    Генерирует ответ от одной модели с таймаутом.
    Возвращает словарь с результатом или ошибкой.
    """
    try:
        enhanced_prompt = prompt + "\n\nВАЖНО: Отвечай кратко (2-4 предложения, максимум 100-150 слов). Не используй markdown разметку (**, ###, ``` и т.д.) - пиши простым текстом. Отвечай по существу вопроса."

        prepared_messages, guard_info = await _prepare_messages(
            enhanced_prompt, model, chat_id, user_id, None
        )

        response, used_model, context_info = await asyncio.wait_for(
            generate_text(
                enhanced_prompt,
                model,
                chat_id,
                user_id,
                prepared_messages=prepared_messages,
                context_info=guard_info,
                platform=platform,
            ),
            timeout=timeout
        )
        return {
            "model": used_model,
            "response": response,
            "success": True,
            "error": None,
            "context_notice": context_info,
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
    user_id: Optional[str] = None,
    platform: Optional[str] = None,
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

    # Избавляемся от дублей, чтобы одна и та же модель не отвечала дважды
    unique_models: list[str] = []
    seen: set[str] = set()
    for model in models:
        if model in seen:
            continue
        unique_models.append(model)
        seen.add(model)

    if len(unique_models) != len(models):
        logger.info(
            "Removed duplicate models from consilium request: %s -> %s",
            models,
            unique_models,
        )
    models = unique_models
    
    timeout = BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("TIMEOUT_PER_MODEL", 60)
    
    # Создаем задачи для параллельного выполнения
    tasks = [
        generate_single_model_response(prompt, model, chat_id, user_id, platform, timeout)
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
                notice = ""
                context_info = result.get("context_notice") or {}
                if context_info.get("summary_text"):
                    notice = "\n\nℹ️ Контекст переполнен — сделана краткая саммаризация истории."
                elif context_info.get("trimmed_from_context"):
                    notice = "\n\nℹ️ Контекст переполнен — часть старых сообщений скрыта в подготовке запроса."
                elif context_info.get("warnings"):
                    notice = "\n\nℹ️ Предупреждение о размере контекста."
                messages.append(f"🤖 {model}:\n\n{clean_response}{notice}")
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
    _models, prompt, has_colon = parse_consilium_request(text)
    if has_colon and prompt:
        return prompt
    return ""
