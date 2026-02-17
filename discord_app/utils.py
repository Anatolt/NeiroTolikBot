import re

import discord

from config import BOT_CONFIG


def extract_discord_channel_link(text: str) -> tuple[str, str] | None:
    match = re.search(r"https?://(?:www\.)?discord\.com/channels/(\d+)/(\d+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


def extract_discord_invite_link(text: str) -> str | None:
    match = re.search(
        r"https?://(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)",
        text,
    )
    if not match:
        return None
    return match.group(1)


def build_start_message(display_name: str | None) -> str:
    user = display_name or "там"
    default_model = BOT_CONFIG["DEFAULT_MODEL"]
    return (
        f"Привет, {user}! Я бот-помощник.\n\n"
        f"📝 Спроси меня что-нибудь — отвечу через {default_model}.\n"
        "🎨 Попроси нарисовать картинку (например, 'нарисуй закат над морем').\n"
        "🤖 Хочешь другую модель? Укажи ее в начале или конце запроса (например, 'chatgpt какой сегодня день?').\n"
        "❓ Команды и помощь: /help"
    )


def build_discord_help_message() -> str:
    return (
        "Команды Discord-бота:\n"
        "• /start — краткое приветствие\n"
        "• /help — справка по командам\n"
        "• /join — подключиться к голосовому каналу, где вы сейчас\n"
        "• /leave — выйти из голосового канала\n"
        "• /say — озвучить текст в голосовом канале\n"
        "• /transcripts_on — включить отправку транскрипций\n"
        "• /transcripts_off — отключить отправку транскрипций\n"
        "• /voice_alerts_on — включить Telegram-оповещения о voice-событиях\n"
        "• /voice_alerts_off — отключить Telegram-оповещения о voice-событиях\n"
        "• /voice_alerts_status — статус voice-оповещений\n"
        "• /summary_now — сделать саммари голосового чата сейчас\n"
        "• /autojoin_on — включить автоподключение к голосу\n"
        "• /autojoin_off — отключить автоподключение к голосу\n\n"
        "В серверах бот отвечает по упоминанию @ИмяБота или с префиксами !/.\n"
        "В личных сообщениях отвечает на любой текст."
    )


def strip_bot_mention(content: str, bot_user: discord.User | discord.ClientUser | None) -> str:
    if not bot_user:
        return content

    cleaned = content
    mention_variants = [f"<@{bot_user.id}>", f"<@!{bot_user.id}>", f"@{bot_user.name}"]
    for mention in mention_variants:
        cleaned = cleaned.replace(mention, "")
    return cleaned.strip()


def format_cost_estimate(cost: float | None) -> str:
    if cost is None:
        return "неизвестно"
    return f"${cost:.4f}"


def count_humans_in_voice(
    channel: discord.abc.GuildChannel, exclude_member_id: int | None = None
) -> int:
    include_bots = bool(BOT_CONFIG.get("VOICE_TEST_ALLOW_BOT_AUDIO", False))
    members = getattr(channel, "members", None) or []
    members_count = 0
    for member in members:
        if member.bot and not include_bots:
            continue
        if exclude_member_id is not None and member.id == exclude_member_id:
            continue
        voice_state = getattr(member, "voice", None)
        if not voice_state or not voice_state.channel or voice_state.channel.id != channel.id:
            continue
        members_count += 1

    voice_states_count = None
    guild = getattr(channel, "guild", None)
    if guild:
        count = 0
        voice_states = getattr(guild, "voice_states", None)
        if voice_states is None:
            voice_states = getattr(guild, "_voice_states", None)
        if not voice_states:
            voice_states = {}
        for member_id, voice_state in voice_states.items():
            if not voice_state or not voice_state.channel:
                continue
            if voice_state.channel.id != channel.id:
                continue
            if exclude_member_id is not None and member_id == exclude_member_id:
                continue
            member = guild.get_member(member_id)
            if member and member.bot and not include_bots:
                continue
            count += 1
        voice_states_count = count

    if voice_states_count is not None:
        return max(members_count, voice_states_count)

    return members_count


def list_human_names_in_voice(
    channel: discord.abc.GuildChannel, exclude_member_id: int | None = None
) -> list[str]:
    members = getattr(channel, "members", None) or []
    names: list[str] = []
    for member in members:
        if getattr(member, "bot", False):
            continue
        if exclude_member_id is not None and getattr(member, "id", None) == exclude_member_id:
            continue
        voice_state = getattr(member, "voice", None)
        if not voice_state or not voice_state.channel or voice_state.channel.id != channel.id:
            continue
        names.append(getattr(member, "display_name", None) or getattr(member, "name", ""))
    return [n for n in names if n]


async def list_human_names_in_voice_via_states(
    channel: discord.abc.GuildChannel, exclude_member_id: int | None = None
) -> list[str]:
    """Best-effort member name resolution using guild voice_states.

    This helps when member caching is incomplete (e.g. without privileged member intents),
    where channel.members may be empty but voice_states still has IDs.
    """
    guild = getattr(channel, "guild", None)
    if not guild:
        return []

    voice_states = getattr(guild, "voice_states", None)
    if voice_states is None:
        voice_states = getattr(guild, "_voice_states", None)
    if not voice_states:
        return []

    names: list[str] = []
    for member_id, voice_state in voice_states.items():
        if exclude_member_id is not None and member_id == exclude_member_id:
            continue
        if not voice_state or not getattr(voice_state, "channel", None):
            continue
        if voice_state.channel.id != channel.id:
            continue

        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guild.fetch_member(member_id)
            except Exception:
                member = None
        if member is None or getattr(member, "bot", False):
            continue
        names.append(getattr(member, "display_name", None) or getattr(member, "name", ""))

    return [n for n in names if n]


def pick_announcement_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.system_channel
    if channel and channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
        return channel

    for text_channel in guild.text_channels:
        if text_channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
            return text_channel
    return None
