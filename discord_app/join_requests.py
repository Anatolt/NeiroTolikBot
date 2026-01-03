import asyncio
import logging

import discord

from discord_app.notifications import notify_discord_user
from discord_app.runtime import get_bot
from discord_app.voice_control import connect_voice_channel
from discord_app.voice_log import ensure_voice_log_task
from services.memory import (
    get_unprocessed_discord_join_requests,
    mark_discord_join_request_processed,
    set_last_voice_channel,
)

logger = logging.getLogger(__name__)

_join_request_task: asyncio.Task | None = None


async def _process_join_requests_loop() -> None:
    bot = get_bot()
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
                        await notify_discord_user(
                            user_id,
                            "Админ разрешил. Пригласите меня на сервер «{guild_name}»: "
                            "https://discord.com/oauth2/authorize?client_id=1451265052978974931&permissions=3147776&scope=bot%20applications.commands",
                        )
                    elif status == "denied":
                        await notify_discord_user(user_id, "Админ отказал в подключении.")

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
                        await notify_discord_user(user_id, "Админ разрешил, но я не нашёл канал.")
                    elif channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                        await notify_discord_user(user_id, "Админ разрешил, но это не голосовой канал.")
                    else:
                        try:
                            voice_client = await connect_voice_channel(channel)
                            if voice_client:
                                ensure_voice_log_task(voice_client)
                            set_last_voice_channel(str(channel.guild.id), str(channel.id))
                            await notify_discord_user(user_id, "Админ разрешил. Подключаюсь.")
                        except Exception as exc:
                            await notify_discord_user(user_id, "Админ разрешил, но не смог подключиться.")
                            logger.warning("Failed to join voice channel: %s", exc)
                elif status == "denied":
                    await notify_discord_user(user_id, "Админ отказал в подключении.")

                mark_discord_join_request_processed(request_id)
            except Exception as exc:
                logger.warning("Failed to process join request: %s", exc)

        await asyncio.sleep(3)


def ensure_join_request_task() -> None:
    global _join_request_task
    if _join_request_task is None or _join_request_task.done():
        _join_request_task = asyncio.create_task(_process_join_requests_loop())
