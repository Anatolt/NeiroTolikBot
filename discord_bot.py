import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from telegram import Bot

from config import BOT_CONFIG
from discord_app.commands import register_commands
from discord_app.constants import COMMAND_PREFIXES
from discord_app.join_requests import ensure_join_request_task
from discord_app.messages import register_message_handlers
from discord_app.runtime import init_runtime
from discord_app.voice_control import connect_voice_channel, sync_discord_voice_channels
from discord_app.voice_log import ensure_voice_log_task
from discord_app.voice_state import register_voice_state_handlers
from discord_selftest import register_discord_selftest
from services.generation import check_model_availability, init_client, refresh_models_from_api
from services.memory import get_last_voice_channel, init_db
from utils.helpers import resolve_system_prompt

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_CONFIG["DISCORD_BOT_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN")
BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
BOT_CONFIG["PIAPI_KEY"] = os.getenv("PIAPI_KEY")
BOT_CONFIG["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
BOT_CONFIG["IMAGE_ROUTER_KEY"] = os.getenv("IMAGE_ROUTER_KEY")
BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = resolve_system_prompt(BASE_DIR)
BOT_CONFIG["ADMIN_PASS"] = os.getenv("PASS")
BOT_CONFIG["BOOT_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
voice_prompt_env = os.getenv("VOICE_TRANSCRIBE_PROMPT")
if voice_prompt_env is not None:
    BOT_CONFIG["VOICE_TRANSCRIBE_PROMPT"] = voice_prompt_env
voice_local_url_env = os.getenv("VOICE_LOCAL_WHISPER_URL")
if voice_local_url_env is not None:
    BOT_CONFIG["VOICE_LOCAL_WHISPER_URL"] = voice_local_url_env
tts_model_env = os.getenv("TTS_MODEL")
if tts_model_env is not None:
    BOT_CONFIG["TTS_MODEL"] = tts_model_env
tts_voice_env = os.getenv("TTS_VOICE")
if tts_voice_env is not None:
    BOT_CONFIG["TTS_VOICE"] = tts_voice_env

# Необязательная настройка кастомных запасных моделей (через запятую)
fallback_models_env = os.getenv("FALLBACK_MODELS")
if fallback_models_env:
    BOT_CONFIG["FALLBACK_MODELS"] = [
        model.strip() for model in fallback_models_env.split(",") if model.strip()
    ]

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
init_runtime(bot, telegram_bot)

register_discord_selftest(bot)
register_commands(bot)
register_message_handlers(bot)
register_voice_state_handlers(bot)

# Инициализация клиентов и БД
init_client()
init_db()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def check_default_model() -> None:
    """Выбирает лучшую доступную модель и обновляет алиасы."""
    try:
        await refresh_models_from_api()
    except Exception as exc:
        logger.error("Failed to refresh models from API: %s", exc)

    models_to_probe = []
    for candidate in [BOT_CONFIG.get("DEFAULT_MODEL"), *BOT_CONFIG.get("FALLBACK_MODELS", [])]:
        if candidate and candidate not in models_to_probe:
            models_to_probe.append(candidate)

    for candidate in models_to_probe:
        if await check_model_availability(candidate):
            BOT_CONFIG["DEFAULT_MODEL"] = candidate
            logger.info("Using available default model: %s", candidate)
            break
    else:
        logger.warning(
            "No available models from the list %s. Falling back to openai/gpt-3.5-turbo",
            models_to_probe,
        )
        BOT_CONFIG["DEFAULT_MODEL"] = "openai/gpt-3.5-turbo"


@bot.event
async def on_ready() -> None:
    logger.info("Discord bot connected as %s (id=%s)", bot.user, bot.user.id if bot.user else "n/a")
    sync_discord_voice_channels()
    if hasattr(bot, "sync_commands"):
        try:
            guild_ids = [guild.id for guild in bot.guilds] if bot.guilds else None
            await bot.sync_commands(force=True, guild_ids=guild_ids)
            logger.info("Discord app commands synced.")
        except Exception as exc:
            logger.warning("Failed to sync Discord app commands: %s", exc)

    ensure_join_request_task()

    for guild in bot.guilds:
        last_channel_id = get_last_voice_channel(str(guild.id))
        if not last_channel_id:
            continue
        try:
            channel = guild.get_channel(int(last_channel_id))
        except (TypeError, ValueError):
            channel = None
        if channel is None or channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
            continue
        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            try:
                await voice_client.disconnect()
            except Exception as exc:
                logger.warning("Failed to disconnect before reconnect: %s", exc)
        try:
            voice_client = await connect_voice_channel(channel)
            if voice_client:
                ensure_voice_log_task(voice_client)
            logger.info("Reconnected to voice channel %s in guild %s", channel.id, guild.id)
        except Exception as exc:
            logger.warning("Failed to reconnect to voice channel %s: %s", channel.id, exc)


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
    except Exception as exc:
        logger.error("Error running Discord bot: %s", exc)
