import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import BOT_CONFIG
from handlers.message_service import MessageProcessingRequest, process_message_request
from services.generation import check_model_availability, init_client, refresh_models_from_api
from services.memory import (
    get_voice_notification_chat_id,
    init_db,
    upsert_discord_voice_channel,
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

bot = commands.Bot(command_prefix=commands.when_mentioned_or(*COMMAND_PREFIXES), intents=intents)
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
        "🤖 Хочешь другую модель? Укажи ее в начале или конце запроса (например, 'chatgpt какой сегодня день?')."
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


@bot.event
async def on_ready():
    logger.info("Discord bot connected as %s (id=%s)", bot.user, bot.user.id if bot.user else "n/a")
    _sync_discord_voice_channels()


@bot.command(name="start")
async def start_command(ctx: commands.Context) -> None:
    await ctx.send(_build_start_message(ctx.author.display_name))


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
