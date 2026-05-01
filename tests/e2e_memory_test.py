
import asyncio
import sys
import os
from telethon import TelegramClient, events

# Конфигурация из системы
API_ID = 559815
API_HASH = 'fd121358f59d764c57c55871aa0807ca'
SESSION_PATH = '/root/.config/telethon-send/session'
BOT_USERNAME = '@NeiroTolikBot'
TEST_GROUP_ID = -5014438247  # тест нейро толик бот 2

async def run_test():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    
    if not await client.is_user_authorized():
        print("Ошибка: Аккаунт liqevb не авторизован.")
        return

    print(f"--- Запуск расширенного E2E теста памяти для {BOT_USERNAME} ---")

    async def send_and_wait(target, message, timeout=120):
        print(f"Отправка в {target}: {message}")
        try:
            async with client.conversation(target, timeout=timeout) as conv:
                await conv.send_message(message)
                
                while True:
                    response = await conv.get_response()
                    text = response.text
                    print(f"--- ПОЛУЧЕНО ---\n{text[:200]}...\n---------------")
                    
                    # Игнорируем сервисные сообщения роутера и подтверждения
                    service_prefixes = ("🤖", "✅", "🔀", "🎯", "🏥")
                    if text.strip().startswith(service_prefixes):
                        if "/yes" in text:
                            print("Обнаружено требование подтверждения, отправляю /yes")
                            await conv.send_message("/yes")
                        continue
                    
                    if not text or text.strip() == "":
                        continue
                        
                    return text
        except Exception as e:
            print(f"Ошибка при ожидании ответа: {e}")
            return None

    # --- ТЕСТ 1: Личный чат ---
    print("\n[ТЕСТ 1] Личный чат: Запоминание")
    p_fact = "Мой любимый напиток — горячий шоколад с солью."
    await send_and_wait(BOT_USERNAME, f"Запомни секретный факт для лички: {p_fact}")
    
    print("\n[ТЕСТ 1] Личный чат: Проверка")
    p_answer = await send_and_wait(BOT_USERNAME, "Какой мой любимый напиток?")
    if p_answer and "шоколад" in p_answer.lower():
        print("✅ Личная память работает.")
    else:
        print("❌ Личная память НЕ работает.")

    # --- ТЕСТ 2: Групповой чат ---
    print("\n[ТЕСТ 2] Группа: Запоминание")
    g_fact = "В этом чате мы решили, что наш девиз — 'Слабоумие и отвага'."
    await send_and_wait(TEST_GROUP_ID, f"{BOT_USERNAME} Запомни наш девиз: {g_fact}")
    
    print("\n[ТЕСТ 2] Группа: Проверка общей памяти")
    g_answer = await send_and_wait(TEST_GROUP_ID, f"{BOT_USERNAME} Какой наш девиз?")
    if g_answer and "отвага" in g_answer.lower():
        print("✅ Групповая память работает.")
    else:
        print("❌ Групповая память НЕ работает.")

    # --- ТЕСТ 3: Изоляция (Личка -> Группа) ---
    print("\n[ТЕСТ 3] Изоляция: Проверка отсутствия личных фактов в группе")
    i_answer = await send_and_wait(TEST_GROUP_ID, f"{BOT_USERNAME} Помнишь, какой мой любимый напиток?")
    if i_answer and "шоколад" in i_answer.lower():
        print("❌ ПРОВАЛ: Личная память протекла в группу!")
    else:
        print("✅ ИЗОЛЯЦИЯ: Бот не выдал личный секрет в группе.")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(run_test())
