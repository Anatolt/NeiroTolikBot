import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

from config import BOT_CONFIG
from handlers.commands import MODELS_HINT_TEXT
from services.consilium import (
    extract_prompt_from_consilium_message,
    format_consilium_results,
    generate_consilium_responses,
    parse_models_from_message,
    select_default_consilium_models,
)
from services.generation import CATEGORY_TITLES, build_models_messages, generate_image, generate_text
from services.memory import (
    add_message,
    get_history,
    get_preferred_model,
    get_routing_mode,
    get_show_response_header,
    set_preferred_model,
    set_routing_mode,
    set_show_response_header,
)
from services.router import route_request
from services.web_search import search_web

logger = logging.getLogger(__name__)


@dataclass
class MessageProcessingRequest:
    text: str
    chat_id: str
    user_id: str
    bot_username: str | None = None
    username: str | None = None


@dataclass
class MessageResponse:
    text: str | None = None
    photo_url: str | None = None
    parse_mode: str | None = None


@dataclass
class RoutedRequest:
    request_type: str
    content: str
    suggested_models: List[str]
    model: str | None
    category: str | None
    use_context: bool
    reason: str | None
    user_routing_mode: str


_ROUTING_RULES_KEYWORDS = {
    "роутинг алгоритмами",
    "роутинг правилами",
    "routing rules",
    "routing algorithms",
    "routing algo",
}

_ROUTING_LLM_KEYWORDS = {
    "роутинг ллм",
    "роутинг llm",
    "routing llm",
    "routing ai",
}

_ROUTING_STATUS_KEYWORDS = {
    "какой роутинг",
    "режим роутинга",
    "routing mode",
}

_HEADER_DISABLE_KEYWORDS = {
    "спрячь шапку",
    "скрой шапку",
    "скрыть шапку",
    "выключи шапку",
    "отключи шапку",
    "убери шапку",
    "без шапки",
    "скрой техшапку",
}

_HEADER_ENABLE_KEYWORDS = {
    "включи шапку",
    "показывай шапку",
    "верни шапку",
    "покажи шапку",
    "включи техшапку",
    "техшапка вкл",
}

_MODEL_PREFERENCE_PATTERN = re.compile(r"^отвечай\s+всегда\s+(?:с|через)\s+(.+)$", re.IGNORECASE)
_MODEL_PREFERENCE_RESET_KEYWORDS = {
    "отвечай как обычно",
    "используй стандартную модель",
    "сбрось модель",
    "отключи модель по умолчанию",
}


def _resolve_model_alias(model_text: str) -> str | None:
    models = BOT_CONFIG.get("MODELS", {})
    normalized = model_text.strip().lower()

    if normalized in models:
        return models[normalized]

    for value in models.values():
        if normalized == value.lower():
            return value

    return None


def _normalize_routing_choice(text: str) -> str | None:
    normalized = text.strip().lower()
    if normalized in _ROUTING_RULES_KEYWORDS:
        return "rules"
    if normalized in _ROUTING_LLM_KEYWORDS:
        return "llm"
    return None


def _is_routing_status_request(text: str) -> bool:
    return text.strip().lower() in _ROUTING_STATUS_KEYWORDS


def _normalize_header_toggle(text: str) -> bool | None:
    normalized = text.strip().lower()
    if normalized in _HEADER_DISABLE_KEYWORDS:
        return False
    if normalized in _HEADER_ENABLE_KEYWORDS:
        return True
    return None


def _format_response_header(
    routing_mode: str | None, context_info: dict | None, model: str | None
) -> str | None:
    parts: list[str] = []

    if routing_mode:
        routing_label = "алгоритмический" if routing_mode == "rules" else "LLM"
        parts.append(f"🔀 Роутинг: {routing_label}")

    if context_info:
        tokens = context_info.get("usage_tokens")
        chars = context_info.get("usage_chars")
        limit = context_info.get("context_limit")

        context_chunks: list[str] = []
        if tokens and limit:
            context_chunks.append(f"{tokens}/{limit} т")
        elif tokens:
            context_chunks.append(f"{tokens} т")

        if chars:
            context_chunks.append(f"{chars} симв")

        if context_chunks:
            parts.append(f"📦 Контекст: {' • '.join(context_chunks)}")

        trimmed = context_info.get("trimmed_from_context")
        if trimmed:
            parts.append(f"✂️ Обрезано: {trimmed}")

        if context_info.get("summary_text"):
            parts.append("🧾 Саммари истории")

        warnings = context_info.get("warnings") or []
        if warnings:
            parts.append(f"⚠️ {warnings[0]}")

    if model:
        parts.append(f"🤖 Модель: {model}")

    return " • ".join(parts) if parts else None


