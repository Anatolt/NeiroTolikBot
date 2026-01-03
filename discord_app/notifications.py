import logging

from services.memory import get_all_admins, get_notification_flows_for_channel, get_voice_notification_chat_id
from discord_app.runtime import get_bot, get_telegram_bot

logger = logging.getLogger(__name__)


async def send_telegram_notification(text: str, discord_channel_id: str | None = None) -> None:
    telegram_bot = get_telegram_bot()
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

    flow_chat_ids: list[str] = []
    if discord_channel_id:
        flows = get_notification_flows_for_channel(discord_channel_id)
        flow_chat_ids = [str(flow["telegram_chat_id"]) for flow in flows if flow.get("telegram_chat_id")]
        for chat_id in flow_chat_ids:
            await _send(chat_id)

    chat_id = get_voice_notification_chat_id()
    if not chat_id or flow_chat_ids:
        if not flow_chat_ids and not chat_id:
            logger.info("No admins or flow/voice notification chat configured.")
        return

    await _send(str(chat_id))


async def send_telegram_join_request(request_id: int, guild_name: str, user_name: str) -> None:
    telegram_bot = get_telegram_bot()
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


async def notify_discord_user(user_id: int, text: str) -> None:
    bot = get_bot()
    try:
        user = await bot.fetch_user(user_id)
        await user.send(text)
    except Exception as exc:
        logger.warning("Failed to notify Discord user %s: %s", user_id, exc)
