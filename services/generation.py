import logging
import json
import asyncio
import aiohttp
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from openai import AsyncOpenAI
from config import BOT_CONFIG
from services.memory import get_history, get_user_summary, save_summary

logger = logging.getLogger(__name__)

# Глобальная переменная для клиента OpenRouter
client = None

CATEGORY_TITLES = {
    "free": "БЕСПЛАТНЫЕ МОДЕЛИ:",
    "large_context": "МОДЕЛИ С БОЛЬШИМ КОНТЕКСТОМ (≥100K):",
    "specialized": "СПЕЦИАЛИЗИРОВАННЫЕ МОДЕЛИ:",
    "paid": "ПЛАТНЫЕ МОДЕЛИ:",
}

def _get_context_guard_config() -> dict:
    defaults = {
        "DEFAULT_CONTEXT_LENGTH": 32768,
        "WARNING_RATIO": 0.8,
        "HARD_RATIO": 0.95,
        "OVERFLOW_STRATEGY": "summarize",
        "MIN_MESSAGES_TO_SUMMARIZE": 4,
        "SUMMARIZATION_MODEL": None,
        "SUMMARY_MAX_TOKENS": 256,
    }
    guard_cfg = BOT_CONFIG.get("CONTEXT_GUARD", {}) or {}
    return {**defaults, **guard_cfg}

def _get_context_length_for_model(model_id: str | None) -> int:
    guard_cfg = _get_context_guard_config()
    catalog: List[Dict[str, Any]] = BOT_CONFIG.get("MODEL_CATALOG") or []
    if model_id:
        for model in catalog:
            if (model.get("id") or "").lower() == model_id.lower():
                return int(model.get("context_length") or guard_cfg["DEFAULT_CONTEXT_LENGTH"])
    return guard_cfg["DEFAULT_CONTEXT_LENGTH"]

def _estimate_messages_size(messages: List[Dict[str, str]]) -> Tuple[int, int]:
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
    estimated_tokens = max(1, round(total_chars / 4))
    return estimated_tokens, total_chars

def init_client():
    """Инициализация клиента OpenRouter после загрузки конфигурации."""
    global client
    if client is None:
        logger.info("Initializing OpenRouter client")
        client = AsyncOpenAI(
            api_key=BOT_CONFIG["OPENROUTER_API_KEY"],
            base_url=BOT_CONFIG["OPENROUTER_BASE_URL"],
            default_headers={
                "HTTP-Referer": BOT_CONFIG["BOT_REFERER"],
                "X-Title": BOT_CONFIG["BOT_TITLE"]
            }
        )
        logger.info("OpenRouter client initialized successfully")
    return client


async def fetch_imagerouter_models() -> list[str]:
    """Получает список моделей для генерации изображений из ImageRouter."""
    url = BOT_CONFIG.get("IMAGE_ROUTER_MODELS_URL") or "https://api.imagerouter.io/v1/models"
    fallback = BOT_CONFIG.get("IMAGE_ROUTER_MODELS", []) or []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"ImageRouter models list error: {error_text} (Status: {response.status})"
                    )
                    return list(fallback)

                data = await response.json()
                if not isinstance(data, dict):
                    logger.error(f"Unexpected ImageRouter models response format: {data}")
                    return list(fallback)

                models: list[str] = []
                for model_id, details in data.items():
                    if not model_id:
                        continue
                    if isinstance(details, dict):
                        outputs = details.get("output")
                        if outputs and "image" not in outputs:
                            continue
                    models.append(model_id)

                models = sorted(set(models))
                return models
    except Exception as e:
        logger.error(f"Error fetching ImageRouter models: {str(e)}")
        return list(fallback)

async def check_model_availability(model: str) -> bool:
    """Проверка доступности модели в OpenRouter API."""
    try:
        client = init_client()
        logger.info(f"Checking availability of model: {model}")
        response = await client.models.list()
        
        if not response or not hasattr(response, 'data'):
            logger.error("Failed to get models list from OpenRouter API")
            return False
            
        # Проверяем наличие модели в списке
        for available_model in response.data:
            model_data = available_model if isinstance(available_model, dict) else available_model.model_dump()
            if model_data.get('id') == model:
                logger.info(f"Model {model} is available")
                return True
                
        logger.error(f"Model {model} is not available in OpenRouter API")
        return False
    except Exception as e:
        logger.error(f"Error checking model availability: {str(e)}")
        return False


