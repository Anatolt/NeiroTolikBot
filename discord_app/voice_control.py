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
    if voice_client and voice_client.is_connected():
        if voice_client.channel and voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)
        return voice_client

    try:
        voice_client = await channel.connect()
    except discord.ClientException as exc:
        existing = channel.guild.voice_client
        if existing and existing.is_connected():
            return existing
        logger.warning("Voice connect failed: %s", exc)
        return None

    try:
        await channel.guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
    except Exception as exc:
        logger.warning("Failed to set voice state for receive: %s", exc)

    return voice_client
