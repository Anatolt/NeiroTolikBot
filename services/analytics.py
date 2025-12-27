import math
from typing import Optional

from config import BOT_CONFIG
from services.memory import log_usage_event


def _estimate_tokens(char_count: int) -> float:
    return max(1.0, round(char_count / 4.0, 2))


def _is_free_model(model_id: Optional[str]) -> bool:
    if not model_id:
        return False
    model_lower = model_id.lower()
    if ":free" in model_lower:
        return True
    free_models = BOT_CONFIG.get("FREE_MODELS", []) or []
    return model_id in free_models


def _estimate_text_cost(tokens: float, model_id: Optional[str]) -> float:
    if _is_free_model(model_id):
        return 0.0
    price = BOT_CONFIG.get("TEXT_COST_PER_1K_TOKENS")
    if price is None:
        return 0.0
    try:
        return float(price) * (tokens / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def log_text_usage(
    platform: str,
    chat_id: str,
    user_id: str,
    model_id: Optional[str],
    prompt: str,
    response: str,
) -> None:
    char_count = len(prompt or "") + len(response or "")
    tokens = _estimate_tokens(char_count)
    cost = _estimate_text_cost(tokens, model_id)
    log_usage_event(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        event_type="text",
        model=model_id,
        char_count=char_count,
        token_estimate=tokens,
        estimated_cost=cost,
        is_free=_is_free_model(model_id),
    )


def log_image_usage(
    platform: str,
    chat_id: str,
    user_id: str,
    model_id: Optional[str],
    prompt: str,
) -> None:
    char_count = len(prompt or "")
    tokens = _estimate_tokens(char_count)
    if _is_free_model(model_id):
        cost = 0.0
    else:
        try:
            cost = float(BOT_CONFIG.get("IMAGE_COST_PER_GENERATION") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
    log_usage_event(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        event_type="image",
        model=model_id,
        char_count=char_count,
        token_estimate=tokens,
        estimated_cost=cost,
        is_free=_is_free_model(model_id),
    )


def log_stt_usage(
    platform: str,
    chat_id: str,
    user_id: str,
    duration_seconds: Optional[float],
    size_bytes: Optional[int],
) -> None:
    cost = None
    price_per_min = BOT_CONFIG.get("VOICE_TRANSCRIBE_COST_PER_MIN")
    minutes = None
    if duration_seconds and duration_seconds > 0:
        minutes = duration_seconds / 60.0
    elif size_bytes and size_bytes > 0:
        minutes = size_bytes / (1024 * 1024)

    if minutes is not None and price_per_min is not None:
        try:
            cost = float(price_per_min) * minutes
        except (TypeError, ValueError):
            cost = None

    log_usage_event(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        event_type="stt",
        model=None,
        char_count=0,
        token_estimate=0.0,
        estimated_cost=cost or 0.0,
        is_free=False,
    )
