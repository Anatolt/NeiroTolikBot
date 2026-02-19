from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from handlers.commands_utils import is_admin_user
from services.memory import (
    add_notification_flow,
    get_discord_voice_channels,
    get_notification_flows,
    get_telegram_chats,
    remove_notification_flow,
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


def _format_discord_voice_channels() -> str:
    channels = get_discord_voice_channels()
    if not channels:
        return "Не нашёл голосовые чаты Discord. Проверь, что Discord-бот запущен."

    grouped: dict[str, list[dict[str, str]]] = {}
    guilds: dict[str, str] = {}
    for index, channel in enumerate(channels, start=1):
        guild_name = str(channel.get("guild_name") or "Без сервера")
        guild_id = str(channel.get("guild_id") or "").strip() or "unknown"
        channel_name = str(channel.get("channel_name") or channel.get("channel_id") or "unknown")
        channel_id = str(channel.get("channel_id") or "").strip() or "unknown"
        grouped.setdefault(guild_name, []).append(
            {
                "index": str(index),
                "channel_name": channel_name,
                "channel_id": channel_id,
            }
        )
        guilds[guild_id] = guild_name

    lines = [
        "🎧 Голосовые чаты Discord:",
        "Серверы (рядом команды для быстрого копипаста):",
    ]
    ordered_guilds = sorted(guilds.items(), key=lambda item: item[1].lower())
    for server_index, (guild_id, guild_name) in enumerate(ordered_guilds, start=1):
        lines.append(f"{server_index}) {guild_name} — guild_id: {guild_id}")
        lines.append(f"   /voice_chunks_status {guild_id} | /voice_chunks_on {guild_id} | /voice_chunks_off {guild_id}")
        lines.append(f"   /voice_alerts_status {guild_id} | /voice_alerts_on {guild_id} | /voice_alerts_off {guild_id}")

    for guild_name in sorted(grouped.keys(), key=lambda item: item.lower()):
        lines.append(f"\n{guild_name}:")
        entries = sorted(grouped[guild_name], key=lambda item: item["channel_name"].lower())
        for entry in entries:
            lines.append(
                f"• {entry['channel_name']} (channel_id: {entry['channel_id']}) "
                f"— /setflow {entry['index']} <буква_чата>"
            )
    lines.append(
        "\nПодсказка: в /voice_chunks_* и /voice_alerts_* можно указывать guild_id или номер сервера из списка."
    )
    lines.append(
        "Быстрый статус тоже поддерживается: /voice_chunks_<guild_id> и /voice_alerts_<guild_id>."
    )

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


async def show_discord_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список голосовых чатов Discord (для админов)."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    await update.message.reply_text(_format_discord_voice_channels())


async def show_tg_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список чатов Telegram (для админов)."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    await update.message.reply_text(_format_telegram_chats())


async def setflow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Настраивает связь Discord-канала и Telegram-чата для уведомлений."""
    if not is_admin_user(update, context):
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
    if not is_admin_user(update, context):
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
    if not is_admin_user(update, context):
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
