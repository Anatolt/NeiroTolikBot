import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from config import BOT_CONFIG
from handlers.message_service import MessageProcessingRequest, process_message_request
from services.generation import check_model_availability, init_client, refresh_models_from_api
from services.memory import (
    get_voice_notification_chat_id,
    init_db,
    upsert_discord_voice_channel,
    get_discord_autojoin,
    set_discord_autojoin,
)
from utils.helpers import resolve_system_prompt
from telegram import Bot

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_CONFIG["DISCORD_BOT_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN")
BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
BOT_CONFIG["PIAPI_KEY"] = os.getenv("PIAPI_KEY")
BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = resolve_system_prompt(BASE_DIR)
BOT_CONFIG["ADMIN_PASS"] = os.getenv("PASS")
BOT_CONFIG["BOOT_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Необязательная настройка кастомных запасных моделей (через запятую)
fallback_models_env = os.getenv("FALLBACK_MODELS")
if fallback_models_env:
    BOT_CONFIG["FALLBACK_MODELS"] = [model.strip() for model in fallback_models_env.split(",") if model.strip()]

COMMAND_PREFIXES = ("!", "/")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.dm_messages = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(*COMMAND_PREFIXES),
    intents=intents,
    help_command=None,
)
telegram_bot = Bot(BOT_CONFIG["TELEGRAM_BOT_TOKEN"]) if BOT_CONFIG.get("TELEGRAM_BOT_TOKEN") else None

# Инициализация клиентов и БД
init_client()
init_db()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def check_default_model():
    """Выбирает лучшую доступную модель и обновляет алиасы."""
    try:
        await refresh_models_from_api()
    except Exception as e:
        logger.error(f"Failed to refresh models from API: {str(e)}")

    models_to_probe = []
    for candidate in [BOT_CONFIG.get("DEFAULT_MODEL"), *BOT_CONFIG.get("FALLBACK_MODELS", [])]:
        if candidate and candidate not in models_to_probe:
            models_to_probe.append(candidate)

    for candidate in models_to_probe:
        if await check_model_availability(candidate):
            BOT_CONFIG["DEFAULT_MODEL"] = candidate
            logger.info(f"Using available default model: {candidate}")
            break
    else:
        logger.warning(
            f"No available models from the list {models_to_probe}. Falling back to openai/gpt-3.5-turbo"
        )
        BOT_CONFIG["DEFAULT_MODEL"] = "openai/gpt-3.5-turbo"


def _build_start_message(display_name: str | None) -> str:
    user = display_name or "там"
    default_model = BOT_CONFIG["DEFAULT_MODEL"]
    return (
        f"Привет, {user}! Я бот-помощник.\n\n"
        f"📝 Спроси меня что-нибудь — отвечу через {default_model}.\n"
        "🎨 Попроси нарисовать картинку (например, 'нарисуй закат над морем').\n"
        "🤖 Хочешь другую модель? Укажи ее в начале или конце запроса (например, 'chatgpt какой сегодня день?').\n"
        "❓ Команды и помощь: /help"
    )


def _build_discord_help_message() -> str:
    return (
        "Команды Discord-бота:\n"
        "• /start — краткое приветствие\n"
        "• /help — справка по командам\n"
        "• /join — подключиться к голосовому каналу, где вы сейчас\n"
        "• /leave — выйти из голосового канала\n"
        "• /autojoin_on — включить автоподключение к голосу\n"
        "• /autojoin_off — отключить автоподключение к голосу\n\n"
        "В серверах бот отвечает по упоминанию @ИмяБота или с префиксами !/.\n"
        "В личных сообщениях отвечает на любой текст."
    )


def _strip_bot_mention(content: str, bot_user: discord.User | discord.ClientUser | None) -> str:
    if not bot_user:
        return content

    cleaned = content
    mention_variants = [f"<@{bot_user.id}>", f"<@!{bot_user.id}>", f"@{bot_user.name}"]
    for mention in mention_variants:
        cleaned = cleaned.replace(mention, "")
    return cleaned.strip()


