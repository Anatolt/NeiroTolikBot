from __future__ import annotations

from typing import Optional

from discord.ext import commands
from telegram import Bot

_bot: commands.Bot | None = None
_telegram_bot: Bot | None = None


def init_runtime(bot: commands.Bot, telegram_bot: Bot | None) -> None:
    global _bot, _telegram_bot
    _bot = bot
    _telegram_bot = telegram_bot


def get_bot() -> commands.Bot:
    if _bot is None:
        raise RuntimeError("Discord bot is not initialized")
    return _bot


def get_telegram_bot() -> Bot | None:
    return _telegram_bot