async def fetch_models_data() -> list[dict]:
    """Получает и нормализует список моделей из OpenRouter."""
    try:
        client = init_client()
        response = await client.models.list()

        if not response:
            logger.error("Empty response while fetching models data")
            return []

        raw_models = []
        if hasattr(response, "data"):
            raw_models = response.data
        elif isinstance(response, list):
            raw_models = response
        else:
            logger.error(f"Unexpected models response format: {response}")
            return []

        normalized_models: list[dict] = []
        for model in raw_models:
            if isinstance(model, dict):
                normalized_models.append(model)
            elif hasattr(model, "model_dump"):
                normalized_models.append(model.model_dump())
            else:
                logger.warning(f"Skipping model with unknown type: {model}")

        return normalized_models
    except Exception as e:
        logger.error(f"Error fetching models data: {str(e)}")
        return []


def _is_free_pricing(prompt_price) -> bool:
    try:
        return float(prompt_price) == 0
    except (TypeError, ValueError):
        return False


def categorize_models(models_data: list[dict]) -> dict[str, list[dict]]:
    """Группирует модели по внутренним категориям."""
    categories: dict[str, list[dict]] = {
        "free": [],
        "large_context": [],
        "specialized": [],
        "paid": [],
    }

    for model in models_data:
        model_id = model.get("id", "Unknown")
        context_length = model.get("context_length", 0) or 0
        pricing = model.get("pricing", {}) if isinstance(model.get("pricing"), dict) else {}
        prompt_price = pricing.get("prompt")

        is_free = ":free" in model_id or _is_free_pricing(prompt_price)
        is_large_context = context_length >= 100_000
        is_specialized = any(
            keyword in model_id.lower()
            for keyword in ["instruct", "coding", "research", "solidity", "math"]
        )

        if is_free:
            categories["free"].append(model)
        elif is_large_context:
            categories["large_context"].append(model)
        elif is_specialized:
            categories["specialized"].append(model)
        else:
            categories["paid"].append(model)

    # Сортируем внутри категорий по длине контекста (убыванию)
    for key, models in categories.items():
        categories[key] = sorted(models, key=lambda m: m.get("context_length", 0) or 0, reverse=True)

    return categories


def format_model_list(
    categories: dict[str, list[dict]],
    order: list[str],
    category_titles: dict[str, str],
    header: str | None = "🤖 Доступные модели по категориям:\n\n",
    max_items_per_category: int | None = 20,
) -> list[str]:
    """Формирует человекочитаемый список моделей и разбивает его на части."""

    max_length = 3000
    message_parts: list[str] = []
    current_part = header or ""

    for key in order:
        models = categories.get(key, [])
        if not models:
            continue

        category_block = f"{category_titles.get(key, key)}\n"
        displayed_models = models if max_items_per_category is None else models[:max_items_per_category]

        for model in displayed_models:
            context_length = model.get("context_length", 0)
            context_kb = context_length / 1024 if context_length else 0
            context_str = f"{context_kb:.0f}K" if context_kb > 0 else "N/A"
            category_block += f"• {model.get('id', 'Unknown')} ({context_str})\n"

        if max_items_per_category is not None:
            remaining = len(models) - len(displayed_models)
            if remaining > 0:
                category_block += f"…и еще {remaining} моделей в этой категории\n"

        category_block += "\n"

        if len(current_part) + len(category_block) > max_length:
            if current_part:
                message_parts.append(current_part)
            current_part = category_block
        else:
            current_part += category_block

    if current_part:
        message_parts.append(current_part)

    return message_parts


async def build_models_messages(
    order: list[str],
    header: str | None = "🤖 Доступные модели по категориям:\n\n",
    max_items_per_category: int | None = 20,
) -> list[str]:
    """Получает список моделей и формирует сообщения для выдачи пользователю."""

    models_data = await fetch_models_data()
    if not models_data:
        return []

    categories = categorize_models(models_data)
    return format_model_list(
        categories,
        order,
        CATEGORY_TITLES,
        header=header,
        max_items_per_category=max_items_per_category,
    )


async def choose_best_free_model(models_data: list[dict] | None = None) -> str | None:
    """Определяет самую мощную бесплатную модель на основе длины контекста."""
    if models_data is None:
        models_data = await fetch_models_data()
    if not models_data:
        return None

    free_models = [
        model
        for model in models_data
        if (
            (":free" in model.get("id", "") or _is_free_pricing(model.get("pricing", {}).get("prompt")))
            and model.get("id") not in BOT_CONFIG.get("EXCLUDED_MODELS", [])
        )
    ]

    if not free_models:
        logger.warning("No free models available in OpenRouter response")
        return None

    best_model = max(free_models, key=lambda m: m.get("context_length", 0) or 0)
    best_model_id = best_model.get("id")
    logger.info(f"Selected best free model: {best_model_id}")
    return best_model_id


def _sorted_models_by_context(models_data: list[dict]) -> list[dict]:
    """Сортирует модели по длине контекста по убыванию."""
    return sorted(models_data, key=lambda m: m.get("context_length", 0) or 0, reverse=True)


