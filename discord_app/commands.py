import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from config import BOT_CONFIG
from discord_app.utils import build_discord_help_message, build_start_message
from discord_app.voice_control import connect_voice_channel
from discord_app.notifications import send_telegram_notification
from discord_app.voice_log import cancel_voice_log_task, ensure_voice_log_task, generate_voice_summary_for_range
from services.tts import synthesize_speech
from services.memory import (
    get_last_voice_channel,
    set_discord_autojoin,
    set_discord_autojoin_announce_sent,
    set_last_voice_channel,
    set_voice_auto_reply,
    set_voice_transcripts_enabled,
)

logger = logging.getLogger(__name__)


def _get_ffmpeg_path() -> str | None:
    for candidate in (shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/bin/ffmpeg"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def register_commands(bot: commands.Bot) -> None:
    @bot.command(name="start")
    async def start_command(ctx: commands.Context) -> None:
        await ctx.send(build_start_message(ctx.author.display_name))

    @bot.command(name="help")
    async def help_command(ctx: commands.Context) -> None:
        await ctx.send(build_discord_help_message())

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="help", description="Справка по командам бота")
        async def help_slash(ctx: discord.ApplicationContext) -> None:
            await ctx.respond(build_discord_help_message())

    async def _reply_ctx(
        ctx: commands.Context | discord.ApplicationContext,
        text: str,
        responded: bool,
    ) -> bool:
        if hasattr(ctx, "respond"):
            if responded:
                await ctx.followup.send(text)
            else:
                await ctx.respond(text)
                responded = True
        else:
            await ctx.send(text)
        return responded

    async def _play_tts_audio(
        ctx: commands.Context | discord.ApplicationContext,
        text: str,
    ) -> None:
        responded = False

        if not ctx.guild:
            await _reply_ctx(ctx, "Команда доступна только на сервере.", responded)
            return

        actor = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        voice_state = getattr(actor, "voice", None)
        if not voice_state or not voice_state.channel:
            await _reply_ctx(ctx, "Сначала зайди в голосовой канал.", responded)
            return

        if hasattr(ctx, "respond"):
            responded = await _reply_ctx(ctx, "🗣️ Подключаюсь к голосу...", responded)

        ffmpeg_path = _get_ffmpeg_path()
        if not ffmpeg_path:
            await _reply_ctx(ctx, "ffmpeg не найден, TTS недоступен.", responded)
            return

        voice_client = await connect_voice_channel(voice_state.channel)
        if not voice_client:
            await _reply_ctx(ctx, "Не удалось подключиться к голосовому каналу.", responded)
            return

        responded = await _reply_ctx(ctx, "🗣️ Озвучиваю...", responded)

        audio_path, error = await synthesize_speech(
            text,
            platform="discord",
            chat_id=str(getattr(ctx.channel, "id", "")),
            user_id=str(getattr(actor, "id", "")),
        )
        if error or not audio_path:
            await _reply_ctx(ctx, f"Ошибка TTS: {error}", responded)
            return

        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after_playback(err: Exception | None) -> None:
            if err:
                logger.warning("TTS playback error: %s", err)
            loop.call_soon_threadsafe(done.set)

        try:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            source = discord.FFmpegPCMAudio(audio_path, executable=ffmpeg_path)
            voice_client.play(source, after=_after_playback)
            await done.wait()
        finally:
            if os.path.exists(audio_path):
                try:
                    os.unlink(audio_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", audio_path)

    @bot.command(name="join")
    async def join_voice_command(ctx: commands.Context) -> None:
        """Join the caller's voice channel."""
        voice_state = getattr(ctx.author, "voice", None)
        if not voice_state or not voice_state.channel:
            await ctx.send("Сначала зайди в голосовой канал.")
            return

        channel = voice_state.channel
        voice_client = ctx.voice_client

        if voice_client and voice_client.is_connected() and voice_client.channel.id == channel.id:
            await ctx.send(f"Уже в канале «{channel.name}».")
            return

        voice_client = await connect_voice_channel(channel)
        if not voice_client:
            await ctx.send("Не удалось подключиться к голосовому каналу.")
            return
        ensure_voice_log_task(voice_client)
        set_last_voice_channel(str(ctx.guild.id), str(channel.id))
        await ctx.send(f"Подключился к «{channel.name}».")

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="join", description="Подключиться к голосовому каналу")
        async def join_voice_slash(ctx: discord.ApplicationContext) -> None:
            if not ctx.guild:
                await ctx.respond("Команда доступна только на сервере.")
                return

            voice_state = getattr(ctx.author, "voice", None)
            if not voice_state or not voice_state.channel:
                await ctx.respond("Сначала зайди в голосовой канал.")
                return

            channel = voice_state.channel
            voice_client = ctx.guild.voice_client
            if voice_client and voice_client.is_connected() and voice_client.channel.id == channel.id:
                await ctx.respond(f"Уже в канале «{channel.name}».")
                return

            voice_client = await connect_voice_channel(channel)
            if not voice_client:
                await ctx.respond("Не удалось подключиться к голосовому каналу.")
                return
            ensure_voice_log_task(voice_client)
            set_last_voice_channel(str(ctx.guild.id), str(channel.id))
            await ctx.respond(f"Подключился к «{channel.name}».")

    @bot.command(name="leave")
    async def leave_voice_command(ctx: commands.Context) -> None:
        """Leave the current voice channel."""
        voice_client = ctx.voice_client
        if not voice_client or not voice_client.is_connected():
            await ctx.send("Я сейчас не в голосовом канале.")
            return

        await voice_client.disconnect()
        if ctx.guild:
            cancel_voice_log_task(ctx.guild.id)
        if ctx.guild:
            set_last_voice_channel(str(ctx.guild.id), None)
        await ctx.send("Отключился от голосового канала.")

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="leave", description="Выйти из голосового канала")
        async def leave_voice_slash(ctx: discord.ApplicationContext) -> None:
            if not ctx.guild:
                await ctx.respond("Команда доступна только на сервере.")
                return

            voice_client = ctx.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                await ctx.respond("Я сейчас не в голосовом канале.")
                return
            await voice_client.disconnect()
            if ctx.guild:
                cancel_voice_log_task(ctx.guild.id)
            if ctx.guild:
                set_last_voice_channel(str(ctx.guild.id), None)
            await ctx.respond("Отключился от голосового канала.")

    @bot.command(name="say")
    async def say_voice_command(ctx: commands.Context, *, text: str | None = None) -> None:
        """Озвучить текст в голосовом канале пользователя."""
        if not text:
            await ctx.send("Использование: /say <текст>")
            return
        await _play_tts_audio(ctx, text)

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="say", description="Озвучить текст в голосовом канале")
        async def say_voice_slash(ctx: discord.ApplicationContext, text: str) -> None:
            await _play_tts_audio(ctx, text)

    async def _toggle_transcripts(
        ctx: commands.Context | discord.ApplicationContext,
        enabled: bool,
    ) -> None:
        if not ctx.guild:
            await _reply_ctx(ctx, "Команда доступна только на сервере.", False)
            return

        channel_id = get_last_voice_channel(str(ctx.guild.id))
        if not channel_id:
            await _reply_ctx(ctx, "Сначала подключись к голосовому каналу.", False)
            return

        set_voice_transcripts_enabled(channel_id, enabled)
        status = "включена" if enabled else "отключена"
        await _reply_ctx(ctx, f"Отправка транскрипций {status}.", False)

    @bot.command(name="transcripts_off")
    async def transcripts_off_command(ctx: commands.Context) -> None:
        """Disable transcript forwarding to Discord text channel."""
        await _toggle_transcripts(ctx, False)

    @bot.command(name="transcripts_on")
    async def transcripts_on_command(ctx: commands.Context) -> None:
        """Enable transcript forwarding to Discord text channel."""
        await _toggle_transcripts(ctx, True)

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="transcripts_off", description="Отключить отправку транскрипций")
        async def transcripts_off_slash(ctx: discord.ApplicationContext) -> None:
            await _toggle_transcripts(ctx, False)

        @bot.slash_command(name="transcripts_on", description="Включить отправку транскрипций")
        async def transcripts_on_slash(ctx: discord.ApplicationContext) -> None:
            await _toggle_transcripts(ctx, True)

    async def _send_summary_now(
        ctx: commands.Context | discord.ApplicationContext,
    ) -> None:
        responded = await _reply_ctx(ctx, "Готовлю саммари...", False)
        if not ctx.guild:
            await _reply_ctx(ctx, "Команда доступна только на сервере.", responded)
            return

        channel = None
        channel_id = get_last_voice_channel(str(ctx.guild.id))
        if channel_id:
            channel = ctx.guild.get_channel(int(channel_id))
        if channel is None:
            voice_state = getattr(getattr(ctx, "author", None), "voice", None)
            channel = getattr(voice_state, "channel", None) if voice_state else None
            if channel:
                set_last_voice_channel(str(ctx.guild.id), str(channel.id))
        if channel is None:
            await _reply_ctx(ctx, "Сначала подключись к голосовому каналу.", responded)
            return

        if channel is None or channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
            await _reply_ctx(ctx, "Не нашёл голосовой канал для саммари.", responded)
            return

        minutes = int(BOT_CONFIG.get("VOICE_SUMMARY_LIVE_MINUTES", 120) or 120)
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(minutes=minutes)
        summary = await generate_voice_summary_for_range(
            channel,
            start_dt,
            end_dt,
            f"Саммари за последние {minutes} мин",
        )
        if not summary:
            await _reply_ctx(ctx, "Нет разговоров для саммари.", responded)
            return

        await send_telegram_notification(summary, discord_channel_id=str(channel.id))
        await _reply_ctx(ctx, "Саммари отправлено.", responded)

    @bot.command(name="summary_now")
    async def summary_now_command(ctx: commands.Context) -> None:
        """Generate and send summary immediately."""
        await _send_summary_now(ctx)

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="summary_now", description="Сделать саммари голосового чата сейчас")
        async def summary_now_slash(ctx: discord.ApplicationContext) -> None:
            await _send_summary_now(ctx)

    @bot.command(name="autojoin_on")
    async def autojoin_on_command(ctx: commands.Context) -> None:
        """Enable auto-join for this guild."""
        if not ctx.guild:
            await ctx.send("Команда доступна только на сервере.")
            return

        set_discord_autojoin(str(ctx.guild.id), True)
        set_discord_autojoin_announce_sent(str(ctx.guild.id), False)
        await ctx.send("Автоподключение включено.")

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="autojoin_on", description="Включить автоподключение к голосу")
        async def autojoin_on_slash(ctx: discord.ApplicationContext) -> None:
            if not ctx.guild:
                await ctx.respond("Команда доступна только на сервере.")
                return

            set_discord_autojoin(str(ctx.guild.id), True)
            set_discord_autojoin_announce_sent(str(ctx.guild.id), False)
            await ctx.respond("Автоподключение включено.")

    @bot.command(name="autojoin_off")
    async def autojoin_off_command(ctx: commands.Context) -> None:
        """Disable auto-join for this guild."""
        if not ctx.guild:
            await ctx.send("Команда доступна только на сервере.")
            return

        set_discord_autojoin(str(ctx.guild.id), False)
        await ctx.send("Автоподключение отключено.")

    @bot.command(name="voice_msg_conversation_on")
    async def voice_msg_conversation_on_command(ctx: commands.Context) -> None:
        set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), True)
        await ctx.send(
            "🔊 Автоответ на голосовые сообщения включён.\n"
            "Отключить: /voice_msg_conversation_off"
        )

    @bot.command(name="voice_msg_conversation_off")
    async def voice_msg_conversation_off_command(ctx: commands.Context) -> None:
        set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), False)
        await ctx.send(
            "🔇 Автоответ на голосовые сообщения отключён.\n"
            "Включить: /voice_msg_conversation_on"
        )

    if hasattr(bot, "slash_command"):
        @bot.slash_command(name="voice_msg_conversation_on", description="Включить автоответ на голосовые сообщения")
        async def voice_msg_conversation_on_slash(ctx: discord.ApplicationContext) -> None:
            set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), True)
            await ctx.respond(
                "🔊 Автоответ на голосовые сообщения включён.\n"
                "Отключить: /voice_msg_conversation_off"
            )

        @bot.slash_command(name="voice_msg_conversation_off", description="Отключить автоответ на голосовые сообщения")
        async def voice_msg_conversation_off_slash(ctx: discord.ApplicationContext) -> None:
            set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), False)
            await ctx.respond(
                "🔇 Автоответ на голосовые сообщения отключён.\n"
                "Включить: /voice_msg_conversation_on"
            )

        @bot.slash_command(name="autojoin_off", description="Отключить автоподключение к голосу")
        async def autojoin_off_slash(ctx: discord.ApplicationContext) -> None:
            if not ctx.guild:
                await ctx.respond("Команда доступна только на сервере.")
                return

            set_discord_autojoin(str(ctx.guild.id), False)
            await ctx.respond("Автоподключение отключено.")
