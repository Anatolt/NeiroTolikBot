from telegram import Update
from telegram.ext import ContextTypes

from handlers.commands_utils import is_admin_user
from services.memory import get_user_profile, upsert_user_profile

ADMIN_COMMANDS_TEXT = (
    "👑 Команды администратора:\n"
    "• /flow — показать текущие связи Discord → Telegram\n"
    "• /setflow — настроить связь Discord → Telegram\n"
    "• /unsetflow — отключить связь Discord → Telegram\n"
    "• /show_discord_chats — показать голосовые чаты Discord\n"
    "• /show_tg_chats — показать чаты Telegram, где есть бот\n"
    "• /voice_log_debug_on — включить подробный лог распознавания\n"
    "• /voice_log_debug_off — отключить подробный лог распознавания\n"
    "• /voice_alerts_off [guild_id] — отключить Telegram-алерты по voice\n"
    "• /voice_alerts_on [guild_id] — включить Telegram-алерты по voice\n"
    "• /voice_alerts_status [guild_id] — статус voice-алертов\n"
    "• /selftest — офлайн-проверка слеш-команд (отправляет файл)\n"
    "• /user_profile [chat_id] <user_id> — профиль пользователя\n"
    "• /admin_help — показать эту справку\n"
    "\n"
    "🎙️ Голосовые модели:\n"
    "• /models_voice — список моделей распознавания\n"
    "• /set_voice_model <номер> — выбрать модель распознавания\n"
    "• /voice_send_raw — слать аудио целиком, без нарезки (дороже, лимит 25MB)\n"
    "• /voice_send_segmented — слать аудио кусками по паузам речи (лимит 25MB)\n"
    "• /tts_voices — список голосов TTS\n"
    "• /set_tts_voice <номер> — выбрать голос TTS\n"
    "\n"
    "Текстовые команды:\n"
    "• покажи чаты дискорд\n"
    "• покажи чаты тг"
)


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по административным командам."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    await update.message.reply_text(ADMIN_COMMANDS_TEXT)


async def user_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает профиль пользователя из памяти."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    args = context.args or []
    chat_id = str(update.effective_chat.id) if update.effective_chat else None
    user_id = str(update.effective_user.id) if update.effective_user else None

    if len(args) == 1:
        if args[0].lower() in {"me", "я"}:
            pass
        else:
            user_id = args[0]
    elif len(args) >= 2:
        chat_id = args[0]
        user_id = args[1]

    if not chat_id or not user_id:
        await update.message.reply_text("Использование: /user_profile [chat_id] <user_id>")
        return

    profile = get_user_profile("telegram", chat_id, user_id)
    if not profile:
        if update.effective_user and update.effective_chat:
            fallback_name = update.effective_user.username or update.effective_user.full_name
            upsert_user_profile("telegram", chat_id, user_id, fallback_name)
            profile = get_user_profile("telegram", chat_id, user_id)

    if not profile:
        await update.message.reply_text("Профиль не найден.")
        return

    user_name = profile.get("user_name") or "(пусто)"
    updated_at = profile.get("updated_at") or "(неизвестно)"
    await update.message.reply_text(
        f"Профиль пользователя:\nchat_id: {chat_id}\nuser_id: {user_id}\nuser_name: {user_name}\nupdated_at: {updated_at}"
    )