def _build_context_guard_notices(context_info: dict | None) -> list[str]:
    if not context_info:
        return []

    notices = []
    if context_info.get("summary_text"):
        notices.append("⚠️ Контекст переполнен — делаю саммари истории.")
    elif context_info.get("trimmed_from_context"):
        notices.append("⚠️ Контекст переполнен — скрываю самые старые сообщения из запроса.")

    for warn in context_info.get("warnings", []):
        notices.append(f"ℹ️ {warn}")

    return notices


async def get_capabilities() -> list[str]:
    """Получение и форматирование информации о доступных моделях."""
    try:
        capabilities = await build_models_messages(
            ["free", "large_context", "specialized", "paid"],
            header="🤖 Доступные модели по категориям:\n\n",
            max_items_per_category=20,
        )

        if not capabilities:
            return ["Извините, не удалось получить информацию о моих возможностях."]

        instructions = "💡 Как использовать:\n"
        instructions += f"• Просто напиши свой вопрос - отвечу через {BOT_CONFIG['DEFAULT_MODEL']}\n"
        instructions += "• Укажи модель в начале ('chatgpt расскажи о погоде')\n"
        instructions += "• Или в конце ('расскажи о погоде через claude')\n"
        instructions += "• Для картинок используй 'нарисуй' или 'сгенерируй картинку'"

        if len(capabilities[-1] + instructions) > 3000:
            capabilities.append(instructions)
        else:
            capabilities[-1] += instructions

        return capabilities
    except Exception as e:
        logger.error(f"Error getting capabilities: {str(e)}")
        return ["Извините, не удалось получить информацию о моих возможностях."]


async def send_models_by_request(
    order: list[str],
    header: str,
    max_items: int | None = 20,
) -> List[MessageResponse]:
    """Возвращает список моделей для указанной категории."""

    parts = await build_models_messages(order, header=header, max_items_per_category=max_items)
    if not parts:
        return [MessageResponse(text="Не удалось получить список моделей. Пожалуйста, попробуйте позже.")]

    return [MessageResponse(text=part) for part in parts]


def _build_routed_request(
    decision,
    effective_text: str,
    user_routing_mode: str,
) -> RoutedRequest:
    request_type = decision.action or "text"
    content = decision.prompt or effective_text
    suggested_models = decision.target_models or []
    model = suggested_models[0] if suggested_models else None
    category = decision.category
    use_context = decision.use_context
    reason = decision.reason

    if request_type == "search" and not content:
        request_type = "search_previous"

    if request_type == "models_category" and category:
        content = category

    if request_type == "text" and len(suggested_models) > 1:
        request_type = "consilium"

    return RoutedRequest(
        request_type=request_type,
        content=content,
        suggested_models=suggested_models,
        model=model,
        category=category,
        use_context=use_context,
        reason=reason,
        user_routing_mode=user_routing_mode,
    )


