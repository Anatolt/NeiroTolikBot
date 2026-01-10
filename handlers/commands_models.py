import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import BOT_CONFIG
from services.generation import (
    build_models_messages,
    categorize_models,
    fetch_models_data,
    fetch_imagerouter_models,
)
from services.memory import (
    get_preferred_model,
    get_voice_log_model,
    get_voice_model,
)
from services.memory import set_preferred_model

logger = logging.getLogger(__name__)

MODELS_HINT_TEXT = (
    "🤖 Списки моделей по категориям:\n"
    "• /models_free — бесплатные\n"
    "• /models_paid — платные\n"
    "• /models_large_context — с большим контекстом\n"
    "• /models_specialized — специализированные\n"
    "• /models_all — полный список (может быть длинным)\n\n"
    "🎙️ /models_voice — модели распознавания речи\n"
    "🎧 /voice_log_models — модели распознавания для логов\n"
    "🖼️ /models_pic — модели генерации изображений"
)

_MODELS_PAGE_SIZE = 15
_MODELS_FREE_CALLBACK_PREFIX = "models_free:page:"
_MODELS_PAID_CALLBACK_PREFIX = "models_paid:page:"
_MODELS_LARGE_CALLBACK_PREFIX = "models_large_context:page:"
_MODELS_SPECIALIZED_CALLBACK_PREFIX = "models_specialized:page:"
_MODELS_PIC_CALLBACK_PREFIX = "models_pic:page:"


