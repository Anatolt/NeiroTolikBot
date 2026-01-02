"""Простой консольный тестер для NeiroTolikBot.

Позволяет:
1) Прогонять смоук-тесты OpenRouter (генерация/переключение/память).
2) Проверять текстовые ответы всех слеш-команд офлайн, без сети.
3) Общаться с ботом из консоли в интерактивном режиме.

Примеры запуска:
    # Полные смоук-тесты (нужен ключ OpenRouter)
    python utils/console_tester.py --run-tests --api-key "<OPENROUTER_KEY>"

    # Только офлайн-проверка команд /help и /models
    python utils/console_tester.py --run-command-tests

    # Интерактивный режим
    python utils/console_tester.py --interactive --api-key "<OPENROUTER_KEY>"
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import List, Tuple
from unittest.mock import AsyncMock, patch
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import BOT_CONFIG
from services.generation import (
    check_model_availability,
    fetch_models_data,
    generate_text,
    init_client,
)
from services.memory import (
    add_message,
    add_notification_flow,
    clear_memory,
    get_history,
    init_db,
    set_voice_notification_chat_id,
    upsert_discord_voice_channel,
    upsert_telegram_chat,
)
from handlers.commands import (
    admin_command,
    admin_help_command,
    clear_memory_command,
    consilium_command,
    help_command,
    models_all_command,
    models_command,
    models_free_command,
    models_large_context_command,
    models_paid_command,
    models_pic_command,
    models_specialized_command,
    models_voice_command,
    models_voice_log_command,
    new_dialog,
    routing_llm_command,
    routing_mode_command,
    routing_rules_command,
    set_pic_model_command,
    setflow_command,
    set_voice_log_model_command,
    set_voice_model_command,
    show_discord_chats_command,
    show_tg_chats_command,
    start,
    flow_command,
    unsetflow_command,
    header_on_command,
    header_off_command,
    voice_msg_conversation_on_command,
    voice_msg_conversation_off_command,
    voice_log_debug_on_command,
    voice_log_debug_off_command,
)
from utils.helpers import escape_markdown_v2, resolve_system_prompt

logger = logging.getLogger(__name__)


def configure_bot(api_key: str | None = None, system_prompt: str | None = None) -> None:
    """Загружает настройки из .env и готовит клиента OpenRouter."""

    load_dotenv()

    BOT_CONFIG["OPENROUTER_API_KEY"] = api_key or os.getenv("OPENROUTER_API_KEY")
    BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = system_prompt or resolve_system_prompt(ROOT_DIR)

    if not BOT_CONFIG["OPENROUTER_API_KEY"]:
        raise RuntimeError(
            "Не найден ключ OpenRouter. Передайте --api-key или задайте OPENROUTER_API_KEY."
        )

    init_db()
    init_client()


@dataclass
class FakeUser:
    id: str
    first_name: str = "Console"

    @property
    def full_name(self) -> str:  # pragma: no cover - упрощённая модель пользователя
        return self.first_name

    def mention_markdown_v2(self) -> str:  # pragma: no cover - утилита для форматирования
        return f"[{self.first_name}](tg://user?id={self.id})"


@dataclass
class FakeChat:
    id: str


@dataclass
class FakeMessage:
    text: str | None = None
    replies: list[str] = field(default_factory=list)

    async def reply_markdown_v2(self, text: str) -> "FakeMessage":
        self.replies.append(text)
        return self

    async def reply_text(self, text: str, *_, **__) -> "FakeMessage":
        self.replies.append(text)
        return self

    async def delete(self) -> None:  # pragma: no cover - поведение статуса консилиума
        self.replies.append("__deleted__")


@dataclass
class FakeUpdate:
    effective_user: FakeUser
    effective_chat: FakeChat
    message: FakeMessage


@dataclass
class FakeContext:
    """Пустой контекст для вызова обработчиков команд."""

    bot_data: dict = field(default_factory=dict)
    user_data: dict = field(default_factory=dict)
    args: list[str] = field(default_factory=list)


async def run_command_tests(chat_id: str, user_id: str) -> List[Tuple[str, bool, str]]:
    """Проверяет ответы всех слеш-команд офлайн (без запроса к API)."""

    results: List[Tuple[str, bool, str]] = []
    user = FakeUser(id=user_id)
    chat = FakeChat(id=chat_id)
    context = FakeContext()
    admin_context = FakeContext(user_data={"is_admin": True})

    init_db()

    # 1. /start
    start_message = FakeMessage()
    start_update = FakeUpdate(effective_user=user, effective_chat=chat, message=start_message)
    await start(start_update, context)
    start_reply = start_message.replies[0] if start_message.replies else ""
    default_model = BOT_CONFIG["DEFAULT_MODEL"]
    default_model_escaped = escape_markdown_v2(default_model)
    start_ok = bool(start_message.replies) and (
        default_model in start_reply or default_model_escaped in start_reply
    )
    results.append(("Команда /start", start_ok, start_reply if start_message.replies else "Нет ответа"))

    # 2. /new
    new_message = FakeMessage()
    new_update = FakeUpdate(effective_user=user, effective_chat=chat, message=new_message)
    await new_dialog(new_update, context)
    new_ok = bool(new_message.replies) and "новый диалог" in new_message.replies[0].lower()
    results.append(("Команда /new", new_ok, new_message.replies[0] if new_message.replies else "Нет ответа"))

    # 3. /clear
    clear_message = FakeMessage()
    clear_update = FakeUpdate(effective_user=user, effective_chat=chat, message=clear_message)
    await clear_memory_command(clear_update, context)
    clear_ok = bool(clear_message.replies) and "память" in clear_message.replies[0].lower()
    results.append(("Команда /clear", clear_ok, clear_message.replies[0] if clear_message.replies else "Нет ответа"))

    # 4. /admin
    admin_message = FakeMessage()
    admin_update = FakeUpdate(effective_user=user, effective_chat=chat, message=admin_message)
    await admin_command(admin_update, context)
    admin_ok = bool(admin_message.replies)
    results.append(("Команда /admin", admin_ok, admin_message.replies[0] if admin_message.replies else "Нет ответа"))

    # 5. /help
    help_message = FakeMessage()
    help_update = FakeUpdate(effective_user=user, effective_chat=chat, message=help_message)
    await help_command(help_update, context)
    help_ok = bool(help_message.replies) and "/models" in help_message.replies[0]
    results.append(("Команда /help", help_ok, help_message.replies[0] if help_message.replies else "Нет ответа"))

    # 5.1 /admin_help
    admin_help_message = FakeMessage()
    admin_help_update = FakeUpdate(effective_user=user, effective_chat=chat, message=admin_help_message)
    await admin_help_command(admin_help_update, admin_context)
    admin_help_ok = bool(admin_help_message.replies) and "Команды администратора" in admin_help_message.replies[0]
    results.append(
        (
            "Команда /admin_help",
            admin_help_ok,
            admin_help_message.replies[0] if admin_help_message.replies else "Нет ответа",
        )
    )

    # 6. /models
    models_hint_message = FakeMessage()
    models_hint_update = FakeUpdate(
        effective_user=user,
        effective_chat=chat,
        message=models_hint_message,
    )
    await models_command(models_hint_update, context)
    models_hint_ok = bool(models_hint_message.replies) and "/models_free" in models_hint_message.replies[0]
    results.append(
        (
            "Команда /models",
            models_hint_ok,
            models_hint_message.replies[0] if models_hint_message.replies else "Нет ответа",
        )
    )

    # 7. Команды списка моделей без обращения к OpenRouter
    fake_models = [
        {"id": "test/ultra-free:free", "context_length": 200_000, "pricing": {"prompt": "0"}},
        {"id": "test/paid-pro:paid", "context_length": 120_000, "pricing": {"prompt": "0.01"}},
        {"id": "test/large_context", "context_length": 150_000, "pricing": {"prompt": "0.002"}},
        {"id": "test/specialized-medical", "context_length": 90_000, "pricing": {"prompt": "0.004"}},
        {"id": "test/coder-instruct", "context_length": 80_000, "pricing": {"prompt": "0.001"}},
    ]

    async def fake_build_messages(order, header=None, max_items_per_category=None):  # pragma: no cover - используется в офлайн-тестах
        parts = [header or ""] if header else []
        selected = []
        for category in order:
            selected.extend(model["id"] for model in fake_models if category in model.get("id", ""))
        parts.append("\n".join(selected))
        return parts

    with patch("services.generation.init_client", return_value=None), patch(
        "handlers.commands.fetch_models_data", AsyncMock(return_value=fake_models)
    ), patch("handlers.commands.build_models_messages", AsyncMock(side_effect=fake_build_messages)):
        for cmd_name, command_fn, title in [
            ("/models_free", models_free_command, "Команда /models_free (офлайн)"),
            ("/models_paid", models_paid_command, "Команда /models_paid (офлайн)"),
            ("/models_large_context", models_large_context_command, "Команда /models_large_context (офлайн)"),
            ("/models_specialized", models_specialized_command, "Команда /models_specialized (офлайн)"),
            ("/models_all", models_all_command, "Команда /models_all (офлайн)"),
        ]:
            msg = FakeMessage()
            upd = FakeUpdate(effective_user=user, effective_chat=chat, message=msg)
            await command_fn(upd, context)
            ok = bool(msg.replies) and any("test/" in part for part in msg.replies)
            results.append((title, ok, msg.replies[0][:400] if msg.replies else "Нет ответа"))

    # 8. /consilium без вопроса
    consilium_message = FakeMessage(text="/consilium")
    consilium_update = FakeUpdate(effective_user=user, effective_chat=chat, message=consilium_message)
    await consilium_command(consilium_update, context)
    consilium_ok = bool(consilium_message.replies) and "Консилиум" in consilium_message.replies[0]
    results.append(
        (
            "Команда /consilium (подсказка)",
            consilium_ok,
            consilium_message.replies[0] if consilium_message.replies else "Нет ответа",
        )
    )

    # 8.1 Админские команды списка чатов и настройка потока
    upsert_telegram_chat("123", "Test Chat", "group")
    upsert_discord_voice_channel("456", "Voice Room", "789", "Test Guild")

    show_discord_message = FakeMessage()
    show_discord_update = FakeUpdate(effective_user=user, effective_chat=chat, message=show_discord_message)
    await show_discord_chats_command(show_discord_update, admin_context)
    show_discord_ok = bool(show_discord_message.replies) and "Voice Room" in show_discord_message.replies[0]
    results.append(
        (
            "Команда /show_discord_chats",
            show_discord_ok,
            show_discord_message.replies[0] if show_discord_message.replies else "Нет ответа",
        )
    )

    show_tg_message = FakeMessage()
    show_tg_update = FakeUpdate(effective_user=user, effective_chat=chat, message=show_tg_message)
    await show_tg_chats_command(show_tg_update, admin_context)
    show_tg_ok = bool(show_tg_message.replies) and "Test Chat" in show_tg_message.replies[0]
    results.append(
        (
            "Команда /show_tg_chats",
            show_tg_ok,
            show_tg_message.replies[0] if show_tg_message.replies else "Нет ответа",
        )
    )

    setflow_context = FakeContext(user_data={"is_admin": True}, args=["123"])
    setflow_message = FakeMessage()
    setflow_update = FakeUpdate(effective_user=user, effective_chat=chat, message=setflow_message)
    set_voice_notification_chat_id("999")
    await setflow_command(setflow_update, setflow_context)
    setflow_ok = bool(setflow_message.replies) and "123" in setflow_message.replies[0]
    results.append(
        (
            "Команда /setflow",
            setflow_ok,
            setflow_message.replies[0] if setflow_message.replies else "Нет ответа",
        )
    )

    # 9. Настройка роутинга
    routing_rules_message = FakeMessage()
    routing_rules_update = FakeUpdate(
        effective_user=user, effective_chat=chat, message=routing_rules_message
    )
    await routing_rules_command(routing_rules_update, context)
    routing_rules_ok = bool(routing_rules_message.replies) and "алгоритмический" in routing_rules_message.replies[0]
    results.append(
        (
            "Команда /rout_algo",
            routing_rules_ok,
            routing_rules_message.replies[0] if routing_rules_message.replies else "Нет ответа",
        )
    )

    routing_llm_message = FakeMessage()
    routing_llm_update = FakeUpdate(effective_user=user, effective_chat=chat, message=routing_llm_message)
    await routing_llm_command(routing_llm_update, context)
    routing_llm_ok = bool(routing_llm_message.replies) and "LLM" in routing_llm_message.replies[0]
    results.append(
        (
            "Команда /rout_llm",
            routing_llm_ok,
            routing_llm_message.replies[0] if routing_llm_message.replies else "Нет ответа",
        )
    )

    routing_mode_message = FakeMessage()
    routing_mode_update = FakeUpdate(
        effective_user=user, effective_chat=chat, message=routing_mode_message
    )
    await routing_mode_command(routing_mode_update, context)
    routing_mode_ok = bool(routing_mode_message.replies) and "роутинга" in routing_mode_message.replies[0]
    results.append(
        (
            "Команда /rout",
            routing_mode_ok,
            routing_mode_message.replies[0] if routing_mode_message.replies else "Нет ответа",
        )
    )

    # 10. Дополнительные команды (голосовые/шапка/картинки/flows)
    original_voice_models = BOT_CONFIG.get("VOICE_MODELS")
    BOT_CONFIG["VOICE_MODELS"] = ["test-voice-1", "test-voice-2"]

    voice_models_message = FakeMessage()
    voice_models_update = FakeUpdate(effective_user=user, effective_chat=chat, message=voice_models_message)
    await models_voice_command(voice_models_update, context)
    voice_models_ok = bool(voice_models_message.replies) and "test-voice-1" in voice_models_message.replies[0]
    results.append(
        (
            "Команда /models_voice",
            voice_models_ok,
            voice_models_message.replies[0] if voice_models_message.replies else "Нет ответа",
        )
    )

    voice_log_models_message = FakeMessage()
    voice_log_models_update = FakeUpdate(effective_user=user, effective_chat=chat, message=voice_log_models_message)
    await models_voice_log_command(voice_log_models_update, context)
    voice_log_models_ok = bool(voice_log_models_message.replies) and "test-voice-1" in voice_log_models_message.replies[0]
    results.append(
        (
            "Команда /voice_log_models",
            voice_log_models_ok,
            voice_log_models_message.replies[0] if voice_log_models_message.replies else "Нет ответа",
        )
    )

    set_voice_context = FakeContext(args=["1"])
    set_voice_message = FakeMessage()
    set_voice_update = FakeUpdate(effective_user=user, effective_chat=chat, message=set_voice_message)
    await set_voice_model_command(set_voice_update, set_voice_context)
    set_voice_ok = bool(set_voice_message.replies) and "установлена" in set_voice_message.replies[0].lower()
    results.append(
        (
            "Команда /set_voice_model",
            set_voice_ok,
            set_voice_message.replies[0] if set_voice_message.replies else "Нет ответа",
        )
    )

    set_voice_log_message = FakeMessage()
    set_voice_log_update = FakeUpdate(effective_user=user, effective_chat=chat, message=set_voice_log_message)
    await set_voice_log_model_command(set_voice_log_update, set_voice_context)
    set_voice_log_ok = bool(set_voice_log_message.replies) and "установлена" in set_voice_log_message.replies[0].lower()
    results.append(
        (
            "Команда /set_voice_log_model",
            set_voice_log_ok,
            set_voice_log_message.replies[0] if set_voice_log_message.replies else "Нет ответа",
        )
    )

    BOT_CONFIG["VOICE_MODELS"] = original_voice_models

    header_on_message = FakeMessage()
    header_on_update = FakeUpdate(effective_user=user, effective_chat=chat, message=header_on_message)
    await header_on_command(header_on_update, context)
    header_on_ok = bool(header_on_message.replies) and "техшапка" in header_on_message.replies[0].lower()
    results.append(
        (
            "Команда /header_on",
            header_on_ok,
            header_on_message.replies[0] if header_on_message.replies else "Нет ответа",
        )
    )

    header_off_message = FakeMessage()
    header_off_update = FakeUpdate(effective_user=user, effective_chat=chat, message=header_off_message)
    await header_off_command(header_off_update, context)
    header_off_ok = bool(header_off_message.replies) and "техшапка" in header_off_message.replies[0].lower()
    results.append(
        (
            "Команда /header_off",
            header_off_ok,
            header_off_message.replies[0] if header_off_message.replies else "Нет ответа",
        )
    )

    voice_on_message = FakeMessage()
    voice_on_update = FakeUpdate(effective_user=user, effective_chat=chat, message=voice_on_message)
    await voice_msg_conversation_on_command(voice_on_update, context)
    voice_on_ok = bool(voice_on_message.replies) and "автоответ" in voice_on_message.replies[0].lower()
    results.append(
        (
            "Команда /voice_msg_conversation_on",
            voice_on_ok,
            voice_on_message.replies[0] if voice_on_message.replies else "Нет ответа",
        )
    )

    voice_off_message = FakeMessage()
    voice_off_update = FakeUpdate(effective_user=user, effective_chat=chat, message=voice_off_message)
    await voice_msg_conversation_off_command(voice_off_update, context)
    voice_off_ok = bool(voice_off_message.replies) and "автоответ" in voice_off_message.replies[0].lower()
    results.append(
        (
            "Команда /voice_msg_conversation_off",
            voice_off_ok,
            voice_off_message.replies[0] if voice_off_message.replies else "Нет ответа",
        )
    )

    voice_debug_on_message = FakeMessage()
    voice_debug_on_update = FakeUpdate(effective_user=user, effective_chat=chat, message=voice_debug_on_message)
    await voice_log_debug_on_command(voice_debug_on_update, context)
    voice_debug_on_ok = bool(voice_debug_on_message.replies) and "лог" in voice_debug_on_message.replies[0].lower()
    results.append(
        (
            "Команда /voice_log_debug_on",
            voice_debug_on_ok,
            voice_debug_on_message.replies[0] if voice_debug_on_message.replies else "Нет ответа",
        )
    )

    voice_debug_off_message = FakeMessage()
    voice_debug_off_update = FakeUpdate(effective_user=user, effective_chat=chat, message=voice_debug_off_message)
    await voice_log_debug_off_command(voice_debug_off_update, context)
    voice_debug_off_ok = bool(voice_debug_off_message.replies) and "лог" in voice_debug_off_message.replies[0].lower()
    results.append(
        (
            "Команда /voice_log_debug_off",
            voice_debug_off_ok,
            voice_debug_off_message.replies[0] if voice_debug_off_message.replies else "Нет ответа",
        )
    )

    fake_pic_models = (["piapi/test-img"], ["imagerouter/test-img"], ["piapi/test-img", "imagerouter/test-img"])
    with patch("handlers.commands._refresh_image_models", AsyncMock(return_value=fake_pic_models)):
        models_pic_message = FakeMessage()
        models_pic_update = FakeUpdate(effective_user=user, effective_chat=chat, message=models_pic_message)
        await models_pic_command(models_pic_update, context)
        models_pic_ok = bool(models_pic_message.replies) and "piapi/test-img" in models_pic_message.replies[0]
        results.append(
            (
                "Команда /models_pic",
                models_pic_ok,
                models_pic_message.replies[0] if models_pic_message.replies else "Нет ответа",
            )
        )

        set_pic_context = FakeContext(args=["1"])
        set_pic_message = FakeMessage()
        set_pic_update = FakeUpdate(effective_user=user, effective_chat=chat, message=set_pic_message)
        await set_pic_model_command(set_pic_update, set_pic_context)
        set_pic_ok = bool(set_pic_message.replies) and "установлена" in set_pic_message.replies[0].lower()
        results.append(
            (
                "Команда /set_pic_model",
                set_pic_ok,
                set_pic_message.replies[0] if set_pic_message.replies else "Нет ответа",
            )
        )

    add_notification_flow("456", "123")
    flow_message = FakeMessage()
    flow_update = FakeUpdate(effective_user=user, effective_chat=chat, message=flow_message)
    await flow_command(flow_update, admin_context)
    flow_ok = bool(flow_message.replies) and "Discord" in flow_message.replies[0]
    results.append(
        (
            "Команда /flow",
            flow_ok,
            flow_message.replies[0] if flow_message.replies else "Нет ответа",
        )
    )

    unsetflow_context = FakeContext(user_data={"is_admin": True}, args=["I"])
    unsetflow_message = FakeMessage()
    unsetflow_update = FakeUpdate(effective_user=user, effective_chat=chat, message=unsetflow_message)
    await unsetflow_command(unsetflow_update, unsetflow_context)
    unsetflow_ok = bool(unsetflow_message.replies)
    results.append(
        (
            "Команда /unsetflow",
            unsetflow_ok,
            unsetflow_message.replies[0] if unsetflow_message.replies else "Нет ответа",
        )
    )

    return results


async def run_single_prompt(prompt: str, model: str, chat_id: str, user_id: str) -> str:
    """Отправляет одиночный запрос в указанную модель с записью в память."""

    add_message(chat_id, user_id, "user", model, prompt)
    response, used_model, _context_info = await generate_text(prompt, model, chat_id, user_id)
    add_message(chat_id, user_id, "assistant", used_model, response)
    return response


async def run_smoke_tests(
    model: str, alternate_model: str, chat_id: str, user_id: str
) -> List[Tuple[str, bool, str]]:
    """Выполняет набор консольных тестов и возвращает результаты."""

    clear_memory(chat_id, user_id)
    results: List[Tuple[str, bool, str]] = []

    # 0. Доступность базового API
    api_models = await fetch_models_data()
    api_ok = bool(api_models)
    results.append(
        (
            "Доступность API (models)",
            api_ok,
            f"Получено {len(api_models)} моделей" if api_ok else "Не удалось получить список моделей",
        )
    )

    # 1. Доступность основной модели
    is_default_available = await check_model_availability(model)
    results.append(
        (
            "Доступность основной модели",
            is_default_available,
            f"Модель {model} {'доступна' if is_default_available else 'недоступна'}",
        )
    )

    if not is_default_available:
        # Продолжаем, но помечаем остальные проверки как пропущенные
        results.append(
            (
                "Базовый ответ",
                False,
                "Пропущено из-за недоступности основной модели",
            )
        )
        results.append(
            (
                "Доступность альтернативной модели",
                False,
                "Пропущено из-за недоступности основной модели",
            )
        )
        results.append(
            (
                "Переключение модели",
                False,
                "Пропущено из-за недоступности основной модели",
            )
        )
        results.append(
            (
                "Память диалога",
                False,
                "Пропущено из-за недоступности основной модели",
            )
        )
        results.append(
            (
                "Размер истории",
                False,
                "Пропущено из-за недоступности основной модели",
            )
        )
        return results

    # 2. Базовая генерация
    base_prompt = "Скажи коротко: бот работает"
    base_response = await run_single_prompt(base_prompt, model, chat_id, user_id)
    base_ok = bool(base_response)
    results.append(
        (
            "Базовый ответ",
            base_ok,
            base_response[:200] if base_response else "Ответ пустой",
        )
    )

    # 3. Переключение модели
    alt_available = await check_model_availability(alternate_model)
    results.append(
        (
            "Доступность альтернативной модели",
            alt_available,
            f"Модель {alternate_model} {'доступна' if alt_available else 'недоступна'}",
        )
    )

    switch_response = None
    if alt_available:
        switch_prompt = "Ответь словом 'переключение'"
        switch_response = await run_single_prompt(
            switch_prompt, alternate_model, chat_id, user_id
        )
        switch_ok = bool(switch_response)
        results.append(
            (
                "Переключение модели",
                switch_ok,
                switch_response[:200] if switch_response else "Ответ пустой",
            )
        )

    # 4. Проверка памяти
    memory_prompt = "Что я просил тебя подтвердить в первом тесте?"
    memory_response = await run_single_prompt(
        memory_prompt, alternate_model if alt_available else model, chat_id, user_id
    )
    history_snapshot = get_history(chat_id, user_id, limit=6)
    memory_ok = bool(memory_response) and any(
        kw in memory_response.lower() for kw in ["бот работает", "бот", "нейротолик", "подтвердить"]
    )
    results.append(
        (
            "Память диалога",
            memory_ok,
            memory_response[:200] if memory_response else "Ответ пустой",
        )
    )

    expected_history_len = 6 if alt_available else 4
    results.append(
        (
            "Размер истории",
            len(history_snapshot) >= expected_history_len,
            f"В памяти {len(history_snapshot)} сообщений (ожидалось ≥ {expected_history_len})",
        )
    )

    return results


def print_results(results: List[Tuple[str, bool, str]]) -> None:
    """Красиво выводит результаты тестов в консоль."""

    for name, success, details in results:
        status = "✅" if success else "❌"
        print(f"{status} {name}: {details}")


async def interactive_chat(model: str, chat_id: str, user_id: str) -> None:
    """Простой REPL для общения с моделью через консоль."""

    print("Введите сообщение (или 'exit' для выхода):")
    while True:
        prompt = input("> ").strip()
        if prompt.lower() in {"exit", "quit"}:
            break

        response = await run_single_prompt(prompt, model, chat_id, user_id)
        print(f"\nОтвет ({model}):\n{response}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Консольный тестер NeiroTolikBot")
    parser.add_argument("--api-key", help="Ключ OpenRouter (иначе берётся из окружения)")
    parser.add_argument(
        "--model",
        default=BOT_CONFIG["DEFAULT_MODEL"],
        help="Основная модель для тестов",
    )
    parser.add_argument(
        "--alternate-model",
        default="meta-llama/llama-3.3-70b-instruct:free",
        help="Альтернативная модель для проверки переключения",
    )
    parser.add_argument("--chat-id", default="console_chat", help="ID чата для тестов")
    parser.add_argument("--user-id", default="console_user", help="ID пользователя для тестов")
    parser.add_argument("--prompt", help="Отправить одиночный промпт вместо тестов")
    parser.add_argument("--run-tests", action="store_true", help="Запустить смоук-тесты")
    parser.add_argument(
        "--run-command-tests",
        action="store_true",
        help="Запустить офлайн-тесты команд /help и /models (без OpenRouter)",
    )
    parser.add_argument("--interactive", action="store_true", help="Интерактивный режим")
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )

    args = parse_args()

    if args.run_command_tests:
        command_results = await run_command_tests(args.chat_id, args.user_id)
        print_results(command_results)

    need_api = args.run_tests or args.prompt or args.interactive

    if need_api:
        configure_bot(api_key=args.api_key)

    if args.run_tests:
        results = await run_smoke_tests(
            args.model, args.alternate_model, args.chat_id, args.user_id
        )
        print_results(results)

    if args.prompt:
        response = await run_single_prompt(args.prompt, args.model, args.chat_id, args.user_id)
        print(response)

    if args.interactive:
        await interactive_chat(args.model, args.chat_id, args.user_id)


if __name__ == "__main__":
    asyncio.run(main())
