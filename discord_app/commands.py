import logging

import discord
from discord.ext import commands

from discord_app.utils import build_discord_help_message, build_start_message
from discord_app.voice_control import connect_voice_channel
from discord_app.voice_log import cancel_voice_log_task, ensure_voice_log_task
from services.memory import set_discord_autojoin, set_discord_autojoin_announce_sent, set_last_voice_channel, set_voice_auto_reply

logger = logging.getLogger(__name__)


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
