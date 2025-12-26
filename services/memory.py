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

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS telegram_chats (
        chat_id TEXT PRIMARY KEY,
        title TEXT,
        chat_type TEXT,
        updated_at DATETIME NOT NULL
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS discord_voice_channels (
        channel_id TEXT PRIMARY KEY,
        channel_name TEXT,
        guild_id TEXT,
        guild_name TEXT,
        updated_at DATETIME NOT NULL
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS notification_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at DATETIME NOT NULL
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS notification_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_channel_id TEXT NOT NULL,
        telegram_chat_id TEXT NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE(discord_channel_id, telegram_chat_id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS discord_join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_user_id TEXT NOT NULL,
        discord_user_name TEXT,
        discord_guild_id TEXT NOT NULL,
        discord_guild_name TEXT,
        discord_channel_id TEXT NOT NULL,
        discord_channel_name TEXT,
        status TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        processed_at DATETIME
    )
    ''')

    # Таблица пользовательских настроек (например, выбор режима роутинга)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        routing_mode TEXT,
        show_response_header BOOLEAN DEFAULT 1,
        preferred_model TEXT,
        voice_auto_reply BOOLEAN DEFAULT 0,
        updated_at DATETIME NOT NULL,
        UNIQUE(chat_id, user_id)
    )
    ''')

    # Добавляем недостающие колонки для уже созданных таблиц
    cursor.execute("PRAGMA table_info(user_settings)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "show_response_header" not in existing_columns:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN show_response_header BOOLEAN DEFAULT 1")
    if "preferred_model" not in existing_columns:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN preferred_model TEXT")
    if "voice_auto_reply" not in existing_columns:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN voice_auto_reply BOOLEAN DEFAULT 0")

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

def upsert_telegram_chat(chat_id: str, title: Optional[str], chat_type: Optional[str]) -> None:
    """Сохраняет или обновляет информацию о чате Telegram."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO telegram_chats (chat_id, title, chat_type, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id)
        DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type, updated_at=excluded.updated_at
        """,
        (chat_id, title, chat_type, datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_telegram_chats() -> List[Dict[str, Any]]:
    """Возвращает список всех чатов Telegram, где видели бота."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT chat_id, title, chat_type FROM telegram_chats ORDER BY updated_at DESC")
    rows = cursor.fetchall()

    conn.close()
    return [dict(row) for row in rows]


def upsert_discord_voice_channel(
    channel_id: str,
    channel_name: Optional[str],
    guild_id: Optional[str],
    guild_name: Optional[str],
) -> None:
    """Сохраняет или обновляет информацию о голосовом канале Discord."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO discord_voice_channels (channel_id, channel_name, guild_id, guild_name, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(channel_id)
        DO UPDATE SET
            channel_name=excluded.channel_name,
            guild_id=excluded.guild_id,
            guild_name=excluded.guild_name,
            updated_at=excluded.updated_at
        """,
        (channel_id, channel_name, guild_id, guild_name, datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_discord_voice_channels() -> List[Dict[str, Any]]:
    """Возвращает список известных голосовых каналов Discord."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT channel_id, channel_name, guild_id, guild_name
        FROM discord_voice_channels
        ORDER BY guild_name, channel_name
        """
    )
    rows = cursor.fetchall()

    conn.close()
    return [dict(row) for row in rows]


def set_voice_notification_chat_id(chat_id: str) -> None:
    """Сохраняет чат Telegram, куда отправлять уведомления о Discord."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO notification_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        ("voice_notification_chat_id", chat_id, datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_voice_notification_chat_id() -> Optional[str]:
    """Возвращает чат Telegram для уведомлений о Discord."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT value FROM notification_settings WHERE key = ?",
        ("voice_notification_chat_id",),
    )
    result = cursor.fetchone()
    conn.close()

    return result[0] if result else None


def add_notification_flow(discord_channel_id: str, telegram_chat_id: str) -> None:
    """Добавляет связку Discord-канала и Telegram-чата для уведомлений."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO notification_flows (discord_channel_id, telegram_chat_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(discord_channel_id, telegram_chat_id)
        DO UPDATE SET updated_at=excluded.updated_at
        """,
        (discord_channel_id, telegram_chat_id, datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_notification_flows() -> List[Dict[str, Any]]:
    """Возвращает все настроенные уведомления Discord -> Telegram."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, discord_channel_id, telegram_chat_id, updated_at
        FROM notification_flows
        ORDER BY id
        """
    )
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_notification_flows_for_channel(discord_channel_id: str) -> List[Dict[str, Any]]:
    """Возвращает уведомления для указанного Discord-канала."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, discord_channel_id, telegram_chat_id, updated_at
        FROM notification_flows
        WHERE discord_channel_id = ?
        ORDER BY id
        """,
        (discord_channel_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def remove_notification_flow(flow_id: int) -> None:
    """Удаляет связку уведомлений по идентификатору."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM notification_flows WHERE id = ?", (flow_id,))

    conn.commit()
    conn.close()


def create_discord_join_request(
    discord_user_id: str,
    discord_user_name: Optional[str],
    discord_guild_id: str,
    discord_guild_name: Optional[str],
    discord_channel_id: str,
    discord_channel_name: Optional[str],
) -> int:
    """Создает запрос на подключение к Discord-каналу."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO discord_join_requests (
            discord_user_id,
            discord_user_name,
            discord_guild_id,
            discord_guild_name,
            discord_channel_id,
            discord_channel_name,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            discord_user_id,
            discord_user_name,
            discord_guild_id,
            discord_guild_name,
            discord_channel_id,
            discord_channel_name,
            "pending",
            datetime.now().isoformat(),
        ),
    )

    request_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return int(request_id)


def get_latest_pending_discord_join_request() -> Optional[Dict[str, Any]]:
    """Возвращает последний ожидающий запрос."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM discord_join_requests
        WHERE status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    conn.close()

    return dict(row) if row else None


def get_pending_discord_join_requests() -> List[Dict[str, Any]]:
    """Возвращает все ожидающие запросы."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM discord_join_requests
        WHERE status = 'pending'
        ORDER BY created_at DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def set_discord_join_request_status(request_id: int, status: str) -> None:
    """Обновляет статус запроса."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE discord_join_requests
        SET status = ?, processed_at = NULL
        WHERE id = ?
        """,
        (status, request_id),
    )

    conn.commit()
    conn.close()


def get_unprocessed_discord_join_requests() -> List[Dict[str, Any]]:
    """Возвращает решения, которые еще не обработаны Discord-ботом."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM discord_join_requests
        WHERE status IN ('approved', 'denied') AND processed_at IS NULL
        ORDER BY created_at
        """
    )
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def mark_discord_join_request_processed(request_id: int) -> None:
    """Отмечает, что решение обработано Discord-ботом."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE discord_join_requests
        SET processed_at = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(), request_id),
    )

    conn.commit()
    conn.close()


