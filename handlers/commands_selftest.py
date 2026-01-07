import logging
from io import BytesIO

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def selftest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает офлайн-проверку слеш-команд и отправляет файл с результатами."""
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    status_message = await update.message.reply_text(
        "🔎 Запускаю офлайн-тест слеш-команд. Это может занять несколько секунд..."
    )

    try:
        from utils.console_tester import run_command_tests

        results = await run_command_tests(chat_id, user_id)
    except Exception as e:  # pragma: no cover
        logger.exception("Selftest failed: %s", e)
        await status_message.edit_text(f"❌ Не удалось выполнить selftest: {e}")
        return

    passed = sum(1 for _name, ok, _details in results if ok)
    total = len(results)

    lines = [
        "Результаты офлайн-теста слеш-команд:",
        f"Чат: {chat_id}",
        f"Пользователь: {user_id}",
        "",
    ]

    for name, success, details in results:
        status = "✅" if success else "❌"
        lines.append(f"{status} {name}")
        lines.append(f"    {details}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend(
        [
            "",
            f"Итого: {passed}/{total} успешных проверок",
        ]
    )

    buffer = BytesIO("\n".join(lines).encode("utf-8"))
    buffer.name = "selftest_results.txt"
    buffer.seek(0)

    await status_message.delete()

    await update.message.reply_document(
        document=buffer,
        caption=f"Selftest завершён: {passed}/{total} успешных проверок.",
    )