def _build_models_page(
    title: str,
    model_items: list[str],
    page: int,
    current_model: str | None,
    page_size: int = _MODELS_PAGE_SIZE,
    set_command: str | None = None,
) -> tuple[str, int, int]:
    total = len(model_items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    lines = [f"{title} (страница {page}/{total_pages}):"]
    if current_model:
        lines.append(f"Текущая: {current_model}")
    if not model_items:
        lines.append("Список моделей пуст.")
        return "\n".join(lines), page, total_pages

    for idx, item in enumerate(model_items[start:end], start=start + 1):
        lines.append(f"{idx}) {item}")
        if set_command:
            lines.append(f"/{set_command}_{idx}")

    return "\n".join(lines), page, total_pages


def _build_models_markup(
    prefix: str, page: int, total_pages: int
) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None

    prev_page = page - 1 if page > 1 else total_pages
    next_page = page + 1 if page < total_pages else 1
    keyboard = [
        [
            InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"{prefix}{prev_page}"),
            InlineKeyboardButton("Следующая ➡️", callback_data=f"{prefix}{next_page}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_image_model_items(
    piapi_models: list[str],
    imagerouter_models: list[str],
    combined_models: list[str],
) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for model in piapi_models + imagerouter_models + combined_models:
        if not model or model in seen:
            continue
        seen.add(model)
        items.append(model)
    return items


def _store_model_list(context: ContextTypes.DEFAULT_TYPE, model_ids: list[str]) -> None:
    context.user_data["model_select_list"] = model_ids


def _store_image_model_list(context: ContextTypes.DEFAULT_TYPE, model_ids: list[str]) -> None:
    context.user_data["image_model_select_list"] = model_ids


def _parse_index_command(text: str, prefix: str) -> int | None:
    match = re.match(rf"^/{re.escape(prefix)}_(\d+)(?:@\\w+)?$", text.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def _refresh_image_models() -> tuple[list[str], list[str], list[str]]:
    piapi_models = BOT_CONFIG.get("PIAPI_IMAGE_MODELS", []) or []
    imagerouter_models = await fetch_imagerouter_models()
    combined_models: list[str] = []
    seen: set[str] = set()
    for model in piapi_models + imagerouter_models:
        if model and model not in seen:
            seen.add(model)
            combined_models.append(model)

    BOT_CONFIG["IMAGE_MODELS"] = combined_models
    BOT_CONFIG["IMAGE_ROUTER_MODELS"] = imagerouter_models
    return piapi_models, imagerouter_models, combined_models


def _build_voice_models_text() -> str:
    voice_models = BOT_CONFIG.get("VOICE_MODELS", [])
    current_model = get_voice_model() or BOT_CONFIG.get("VOICE_MODEL")
    lines = ["🎙️ Модели распознавания речи:"]
    if current_model:
        lines.append(f"Текущая: {current_model}")
    if voice_models:
        for idx, model in enumerate(voice_models, start=1):
            lines.append(f"{idx}) {model} — `/set_voice_model {idx}`")
    return "\n".join(lines)


def _build_voice_log_models_text() -> str:
    voice_models = BOT_CONFIG.get("VOICE_MODELS", [])
    current_model = get_voice_log_model() or get_voice_model() or BOT_CONFIG.get("VOICE_MODEL")
    lines = ["🎧 Модели распознавания для голосовых логов:"]
    if current_model:
        lines.append(f"Текущая: {current_model}")
    if not voice_models:
        lines.append("Список моделей распознавания речи пуст.")
        return "\n".join(lines)

    lines.append("Доступные модели:")
    for idx, model in enumerate(voice_models, start=1):
        lines.append(f"{idx}) {model} — `/set_voice_log_model {idx}`")
    return "\n".join(lines)


async def _get_free_model_ids() -> list[str]:
    models_data = await fetch_models_data()
    if not models_data:
        return []
    categories = categorize_models(models_data)
    excluded = set(BOT_CONFIG.get("EXCLUDED_MODELS", []))
    return [
        model.get("id")
        for model in categories.get("free", [])
        if model.get("id") and model.get("id") not in excluded
    ]


async def _get_model_ids_by_category(category: str) -> list[str]:
    models_data = await fetch_models_data()
    if not models_data:
        return []
    categories = categorize_models(models_data)
    excluded = set(BOT_CONFIG.get("EXCLUDED_MODELS", []))
    return [
        model.get("id")
        for model in categories.get(category, [])
        if model.get("id") and model.get("id") not in excluded
    ]


def _build_free_models_page(
    model_ids: list[str],
    page: int,
    current_model: str | None,
    page_size: int = _MODELS_PAGE_SIZE,
) -> tuple[str, int, int]:
    return _build_models_page(
        "🆓 Бесплатные модели",
        model_ids,
        page,
        current_model,
        page_size=page_size,
        set_command="set_model",
    )


def _build_free_models_markup(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    return _build_models_markup(_MODELS_FREE_CALLBACK_PREFIX, page, total_pages)


async def _send_models(update: Update, order: list[str], header: str, max_items: int | None = 20) -> None:
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
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    model_ids = await _get_free_model_ids()
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_free_models_page(model_ids, page, current_model)
    markup = _build_free_models_markup(resolved_page, total_pages)
    await update.message.reply_text(message, reply_markup=markup)


async def models_free_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия пагинации для /models_free."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(_MODELS_FREE_CALLBACK_PREFIX):
        return

    try:
        page = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Некорректная страница.")
        return

    model_ids = await _get_free_model_ids()
    chat_id = str(query.message.chat_id) if query.message else ""
    user_id = str(query.from_user.id) if query.from_user else ""
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_free_models_page(model_ids, page, current_model)
    markup = _build_free_models_markup(resolved_page, total_pages)

    await query.answer()
    if query.message:
        await query.edit_message_text(message, reply_markup=markup)


async def models_paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия пагинации для /models_paid."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(_MODELS_PAID_CALLBACK_PREFIX):
        return

    try:
        page = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Некорректная страница.")
        return

    model_ids = await _get_model_ids_by_category("paid")
    chat_id = str(query.message.chat_id) if query.message else ""
    user_id = str(query.from_user.id) if query.from_user else ""
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_models_page(
        "💳 Платные модели",
        model_ids,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_model",
    )
    markup = _build_models_markup(_MODELS_PAID_CALLBACK_PREFIX, resolved_page, total_pages)

    await query.answer()
    if query.message:
        await query.edit_message_text(message, reply_markup=markup)


async def models_large_context_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия пагинации для /models_large_context."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(_MODELS_LARGE_CALLBACK_PREFIX):
        return

    try:
        page = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Некорректная страница.")
        return

    model_ids = await _get_model_ids_by_category("large_context")
    chat_id = str(query.message.chat_id) if query.message else ""
    user_id = str(query.from_user.id) if query.from_user else ""
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_models_page(
        "📦 Модели с большим контекстом",
        model_ids,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_model",
    )
    markup = _build_models_markup(_MODELS_LARGE_CALLBACK_PREFIX, resolved_page, total_pages)

    await query.answer()
    if query.message:
        await query.edit_message_text(message, reply_markup=markup)


async def models_pic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия пагинации для /models_pic."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(_MODELS_PIC_CALLBACK_PREFIX):
        return

    try:
        page = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Некорректная страница.")
        return

    piapi_models, imagerouter_models, combined_models = await _refresh_image_models()
    items = _build_image_model_items(piapi_models, imagerouter_models, combined_models)
    current_model = BOT_CONFIG.get("IMAGE_GENERATION", {}).get("MODEL")
    _store_image_model_list(context, combined_models)
    message, resolved_page, total_pages = _build_models_page(
        "🖼️ Модели генерации изображений",
        items,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_pic_model",
    )
    markup = _build_models_markup(_MODELS_PIC_CALLBACK_PREFIX, resolved_page, total_pages)

    await query.answer()
    if query.message:
        await query.edit_message_text(message, reply_markup=markup)


async def models_specialized_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия пагинации для /models_specialized."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(_MODELS_SPECIALIZED_CALLBACK_PREFIX):
        return

    try:
        page = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Некорректная страница.")
        return

    model_ids = await _get_model_ids_by_category("specialized")
    chat_id = str(query.message.chat_id) if query.message else ""
    user_id = str(query.from_user.id) if query.from_user else ""
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_models_page(
        "🎯 Специализированные модели",
        model_ids,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_model",
    )
    markup = _build_models_markup(_MODELS_SPECIALIZED_CALLBACK_PREFIX, resolved_page, total_pages)

    await query.answer()
    if query.message:
        await query.edit_message_text(message, reply_markup=markup)


async def models_paid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает платные модели."""
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    model_ids = await _get_model_ids_by_category("paid")
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_models_page(
        "💳 Платные модели",
        model_ids,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_model",
    )
    markup = _build_models_markup(_MODELS_PAID_CALLBACK_PREFIX, resolved_page, total_pages)
    await update.message.reply_text(message, reply_markup=markup)


async def models_large_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели с большим контекстом."""
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    model_ids = await _get_model_ids_by_category("large_context")
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_models_page(
        "📦 Модели с большим контекстом",
        model_ids,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_model",
    )
    markup = _build_models_markup(_MODELS_LARGE_CALLBACK_PREFIX, resolved_page, total_pages)
    await update.message.reply_text(message, reply_markup=markup)


async def models_specialized_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает специализированные модели."""
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    model_ids = await _get_model_ids_by_category("specialized")
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    current_model = get_preferred_model(chat_id, user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    _store_model_list(context, model_ids)
    message, resolved_page, total_pages = _build_models_page(
        "🎯 Специализированные модели",
        model_ids,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_model",
    )
    markup = _build_models_markup(_MODELS_SPECIALIZED_CALLBACK_PREFIX, resolved_page, total_pages)
    await update.message.reply_text(message, reply_markup=markup)


async def models_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает полный список моделей по категориям."""
    await _send_models(update, ["free", "large_context", "specialized", "paid"], MODELS_HINT_TEXT, max_items=None)


async def models_voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели распознавания речи."""
    await update.message.reply_text(_build_voice_models_text(), parse_mode="Markdown")


async def models_voice_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели распознавания для голосовых логов."""
    await update.message.reply_text(_build_voice_log_models_text(), parse_mode="Markdown")


async def models_pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели генерации изображений."""
    piapi_models, imagerouter_models, combined_models = await _refresh_image_models()
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    items = _build_image_model_items(piapi_models, imagerouter_models, combined_models)
    current_model = BOT_CONFIG.get("IMAGE_GENERATION", {}).get("MODEL")
    _store_image_model_list(context, combined_models)
    message, resolved_page, total_pages = _build_models_page(
        "🖼️ Модели генерации изображений",
        items,
        page,
        current_model,
        page_size=_MODELS_PAGE_SIZE,
        set_command="set_pic_model",
    )
    markup = _build_models_markup(_MODELS_PIC_CALLBACK_PREFIX, resolved_page, total_pages)
    await update.message.reply_text(message, reply_markup=markup)


async def set_model_number_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меняет модель генерации текста по номеру из последнего списка."""
    if not update.message or not update.message.text:
        return

    index = _parse_index_command(update.message.text, "set_model")
    if index is None:
        return

    model_ids = context.user_data.get("model_select_list") if context else None
    if not model_ids:
        model_ids = await _get_free_model_ids()

    if not model_ids:
        await update.message.reply_text("Список моделей пуст. Сначала открой список моделей.")
        return

    if index < 1 or index > len(model_ids):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = model_ids[index - 1]
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    set_preferred_model(chat_id, user_id, selected)
    await update.message.reply_text(f"✅ Модель текста установлена: {selected}")


async def set_pic_model_number_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меняет модель генерации изображений по номеру из последнего списка."""
    if not update.message or not update.message.text:
        return

    index = _parse_index_command(update.message.text, "set_pic_model")
    if index is None:
        return

    model_ids = context.user_data.get("image_model_select_list") if context else None
    if not model_ids:
        _piapi_models, _imagerouter_models, model_ids = await _refresh_image_models()

    if not model_ids:
        await update.message.reply_text("Список моделей генерации изображений пуст.")
        return

    if index < 1 or index > len(model_ids):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = model_ids[index - 1]
    BOT_CONFIG.setdefault("IMAGE_GENERATION", {})["MODEL"] = selected
    await update.message.reply_text(f"✅ Модель генерации изображений установлена: {selected}")


async def set_text_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меняет модель генерации текста для пользователя в текущем чате."""
    model_ids = await _get_free_model_ids()
    if not model_ids:
        await update.message.reply_text("Список бесплатных моделей пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /set_text_model <номер>")
        return

    index = int(args[0])
    if index < 1 or index > len(model_ids):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = model_ids[index - 1]
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    set_preferred_model(chat_id, user_id, selected)
    await update.message.reply_text(f"✅ Модель текста установлена: {selected}")


async def set_pic_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меняет модель генерации изображений."""
    _piapi_models, _imagerouter_models, image_models = await _refresh_image_models()
    if not image_models:
        await update.message.reply_text("Список моделей генерации изображений пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /set_pic_model <номер>")
        return

    index = int(args[0])
    if index < 1 or index > len(image_models):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = image_models[index - 1]
    BOT_CONFIG.setdefault("IMAGE_GENERATION", {})["MODEL"] = selected
    await update.message.reply_text(f"✅ Модель генерации изображений установлена: {selected}")