def _pick_model_by_keywords(
    models_data: list[dict],
    include: list[str],
    exclude: list[str] | None = None,
) -> str | None:
    """Возвращает id модели, содержащей нужные ключевые слова, с максимальным контекстом."""
    exclude = exclude or []
    for model in _sorted_models_by_context(models_data):
        model_id = model.get("id", "")
        if model_id in BOT_CONFIG.get("EXCLUDED_MODELS", []):
            continue
        model_id_lower = model_id.lower()
        if all(key.lower() in model_id_lower for key in include) and not any(
            bad.lower() in model_id_lower for bad in exclude
        ):
            return model_id
    return None


def _resolve_user_model_keyword(keyword: str) -> str | None:
    """
    Разрешает пользовательский ключ (первая часть запроса) в id модели.
    Правила:
    - точное совпадение id
    - префиксное совпадение (берём модель с максимальным контекстом)
    - алиасы из BOT_CONFIG["MODELS"]
    """
    if not keyword:
        return None
    kw = keyword.strip().lower()
    catalog: list[dict] = BOT_CONFIG.get("MODEL_CATALOG") or []
    if not catalog:
        logger.info("Model catalog is empty, refreshing from API")
        try:
            # best effort, не падаем при ошибках
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(refresh_models_from_api())
            else:
                loop.run_until_complete(refresh_models_from_api())
            catalog = BOT_CONFIG.get("MODEL_CATALOG") or []
        except Exception as e:
            logger.error(f"Failed to refresh model catalog: {e}")

    # Точное совпадение
    for model in catalog:
        mid = (model.get("id") or "").lower()
        if mid == kw and model.get("id") not in BOT_CONFIG.get("EXCLUDED_MODELS", []):
            return model.get("id")

    # Префиксное совпадение — выбираем с максимальным контекстом
    prefix_matches = [
        m
        for m in catalog
        if (m.get("id") or "").lower().startswith(kw)
        and m.get("id") not in BOT_CONFIG.get("EXCLUDED_MODELS", [])
    ]
    if prefix_matches:
        best = max(prefix_matches, key=lambda m: m.get("context_length", 0) or 0)
        return best.get("id")

    # Алиасы
    alias = BOT_CONFIG.get("MODELS", {}).get(kw)
    if alias and alias not in BOT_CONFIG.get("EXCLUDED_MODELS", []):
        return alias

    return None


def _model_disallows_system_messages(model: str | None) -> bool:
    if not model:
        return False
    model_lower = model.lower()
    for prefix in BOT_CONFIG.get("NO_SYSTEM_MODELS", []):
        if model_lower.startswith(prefix.lower()):
            return True
    return False


def _merge_system_into_user(messages: list[dict], model: str | None) -> list[dict]:
    if not _model_disallows_system_messages(model):
        return messages

    system_texts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    if not system_texts:
        return messages

    merged_system = "\n\n".join(system_texts)
    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        return [{"role": "user", "content": merged_system}]

    for idx, msg in enumerate(non_system):
        if msg.get("role") == "user":
            non_system[idx] = {
                "role": "user",
                "content": f"{merged_system}\n\n{msg.get('content', '')}".strip(),
            }
            break
    else:
        non_system.insert(0, {"role": "user", "content": merged_system})

    return non_system


def _build_alias_map(models_data: list[dict]) -> dict[str, str]:
    """Формирует динамическое соответствие алиасов и реальных id моделей."""
    alias_map: dict[str, str] = {}

    alias_map["claude_opus"] = _pick_model_by_keywords(models_data, ["anthropic/claude", "opus"])
    alias_map["claude_sonnet"] = _pick_model_by_keywords(models_data, ["anthropic/claude", "sonnet"])
    alias_map["claude"] = alias_map.get("claude_sonnet") or alias_map.get("claude_opus") or _pick_model_by_keywords(
        models_data, ["anthropic/claude"]
    )

    # ChatGPT семейство
    alias_map["chatgpt"] = (
        _pick_model_by_keywords(models_data, ["openai/gpt-4o"], exclude=["mini"])
        or _pick_model_by_keywords(models_data, ["openai/gpt-4o"])
        or _pick_model_by_keywords(models_data, ["openai/gpt-4"], exclude=["mini"])
        or _pick_model_by_keywords(models_data, ["openai/gpt-3.5-turbo"])
    )

    alias_map["mistral"] = _pick_model_by_keywords(models_data, ["mistral"])

    llama_variant = _pick_model_by_keywords(models_data, ["meta-llama/llama", "instruct"]) or _pick_model_by_keywords(
        models_data, ["meta-llama/llama"]
    )
    alias_map["llama"] = llama_variant
    alias_map["meta"] = llama_variant

    deepseek_variant = _pick_model_by_keywords(models_data, ["deepseek", "r1"]) or _pick_model_by_keywords(
        models_data, ["deepseek"]
    )
    alias_map["deepseek"] = deepseek_variant

    alias_map["qwen"] = _pick_model_by_keywords(models_data, ["qwen", "instruct"]) or _pick_model_by_keywords(
        models_data, ["qwen"]
    )

    alias_map["gemini"] = _pick_model_by_keywords(models_data, ["gemini"]) or _pick_model_by_keywords(
        models_data, ["gemma"]
    )

    alias_map["gpt"] = _pick_model_by_keywords(models_data, ["gpt", "mini"]) or _pick_model_by_keywords(
        models_data, ["gpt"]
    )

    fimbulvetr_variant = _pick_model_by_keywords(models_data, ["fimbulvetr"])
    alias_map["fimbulvetr"] = fimbulvetr_variant
    alias_map["sao10k"] = fimbulvetr_variant

    return {k: v for k, v in alias_map.items() if v}


