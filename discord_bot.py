import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import BOT_CONFIG
from handlers.message_service import MessageProcessingRequest, process_message_request
from services.speech_to_text import transcribe_audio
from services.generation import check_model_availability, init_client, refresh_models_from_api
from services.memory import (
    create_discord_join_request,
    get_all_admins,
    get_discord_autojoin,
    get_discord_autojoin_announce_sent,
    get_notification_flows_for_channel,
    get_unprocessed_discord_join_requests,
    get_voice_notification_chat_id,
    init_db,
    mark_discord_join_request_processed,
    set_discord_autojoin,
    set_discord_autojoin_announce_sent,
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
BOT_CONFIG["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
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
_join_request_task: asyncio.Task | None = None
_voice_disconnect_tasks: dict[int, asyncio.Task] = {}
_VOICE_DISCONNECT_DELAY_SECONDS = 15

# Инициализация клиентов и БД
init_client()
init_db()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def _extract_discord_channel_link(text: str) -> tuple[str, str] | None:
    match = re.search(r"https?://(?:www\.)?discord\.com/channels/(\d+)/(\d+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


def _extract_discord_invite_link(text: str) -> str | None:
    match = re.search(
        r"https?://(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)",
        text,
    )
    if not match:
        return None
    return match.group(1)


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

    async def _ack() -> None:
        await message.channel.send("✅ Принял запрос, думаю...")

    responses = await process_message_request(request, ack_callback=_ack)

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


async def _send_telegram_notification(text: str, discord_channel_id: str | None = None) -> None:
    if not telegram_bot:
        logger.warning("Telegram bot token not configured, cannot send notifications.")
        return

    sent_chat_ids: set[str] = set()

    async def _send(chat_id: str) -> None:
        if chat_id in sent_chat_ids:
            return
        sent_chat_ids.add(chat_id)
        try:
            await telegram_bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:
            logger.warning("Failed to send Telegram notification to chat %s: %s", chat_id, exc)

    admins = get_all_admins()
    if admins:
        for admin in admins:
            chat_id = admin.get("chat_id")
            if not chat_id:
                continue
            await _send(str(chat_id))

    flow_chat_ids: list[str] = []
    if discord_channel_id:
        flows = get_notification_flows_for_channel(discord_channel_id)
        flow_chat_ids = [str(flow["telegram_chat_id"]) for flow in flows if flow.get("telegram_chat_id")]
        for chat_id in flow_chat_ids:
            await _send(chat_id)

    chat_id = get_voice_notification_chat_id()
    if not chat_id or flow_chat_ids:
        if not admins and not flow_chat_ids and not chat_id:
            logger.info("No admins or flow/voice notification chat configured.")
        return

    await _send(str(chat_id))


async def _send_telegram_join_request(request_id: int, guild_name: str, user_name: str) -> None:
    if not telegram_bot:
        logger.warning("Telegram bot token not configured, cannot send join request.")
        return

    admins = get_all_admins()
    if not admins:
        logger.warning("No admins configured; join request cannot be delivered.")
        return

    text = (
        "Просят присоединиться к Discord.\n"
        f"Сервер: {guild_name}\n"
        f"Пользователь: {user_name}\n"
        f"Запрос: {request_id}\n\n"
        "Ответьте: yes или no (можно с номером, например: yes 12)."
    )

    for admin in admins:
        chat_id = admin.get("chat_id")
        if not chat_id:
            continue
        try:
            await telegram_bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:
            logger.warning("Failed to send join request to admin %s: %s", chat_id, exc)


async def _notify_discord_user(user_id: int, text: str) -> None:
    try:
        user = await bot.fetch_user(user_id)
        await user.send(text)
    except Exception as exc:
        logger.warning("Failed to notify Discord user %s: %s", user_id, exc)


async def _process_join_requests_loop() -> None:
    while True:
        requests = get_unprocessed_discord_join_requests()
        for request in requests:
            try:
                request_id = int(request["id"])
                status = request.get("status")
                channel_id_raw = str(request.get("discord_channel_id", ""))
                user_id = int(request["discord_user_id"])
                guild_name = request.get("discord_guild_name") or "Discord"

                if not channel_id_raw.isdigit():
                    if status == "approved":
                        await _notify_discord_user(
                            user_id,
                            "Админ разрешил. Пригласите меня на сервер «{guild_name}»: "
                            "https://discord.com/oauth2/authorize?client_id=1451265052978974931&permissions=3147776&scope=bot%20applications.commands",
                        )
                    elif status == "denied":
                        await _notify_discord_user(user_id, "Админ отказал в подключении.")

                    mark_discord_join_request_processed(request_id)
                    continue

                channel_id = int(channel_id_raw)

                channel = bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(channel_id)
                    except Exception:
                        channel = None

                if status == "approved":
                    if channel is None:
                        await _notify_discord_user(user_id, "Админ разрешил, но я не нашёл канал.")
                    elif channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                        await _notify_discord_user(user_id, "Админ разрешил, но это не голосовой канал.")
                    else:
                        voice_client = channel.guild.voice_client
                        try:
                            if voice_client and voice_client.is_connected():
                                await voice_client.move_to(channel)
                            else:
                                await channel.connect()
                            await _notify_discord_user(user_id, "Админ разрешил. Подключаюсь.")
                        except Exception as exc:
                            await _notify_discord_user(user_id, "Админ разрешил, но не смог подключиться.")
                            logger.warning("Failed to join voice channel: %s", exc)
                elif status == "denied":
                    await _notify_discord_user(user_id, "Админ отказал в подключении.")

                mark_discord_join_request_processed(request_id)
            except Exception as exc:
                logger.warning("Failed to process join request: %s", exc)

        await asyncio.sleep(3)


async def _disconnect_if_empty(guild_id: int) -> None:
    await asyncio.sleep(_VOICE_DISCONNECT_DELAY_SECONDS)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return
    channel = voice_client.channel
    if not channel:
        return
    humans = [m for m in channel.members if not m.bot]
    if not humans:
        try:
            await voice_client.disconnect()
        except Exception as exc:
            logger.warning("Failed to auto-leave voice channel: %s", exc)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    logger.info("Joined new guild: %s (%s)", guild.name, guild.id)
    _sync_discord_voice_channels()
    set_discord_autojoin_announce_sent(str(guild.id), False)


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

    global _join_request_task
    if _join_request_task is None or _join_request_task.done():
        _join_request_task = asyncio.create_task(_process_join_requests_loop())


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
    set_discord_autojoin_announce_sent(str(ctx.guild.id), False)
    await ctx.send("Автоподключение включено.")


@bot.tree.command(name="autojoin_on", description="Включить автоподключение к голосу")
async def autojoin_on_slash(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(interaction.guild.id), True)
    set_discord_autojoin_announce_sent(str(interaction.guild.id), False)
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

    if is_dm and content:
        link = _extract_discord_channel_link(content)
        if link:
            guild_id, channel_id = link
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except Exception:
                    channel = None

            if channel is None or not getattr(channel, "guild", None):
                await message.channel.send("Не вижу такой канал или у меня нет доступа.")
                return

            if str(channel.guild.id) != guild_id:
                await message.channel.send("Ссылка не совпадает с сервером канала.")
                return

            if channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                await message.channel.send("Это не голосовой канал.")
                return

            await message.channel.send(
                "Вижу ссылку на Discord. Пошёл спрашивать у админа, можно ли мне присоединиться."
            )

            request_id = create_discord_join_request(
                discord_user_id=str(message.author.id),
                discord_user_name=str(message.author),
                discord_guild_id=str(channel.guild.id),
                discord_guild_name=channel.guild.name,
                discord_channel_id=str(channel.id),
                discord_channel_name=getattr(channel, "name", str(channel.id)),
            )
            await _send_telegram_join_request(request_id, channel.guild.name, str(message.author))
            return

        invite_code = _extract_discord_invite_link(content)
        if invite_code:
            invite = None
            try:
                invite = await bot.fetch_invite(invite_code)
            except Exception as exc:
                logger.warning("Failed to fetch invite %s: %s", invite_code, exc)

            if invite and invite.guild:
                guild_name = invite.guild.name
                guild_id = str(invite.guild.id)
            else:
                guild_name = "неизвестный сервер"
                guild_id = "unknown"

            channel_id = f"invite:{invite_code}"
            channel_name = "invite"
            if invite and invite.channel:
                channel_name = getattr(invite.channel, "name", "invite")
                if invite.channel.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                    channel_id = str(invite.channel.id)

            await message.channel.send(
                "Вижу ссылку на Discord. Пошёл спрашивать у админа, можно ли мне присоединиться."
            )

            request_id = create_discord_join_request(
                discord_user_id=str(message.author.id),
                discord_user_name=str(message.author),
                discord_guild_id=guild_id,
                discord_guild_name=guild_name,
                discord_channel_id=channel_id,
                discord_channel_name=channel_name,
            )
            await _send_telegram_join_request(request_id, guild_name, str(message.author))
            return

    if message.attachments:
        audio_attachment = None
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("audio/"):
                audio_attachment = attachment
                break
            if attachment.filename.lower().endswith((".ogg", ".mp3", ".wav", ".m4a")):
                audio_attachment = attachment
                break

        if audio_attachment:
            tmp_path = None
            try:
                suffix = ""
                if audio_attachment.filename and "." in audio_attachment.filename:
                    suffix = "." + audio_attachment.filename.rsplit(".", 1)[-1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".ogg") as tmp_file:
                    tmp_path = tmp_file.name
                await audio_attachment.save(tmp_path)
                transcript, error = await transcribe_audio(tmp_path)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", tmp_path)

            if transcript:
                await _send_responses(message, transcript)
            else:
                await message.channel.send("Не удалось распознать голосовое сообщение.")
                if error:
                    logger.warning("Discord audio STT error: %s", error)
            return

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
        await _send_telegram_notification(notification, discord_channel_id=str(channel.id))

        if channel.guild and get_discord_autojoin(str(channel.guild.id)):
            voice_client = channel.guild.voice_client
            if voice_client is None or not voice_client.is_connected():
                try:
                    await channel.connect()
                    if not get_discord_autojoin_announce_sent(str(channel.guild.id)):
                        announce_channel = _pick_announcement_channel(channel.guild)
                        if announce_channel:
                            await announce_channel.send(
                                f"Подключился к голосовому каналу «{channel.name}», "
                                "т.к. кто-то в него зашёл.\n"
                                "Чтобы я вышел, напишите /leave.\n"
                                "Чтобы я не подключался автоматически, напишите /autojoin_off.\n"
                                "Чтобы снова включить автоподключение, напишите /autojoin_on."
                            )
                        set_discord_autojoin_announce_sent(str(channel.guild.id), True)
                except Exception as exc:
                    logger.warning("Failed to auto-join voice channel: %s", exc)

    voice_client = member.guild.voice_client
    guild_id = member.guild.id
    if voice_client and voice_client.is_connected():
        channel = voice_client.channel
        if channel:
            humans = [m for m in channel.members if not m.bot]
            existing_task = _voice_disconnect_tasks.pop(guild_id, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            if not humans:
                _voice_disconnect_tasks[guild_id] = asyncio.create_task(
                    _disconnect_if_empty(guild_id)
                )


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
