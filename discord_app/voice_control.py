import asyncio
import logging

import discord

from services.memory import upsert_discord_voice_channel
from discord_app.runtime import get_bot

logger = logging.getLogger(__name__)


def sync_discord_voice_channels() -> None:
    bot = get_bot()
    for guild in bot.guilds:
        for channel in list(guild.voice_channels) + list(guild.stage_channels):
            upsert_discord_voice_channel(
                channel_id=str(channel.id),
                channel_name=channel.name,
                guild_id=str(guild.id),
                guild_name=guild.name,
            )


async def connect_voice_channel(
    channel: discord.VoiceChannel | discord.StageChannel,
) -> discord.VoiceClient | None:
    voice_client = channel.guild.voice_client
    if voice_client:
        if voice_client.is_connected():
            if voice_client.channel and voice_client.channel.id != channel.id:
                await voice_client.move_to(channel)
            return voice_client
        # Stale voice client object can remain attached to guild after reconnect races.
        # Disconnect it explicitly before creating a new connection.
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            pass

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            voice_client = await channel.connect()
            break
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            last_error = exc
            existing = channel.guild.voice_client
            # Discord can throw "Already connected..." during reconnect races.
            # Reuse the current client or retry once after a short delay.
            if isinstance(exc, discord.ClientException) and "already connected" in str(exc).lower():
                if existing and existing.is_connected():
                    if existing.channel and existing.channel.id != channel.id:
                        await existing.move_to(channel)
                    return existing
                for vc in get_bot().voice_clients:
                    if getattr(getattr(vc, "guild", None), "id", None) == channel.guild.id:
                        if vc.is_connected() and vc.channel and vc.channel.id != channel.id:
                            await vc.move_to(channel)
                        return vc
            if attempt == 1:
                await asyncio.sleep(0.7)
                continue
            logger.warning(
                "Voice connect failed guild=%s channel=%s error=%s",
                channel.guild.id,
                channel.id,
                exc,
            )
            return None

    if not voice_client:
        if last_error:
            logger.warning(
                "Voice connect failed guild=%s channel=%s error=%s",
                channel.guild.id,
                channel.id,
                last_error,
            )
        return None

    try:
        await channel.guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
    except Exception as exc:
        logger.warning("Failed to set voice state for receive: %s", exc)

    return voice_client