def _resolve_priority_models(order: list[str], alias_map: dict[str, str]) -> list[str]:
    """Преобразует приоритетный список алиасов в список id моделей без дублей."""
    resolved: list[str] = []
    for alias in order:
        candidate = alias_map.get(alias) or BOT_CONFIG.get("MODELS", {}).get(alias)
        if candidate and candidate not in BOT_CONFIG.get("EXCLUDED_MODELS", []) and candidate not in resolved:
            resolved.append(candidate)
    return resolved


def _build_fallback_models(default_model: str | None, alias_map: dict[str, str]) -> list[str]:
    """Формирует список запасных моделей с учетом алиасов."""
    priority_order = BOT_CONFIG.get("PREFERRED_MODEL_ORDER", [])
    preferred = _resolve_priority_models(priority_order, alias_map)

    if default_model and default_model not in preferred:
        preferred.insert(0, default_model)

    # default модель будет первой, остальные — очередные фолбэки
    result: list[str] = preferred[1:] if preferred else []

    # если список пустой, вернем предыдущее значение из конфигурации
    if not result:
        result.extend(BOT_CONFIG.get("FALLBACK_MODELS", []))
    return result


async def refresh_models_from_api() -> dict[str, str]:
    """
    Перезапрашивает список моделей из OpenRouter и обновляет алиасы/фолбэки.
    Возвращает словарь новых алиасов.
    """
    models_data = await fetch_models_data()
    if not models_data:
        logger.warning("Failed to refresh models from API: empty list")
        return {}

    # Сохраняем сырую витрину (фильтруя исключенные)
    BOT_CONFIG["MODEL_CATALOG"] = [
        m for m in models_data if m.get("id") not in BOT_CONFIG.get("EXCLUDED_MODELS", [])
    ]

    alias_map = _build_alias_map(models_data)

    # Обновляем алиасы в конфиге
    merged_aliases = BOT_CONFIG.get("MODELS", {}).copy()
    merged_aliases.update(alias_map)
    BOT_CONFIG["MODELS"] = merged_aliases

    # Пересчитываем модель по умолчанию и список фолбэков согласно приоритету
    priority_models = _resolve_priority_models(BOT_CONFIG.get("PREFERRED_MODEL_ORDER", []), BOT_CONFIG["MODELS"])
    if priority_models:
        BOT_CONFIG["DEFAULT_MODEL"] = priority_models[0]
        BOT_CONFIG["FALLBACK_MODELS"] = priority_models[1:]
    else:
        BOT_CONFIG["FALLBACK_MODELS"] = _build_fallback_models(BOT_CONFIG.get("DEFAULT_MODEL"), BOT_CONFIG["MODELS"])

    logger.info(
        "Models refreshed from API. Default: %s, aliases updated: %s",
        BOT_CONFIG.get("DEFAULT_MODEL"),
        {k: BOT_CONFIG['MODELS'][k] for k in alias_map.keys()},
    )

    return alias_map

def _is_model_not_found_error(error: Exception) -> bool:
    """Определяем ошибки недоступной модели (404 / No endpoints)."""
    message = str(error).lower()
    status = getattr(error, "status_code", None)
    return (
        status == 404
        or "no endpoints found" in message
        or "model_not_found" in message
        or "not found" in message
    )


def _is_rate_limit_error(error: Exception) -> bool:
    """Определяем ошибки ограничения скорости (429 / temporarily rate-limited)."""
    message = str(error).lower()
    status = getattr(error, "status_code", None)
    return status == 429 or "rate limit" in message or "temporarily rate-limited" in message

def _is_audio_required_error(error: Exception) -> bool:
    """Определяем ошибку, когда модель требует аудио."""
    message = str(error).lower()
    return "requires that either input content or output modality contain audio" in message


