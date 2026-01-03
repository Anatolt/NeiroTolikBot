import logging
import time
from io import BytesIO
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from config import BOT_CONFIG
from services.memory import (
    add_admin,
    add_message,
    clear_memory,
    get_discord_voice_channels,
    get_notification_flows,
    get_telegram_chats,
    get_all_admins,
    get_routing_mode,
    get_preferred_model,
    get_voice_log_debug,
    get_voice_log_model,
    get_voice_model,
    get_voice_transcribe_mode,
    is_admin,
    add_notification_flow,
    remove_notification_flow,
    set_routing_mode,
    set_show_response_header,
    start_new_dialog,
    set_voice_auto_reply,
    set_voice_log_debug,
    set_voice_log_model,
    set_voice_model,
    set_voice_transcribe_mode,
    set_preferred_model,
)
from services.generation import (
    CATEGORY_TITLES,
    build_models_messages,
    categorize_models,
    fetch_models_data,
    fetch_imagerouter_models,
)
from services.analytics import log_text_usage
from services.consilium import (
    parse_consilium_request,
    select_default_consilium_models,
    generate_consilium_responses,
    format_consilium_results,
)

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

ADMIN_COMMANDS_TEXT = (
    "👑 Команды администратора:\n"
    "• /flow — показать текущие связи Discord → Telegram\n"
    "• /setflow — настроить связь Discord → Telegram\n"
    "• /unsetflow — отключить связь Discord → Telegram\n"
    "• /show_discord_chats — показать голосовые чаты Discord\n"
    "• /show_tg_chats — показать чаты Telegram, где есть бот\n"
    "• /voice_log_debug_on — включить подробный лог распознавания\n"
    "• /voice_log_debug_off — отключить подробный лог распознавания\n"
    "• /selftest — офлайн-проверка слеш-команд (отправляет файл)\n"
    "• /admin_help — показать эту справку\n"
    "\n"
    "🎙️ Голосовые модели:\n"
    "• /models_voice — список моделей распознавания\n"
    "• /set_voice_model <номер> — выбрать модель распознавания\n"
    "• /voice_send_raw — слать аудио без нарезки (дороже, лимит 25MB)\n"
    "• /voice_send_segmented — слать аудио с нарезкой (лимит 25MB)\n"
    "\n"
    "Текстовые команды:\n"
    "• покажи чаты дискорд\n"
    "• покажи чаты тг"
)

_ROMAN_NUMERALS = [
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
    "xi",
    "xii",
    "xiii",
    "xiv",
    "xv",
    "xvi",
    "xvii",
    "xviii",
    "xix",
    "xx",
]


def _index_to_letter(index: int) -> str:
    result = ""
    value = index
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _letter_to_index(value: str) -> Optional[int]:
    if not value or not value.isalpha():
        return None
    value = value.upper()
    index = 0
    for char in value:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def _index_to_roman(index: int) -> str:
    if 1 <= index <= len(_ROMAN_NUMERALS):
        return _ROMAN_NUMERALS[index - 1]
    return str(index)


def _roman_to_index(value: str) -> Optional[int]:
    if not value:
        return None
    value = value.lower().strip()
    if value in _ROMAN_NUMERALS:
        return _ROMAN_NUMERALS.index(value) + 1
    if value.isdigit():
        return int(value)
    return None


def _is_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    return is_admin(chat_id, user_id) or context.user_data.get("is_admin", False)


def _format_discord_voice_channels() -> str:
    channels = get_discord_voice_channels()
    if not channels:
        return "Не нашёл голосовые чаты Discord. Проверь, что Discord-бот запущен."

    grouped: dict[str, list[str]] = {}
    for channel in channels:
        guild_name = channel.get("guild_name") or "Без сервера"
        channel_name = channel.get("channel_name") or channel.get("channel_id")
        grouped.setdefault(guild_name, []).append(channel_name)

    lines = ["🎧 Голосовые чаты Discord:"]
    for guild_name, channel_names in grouped.items():
        lines.append(f"\n{guild_name}:")
        for name in channel_names:
            lines.append(f"• {name}")

    return "\n".join(lines)


