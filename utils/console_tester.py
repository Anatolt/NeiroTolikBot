"""Простой консольный тестер для NeiroTolikBot.

Позволяет:
1) Прогонять смоук-тесты OpenRouter (генерация/переключение/память).
2) Проверять текстовые ответы команд /help и /models офлайн, без сети.
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
from services.generation import check_model_availability, generate_text, init_client
from services.memory import add_message, clear_memory, get_history, init_db
from handlers.commands import help_command, models_command, models_free_command

logger = logging.getLogger(__name__)


def configure_bot(api_key: str | None = None, system_prompt: str | None = None) -> None:
    """Загружает настройки из .env и готовит клиента OpenRouter."""

    load_dotenv()

    BOT_CONFIG["OPENROUTER_API_KEY"] = api_key or os.getenv("OPENROUTER_API_KEY")
    BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = system_prompt or os.getenv(
        "CUSTOM_SYSTEM_PROMPT", "You are a helpful assistant."
    )

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

    def mention_markdown_v2(self) -> str:  # pragma: no cover - утилита для форматирования
        return f"[{self.first_name}](tg://user?id={self.id})"


@dataclass
class FakeChat:
    id: str


@dataclass
class FakeMessage:
    replies: list[str] = field(default_factory=list)

    async def reply_markdown_v2(self, text: str) -> None:
        self.replies.append(text)

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


@dataclass
class FakeUpdate:
    effective_user: FakeUser
    effective_chat: FakeChat
    message: FakeMessage


class FakeContext:
    """Пустой контекст для вызова обработчиков команд."""

    bot_data: dict = {}


async def run_command_tests(chat_id: str, user_id: str) -> List[Tuple[str, bool, str]]:
    """Проверяет ответы команд /help и /models офлайн (без запроса к API)."""

    results: List[Tuple[str, bool, str]] = []
    user = FakeUser(id=user_id)
    chat = FakeChat(id=chat_id)

    # 1. /help
    help_message = FakeMessage()
    help_update = FakeUpdate(effective_user=user, effective_chat=chat, message=help_message)
    await help_command(help_update, FakeContext())
    help_ok = bool(help_message.replies) and "/models" in help_message.replies[0]
    results.append(
        ("Команда /help", help_ok, help_message.replies[0] if help_message.replies else "Нет ответа")
    )

    # 2. /models_free без обращения к OpenRouter — подставляем тестовые данные
    fake_models = [
        {"id": "test/ultra-free:free", "context_length": 200_000, "pricing": {"prompt": "0"}},
        {"id": "test/large-context", "context_length": 150_000, "pricing": {"prompt": "0.002"}},
        {"id": "test/coder-instruct", "context_length": 80_000, "pricing": {"prompt": "0.001"}},
    ]

    models_message = FakeMessage()
    models_update = FakeUpdate(
        effective_user=user,
        effective_chat=chat,
        message=models_message,
    )

    with patch("services.generation.init_client", return_value=None), patch(
        "services.generation.fetch_models_data", AsyncMock(return_value=fake_models)
    ):
        await models_free_command(models_update, FakeContext())

    models_ok = bool(models_message.replies) and "test/ultra-free" in models_message.replies[0]
    results.append(
        (
            "Команда /models_free (офлайн)",
            models_ok,
            models_message.replies[0][:400] if models_message.replies else "Нет ответа",
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