def _is_conversation_order_error(error: Exception) -> bool:
    """Определяем ошибку, связанную с порядком сообщений (assistant не может быть первым)."""
    message = str(error).lower()
    return "assistant messages cannot be the first non-system message" in message


def _build_models_to_try(requested_model: str | None) -> list[str]:
    """Формирует последовательность моделей для запроса с учетом фолбэков."""
    models_to_try: list[str] = []
    for candidate in [
        requested_model,
        BOT_CONFIG.get("DEFAULT_MODEL"),
        *BOT_CONFIG.get("FALLBACK_MODELS", []),
    ]:
        if candidate and candidate not in BOT_CONFIG.get("EXCLUDED_MODELS", []) and candidate not in models_to_try:
            models_to_try.append(candidate)
    return models_to_try


def _normalize_history(history: list[dict]) -> list[dict]:
    """
    Делает так, чтобы первым не-системным сообщением был user.
    Убирает ведущие assistant-сообщения в хронологическом порядке.
    """
    normalized: list[dict] = []
    found_user = False
    for msg in reversed(history):  # oldest -> newest
        role = msg.get("role")
        if not found_user:
            if role == "assistant":
                continue
            if role == "user":
                found_user = True
        normalized.append(msg)
    return normalized

async def _summarize_removed_messages(
    chat_id: str,
    user_id: str,
    removed_fragments: List[str],
    model: str | None,
) -> str | None:
    if not removed_fragments:
        return None

    guard_cfg = _get_context_guard_config()
    summarizer_model = guard_cfg.get("SUMMARIZATION_MODEL") or model or BOT_CONFIG.get("DEFAULT_MODEL")
    summary_prompt = (
        "Суммаризируй удаленные части переписки в 4-6 предложениях, сохранив ключевые факты, решения и договоренности. "
        "Пиши на русском."
    )
    history_block = "\n".join(removed_fragments)

    try:
        client = init_client()
        response = await client.chat.completions.create(
            model=summarizer_model,
            messages=[
                {"role": "system", "content": "Ты помощник, который сжимает историю диалога без потери сути."},
                {"role": "user", "content": f"История для сжатия:\n\n{history_block}\n\n{summary_prompt}"},
            ],
            max_tokens=guard_cfg.get("SUMMARY_MAX_TOKENS", 256),
            temperature=0.2,
        )
        summary = response.choices[0].message.content.strip()
        save_summary(chat_id, user_id, summary)
        return summary
    except Exception as e:
        logger.error(f"Failed to summarize removed history: {e}")
        return None