def _format_telegram_chats() -> str:
    chats = get_telegram_chats()
    if not chats:
        return "Не нашёл чаты Telegram. Напишите боту хотя бы одно сообщение в нужном чате."

    lines = ["💬 Чаты Telegram:"]
    for chat in chats:
        title = chat.get("title") or "Без названия"
        chat_type = chat.get("chat_type") or "unknown"
        chat_id = chat.get("chat_id")
        lines.append(f"• {title} ({chat_type}) — {chat_id}")

    return "\n".join(lines)

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
    if is_admin(chat_id, user_id) or context.user_data.get("is_admin"):
        await update.message.reply_text(
            f"Уже в режиме админа. Бот запущен: {BOT_CONFIG.get('BOOT_TIME')}"
        )
        return

    context.user_data["awaiting_admin_pass"] = True
    await update.message.reply_text("Введите пароль администратора:")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help - справка по командам."""
    user = update.effective_user
    user_mention = user.full_name
    
    text = (
        f"Привет, {user_mention}! Вот список доступных команд:\n\n"
        f"📝 /new - Начать новый диалог (сохраняет историю для будущего использования)\n"
        f"🧹 /clear - Полностью очистить память бота\n"
        f"❓ /help - Показать эту справку\n"
        f"🤖 /models - Подсказка по спискам моделей\n"
        f"   /models_free, /models_paid, /models_large_context, /models_specialized\n"
        f"   /models_all — полный список моделей\n"
        f"🔀 /rout_algo или /rout_llm — выбрать алгоритмический или LLM роутинг\n"
        f"   /rout — показать текущий режим\n"
        f"🛠 /header_on или /header_off — показать или спрятать техшапку над ответом\n"
        f"🏥 /consilium - Получить ответы от нескольких моделей одновременно\n\n"
        f"Также вы можете:\n"
        f"• Задавать вопросы боту\n"
        f"• Просить нарисовать картинки\n"
        f"• Указывать модель для ответа (например, 'chatgpt расскажи о погоде')\n"
        f"• Использовать консилиум: 'консилиум: ваш вопрос' или 'консилиум через chatgpt, claude: вопрос'\n"
        f"• Написать 'модели' или 'models' для просмотра списка моделей"
    )
    
    await update.message.reply_text(text=text)


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по административным командам."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    await update.message.reply_text(ADMIN_COMMANDS_TEXT)


async def show_discord_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список голосовых чатов Discord (для админов)."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    await update.message.reply_text(_format_discord_voice_channels())


async def show_tg_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список чатов Telegram (для админов)."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    await update.message.reply_text(_format_telegram_chats())


async def setflow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Настраивает связь Discord-канала и Telegram-чата для уведомлений."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    args = context.args or []
    discord_channels = get_discord_voice_channels()
    telegram_chats = get_telegram_chats()

    if len(args) >= 2:
        discord_index = args[0]
        telegram_index = args[1]

        if not discord_index.isdigit():
            await update.message.reply_text("Первый аргумент должен быть номером Discord-канала.")
            return

        discord_pos = int(discord_index)
        telegram_pos = _letter_to_index(telegram_index)

        if discord_pos < 1 or discord_pos > len(discord_channels):
            await update.message.reply_text("Номер Discord-канала вне диапазона.")
            return

        if telegram_pos is None or telegram_pos < 1 or telegram_pos > len(telegram_chats):
            await update.message.reply_text("Буква Telegram-чата вне диапазона.")
            return

        discord_channel = discord_channels[discord_pos - 1]
        telegram_chat = telegram_chats[telegram_pos - 1]

        add_notification_flow(
            discord_channel_id=str(discord_channel["channel_id"]),
            telegram_chat_id=str(telegram_chat["chat_id"]),
        )
        await update.message.reply_text(
            f"Готово! Связал Discord «{discord_channel.get('channel_name')}» "
            f"с Telegram «{telegram_chat.get('title') or telegram_chat.get('chat_id')}»."
        )
        return

    if not discord_channels or not telegram_chats:
        discord_info = _format_discord_voice_channels()
        telegram_info = _format_telegram_chats()
        await update.message.reply_text(f"{discord_info}\n\n{telegram_info}")
        return

    discord_lines = ["🎧 Голосовые чаты Discord (по номерам):"]
    for idx, channel in enumerate(discord_channels, start=1):
        guild_name = channel.get("guild_name") or "Без сервера"
        channel_name = channel.get("channel_name") or channel.get("channel_id")
        discord_lines.append(f"{idx}) {guild_name} / {channel_name} — {channel.get('channel_id')}")

    telegram_lines = ["💬 Чаты Telegram (по буквам):"]
    for idx, chat in enumerate(telegram_chats, start=1):
        letter = _index_to_letter(idx)
        title = chat.get("title") or "Без названия"
        chat_type = chat.get("chat_type") or "unknown"
        telegram_lines.append(f"{letter}) {title} ({chat_type}) — {chat.get('chat_id')}")

    instruction = "\n\nЧтобы связать, отправьте: /setflow <номер> <буква>\nПример: /setflow 2 C"

    await update.message.reply_text(
        "\n".join(discord_lines) + "\n\n" + "\n".join(telegram_lines) + instruction
    )


async def flow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текущие настройки flows Discord -> Telegram."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    flows = get_notification_flows()
    if not flows:
        await update.message.reply_text(
            "Связки Discord → Telegram не настроены.\n"
            "Подсказка: используйте /setflow, чтобы связать голосовой канал с Telegram-чатом."
        )
        return

    discord_channels = {c["channel_id"]: c for c in get_discord_voice_channels()}
    telegram_chats = {c["chat_id"]: c for c in get_telegram_chats()}

    lines = ["🔁 Текущие связи Discord → Telegram:"]
    for idx, flow in enumerate(flows, start=1):
        roman = _index_to_roman(idx)
        discord_info = discord_channels.get(flow["discord_channel_id"], {})
        telegram_info = telegram_chats.get(flow["telegram_chat_id"], {})
        discord_name = discord_info.get("channel_name") or flow["discord_channel_id"]
        discord_guild = discord_info.get("guild_name") or "Без сервера"
        telegram_title = telegram_info.get("title") or flow["telegram_chat_id"]
        lines.append(
            f"{roman}) {discord_guild} / {discord_name} → {telegram_title} ({flow['telegram_chat_id']})"
        )

    lines.append("\nПодсказка: /setflow — добавить связь, /unsetflow — удалить связь.")
    await update.message.reply_text("\n".join(lines))


async def unsetflow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет настройку flow по римской цифре."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    flows = get_notification_flows()
    if not flows:
        await update.message.reply_text("Связки Discord → Telegram не настроены.")
        return

    args = context.args or []
    if not args:
        lines = ["🧹 Выберите связь для удаления:"]
        discord_channels = {c["channel_id"]: c for c in get_discord_voice_channels()}
        telegram_chats = {c["chat_id"]: c for c in get_telegram_chats()}

        for idx, flow in enumerate(flows, start=1):
            roman = _index_to_roman(idx)
            discord_info = discord_channels.get(flow["discord_channel_id"], {})
            telegram_info = telegram_chats.get(flow["telegram_chat_id"], {})
            discord_name = discord_info.get("channel_name") or flow["discord_channel_id"]
            discord_guild = discord_info.get("guild_name") or "Без сервера"
            telegram_title = telegram_info.get("title") or flow["telegram_chat_id"]
            lines.append(
                f"{roman}) {discord_guild} / {discord_name} → {telegram_title} ({flow['telegram_chat_id']})"
            )

        lines.append("\nЧтобы удалить, отправьте: /unsetflow <римская_цифра>")
        await update.message.reply_text("\n".join(lines))
        return

    index = _roman_to_index(args[0])
    if index is None or index < 1 or index > len(flows):
        await update.message.reply_text("Некорректный номер связки. Используйте римскую цифру из списка.")
        return

    flow = flows[index - 1]
    remove_notification_flow(int(flow["id"]))
    await update.message.reply_text("Связка удалена.")


def _format_routing_mode_label(mode: str) -> str:
    return "алгоритмический" if mode == "rules" else "LLM"


async def routing_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает алгоритмический роутер для пользователя."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_routing_mode(chat_id, user_id, "rules")
    await update.message.reply_text(
        "🔀 Включён алгоритмический роутинг. Чтобы вернуться к LLM, используйте /rout_llm или напишите 'роутинг ллм'."
    )


async def routing_llm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает LLM роутер для пользователя."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_routing_mode(chat_id, user_id, "llm")
    await update.message.reply_text(
        "🔀 Включён LLM роутинг. Чтобы вернуться к алгоритмам, используйте /rout_algo или напишите 'роутинг алгоритмами'."
    )


async def routing_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текущий режим роутинга для пользователя."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    current_mode = get_routing_mode(chat_id, user_id) or BOT_CONFIG.get("ROUTING_MODE", "rules")
    await update.message.reply_text(
        "🔎 Текущий режим роутинга: "
        f"{_format_routing_mode_label(current_mode)}.\n"
        "Переключение: /rout_algo (алгоритмы), /rout_llm (LLM)."
    )


async def voice_msg_conversation_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает автоответ на голосовые сообщения."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_voice_auto_reply(chat_id, user_id, True)
    await update.message.reply_text(
        "🔊 Автоответ на голосовые сообщения включён.\n"
        "Отключить: /voice_msg_conversation_off"
    )


async def voice_msg_conversation_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает автоответ на голосовые сообщения."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_voice_auto_reply(chat_id, user_id, False)
    await update.message.reply_text(
        "🔇 Автоответ на голосовые сообщения отключён.\n"
        "Включить: /voice_msg_conversation_on"
    )


async def header_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает вывод техшапки над ответами."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_show_response_header(chat_id, user_id, True)
    await update.message.reply_text(
        "🛠 Техшапка включена. Чтобы скрыть, используйте /header_off или отправьте 'скрыть шапку'."
    )


async def header_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает вывод техшапки над ответами."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_show_response_header(chat_id, user_id, False)
    await update.message.reply_text(
        "🫥 Техшапка скрыта. Чтобы вернуть её, используйте /header_on или отправьте 'показывай шапку'."
    )

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


async def set_voice_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меняет модель распознавания речи."""
    voice_models = BOT_CONFIG.get("VOICE_MODELS", [])
    if not voice_models:
        await update.message.reply_text("Список моделей распознавания речи пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        lines = ["Использование: /set_voice_model <номер>", "", "Доступные модели:"]
        for idx, model in enumerate(voice_models, start=1):
            lines.append(f"{idx}) {model}")
        await update.message.reply_text("\n".join(lines))
        return

    index = int(args[0])
    if index < 1 or index > len(voice_models):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = voice_models[index - 1]
    set_voice_model(selected)
    set_voice_log_model(selected)
    await update.message.reply_text(
        f"✅ Модель распознавания речи установлена: {selected}\n"
        "Также обновил модель для голосовых логов."
    )


async def voice_send_raw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает отправку аудио в STT без нарезки."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    set_voice_transcribe_mode("raw")
    await update.message.reply_text(
        "✅ Режим отправки аудио: raw (без нарезки).\n"
        "Это дороже. Переключить: /voice_send_segmented"
    )


async def voice_send_segmented_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает отправку аудио в STT с нарезкой."""
    if not _is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    set_voice_transcribe_mode("segmented")
    await update.message.reply_text(
        "✅ Режим отправки аудио: segmented (с нарезкой).\n"
        "Переключить: /voice_send_raw"
    )


async def set_voice_log_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меняет модель распознавания для голосовых логов."""
    voice_models = BOT_CONFIG.get("VOICE_MODELS", [])
    if not voice_models:
        await update.message.reply_text("Список моделей распознавания речи пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /set_voice_log_model <номер>")
        return

    index = int(args[0])
    if index < 1 or index > len(voice_models):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = voice_models[index - 1]
    set_voice_log_model(selected)
    await update.message.reply_text(
        f"✅ Модель распознавания логов установлена: {selected}"
    )


async def voice_log_debug_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает подробный лог распознавания."""
    set_voice_log_debug(True)
    await update.message.reply_text("✅ Подробный лог распознавания включен.")


async def voice_log_debug_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отключает подробный лог распознавания."""
    set_voice_log_debug(False)
    await update.message.reply_text("✅ Подробный лог распознавания отключен.")


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


async def selftest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает офлайн-проверку слеш-команд и отправляет файл с результатами."""

    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    status_message = await update.message.reply_text(
        "🔎 Запускаю офлайн-тест слеш-команд. Это может занять несколько секунд..."
    )

    try:
        # Импортируем внутри функции, чтобы избежать циклических зависимостей
        from utils.console_tester import run_command_tests

        results = await run_command_tests(chat_id, user_id)
    except Exception as e:  # pragma: no cover - для телеграм-обработчика
        logger.exception("Selftest failed: %s", e)
        await status_message.edit_text(f"❌ Не удалось выполнить selftest: {e}")
        return

    passed = sum(1 for _name, ok, _details in results if ok)
    total = len(results)

    lines = [
        "Результаты офлайн-теста слеш-команд:",
        f"Чат: {chat_id}",
        f"Пользователь: {user_id}",
        "",
    ]

    for name, success, details in results:
        status = "✅" if success else "❌"
        lines.append(f"{status} {name}")
        lines.append(f"    {details}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend(
        [
            "",
            f"Итого: {passed}/{total} успешных проверок",
        ]
    )

    buffer = BytesIO("\n".join(lines).encode("utf-8"))
    buffer.name = "selftest_results.txt"
    buffer.seek(0)

    await status_message.delete()

    await update.message.reply_document(
        document=buffer,
        caption=f"Selftest завершён: {passed}/{total} успешных проверок.",
    )


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
            "• /consilium: ваш вопрос — автоматический выбор 3 моделей\n"
            "• /consilium через chatgpt, claude, deepseek: ваш вопрос — указанные модели\n"
            "• консилиум: ваш вопрос — через текст\n"
            "• консилиум через chatgpt, claude: ваш вопрос — через текст с моделями\n\n"
            "Примеры:\n"
            "• /consilium: какая погода в Москве?\n"
            "• /consilium через chatgpt, claude: объясни квантовую физику"
        )
        await message.reply_text(help_text)
        return
    
    full_text = f"консилиум {command_text}"

    models, prompt, has_colon = parse_consilium_request(full_text)
    if not has_colon:
        await message.reply_text(
            "❗ Для консилиума нужен двоеточие после списка моделей.\n"
            "Пример: /consilium gpt, claude: ваш вопрос"
        )
        return

    if not prompt:
        await message.reply_text("❌ Не указан вопрос для консилиума. Используйте: /consilium модели: ваш вопрос")
        return

    if not models:
        models = await select_default_consilium_models()
        if not models:
            await message.reply_text("❌ Не удалось выбрать модели для консилиума. Попробуйте указать модели явно.")
            return

    pending = context.user_data.get("pending_consilium_requests", {})
    key = f"{chat_id}:{user_id}"
    pending[key] = {"prompt": prompt, "models": models}
    context.user_data["pending_consilium_requests"] = pending

    models_list = ", ".join(models)
    await message.reply_text(
        "🏥 Консилиум готов к запуску.\n"
        f"Модели: {models_list}\n"
        f"Вопрос: {prompt}\n"
        "Нужен ответ? /yes"
    )


async def execute_consilium_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, models: list[str]
) -> None:
    message = update.message
    if not message:
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    status_message = await message.reply_text(f"🏥 Генерирую ответы от {len(models)} моделей...")

    if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
        add_message(chat_id, user_id, "user", models[0], prompt)

    start_time = time.time()
    results = await generate_consilium_responses(prompt, models, chat_id, user_id)
    execution_time = time.time() - start_time
    formatted_messages = format_consilium_results(results, execution_time)

    try:
        await status_message.delete()
    except Exception as e:
        logger.warning(f"Could not delete status message: {e}")

    if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
        for result in results:
            if result.get("success") and result.get("response"):
                add_message(chat_id, user_id, "assistant", result.get("model"), result.get("response"))

    for result in results:
        if result.get("success") and result.get("response"):
            log_text_usage(
                platform="telegram",
                chat_id=str(chat_id),
                user_id=str(user_id),
                model_id=result.get("model"),
                prompt=prompt,
                response=result.get("response"),
            )

    max_length = 4000
    for msg in formatted_messages:
        if len(msg) > max_length:
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

            for i, part in enumerate(parts):
                if i == 0:
                    await message.reply_text(part)
                else:
                    await message.reply_text(
                        f"*(продолжение {i+1}/{len(parts)})*\n\n{part}", parse_mode="Markdown"
                    )
        else:
            await message.reply_text(msg)
