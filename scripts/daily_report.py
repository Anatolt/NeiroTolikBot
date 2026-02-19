#!/usr/bin/env python3
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # Ensure imports work when running the script directly.
    sys.path.insert(0, str(PROJECT_ROOT))

from services.memory import get_all_admins, get_usage_summary


def _format_money(value: float) -> str:
    return f"${value:.4f}"


def _resolve_report_chat_ids() -> list[int]:
    raw = os.getenv("DAILY_REPORT_CHAT_IDS", "").strip()
    if raw:
        chat_ids: list[int] = []
        for part in raw.split(","):
            item = part.strip()
            if not item:
                continue
            try:
                chat_ids.append(int(item))
            except ValueError:
                raise SystemExit(f"Invalid DAILY_REPORT_CHAT_IDS value: {item}")
        if chat_ids:
            return chat_ids


def _build_report_section(title: str, summary: dict) -> str:
    return (
        f"{title}\n"
        f"Пользователи: {summary['users']}\n"
        f"События: {summary['events']}\n"
        f"Всего: {_format_money(summary['total_cost'])}\n"
        f"Текст: {_format_money(summary['text_cost'])}\n"
        f"Картинки: {_format_money(summary['image_cost'])}\n"
        f"STT: {_format_money(summary['stt_cost'])}\n"
        f"TTS: {_format_money(summary.get('tts_cost', 0.0))}"
    )


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    chat_ids = _resolve_report_chat_ids()
    if not chat_ids:
        admins = get_all_admins()
        if not admins:
            raise SystemExit("No admins configured")
        chat_ids = [int(a["chat_id"]) for a in admins if a.get("chat_id")]
    if not chat_ids:
        raise SystemExit("No report chat ids configured")

    now = datetime.now()
    start = now - timedelta(days=1)

    telegram_summary = get_usage_summary("telegram", start.isoformat(), now.isoformat())
    discord_summary = get_usage_summary("discord", start.isoformat(), now.isoformat())

    header = f"📊 Отчет за последние 24 часа ({start:%Y-%m-%d %H:%M} – {now:%Y-%m-%d %H:%M})"
    telegram_section = _build_report_section("Telegram", telegram_summary)
    discord_section = _build_report_section("Discord", discord_summary)
    text = f"{header}\n\n{telegram_section}\n\n{discord_section}"

    bot = Bot(token=token)
    for chat_id in chat_ids:
        await bot.send_message(chat_id=chat_id, text=text)


if __name__ == "__main__":
    asyncio.run(main())
