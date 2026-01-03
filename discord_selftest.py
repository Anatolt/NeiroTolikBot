import asyncio
import logging

import discord
from discord.ext import commands

from services.memory import get_discord_voice_channels

logger = logging.getLogger(__name__)

_TEST_GUILD_NAME = "Just another server"


def register_discord_selftest(bot: commands.Bot) -> None:
    @bot.command(name="selftest")
    async def selftest_command(ctx: commands.Context) -> None:
        if not ctx.guild:
            await ctx.send("Selftest доступен только на сервере.")
            return

        if ctx.guild.name != _TEST_GUILD_NAME:
            await ctx.send("Selftest доступен только на тестовом сервере.")
            return

        report_lines = ["🧪 Discord selftest"]

        voice_channels = get_discord_voice_channels()
        entry = next(
            (row for row in voice_channels if row.get("guild_name") == _TEST_GUILD_NAME), None
        )
        if not entry:
            await ctx.send("Не нашёл тестовый сервер в базе.")
            return

        guild_id = entry.get("guild_id")
        channel_id = entry.get("channel_id")
        if not guild_id or not channel_id:
            await ctx.send("В базе нет ID тестового сервера или канала.")
            return

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            try:
                guild = await bot.fetch_guild(int(guild_id))
            except Exception as exc:
                await ctx.send(f"Не удалось получить сервер: {exc}")
                return

        voice_channel = bot.get_channel(int(channel_id))
        if voice_channel is None:
            try:
                voice_channel = await bot.fetch_channel(int(channel_id))
            except Exception as exc:
                await ctx.send(f"Не удалось получить голосовой канал: {exc}")
                return

        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            await ctx.send("Указанный канал не является голосовым.")
            return

        report_lines.append(f"✅ Сервер: {guild.name} ({guild.id})")
        report_lines.append(f"✅ Голосовой канал: {voice_channel.name} ({voice_channel.id})")

        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            report_lines.append(
                f"ℹ️ Бот уже подключен к голосу: {voice_client.channel.name}"
            )
        else:
            try:
                await voice_channel.connect()
                report_lines.append("✅ Подключение к голосу: ok")
                await asyncio.sleep(1)
                if guild.voice_client:
                    await guild.voice_client.disconnect()
                report_lines.append("✅ Отключение от голоса: ok")
            except Exception as exc:
                report_lines.append(f"❌ Подключение/отключение: {exc}")

        report_text = "\n".join(report_lines)
        text_channel = guild.system_channel
        if not text_channel or not text_channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
            text_channel = None
            for candidate in guild.text_channels:
                if candidate.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
                    text_channel = candidate
                    break

        if text_channel:
            await text_channel.send(report_text)
            if ctx.channel != text_channel:
                await ctx.send("Selftest завершён, отчёт отправлен в системный канал.")
            return

        await ctx.send(report_text)
