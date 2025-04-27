# Модуль: history_manager.py

# 1. Структура таблицы (SQLite)
# Таблица: messages
# Поля:
#   id (int, primary key)
#   chat_id (str)
#   user_id (str)
#   role (str: 'user' | 'assistant')
#   model (str)
#   text (str)
#   timestamp (datetime)
#   session_id (str, optional)

# 2. Добавить сообщение в историю
def add_message(chat_id, user_id, role, model, text, timestamp, session_id=None):
    # Вставить запись в таблицу messages

# 3. Получить историю сообщений (опционально: только последние N, или всю)
def get_history(chat_id, user_id=None, limit=None, session_id=None):
    # Вернуть список сообщений по chat_id (и user_id для личных чатов)
    # Если limit задан — вернуть только последние N сообщений

# 4. Очистить историю (по chat_id и/или user_id)
def clear_history(chat_id, user_id=None):
    # Удалить все сообщения для данного чата (и пользователя, если задан)

# 5. Суммаризация истории
def summarize_history(chat_id, user_id=None):
    # Получить всю историю, сжать её с помощью LLM, сохранить результат как новое сообщение

# 6. Проверка длины контекста
def check_context_length(history, model):
    # Подсчитать количество токенов в истории + новом сообщении
    # Вернуть True/False, хватает ли места в контексте

# 7. Обработчик переполнения контекста
def handle_context_overflow(chat_id, user_id, model):
    # Если история не влезает:
    # 1. Предложить начать новый диалог (очистить историю)
    # 2. Предложить суммаризировать историю

# 8. Обработка текстовых команд ("новый диалог", "очисти память")
def handle_text_command(text, chat_id, user_id):
    # Если текст == "новый диалог" или "очисти память":
    #   вызвать clear_history
    #   отправить подтверждение пользователю

# 9. Автоматическая периодическая суммаризация (опционально)
def auto_summarize(chat_id, user_id=None):
    # Если история слишком длинная — автоматически суммаризировать и сохранить

# 10. Получить краткую "память" о пользователе (summary)
def get_user_summary(chat_id, user_id):
    # Вернуть последнее суммаризированное сообщение (если есть)