def set_discord_autojoin(guild_id: str, enabled: bool) -> None:
    """Сохраняет настройку автоподключения для Discord-гильдии."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO notification_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (f"discord_autojoin_{guild_id}", "1" if enabled else "0", datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_discord_autojoin(guild_id: str) -> bool:
    """Возвращает настройку автоподключения для Discord-гильдии."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT value FROM notification_settings WHERE key = ?",
        (f"discord_autojoin_{guild_id}",),
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        return True

    value = result[0]
    return str(value).strip() not in {"0", "false", "False", "no", "off"}


def set_discord_autojoin_announce_sent(guild_id: str, sent: bool) -> None:
    """Сохраняет, отправлялось ли уведомление автоподключения."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO notification_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (f"discord_autojoin_announce_{guild_id}", "1" if sent else "0", datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_discord_autojoin_announce_sent(guild_id: str) -> bool:
    """Возвращает флаг, было ли отправлено уведомление автоподключения."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT value FROM notification_settings WHERE key = ?",
        (f"discord_autojoin_announce_{guild_id}",),
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        return False

    value = result[0]
    return str(value).strip() not in {"0", "false", "False", "no", "off"}

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

def set_routing_mode(chat_id: str, user_id: str, routing_mode: str | None) -> None:
    """Сохраняет выбранный пользователем режим роутинга."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO user_settings (chat_id, user_id, routing_mode, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET routing_mode=excluded.routing_mode, updated_at=excluded.updated_at
        """,
        (chat_id, user_id, routing_mode, datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_routing_mode(chat_id: str, user_id: str) -> Optional[str]:
    """Возвращает сохранённый режим роутинга пользователя, если он есть."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT routing_mode FROM user_settings WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    result = cursor.fetchone()
    conn.close()

    return result[0] if result and result[0] else None


def set_preferred_model(chat_id: str, user_id: str, preferred_model: Optional[str]) -> None:
    """Сохраняет выбранную пользователем модель по умолчанию."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO user_settings (chat_id, user_id, preferred_model, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET preferred_model=excluded.preferred_model, updated_at=excluded.updated_at
        """,
        (chat_id, user_id, preferred_model, datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_preferred_model(chat_id: str, user_id: str) -> Optional[str]:
    """Возвращает сохранённую модель пользователя, если она есть."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT preferred_model FROM user_settings WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    result = cursor.fetchone()
    conn.close()

    return result[0] if result and result[0] else None


def set_show_response_header(chat_id: str, user_id: str, show_header: bool) -> None:
    """Сохраняет выбор отображения техшапки для пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO user_settings (chat_id, user_id, show_response_header, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET show_response_header=excluded.show_response_header, updated_at=excluded.updated_at
        """,
        (chat_id, user_id, int(show_header), datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_show_response_header(chat_id: str, user_id: str) -> bool:
    """Возвращает флаг отображения техшапки (по умолчанию включён)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT show_response_header FROM user_settings WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    result = cursor.fetchone()
    conn.close()

    if result is None:
        return True

    value = result[0]
    return bool(value) if value is not None else True


def set_voice_auto_reply(chat_id: str, user_id: str, enabled: bool) -> None:
    """Сохраняет выбор автоответа на голосовые сообщения."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO user_settings (chat_id, user_id, voice_auto_reply, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET voice_auto_reply=excluded.voice_auto_reply, updated_at=excluded.updated_at
        """,
        (chat_id, user_id, int(enabled), datetime.now().isoformat()),
    )

    conn.commit()
    conn.close()


def get_voice_auto_reply(chat_id: str, user_id: str) -> bool:
    """Возвращает флаг автоответа на голосовые сообщения (по умолчанию выключен)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT voice_auto_reply FROM user_settings WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    result = cursor.fetchone()
    conn.close()

    if result is None:
        return False

    value = result[0]
    return bool(value) if value is not None else False

# Инициализация базы данных при импорте модуля
init_db()