async def execute_routed_request(
    request: MessageProcessingRequest,
    routed: RoutedRequest,
    ack_callback: Optional[Callable[[], Awaitable[None]]] = None,
) -> List[MessageResponse]:
    responses: List[MessageResponse] = []

    chat_id = request.chat_id
    user_id = request.user_id
    preferred_model = get_preferred_model(chat_id, user_id)
    show_response_header = get_show_response_header(chat_id, user_id)

    request_type = routed.request_type
    content = routed.content
    suggested_models = routed.suggested_models
    model = routed.model
    use_context = routed.use_context

    async def notify_model_switch(failed_model: str, next_model: str, error_text: str | None) -> None:
        reason = f" ({error_text})" if error_text else ""
        responses.append(
            MessageResponse(
                text=f"⚠️ Модель {failed_model} не ответила{reason}. Пошел спрашивать другую модель {next_model}."
            )
        )

    if request_type in {"text", "search", "search_previous", "consilium"} and ack_callback:
        try:
            await ack_callback()
        except Exception as exc:
            logger.warning("Failed to send ack message: %s", exc)

    if request_type == "help":
        capabilities = await get_capabilities()
        responses.extend(MessageResponse(text=part) for part in capabilities)
    elif request_type == "models_hint":
        responses.append(MessageResponse(text=MODELS_HINT_TEXT))
    elif request_type == "models_category":
        if content == "all":
            responses.extend(
                await send_models_by_request(
                    ["free", "large_context", "specialized", "paid"], MODELS_HINT_TEXT, max_items=None
                )
            )
        else:
            responses.extend(
                await send_models_by_request(
                    [content], CATEGORY_TITLES.get(content, "Список моделей:"), max_items=20
                )
            )
    elif request_type == "image":
        responses.append(MessageResponse(text="Генерирую изображение..."))
        image_url = await generate_image(content)
        if image_url:
            responses.append(MessageResponse(photo_url=image_url))
        else:
            responses.append(MessageResponse(text="Не удалось сгенерировать изображение."))
    elif request_type == "search":
        chat_id = str(chat_id)
        user_id = str(user_id)
        model_name = model or preferred_model or BOT_CONFIG["DEFAULT_MODEL"]

        responses.append(MessageResponse(text="Ищу информацию в интернете..."))
        search_results = await search_web(content)

        prompt_with_search = (
            f"Пользователь попросил найти информацию: '{content}'. Вот результаты поиска в интернете:\n\n{search_results}\n\n"
            "Пожалуйста, проанализируй найденную информацию и дай развернутый ответ на запрос пользователя."
        )

        add_message(chat_id, user_id, "user", model_name, f"погугли {content}")

        response_text, used_model, context_info = await generate_text(
            prompt_with_search,
            model_name,
            chat_id,
            user_id,
            search_results=search_results,
            use_context=use_context,
            on_model_switch=notify_model_switch,
        )

        responses.extend(MessageResponse(text=notice) for notice in _build_context_guard_notices(context_info))

        add_message(chat_id, user_id, "assistant", used_model, response_text)

        header = _format_response_header(routed.user_routing_mode, context_info, used_model) if show_response_header else None
        reply_text = f"{header}\n\n{response_text}" if header else response_text
        responses.append(MessageResponse(text=reply_text))
    elif request_type == "search_previous":
        chat_id = str(chat_id)
        user_id = str(user_id)
        model_name = model or preferred_model or BOT_CONFIG["DEFAULT_MODEL"]

        history = get_history(chat_id, user_id, limit=10)

        previous_user_message: str | None = None
        previous_assistant_message: str | None = None

        for msg in history:
            if msg["role"] == "assistant" and not previous_assistant_message:
                previous_assistant_message = msg["text"]
            elif (
                msg["role"] == "user"
                and msg["text"].lower() not in ["погугли", "поищи"]
                and not previous_user_message
            ):
                previous_user_message = msg["text"]
                if previous_assistant_message:
                    break

        if not previous_user_message or not previous_assistant_message:
            responses.append(
                MessageResponse(
                    text=(
                        "Не найдено предыдущего сообщения для поиска. Пожалуйста, укажите, что искать, например: 'погугли погода в Москве'"
                    )
                )
            )
            return responses

        responses.append(
            MessageResponse(
                text=f"Ищу дополнительную информацию по вашему предыдущему вопросу: '{previous_user_message}'..."
            )
        )

        search_prompt = (
            f"Пользователь ранее спросил: '{previous_user_message}'\n\n"
            f"Я ответил: '{previous_assistant_message}'\n\n"
            "Теперь пользователь просит найти дополнительную информацию в интернете. Сформулируй краткий поисковый запрос (2-5 слов) для поиска в интернете, который поможет дополнить или уточнить мой ответ. Ответь только поисковым запросом, без дополнительных слов."
        )

        search_query_response, _used_model, _context_info = await generate_text(
            search_prompt,
            model_name,
            None,
            None,
            use_context=False,
            on_model_switch=notify_model_switch,
        )
        search_query = search_query_response.strip().strip('"').strip("'")

        logger.info("Model formulated search query: '%s'", search_query)

        search_results = await search_web(search_query)

        final_prompt = (
            f"Пользователь ранее спросил: '{previous_user_message}'\n\n"
            f"Я ранее ответил: '{previous_assistant_message}'\n\n"
            f"Теперь я нашел дополнительную информацию в интернете по запросу '{search_query}':\n\n{search_results}\n\n"
            "Пожалуйста, проанализируй найденную информацию и дополни мой предыдущий ответ актуальными данными из интернета."
        )

        add_message(chat_id, user_id, "user", model_name, "погугли")

        response_text, used_model, context_info = await generate_text(
            final_prompt,
            model_name,
            chat_id,
            user_id,
            search_results=search_results,
            use_context=use_context,
            on_model_switch=notify_model_switch,
        )

        responses.extend(MessageResponse(text=notice) for notice in _build_context_guard_notices(context_info))

        add_message(chat_id, user_id, "assistant", used_model, response_text)

        header = _format_response_header(routed.user_routing_mode, context_info, used_model) if show_response_header else None
        reply_text = f"{header}\n\n{response_text}" if header else response_text
        responses.append(MessageResponse(text=reply_text))

    elif request_type == "consilium":
        chat_id = str(chat_id)
        user_id = str(user_id)

        models = suggested_models or await parse_models_from_message(content)

        if not models:
            models = await select_default_consilium_models()
            if not models:
                responses.append(
                    MessageResponse(
                        text="❌ Не удалось выбрать модели для консилиума. Попробуйте указать модели явно."
                    )
                )
                return responses

        prompt = extract_prompt_from_consilium_message(content)

        if not prompt:
            responses.append(
                MessageResponse(
                    text="❌ Не указан вопрос для консилиума. Используйте: консилиум: ваш вопрос"
                )
            )
            return responses

        status_message = MessageResponse(text=f"🏥 Генерирую ответы от {len(models)} моделей...")
        responses.append(status_message)

        if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
            add_message(chat_id, user_id, "user", models[0], prompt)

        start_time = time.time()

        results = await generate_consilium_responses(prompt, models, chat_id, user_id)

        execution_time = time.time() - start_time

        formatted_messages = format_consilium_results(results, execution_time)

        if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
            for result in results:
                if result.get("success") and result.get("response"):
                    add_message(chat_id, user_id, "assistant", result.get("model"), result.get("response"))

        max_length = 4000
        for msg in formatted_messages:
            if len(msg) > max_length:
                parts: list[str] = []
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
                        responses.append(MessageResponse(text=part))
                    else:
                        responses.append(
                            MessageResponse(
                                text=f"*(продолжение {i+1}/{len(parts)})*\n\n{part}", parse_mode="Markdown"
                            )
                        )
            else:
                responses.append(MessageResponse(text=msg))

    elif request_type == "text":
        chat_id = str(chat_id)
        user_id = str(user_id)
        model_name = model or preferred_model or BOT_CONFIG["DEFAULT_MODEL"]
        add_message(chat_id, user_id, "user", model_name, content)

        response_text, used_model, context_info = await generate_text(
            content,
            model_name,
            chat_id,
            user_id,
            use_context=use_context,
            on_model_switch=notify_model_switch,
        )

        responses.extend(MessageResponse(text=notice) for notice in _build_context_guard_notices(context_info))

        add_message(chat_id, user_id, "assistant", used_model, response_text)

        header = _format_response_header(routed.user_routing_mode, context_info, used_model) if show_response_header else None
        reply_text = f"{header}\n\n{response_text}" if header else response_text
        responses.append(MessageResponse(text=reply_text))
    else:
        logger.warning("Unknown request type: %s", request_type)
        responses.append(MessageResponse(text="Извините, не удалось обработать ваш запрос."))

    return responses


