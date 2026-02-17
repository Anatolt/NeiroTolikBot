import asyncio
import logging

import discord
from discord.ext import commands

from config import BOT_CONFIG
from discord_app.notifications import send_telegram_notification
from discord_app.runtime import get_bot
from discord_app.utils import (
    count_humans_in_voice,
    list_human_names_in_voice,
    list_human_names_in_voice_via_states,
    pick_announcement_channel,
)
from discord_app.voice_control import connect_voice_channel, sync_discord_voice_channels
from discord_app.voice_log import cancel_voice_log_task, ensure_voice_log_task
from services.memory import (
    get_discord_autojoin,
    get_discord_autojoin_announce_sent,
    get_voice_presence_notifications_enabled,
    set_discord_autojoin_announce_sent,
    set_last_voice_channel,
)

logger = logging.getLogger(__name__)

_VOICE_DISCONNECT_DELAY_SECONDS = 15
_VOICE_EMPTY_NOTIFY_DELAY_SECONDS = 300
_voice_disconnect_tasks: dict[int, asyncio.Task] = {}
_voice_empty_notify_tasks: dict[int, asyncio.Task] = {}

def _format_names_ru(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} и {names[1]}"
    return f"{', '.join(names[:-1])} и {names[-1]}"


async def _disconnect_if_empty(guild_id: int) -> None:
    await asyncio.sleep(_VOICE_DISCONNECT_DELAY_SECONDS)
    bot = get_bot()
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
            cancel_voice_log_task(guild_id)
            await voice_client.disconnect()
            set_last_voice_channel(str(guild_id), None)
        except Exception as exc:
            logger.warning("Failed to auto-leave voice channel: %s", exc)


async def _notify_if_voice_empty(channel_id: int, guild_id: int) -> None:
    await asyncio.sleep(_VOICE_EMPTY_NOTIFY_DELAY_SECONDS)
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    if channel is None:
        return
    if channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
        return
    if not get_voice_presence_notifications_enabled(str(guild_id)):
        return
    humans = [m for m in channel.members if not m.bot]
    if humans:
        return
    guild_name = guild.name or "Discord"
    guild_id = str(getattr(guild, "id", ""))
    notification = (
        f"🎧 Все вышли из голосового канала «{channel.name}» ({guild_name}). "
        "Уже 5 минут никого нет.\n"
        "Отключить такие оповещения:\n"
        "• в Discord: /voice_alerts_off\n"
        f"• в Telegram: /voice_alerts_off {guild_id}"
    )
    await send_telegram_notification(notification, discord_channel_id=str(channel.id))


def _cleanup_voice_empty_task(channel_id: int, task: asyncio.Task) -> None:
    if _voice_empty_notify_tasks.get(channel_id) is task:
        _voice_empty_notify_tasks.pop(channel_id, None)


def register_voice_state_handlers(bot: commands.Bot) -> None:
    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        logger.info("Joined new guild: %s (%s)", guild.name, guild.id)
        sync_discord_voice_channels()
        set_discord_autojoin_announce_sent(str(guild.id), False)

    @bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot and not BOT_CONFIG.get("VOICE_TEST_ALLOW_BOT_AUDIO", False):
            return
        if after.channel is not None:
            existing_task = _voice_empty_notify_tasks.pop(after.channel.id, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()

        if before.channel is None and after.channel is not None:
            channel = after.channel
            guild_name = channel.guild.name if channel.guild else "Discord"
            others_names = list_human_names_in_voice(channel, exclude_member_id=member.id)
            if not others_names:
                others_names = await list_human_names_in_voice_via_states(
                    channel, exclude_member_id=member.id
                )
            others_count = count_humans_in_voice(channel, exclude_member_id=member.id)
            # Make sure we can enumerate members when possible; fall back to count-only.
            if not others_names and others_count > 0:
                others_names = [f"{others_count} чел"]

            if not others_names and others_count == 0:
                others_part = "В чате пока больше никого."
            else:
                others_part = f"Еще в чате есть {_format_names_ru(others_names)}."

            if get_voice_presence_notifications_enabled(str(channel.guild.id)):
                guild_id = str(getattr(channel.guild, "id", ""))
                notification = (
                    f"🎧 В чат вошёл {member.display_name}: голосовой канал "
                    f"«{channel.name}» ({guild_name}). "
                    f"{others_part}\n"
                    "Отключить такие оповещения:\n"
                    "• в Discord: /voice_alerts_off\n"
                    f"• в Telegram: /voice_alerts_off {guild_id}"
                )
                await send_telegram_notification(notification, discord_channel_id=str(channel.id))

            if channel.guild and get_discord_autojoin(str(channel.guild.id)):
                voice_client = channel.guild.voice_client
                if voice_client is None or not voice_client.is_connected():
                    try:
                        voice_client = await connect_voice_channel(channel)
                        if voice_client:
                            ensure_voice_log_task(voice_client)
                        else:
                            logger.warning("Auto-join failed to connect to channel %s", channel.id)
                        set_last_voice_channel(str(channel.guild.id), str(channel.id))
                        if not get_discord_autojoin_announce_sent(str(channel.guild.id)):
                            announce_channel = pick_announcement_channel(channel.guild)
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

        if before.channel is not None and before.channel != after.channel:
            channel = before.channel
            humans = [m for m in channel.members if not m.bot]
            existing_task = _voice_empty_notify_tasks.pop(channel.id, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            if not humans:
                task = asyncio.create_task(
                    _notify_if_voice_empty(channel.id, channel.guild.id)
                )
                _voice_empty_notify_tasks[channel.id] = task
                task.add_done_callback(lambda t, cid=channel.id: _cleanup_voice_empty_task(cid, t))
