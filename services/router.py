import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from config import BOT_CONFIG
from services.generation import init_client

logger = logging.getLogger(__name__)


@dataclass
class RouterDecision:
    action: str
    prompt: str
    use_context: bool = True
    target_models: List[str] = field(default_factory=list)
    category: Optional[str] = None
    reason: Optional[str] = None


_SUPPORTED_ACTIONS = {
    "help",
    "models_hint",
    "models_category",
    "image",
    "search",
    "search_previous",
    "consilium",
    "text",
}


def _extract_json_block(content: str) -> dict:
    """Извлекает и валидирует JSON из ответа сортирующей модели."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        # Убираем возможные маркировки кода
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Router returned non-JSON payload, falling back to defaults: %s", content)
        return {}


def _sanitize_models(models: list[str] | None) -> list[str]:
    if not models:
        return []

    known_models = {m.lower(): m for m in BOT_CONFIG.get("MODELS", {}).values()}
    sanitized: list[str] = []
    for model in models:
        normalized = model.strip()
        if not normalized:
            continue
        lookup_key = normalized.lower()
        if lookup_key in known_models:
            sanitized.append(known_models[lookup_key])
        else:
            sanitized.append(normalized)
    return sanitized


async def analyze_request(user_text: str, bot_username: str | None = None) -> RouterDecision:
    """Отправляет запрос в сортирующую модель и возвращает решение."""

    client = init_client()
    router_model = BOT_CONFIG.get("ROUTER_MODEL") or BOT_CONFIG.get("DEFAULT_MODEL")

    available_models = ", ".join(sorted(set(BOT_CONFIG.get("MODELS", {}).values())))

    system_prompt = (
        "Ты работаешь как маршрутизатор запросов."
        " Классифицируй пользовательский ввод и верни JSON с полями:"
        " action (help, models_hint, models_category, image, search, search_previous, consilium, text),"
        " prompt (уточненный текст запроса без служебных слов),"
        " use_context (true/false — использовать ли историю диалога и заметки),"
        " target_models (массив id моделей, если нужно указать конкретные варианты),"
        " category (free, paid, large_context, specialized, all для списков моделей),"
        " reason (короткое пояснение)."
        " Если пользователь просто пишет 'погугли' без уточнений — используй action=search_previous."
        " Если речь о генерации изображения или картинок — action=image."
        " Если нужно опросить несколько моделей или запрос сложный — выбери action=consilium и предложи 2-3 модели."
        " Если просит справку или что ты умеешь — help."
        " Если просит список моделей — models_hint или models_category."
        " Ответ строго в формате JSON без дополнительного текста."
    )

    user_prompt = (
        f"Текст пользователя: {user_text}\n"
        f"Имя бота (если использовано упоминание): {bot_username or 'не указано'}\n"
        "Доступные модели (можно рекомендовать только их или оставить пусто):\n"
        f"{available_models}"
    )

    try:
        response = await client.chat.completions.create(
            model=router_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=300,
            temperature=0,
        )
        content = response.choices[0].message.content
        payload = _extract_json_block(content)

        action = payload.get("action", "text")
        if action not in _SUPPORTED_ACTIONS:
            action = "text"

        prompt = payload.get("prompt") or user_text
        use_context = bool(payload.get("use_context", True))
        category = payload.get("category")
        reason = payload.get("reason")
        target_models = _sanitize_models(payload.get("target_models"))

        decision = RouterDecision(
            action=action,
            prompt=prompt,
            use_context=use_context,
            target_models=target_models,
            category=category,
            reason=reason,
        )
        logger.info("Router decision: %s", decision)
        return decision
    except Exception as e:
        logger.error("Failed to analyze request via router model: %s", e)
        return RouterDecision(action="text", prompt=user_text)


def _route_with_rules(user_text: str) -> RouterDecision:
    """Простая маршрутизация на правилах без LLM."""

    text_lower = user_text.lower().strip()
    keywords = BOT_CONFIG.get("KEYWORDS", {})

    # Help / capabilities
    if any(k in text_lower for k in keywords.get("CAPABILITIES", [])):
        return RouterDecision(action="help", prompt=user_text, reason="rule: capabilities keywords")

    # Consilium
    if text_lower.startswith("консилиум") or text_lower.startswith("/consilium"):
        return RouterDecision(action="consilium", prompt=user_text, reason="rule: consilium keyword")

    # Image generation
    if any(keyword in text_lower for keyword in keywords.get("IMAGE", [])):
        cleaned_prompt = user_text
        for keyword in keywords.get("IMAGE", []):
            cleaned_prompt = cleaned_prompt.replace(keyword, "").strip()
        return RouterDecision(action="image", prompt=cleaned_prompt or user_text, reason="rule: image keyword")

    # Web search
    if text_lower in {"погугли", "поищи"}:
        return RouterDecision(action="search_previous", prompt=user_text, use_context=False, reason="rule: search previous")

    if text_lower.startswith("погугли") or text_lower.startswith("поищи"):
        query = user_text.split(" ", 1)
        prompt = query[1].strip() if len(query) > 1 else user_text
        action = "search" if prompt else "search_previous"
        return RouterDecision(action=action, prompt=prompt or user_text, use_context=False, reason="rule: search keyword")

    # Default
    return RouterDecision(action="text", prompt=user_text, reason="rule: fallback text")


async def route_request(
    user_text: str,
    bot_username: str | None = None,
    routing_mode: str | None = None,
) -> RouterDecision:
    """Определяет стратегию маршрутизации: правила или LLM."""

    mode = (routing_mode or BOT_CONFIG.get("ROUTING_MODE") or "rules").lower()

    if mode == "llm":
        return await analyze_request(user_text, bot_username)

    return _route_with_rules(user_text)