async def process_message_request(
    request: MessageProcessingRequest,
    ack_callback: Optional[Callable[[], Awaitable[None]]] = None,
    router_start_callback: Optional[Callable[[], Awaitable[None]]] = None,
    router_decision_callback: Optional[Callable[[RoutedRequest], Awaitable[bool]]] = None,
) -> List[MessageResponse]:
    """Общая бизнес-логика обработки входящих сообщений для любых платформ."""

    responses: List[MessageResponse] = []

    if not request.text:
        logger.debug("Received empty text, ignoring")
        return responses

    text = request.text
    chat_id = request.chat_id
    user_id = request.user_id
    bot_username = request.bot_username

    effective_text = text

    logger.info(
        "Processing message '%s' from user %s in chat %s",
        text,
        request.username or user_id,
        chat_id,
    )

    normalized_text = effective_text.strip()

    if normalized_text.lower() in _MODEL_PREFERENCE_RESET_KEYWORDS:
        set_preferred_model(chat_id, user_id, None)
        responses.append(
            MessageResponse(
                text=(
                    "🔄 Вернулся к стандартной модели. Чтобы снова закрепить модель, напиши 'отвечай всегда с gpt'."
                )
            )
        )
        return responses

    model_preference_match = _MODEL_PREFERENCE_PATTERN.match(normalized_text)
    if model_preference_match:
        requested_model = model_preference_match.group(1).strip()
        resolved_model = _resolve_model_alias(requested_model)

        if not resolved_model:
            available_aliases = ", ".join(sorted(BOT_CONFIG.get("MODELS", {}).keys()))
            responses.append(
                MessageResponse(text=f"❌ Я не знаю модель '{requested_model}'. Доступные варианты: {available_aliases}.")
            )
            return responses

        set_preferred_model(chat_id, user_id, resolved_model)
        preferred_model = resolved_model
        responses.append(
            MessageResponse(
                text=(
                    "✅ Запомнил: буду отвечать через выбранную модель, пока не попросишь иначе. "
                    "Чтобы вернуться к стандартной, напиши 'отвечай как обычно'."
                )
            )
        )
        return responses

    header_toggle = _normalize_header_toggle(effective_text)
    if header_toggle is not None:
        set_show_response_header(chat_id, user_id, header_toggle)
        reply = (
            "🛠 Техшапка включена и будет показываться над ответами.\n"
            "Чтобы скрыть, отправьте 'скрыть шапку' или команду /header_off."
        )
        if not header_toggle:
            reply = (
                "🫥 Техшапка скрыта.\n"
                "Чтобы вернуть её, отправьте 'показывай шапку' или команду /header_on."
            )

        responses.append(MessageResponse(text=reply))
        return responses

    routing_choice = _normalize_routing_choice(effective_text)
    if routing_choice:
        set_routing_mode(chat_id, user_id, routing_choice)
        mode_label = "алгоритмический" if routing_choice == "rules" else "LLM"
        responses.append(
            MessageResponse(
                text=(
                    f"🔀 Включён {mode_label} роутинг для ваших сообщений в этом чате.\n"
                    f"Чтобы переключиться, отправьте 'роутинг алгоритмами' или 'роутинг ллм', либо используйте слеш-команды /routing_rules и /routing_llm."
                )
            )
        )
        return responses

    if _is_routing_status_request(effective_text):
        current_mode = get_routing_mode(chat_id, user_id) or BOT_CONFIG.get("ROUTING_MODE", "rules")
        mode_label = "алгоритмический" if current_mode == "rules" else "LLM"
        responses.append(MessageResponse(text=f"🔎 Текущий режим роутинга: {mode_label}."))
        return responses

    user_routing_mode = get_routing_mode(chat_id, user_id) or BOT_CONFIG.get("ROUTING_MODE", "rules")
    if user_routing_mode == "llm" and router_start_callback:
        try:
            await router_start_callback()
        except Exception as exc:
            logger.warning("Failed to send router start message: %s", exc)

    logger.info("Routing request (mode=%s): '%s'", user_routing_mode, effective_text)
    decision = await route_request(effective_text, bot_username, routing_mode=user_routing_mode)
    routed = _build_routed_request(decision, effective_text, user_routing_mode)
    logger.info(
        "Router resolved request to: %s, model: %s, use_context: %s, reason: %s",
        routed.request_type,
        routed.model,
        routed.use_context,
        routed.reason,
    )

    if user_routing_mode == "llm" and router_decision_callback:
        proceed = await router_decision_callback(routed)
        if not proceed:
            return responses

    return await execute_routed_request(request, routed, ack_callback=ack_callback)
