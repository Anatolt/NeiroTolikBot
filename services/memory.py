import logging
import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

# Путь к базе данных
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "memory.db")

# Создание директории для базы данных, если она не существует
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_db():
    """Инициализация базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Создание таблицы сообщений
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        model TEXT NOT NULL,
        text TEXT NOT NULL,
        timestamp DATETIME NOT NULL,
        session_id TEXT,
        is_summarized BOOLEAN DEFAULT 0
    )
    ''')
    
    # Создание таблицы суммаризаций
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        summary TEXT NOT NULL,
        timestamp DATETIME NOT NULL
    )
    ''')
    
    # Создание таблицы админов
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        timestamp DATETIME NOT NULL,
        UNIQUE(chat_id, user_id)
    )
    ''')
    
    conn.commit()
    conn.close()

def add_message(chat_id: str, user_id: str, role: str, model: str, text: str, session_id: Optional[str] = None) -> None:
    """Добавление сообщения в историю."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "INSERT INTO messages (chat_id, user_id, role, model, text, timestamp, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, role, model, text, datetime.now().isoformat(), session_id)
    )
    
    conn.commit()
    conn.close()

def remove_messages_by_ids(message_ids: List[int]) -> None:
    """Удаляет сообщения с указанными идентификаторами."""
    if not message_ids:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    placeholders = ",".join("?" for _ in message_ids)
    cursor.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", message_ids)

    conn.commit()
    conn.close()

def get_history(chat_id: str, user_id: Optional[str] = None, limit: Optional[int] = None, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Получение истории сообщений."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM messages WHERE chat_id = ?"
    params = [chat_id]
    
    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    
    query += " ORDER BY timestamp DESC"
    
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    conn.close()
    
    return [dict(row) for row in rows]

def start_new_dialog(chat_id: str, user_id: str) -> str:
    """Начало нового диалога (сохраняет историю для будущей суммаризации)."""
    # Генерируем новый session_id для текущего диалога
    session_id = f"{chat_id}_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    # Добавляем системное сообщение о начале нового диалога
    add_message(
        chat_id=chat_id,
        user_id=user_id,
        role="system",
        model="system",
        text="Начало нового диалога",
        session_id=session_id
    )
    
    return session_id

def clear_memory(chat_id: str, user_id: Optional[str] = None) -> None:
    """Полное удаление истории сообщений."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if user_id:
        cursor.execute("DELETE FROM messages WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        cursor.execute("DELETE FROM summaries WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    else:
        cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        cursor.execute("DELETE FROM summaries WHERE chat_id = ?", (chat_id,))
    
    conn.commit()
    conn.close()

def save_summary(chat_id: str, user_id: str, summary: str) -> None:
    """Сохранение суммаризации истории."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "INSERT INTO summaries (chat_id, user_id, summary, timestamp) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, summary, datetime.now().isoformat())
    )
    
    conn.commit()
    conn.close()

def get_user_summary(chat_id: str, user_id: str) -> Optional[str]:
    """Получение последней суммаризации для пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT summary FROM summaries WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC LIMIT 1",
        (chat_id, user_id)
    )
    
    result = cursor.fetchone()
    conn.close()
    
    return result[0] if result else None

def add_admin(chat_id: str, user_id: str) -> None:
    """Добавление администратора в базу данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Используем INSERT OR REPLACE для обновления существующей записи
    cursor.execute(
        "INSERT OR REPLACE INTO admins (chat_id, user_id, timestamp) VALUES (?, ?, ?)",
        (chat_id, user_id, datetime.now().isoformat())
    )
    
    conn.commit()
    conn.close()

def is_admin(chat_id: str, user_id: str) -> bool:
    """Проверка, является ли пользователь администратором."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT 1 FROM admins WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id)
    )
    
    result = cursor.fetchone()
    conn.close()
    
    return result is not None

def get_all_admins() -> List[Dict[str, Any]]:
    """Получение списка всех администраторов."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT chat_id, user_id FROM admins")
    rows = cursor.fetchall()
    
    conn.close()
    
    return [dict(row) for row in rows]

def remove_admin(chat_id: str, user_id: str) -> None:
    """Удаление администратора из базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "DELETE FROM admins WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id)
    )
    
    conn.commit()
    conn.close()

# Инициализация базы данных при импорте модуля
init_db() 