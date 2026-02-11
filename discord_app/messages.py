import logging
import os
import tempfile

import discord
from discord.ext import commands

from config import BOT_CONFIG
from discord_app.constants import COMMAND_PREFIXES
from discord_app.notifications import send_telegram_join_request
from discord_app.runtime import get_bot
from discord_app.utils import (
    extract_discord_channel_link,
    extract_discord_invite_link,
    format_cost_estimate,
    strip_bot_mention,
)
from handlers.message_service import MessageProcessingRequest, process_message_request
from services.analytics import log_stt_usage
from services.memory import create_discord_join_request, get_voice_auto_reply, upsert_user_profile
from services.speech_to_text import estimate_transcription_cost, transcribe_audio, trim_silence

logger = logging.getLogger(__name__)

_pending_voice_transcripts: dict[tuple[str, str], str] = {}
_pending_voice_files: dict[tuple[str, str], dict] = {}


async def _send_responses(message: discord.Message, clean_content: str) -> None:
    bot = get_bot()
    user_name = None
    if message.author:
        user_name = message.author.display_name or str(message.author)
        upsert_user_profile("discord", str(message.channel.id), str(message.author.id), user_name)

    request = MessageProcessingRequest(
        text=clean_content,
        chat_id=str(message.channel.id),
        user_id=str(message.author.id),
        bot_username=bot.user.name if bot.user else None,
        username=str(message.author),
        platform="discord",
    )

    async def _ack() -> None:
        await message.channel.send("✅ Принял запрос, думаю...")

    responses = await process_message_request(request, ack_callback=_ack)

    for response in responses:
        if response.photo_url:
            await message.channel.send(response.photo_url)
        elif response.text:
            await message.channel.send(response.text)


async def _handle_voice_confirmation(message: discord.Message) -> bool:
    content = (message.content or "").strip().lower()
    if content.startswith("/"):
        content = content[1:]

    if content not in {"yes", "y"}:
        return False

    key = (str(message.channel.id), str(message.author.id))

    file_entry = _pending_voice_files.pop(key, None)
    if file_entry:
        file_path = file_entry.get("path")
        if not file_path or not os.path.exists(file_path):
            return True
        await message.channel.send("Ок, распознаю голосовое...")
        transcript, error = await transcribe_audio(file_path, user_id=str(message.author.id))
        try:
            os.unlink(file_path)
        except OSError:
            logger.warning("Failed to remove temp file %s", file_path)
        if transcript:
            log_stt_usage(
                platform="discord",
                chat_id=str(message.channel.id),
                user_id=str(message.author.id),
                duration_seconds=file_entry.get("duration"),
                size_bytes=file_entry.get("size_bytes"),
            )
        await _handle_transcript_result(message, transcript, error)
        return True

    transcript = _pending_voice_transcripts.pop(key, None)
    if not transcript:
        return False

    await _send_responses(message, transcript)
    if not get_voice_auto_reply(str(message.channel.id), str(message.author.id)):
        await message.channel.send(
            "Можно перейти в режим диалога, чтобы я не переспрашивал отвечать ли на голосовухи: "
            "/voice_msg_conversation_on"
        )
    return True


async def _handle_transcript_result(
    message: discord.Message, transcript: str | None, error: str | None
) -> bool:
    if not transcript:
        await message.channel.send("Не удалось распознать голосовое сообщение.")
        if error:
            logger.warning("Discord audio STT error: %s", error)
        return False

    await message.channel.send(f"Текст голосового:\n{transcript}")

    if get_voice_auto_reply(str(message.channel.id), str(message.author.id)):
        await _send_responses(message, transcript)
        return True

    key = (str(message.channel.id), str(message.author.id))
    _pending_voice_transcripts[key] = transcript
    await message.channel.send("Нужен ответ? /yes")
    return True


async def _handle_dm_message(message: discord.Message, clean_content: str) -> None:
    await _send_responses(message, clean_content)


async def _handle_guild_message(message: discord.Message, clean_content: str) -> None:
    bot = get_bot()
    bot_mentioned = bot.user is not None and bot.user.mentioned_in(message)
    has_prefix = message.content.startswith(COMMAND_PREFIXES)

    if not bot_mentioned and not has_prefix:
        return

    filtered_content = strip_bot_mention(clean_content, bot.user)
    if has_prefix:
        for prefix in COMMAND_PREFIXES:
            if filtered_content.startswith(prefix):
                filtered_content = filtered_content[len(prefix) :].strip()
                break

    if not filtered_content:
        return

    await _send_responses(message, filtered_content)


def register_message_handlers(bot: commands.Bot) -> None:
    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        content = message.content or ""
        is_dm = message.guild is None

        if is_dm and content:
            link = extract_discord_channel_link(content)
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
                await send_telegram_join_request(request_id, channel.guild.name, str(message.author))
                return

            invite_code = extract_discord_invite_link(content)
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
                await send_telegram_join_request(request_id, guild_name, str(message.author))
                return

        if await _handle_voice_confirmation(message):
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
                await message.channel.send("Распознаю голосовое сообщение...")

                tmp_path = None
                size_bytes = None
                try:
                    suffix = ""
                    if audio_attachment.filename and "." in audio_attachment.filename:
                        suffix = "." + audio_attachment.filename.rsplit(".", 1)[-1]
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".ogg") as tmp_file:
                        tmp_path = tmp_file.name
                    await audio_attachment.save(tmp_path)

                    trimmed_path, trimmed = trim_silence(tmp_path)
                    if trimmed:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            logger.warning("Failed to remove temp file %s", tmp_path)
                        tmp_path = trimmed_path

                    size_bytes = os.path.getsize(tmp_path)
                    max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_MAX_MB", 10)
                    confirm_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_CONFIRM_MB", 5)
                    max_bytes = int(max_mb * 1024 * 1024)
                    confirm_bytes = int(confirm_mb * 1024 * 1024)

                    if size_bytes > max_bytes:
                        await message.channel.send("Файл слишком большой для распознавания (лимит 10 МБ).")
                        return

                    if size_bytes >= confirm_bytes:
                        cost = estimate_transcription_cost(None, size_bytes)
                        key = (str(message.channel.id), str(message.author.id))
                        _pending_voice_files[key] = {
                            "path": tmp_path,
                            "size_bytes": size_bytes,
                        }
                        await message.channel.send(
                            f"Файл большой, распознавание будет стоить примерно {format_cost_estimate(cost)}. "
                            "Отправлять? /yes"
                        )
                        tmp_path = None
                        return

                    transcript, error = await transcribe_audio(tmp_path, user_id=str(message.author.id))
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            logger.warning("Failed to remove temp file %s", tmp_path)

                if transcript:
                    log_stt_usage(
                        platform="discord",
                        chat_id=str(message.channel.id),
                        user_id=str(message.author.id),
                        duration_seconds=None,
                        size_bytes=size_bytes,
                    )
                await _handle_transcript_result(message, transcript, error)
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