async def _send_responses(message: discord.Message, clean_content: str) -> None:
    request = MessageProcessingRequest(
        text=clean_content,
        chat_id=str(message.channel.id),
        user_id=str(message.author.id),
        bot_username=bot.user.name if bot.user else None,
        username=str(message.author),
    )

    responses = await process_message_request(request)

    for response in responses:
        if response.photo_url:
            await message.channel.send(response.photo_url)
        elif response.text:
            await message.channel.send(response.text)


def _sync_discord_voice_channels() -> None:
    for guild in bot.guilds:
        for channel in list(guild.voice_channels) + list(guild.stage_channels):
            upsert_discord_voice_channel(
                channel_id=str(channel.id),
                channel_name=channel.name,
                guild_id=str(guild.id),
                guild_name=guild.name,
            )


async def _send_telegram_notification(text: str) -> None:
    chat_id = get_voice_notification_chat_id()
    if not chat_id:
        return
    if not telegram_bot:
        logger.warning("Telegram bot token not configured, cannot send notifications.")
        return

    try:
        await telegram_bot.send_message(chat_id=int(chat_id), text=text)
    except Exception as exc:
        logger.warning("Failed to send Telegram notification: %s", exc)


async def _handle_dm_message(message: discord.Message, clean_content: str) -> None:
    await _send_responses(message, clean_content)


async def _handle_guild_message(message: discord.Message, clean_content: str) -> None:
    bot_mentioned = bot.user is not None and bot.user.mentioned_in(message)
    has_prefix = message.content.startswith(COMMAND_PREFIXES)

    if not bot_mentioned and not has_prefix:
        return

    filtered_content = _strip_bot_mention(clean_content, bot.user)
    if has_prefix:
        for prefix in COMMAND_PREFIXES:
            if filtered_content.startswith(prefix):
                filtered_content = filtered_content[len(prefix) :].strip()
                break

    if not filtered_content:
        return

    await _send_responses(message, filtered_content)


def _pick_announcement_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.system_channel
    if channel and channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
        return channel

    for text_channel in guild.text_channels:
        if text_channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
            return text_channel

    return None


@bot.event
async def on_ready():
    logger.info("Discord bot connected as %s (id=%s)", bot.user, bot.user.id if bot.user else "n/a")
    _sync_discord_voice_channels()
    try:
        await bot.tree.sync()
        logger.info("Discord app commands synced.")
    except Exception as exc:
        logger.warning("Failed to sync Discord app commands: %s", exc)


@bot.command(name="start")
async def start_command(ctx: commands.Context) -> None:
    await ctx.send(_build_start_message(ctx.author.display_name))


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.send(_build_discord_help_message())


@bot.tree.command(name="help", description="Справка по командам бота")
async def help_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(_build_discord_help_message())


@bot.command(name="join")
async def join_voice_command(ctx: commands.Context) -> None:
    """Join the caller's voice channel."""
    voice_state = getattr(ctx.author, "voice", None)
    if not voice_state or not voice_state.channel:
        await ctx.send("Сначала зайди в голосовой канал.")
        return

    channel = voice_state.channel
    voice_client = ctx.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel.id == channel.id:
            await ctx.send(f"Уже в канале «{channel.name}».")
            return
        await voice_client.move_to(channel)
        await ctx.send(f"Перешёл в «{channel.name}».")
        return

    await channel.connect()
    await ctx.send(f"Подключился к «{channel.name}».")


