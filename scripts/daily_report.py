#!/usr/bin/env python3
import asyncio
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import Bot

from services.memory import get_all_admins, get_usage_summary


def _format_money(value: float) -> str:
    return f"${value:.4f}"


def _build_report_section(title: str, summary: dict) -> str:
    return (
        f"{title}\n"
        f"Пользователи: {summary['users']}\n"
        f"События: {summary['events']}\n"
        f"Всего: {_format_money(summary['total_cost'])}\n"
        f"Текст: {_format_money(summary['text_cost'])}\n"
        f"Картинки: {_format_money(summary['image_cost'])}\n"
        f"STT: {_format_money(summary['stt_cost'])}"
    )


async def main() -> None:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    admins = get_all_admins()
    if not admins:
        raise SystemExit("No admins configured")

    now = datetime.now()
    start = now - timedelta(days=1)

    telegram_summary = get_usage_summary("telegram", start.isoformat(), now.isoformat())
    discord_summary = get_usage_summary("discord", start.isoformat(), now.isoformat())

    header = f"📊 Отчет за последние 24 часа ({start:%Y-%m-%d %H:%M} – {now:%Y-%m-%d %H:%M})"
    telegram_section = _build_report_section("Telegram", telegram_summary)
    discord_section = _build_report_section("Discord", discord_summary)
    text = f"{header}\n\n{telegram_section}\n\n{discord_section}"

    bot = Bot(token=token)
    for admin in admins:
        chat_id = admin.get("chat_id")
        if not chat_id:
            continue
        await bot.send_message(chat_id=int(chat_id), text=text)


if __name__ == "__main__":
    asyncio.run(main())