async def _ensure_context_fits(
    messages: List[Dict[str, str]],
    model: str | None,
    chat_id: str | None,
    user_id: str | None,
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    guard_cfg = _get_context_guard_config()
    context_limit = _get_context_length_for_model(model)
    tokens, chars = _estimate_messages_size(messages)
    info: Dict[str, Any] = {
        "usage_tokens": tokens,
        "usage_chars": chars,
        "context_limit": context_limit,
        "warnings": [],
        "trimmed_from_context": 0,
        "summary_text": None,
    }

    ratio = tokens / context_limit if context_limit else 0
    if ratio >= guard_cfg.get("WARNING_RATIO", 0.8):
        warning = (
            f"Контекст почти заполнен ({tokens}/{context_limit} токенов, {ratio:.0%}). "
            "При необходимости буду обрезать или суммировать историю."
        )
        info["warnings"].append(warning)
        logger.warning(warning)

    target_limit = int(context_limit * guard_cfg.get("HARD_RATIO", 0.95))
    if not context_limit or tokens <= target_limit:
        return messages, info

    # Попытка обрезать самые старые сообщения
    removed_texts: List[str] = []
    removable_indexes = [i for i in range(len(messages) - 1) if messages[i].get("role") in {"user", "assistant"}]
    removable_indexes = list(sorted(removable_indexes, reverse=True))
    while tokens > target_limit and removable_indexes:
        idx = removable_indexes.pop(0)
        removed_texts.append(messages[idx].get("content", ""))
        messages.pop(idx)
        info["trimmed_from_context"] += 1
        tokens, chars = _estimate_messages_size(messages)

    info["usage_tokens"] = tokens
    info["usage_chars"] = chars

    if tokens <= target_limit:
        if info["trimmed_from_context"]:
            logger.info(f"Trimmed {info['trimmed_from_context']} messages from prepared context")
            info["warnings"].append("Часть старых сообщений скрыта из запроса, чтобы освободить место в контексте.")
        return messages, info

    if guard_cfg.get("OVERFLOW_STRATEGY", "truncate") == "summarize" and chat_id and user_id:
        summary = await _summarize_removed_messages(chat_id, user_id, removed_texts, model)
        if summary:
            info["summary_text"] = summary
            summary_message = {"role": "system", "content": f"Краткая история диалога: {summary}"}
            insertion_index = 1 if messages else 0
            messages.insert(insertion_index, summary_message)
            tokens, chars = _estimate_messages_size(messages)
            info["usage_tokens"] = tokens
            info["usage_chars"] = chars
            logger.info("Context overflow: replaced part of history with summary")
            info["warnings"].append("Сделана саммаризация части истории, чтобы уложиться в лимит контекста.")
    return messages, info

async def _prepare_messages(
    prompt: str,
    model: str | None,
    chat_id: str | None,
    user_id: str | None,
    search_results: str | None,
    include_history: bool = True,
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    messages: List[Dict[str, str]] = []

    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_content = f"Текущая дата и время: {current_datetime}"
    if BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"]:
        system_content += f"\n\n{BOT_CONFIG['CUSTOM_SYSTEM_PROMPT']}"
    messages.append({"role": "system", "content": system_content})

    history: List[Dict[str, Any]] = []
    if chat_id and user_id and include_history:
        history = get_history(chat_id, user_id, limit=10)
        history = _normalize_history(history)

        summary = get_user_summary(chat_id, user_id)
        if summary:
            messages.append({"role": "system", "content": f"Краткая история нашего общения: {summary}"})

        for msg in reversed(history):
            if msg["role"] in ["user", "assistant"]:
                messages.append({"role": msg["role"], "content": msg["text"]})

    if search_results:
        messages.append({
            "role": "system",
            "content": f"Дополнительная информация из интернета:\n\n{search_results}"
        })

    current_prompt_in_history = False
    if chat_id and user_id:
        if history and history[0].get("role") == "user" and history[0].get("text") == prompt:
            current_prompt_in_history = True

    if not current_prompt_in_history:
        messages.append({"role": "user", "content": prompt})

    messages, guard_info = await _ensure_context_fits(messages, model, chat_id, user_id)

    return messages, guard_info


async def generate_text(
    prompt: str,
    model: str,
    chat_id: str = None,
    user_id: str = None,
    search_results: str = None,
    prepared_messages: List[Dict[str, str]] | None = None,
    context_info: Dict[str, Any] | None = None,
    use_context: bool = True,
    on_model_switch: Callable[[str, str, str | None], Awaitable[None]] | None = None,
) -> tuple[str, str, Dict[str, Any]]:
    """
    Генерация текста с помощью OpenRouter API.
    
    Args:
        prompt: Текст запроса пользователя
        model: Имя модели для использования
        chat_id: ID чата (опционально)
        user_id: ID пользователя (опционально)
        search_results: Результаты веб-поиска для добавления в контекст (опционально)
    """
    client = init_client()
    
    guard_info = context_info or {}
    if prepared_messages is not None:
        messages = prepared_messages
    else:
        messages, guard_info = await _prepare_messages(
            prompt,
            model,
            chat_id,
            user_id,
            search_results,
            include_history=use_context,
        )
    messages = _merge_system_into_user(messages, model)

    # Список моделей, которые будем пробовать по очереди
    models_to_try: list[str] = _build_models_to_try(model)
    tried_models: set[str] = set()
    last_error: Exception | None = None
    refreshed = False
    idx = 0

    def _find_next_model(start_from: int) -> str | None:
        for future_model in models_to_try[start_from:]:
            if future_model not in tried_models and future_model not in BOT_CONFIG.get("EXCLUDED_MODELS", []):
                return future_model
        return None

    while idx < len(models_to_try):
        candidate_model = models_to_try[idx]
        if candidate_model in tried_models:
            idx += 1
            continue
        tried_models.add(candidate_model)
        try:
            logger.info(
                f"Sending text generation request to OpenRouter with model: {candidate_model}, prompt: {prompt}"
            )
            response = await client.chat.completions.create(
                model=candidate_model,
                messages=messages,
                max_tokens=BOT_CONFIG["TEXT_GENERATION"]["MAX_TOKENS"],
                temperature=BOT_CONFIG["TEXT_GENERATION"]["TEMPERATURE"],
            )

            # Проверяем структуру ответа
            if not response or not hasattr(response, "choices") or not response.choices:
                logger.error("Empty or invalid response from OpenRouter API")
                return (
                    "Извините, произошла ошибка при получении ответа от API. Пожалуйста, попробуйте позже.",
                    candidate_model,
                    guard_info,
                )

            try:
                result = response.choices[0].message.content.strip()
                if not result:
                    logger.error("Empty content in response from OpenRouter API")
                    return (
                        "Извините, получен пустой ответ от API. Пожалуйста, попробуйте позже.",
                        candidate_model,
                        guard_info,
                    )
                logger.info(f"Received response from OpenRouter: {result[:100]}...")
                return result, candidate_model, guard_info
            except (AttributeError, IndexError) as e:
                logger.error(f"Error extracting content from response: {str(e)}")
                return (
                    "Извините, произошла ошибка при обработке ответа от API. Пожалуйста, попробуйте позже.",
                    candidate_model,
                    guard_info,
                )

        except Exception as e:
            last_error = e
            logger.error(f"Error generating text with model {candidate_model}: {str(e)}")
            # Если модель недоступна, пробуем обновить список моделей и попытаться еще раз
            if _is_model_not_found_error(e):
                logger.warning(f"Model {candidate_model} unavailable, trying fallback if available")
                if not refreshed:
                    refreshed = True
                    await refresh_models_from_api()
                    models_to_try = _build_models_to_try(model)
                    next_candidate = _find_next_model(0)
                    idx = 0
                else:
                    idx += 1
                    next_candidate = _find_next_model(idx)

                if on_model_switch and next_candidate:
                    try:
                        await on_model_switch(candidate_model, next_candidate, str(e))
                    except Exception as notify_error:
                        logger.warning(f"Failed to notify about model switch: {notify_error}")
                continue
            # Если ограничение по rate limit, пробуем следующую модель
            if _is_rate_limit_error(e):
                logger.warning(f"Model {candidate_model} rate-limited, trying next available model")
                idx += 1
                next_candidate = _find_next_model(idx)
                if on_model_switch and next_candidate:
                    try:
                        await on_model_switch(candidate_model, next_candidate, str(e))
                    except Exception as notify_error:
                        logger.warning(f"Failed to notify about model switch: {notify_error}")
                continue
            # Если модель требует аудио, пропускаем её
            if _is_audio_required_error(e):
                logger.warning(f"Model {candidate_model} requires audio, skipping to next model")
                idx += 1
                next_candidate = _find_next_model(idx)
                if on_model_switch and next_candidate:
                    try:
                        await on_model_switch(candidate_model, next_candidate, str(e))
                    except Exception as notify_error:
                        logger.warning(f"Failed to notify about model switch: {notify_error}")
                continue
            # Если провайдер ругается на порядок сообщений, пробуем следующую модель
            if _is_conversation_order_error(e):
                logger.warning(f"Model {candidate_model} rejected conversation order, trying next model")
                idx += 1
                next_candidate = _find_next_model(idx)
                if on_model_switch and next_candidate:
                    try:
                        await on_model_switch(candidate_model, next_candidate, str(e))
                    except Exception as notify_error:
                        logger.warning(f"Failed to notify about model switch: {notify_error}")
                continue
            # Для других ошибок не пытаемся бесконечно менять модели
            break
        idx += 1

    fallback_message = (
        f"Произошла ошибка при генерации текста: {str(last_error)}"
        if last_error
        else "Произошла ошибка при генерации текста: неизвестная ошибка"
    )
    failed_model = model or BOT_CONFIG.get("DEFAULT_MODEL") or "unknown"
    return fallback_message, failed_model, guard_info


async def translate_prompt(prompt: str, model: str) -> str | None:
    if not prompt or not model:
        return None

    client = init_client()
    system_prompt = (
        "Translate the user's image prompt into concise English for image generation. "
        "Preserve proper names and key details. "
        "Return only the translated prompt, without quotes or extra text."
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0,
        )

        if not response or not hasattr(response, "choices") or not response.choices:
            logger.error("Empty or invalid response during prompt translation")
            return None

        translated = response.choices[0].message.content.strip()
        return translated or None
    except Exception as exc:
        logger.error("Error translating prompt with model %s: %s", model, exc)
        return None

def _get_image_router_models() -> set[str]:
    models = BOT_CONFIG.get("IMAGE_ROUTER_MODELS", []) or []
    return {model.lower() for model in models if isinstance(model, str)}


async def _generate_image_imagerouter(prompt: str, model: str) -> str | None:
    if not BOT_CONFIG.get("IMAGE_ROUTER_KEY"):
        logger.error("IMAGE_ROUTER_KEY environment variable is not set.")
        return None

    url = BOT_CONFIG.get("IMAGE_ROUTER_BASE_URL") or "https://api.imagerouter.io/v1/openai/images/generations"
    headers = {
        "Authorization": f"Bearer {BOT_CONFIG['IMAGE_ROUTER_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "model": model,
    }

    try:
        async with aiohttp.ClientSession() as session:
            logger.info(f"Sending image generation request to ImageRouter for prompt: {prompt}")
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"ImageRouter Error Response: {error_text} (Status: {response.status})"
                    )
                    raise Exception(f"Failed to start ImageRouter image generation: {error_text}")

                response_data = await response.json()
                data_list = response_data.get("data") if isinstance(response_data, dict) else None
                if not isinstance(data_list, list) or not data_list:
                    logger.error(f"No image data received from ImageRouter: {response_data}")
                    raise Exception("No image data received from ImageRouter")

                image_url = data_list[0].get("url") if isinstance(data_list[0], dict) else None
                if image_url:
                    logger.info(f"ImageRouter image generation successful: {image_url}")
                    return image_url

                logger.error(f"No image URL in ImageRouter response: {response_data}")
                raise Exception("No image URL in ImageRouter response")
    except Exception as e:
        logger.error(f"Error generating image with ImageRouter: {str(e)}", exc_info=True)
        return None


async def _generate_image_piapi(prompt: str, model: str) -> str | None:
    if not BOT_CONFIG.get("PIAPI_KEY"):
        logger.error("PIAPI_KEY environment variable is not set.")
        return None

    try:
        url = "https://api.piapi.ai/api/v1/task"
        headers = {
            "X-API-Key": BOT_CONFIG["PIAPI_KEY"],
            "Content-Type": "application/json"
        }

        payload = {
            "model": model,
            "task_type": BOT_CONFIG["IMAGE_GENERATION"]["TASK_TYPE"],
            "input": {
                "prompt": prompt,
                "negative_prompt": BOT_CONFIG["IMAGE_GENERATION"]["NEGATIVE_PROMPT"],
                "aspect_ratio": BOT_CONFIG["IMAGE_GENERATION"]["ASPECT_RATIO"]
            }
        }

        async with aiohttp.ClientSession() as session:
            # 1. Запуск задачи генерации
            logger.info(f"Sending image generation request to PiAPI.ai for prompt: {prompt}")
            async with session.post(url, headers=headers, data=json.dumps(payload)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"PiAPI.ai Error Response: {error_text} (Status: {response.status})")
                    raise Exception(f"Failed to start PiAPI.ai image generation: {error_text}")

                task_data = await response.json()
                data_dict = task_data.get("data")
                task_id = data_dict.get("task_id") if data_dict else None

                if not task_id:
                    logger.error(f"No task_id received from PiAPI.ai: {task_data}")
                    raise Exception("No task_id received from PiAPI.ai")

                logger.info(f"Started PiAPI.ai image generation task: {task_id}")

            # 2. Ожидание завершения задачи
            max_attempts = BOT_CONFIG["IMAGE_GENERATION"]["MAX_ATTEMPTS"]
            attempts = 0
            status_check_url = f"{url}/{task_id}"

            while attempts < max_attempts:
                await asyncio.sleep(BOT_CONFIG["IMAGE_GENERATION"]["POLLING_INTERVAL"])
                logger.info(f"Checking status for task {task_id} (Attempt {attempts + 1}/{max_attempts})")
                async with session.get(status_check_url, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(
                            f"Status check failed for task {task_id}: {error_text} (Status: {response.status})"
                        )
                        attempts += 1
                        continue

                    status_data = await response.json()
                    data_dict = status_data.get("data", {})
                    task_status = data_dict.get("status")
                    logger.info(f"Task {task_id} status: {task_status}")

                    if task_status == "completed":
                        output_dict = data_dict.get("output", {})
                        image_url = output_dict.get("image_url")
                        if image_url:
                            logger.info(f"Image generation successful for task {task_id}: {image_url}")
                            return image_url
                        else:
                            logger.error(f"Completed task {task_id} but no result URL found: {status_data}")
                            raise Exception("No image URL in successful PiAPI.ai response")
                    elif task_status == "failed":
                        error_details = data_dict.get("error", {}).get("message", "Unknown error")
                        logger.error(f"Image generation failed for task {task_id}: {error_details}")
                        raise Exception(f"PiAPI.ai image generation failed: {error_details}")
                    elif task_status in ["processing", "pending"]:
                        pass
                    else:
                        logger.warning(f"Unknown task status for {task_id}: {task_status}")

                    attempts += 1

            logger.error(f"Image generation timed out for task {task_id}")
            raise Exception("Image generation timed out with PiAPI.ai")

    except Exception as e:
        logger.error(f"Error generating image with PiAPI.ai: {str(e)}", exc_info=True)
        return None


async def generate_image(prompt: str) -> str | None:
    """Генерация изображения через PiAPI.ai или ImageRouter."""
    model = BOT_CONFIG.get("IMAGE_GENERATION", {}).get("MODEL")
    if not model:
        logger.error("Image generation model is not configured.")
        return None

    if model.lower() in _get_image_router_models():
        return await _generate_image_imagerouter(prompt, model)

    return await _generate_image_piapi(prompt, model)