@bot.tree.command(name="join", description="Подключиться к голосовому каналу")
async def join_voice_slash(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.")
        return

    voice_state = getattr(interaction.user, "voice", None)
    if not voice_state or not voice_state.channel:
        await interaction.response.send_message("Сначала зайди в голосовой канал.")
        return

    channel = voice_state.channel
    voice_client = interaction.guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel.id == channel.id:
            await interaction.response.send_message(f"Уже в канале «{channel.name}».")
            return
        await voice_client.move_to(channel)
        await interaction.response.send_message(f"Перешёл в «{channel.name}».")
        return

    await channel.connect()
    await interaction.response.send_message(f"Подключился к «{channel.name}».")


@bot.command(name="leave")
async def leave_voice_command(ctx: commands.Context) -> None:
    """Leave the current voice channel."""
    voice_client = ctx.voice_client
    if not voice_client or not voice_client.is_connected():
        await ctx.send("Я сейчас не в голосовом канале.")
        return

    await voice_client.disconnect()
    await ctx.send("Отключился от голосового канала.")


@bot.tree.command(name="leave", description="Выйти из голосового канала")
async def leave_voice_slash(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.")
        return

    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("Я сейчас не в голосовом канале.")
        return

    await voice_client.disconnect()
    await interaction.response.send_message("Отключился от голосового канала.")


@bot.command(name="autojoin_on")
async def autojoin_on_command(ctx: commands.Context) -> None:
    """Enable auto-join for this guild."""
    if not ctx.guild:
        await ctx.send("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(ctx.guild.id), True)
    await ctx.send("Автоподключение включено.")


@bot.tree.command(name="autojoin_on", description="Включить автоподключение к голосу")
async def autojoin_on_slash(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(interaction.guild.id), True)
    await interaction.response.send_message("Автоподключение включено.")


@bot.command(name="autojoin_off")
async def autojoin_off_command(ctx: commands.Context) -> None:
    """Disable auto-join for this guild."""
    if not ctx.guild:
        await ctx.send("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(ctx.guild.id), False)
    await ctx.send("Автоподключение отключено.")


@bot.tree.command(name="autojoin_off", description="Отключить автоподключение к голосу")
async def autojoin_off_slash(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(interaction.guild.id), False)
    await interaction.response.send_message("Автоподключение отключено.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    content = message.content or ""
    is_dm = message.guild is None

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return
    if content.startswith(COMMAND_PREFIXES):
        await bot.process_commands(message)
        return

    if is_dm:
        if content:
            await _handle_dm_message(message, content)
    else:
        await _handle_guild_message(message, content)

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return

    if before.channel is None and after.channel is not None:
        channel = after.channel
        guild_name = channel.guild.name if channel.guild else "Discord"
        notification = (
            f"🎧 {member.display_name} подключился к голосовому каналу "
            f"«{channel.name}» ({guild_name})."
        )
        await _send_telegram_notification(notification)

        if channel.guild and get_discord_autojoin(str(channel.guild.id)):
            voice_client = channel.guild.voice_client
            if voice_client is None or not voice_client.is_connected():
                try:
                    await channel.connect()
                    announce_channel = _pick_announcement_channel(channel.guild)
                    if announce_channel:
                        await announce_channel.send(
                            f"Подключился к голосовому каналу «{channel.name}», "
                            "т.к. кто-то в него зашёл.\n"
                            "Чтобы я вышел, напишите /leave.\n"
                            "Чтобы я не подключался автоматически, напишите /autojoin_off.\n"
                            "Чтобы снова включить автоподключение, напишите /autojoin_on."
                        )
                except Exception as exc:
                    logger.warning("Failed to auto-join voice channel: %s", exc)


async def main() -> None:
    if not BOT_CONFIG["DISCORD_BOT_TOKEN"] or not BOT_CONFIG["OPENROUTER_API_KEY"]:
        logger.error("Please set DISCORD_BOT_TOKEN and OPENROUTER_API_KEY in .env file")
        return

    await check_default_model()

    async with bot:
        await bot.start(BOT_CONFIG["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Discord bot stopped by user")
    except Exception as e:
        logger.error(f"Error running Discord bot: {str(e)}")
