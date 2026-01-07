import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import BOT_CONFIG
from services.generation import (
    CATEGORY_TITLES,
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
    "🖼️ /models_pic — модели генерации изображений\n\n"
    "Можно также написать: 'покажи бесплатные модели', 'покажи платные модели' и т.д."
)

_MODELS_FREE_PAGE_SIZE = 15
_MODELS_FREE_CALLBACK_PREFIX = "models_free:page:"


def _build_image_models_text(
    piapi_models: list[str],
    imagerouter_models: list[str],
    combined_models: list[str],
) -> str:
    model = BOT_CONFIG.get("IMAGE_GENERATION", {}).get("MODEL")
    lines = ["🖼️ Модели генерации изображений:"]
    if model:
        lines.append(f"Текущая: {model}")
    if not combined_models:
        lines.append("Список моделей генерации изображений пуст.")
        return "\n".join(lines)

    index = 1
    seen: set[str] = set()

    def _append_section(title: str, models: list[str], index: int) -> int:
        added = False
        for item in models:
            if item in seen:
                continue
            if not added:
                lines.append(title)
                added = True
            seen.add(item)
            lines.append(f"{index}) {item} — `/set_pic_model {index}`")
            index += 1
        return index

    index = _append_section("PiAPI:", piapi_models, index)
    _append_section("ImageRouter:", imagerouter_models, index)
    return "\n".join(lines)


async def _reply_text_in_parts(
    update: Update, text: str, parse_mode: str | None = None, max_length: int = 4000
) -> None:
    if len(text) <= max_length:
        await update.message.reply_text(text, parse_mode=parse_mode)
        return

    parts: list[str] = []
    current_part = ""
    for line in text.split("\n"):
        if len(current_part) + len(line) + 1 > max_length:
            if current_part:
                parts.append(current_part)
            current_part = line + "\n"
        else:
            current_part += line + "\n"

    if current_part:
        parts.append(current_part)

    for idx, part in enumerate(parts):
        if idx == 0:
            await update.message.reply_text(part, parse_mode=parse_mode)
        else:
            await update.message.reply_text(
                f"*(продолжение {idx + 1}/{len(parts)})*\n\n{part}",
                parse_mode="Markdown",
            )


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


def _build_free_models_page(
    model_ids: list[str],
    page: int,
    current_model: str | None,
    page_size: int = _MODELS_FREE_PAGE_SIZE,
) -> tuple[str, int, int]:
    total = len(model_ids)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    lines = [f"🆓 Бесплатные модели (страница {page}/{total_pages}):"]
    if current_model:
        lines.append(f"Текущая: {current_model}")
    if not model_ids:
        lines.append("Список моделей пуст.")
        return "\n".join(lines), page, total_pages

    for idx, model_id in enumerate(model_ids[start:end], start=start + 1):
        lines.append(f"{idx}) {model_id} — `/set_text_model {idx}`")

    return "\n".join(lines), page, total_pages


def _build_free_models_markup(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None

    prev_page = page - 1 if page > 1 else total_pages
    next_page = page + 1 if page < total_pages else 1
    keyboard = [
        [
            InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"{_MODELS_FREE_CALLBACK_PREFIX}{prev_page}"),
            InlineKeyboardButton("Следующая ➡️", callback_data=f"{_MODELS_FREE_CALLBACK_PREFIX}{next_page}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


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
    message, resolved_page, total_pages = _build_free_models_page(model_ids, page, current_model)
    markup = _build_free_models_markup(resolved_page, total_pages)
    await update.message.reply_text(message, parse_mode="Markdown", reply_markup=markup)


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
    message, resolved_page, total_pages = _build_free_models_page(model_ids, page, current_model)
    markup = _build_free_models_markup(resolved_page, total_pages)

    await query.answer()
    if query.message:
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=markup)


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


async def models_voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели распознавания речи."""
    await update.message.reply_text(_build_voice_models_text(), parse_mode="Markdown")


async def models_voice_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели распознавания для голосовых логов."""
    await update.message.reply_text(_build_voice_log_models_text(), parse_mode="Markdown")


async def models_pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели генерации изображений."""
    piapi_models, imagerouter_models, combined_models = await _refresh_image_models()
    await _reply_text_in_parts(
        update,
        _build_image_models_text(piapi_models, imagerouter_models, combined_models),
        parse_mode="Markdown",
    )


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
