import asyncio
import contextlib
import os
import sqlite3
import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path
import time

import aiohttp
import discord
from discord.ext import voice_recv
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_VOICE_RECV_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_VOICE_RECV_GUILD_ID") or os.getenv("DISCORD_TEST_GUILD_ID") or "0")
CHANNEL_ID = int(os.getenv("DISCORD_VOICE_RECV_CHANNEL_ID") or os.getenv("DISCORD_TEST_CHANNEL_ID") or "0")
WHISPER_URL = os.getenv("VOICE_LOCAL_WHISPER_URL") or "http://whisper:8000/transcribe"
CHUNK_SECONDS = float(os.getenv("VOICE_RECV_CHUNK_SECONDS") or "4")
IDLE_FLUSH_SECONDS = float(os.getenv("VOICE_RECV_IDLE_FLUSH_SECONDS") or "1.4")
INCLUDE_BOTS = str(os.getenv("VOICE_TEST_ALLOW_BOT_AUDIO") or "").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SEND_TELEGRAM_CHUNKS = str(os.getenv("VOICE_RECV_TELEGRAM_CHUNKS", "1")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DB_PATH = Path("data/memory.db")
LOG_DIR = Path("data/voice_recv_chunks")


def _ensure_voice_logs_table() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT,
                text TEXT NOT NULL,
                timestamp DATETIME NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _add_voice_log(guild_id: int, channel_id: int, user_id: int, username: str, text: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO voice_logs (platform, guild_id, channel_id, user_id, username, text, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "discord",
            str(guild_id),
            str(channel_id),
            str(user_id),
            username,
            text,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _resolve_telegram_chat_ids(channel_id: int) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        chat_ids: list[str] = []
        cur.execute(
            """
            SELECT telegram_chat_id
            FROM notification_flows
            WHERE discord_channel_id = ?
            ORDER BY id
            """,
            (str(channel_id),),
        )
        for row in cur.fetchall():
            value = str((row[0] or "")).strip()
            if value:
                chat_ids.append(value)
        if chat_ids:
            return list(dict.fromkeys(chat_ids))

        cur.execute(
            """
            SELECT value
            FROM notification_settings
            WHERE key = 'voice_notification_chat_id'
            """
        )
        fallback = cur.fetchone()
        if fallback and str(fallback[0] or "").strip():
            chat_ids.append(str(fallback[0]).strip())
        return list(dict.fromkeys(chat_ids))
    except sqlite3.Error as exc:
        print(f"[voice-recv] telegram chat lookup failed: {exc}")
        return []
    finally:
        conn.close()


async def _transcribe(path: Path) -> str:
    data = aiohttp.FormData()
    data.add_field("file", path.read_bytes(), filename=path.name, content_type="audio/wav")
    async with aiohttp.ClientSession() as session:
        async with session.post(WHISPER_URL, data=data, timeout=120) as response:
            if response.status != 200:
                return ""
            payload = await response.json()
            return str(payload.get("text") or "").strip()


async def _send_telegram_chunk(path: Path, username: str, user_id: int, transcript: str) -> None:
    if not SEND_TELEGRAM_CHUNKS or not TELEGRAM_TOKEN:
        return
    chat_ids = _resolve_telegram_chat_ids(CHANNEL_ID)
    if not chat_ids:
        return

    text = transcript.strip() if transcript else ""
    if not text:
        text = "(без распознавания)"
    if len(text) > 800:
        text = text[:797] + "..."
    caption = f"🎧 {username} ({user_id})\n{text}"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    file_bytes = path.read_bytes()
    async with aiohttp.ClientSession() as session:
        for chat_id in chat_ids:
            try:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                form.add_field("caption", caption)
                form.add_field("disable_notification", "true")
                form.add_field(
                    "document",
                    file_bytes,
                    filename=path.name,
                    content_type="audio/wav",
                )
                async with session.post(url, data=form, timeout=45) as response:
                    if response.status != 200:
                        body = await response.text()
                        print(f"[voice-recv] telegram send failed chat={chat_id} status={response.status} body={body[:200]}")
            except Exception as exc:
                print(f"[voice-recv] telegram send error chat={chat_id}: {exc}")


class RollingChunker:
    def __init__(self, chunk_seconds: float, sample_rate: int = 48000, channels: int = 2) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = 2
        self.target_bytes = int(chunk_seconds * sample_rate * channels * self.sample_width)
        self._lock = threading.Lock()
        self._states_lock = threading.Lock()
        self._states: dict[int, dict] = {}
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _open(self, user_id: int, username: str) -> dict:
        file_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        wav = wave.open(file_handle, "wb")
        wav.setnchannels(self.channels)
        wav.setsampwidth(self.sample_width)
        wav.setframerate(self.sample_rate)
        state = {
            "uid": user_id,
            "name": username,
            "fh": file_handle,
            "wav": wav,
            "path": Path(file_handle.name),
            "bytes": 0,
            "last_write_ts": time.monotonic(),
        }
        self._states[user_id] = state
        return state

    def write(self, user_id: int, username: str, pcm: bytes) -> list[tuple[int, str, Path]]:
        if not pcm:
            return []
        with self._states_lock:
            state = self._states.get(user_id) or self._open(user_id, username)
            state["wav"].writeframesraw(pcm)
            state["bytes"] += len(pcm)
            state["last_write_ts"] = time.monotonic()
            if state["bytes"] < self.target_bytes:
                return []
            return [self._finalize(user_id)]

    def _finalize(self, user_id: int) -> tuple[int, str, Path]:
        state = self._states.pop(user_id)
        state["wav"].close()
        state["fh"].close()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_path = LOG_DIR / f"{ts}_{state['name']}_{state['uid']}.wav"
        with self._lock:
            os.replace(state["path"], out_path)
        return state["uid"], state["name"], out_path

    def flush(self) -> list[tuple[int, str, Path]]:
        out = []
        with self._states_lock:
            for uid in list(self._states.keys()):
                out.append(self._finalize(uid))
        return out

    def flush_stale(self, max_idle_seconds: float) -> list[tuple[int, str, Path]]:
        out: list[tuple[int, str, Path]] = []
        if max_idle_seconds <= 0:
            return out
        now = time.monotonic()
        with self._states_lock:
            for uid, state in list(self._states.items()):
                last_write = float(state.get("last_write_ts") or 0.0)
                if state.get("bytes", 0) and now - last_write >= max_idle_seconds:
                    out.append(self._finalize(uid))
        return out


class VoiceRecvWorker(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.voice: voice_recv.VoiceRecvClient | None = None
        self.done = asyncio.Event()
        self.chunker = RollingChunker(CHUNK_SECONDS)
        self.queue: asyncio.Queue[tuple[int, str, Path]] = asyncio.Queue()
        self._consumer_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._packet_count = 0

    async def on_ready(self) -> None:
        self._consumer_task = asyncio.create_task(self._consume())
        self._flush_task = asyncio.create_task(self._flush_loop())
        asyncio.create_task(self._run())

    def _on_voice(self, user, data: voice_recv.VoiceData) -> None:
        member = user or getattr(data, "source", None)
        if member is None:
            return
        if getattr(member, "bot", False) and not INCLUDE_BOTS:
            return
        uid = int(getattr(member, "id", 0) or 0)
        if uid <= 0:
            return
        self._packet_count += 1
        if self._packet_count == 1:
            pcm_len = len(getattr(data, "pcm", b"") or b"")
            opus_len = len(getattr(data, "opus", b"") or b"")
            print(f"[voice-recv] first packet user={uid} pcm={pcm_len} opus={opus_len}")
        elif self._packet_count % 50 == 0:
            print(f"[voice-recv] packets={self._packet_count} user={uid}")
        name = str(getattr(member, "display_name", None) or getattr(member, "name", None) or uid)
        ready = self.chunker.write(uid, name, getattr(data, "pcm", b"") or b"")
        for item in ready:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, item)

    async def _consume(self) -> None:
        while not self.done.is_set():
            uid, name, path = await self.queue.get()
            try:
                text = await _transcribe(path)
                if text:
                    _add_voice_log(GUILD_ID, CHANNEL_ID, uid, name, text)
                    print(f"[voice-recv] {name}({uid}): {text}")
                else:
                    print(f"[voice-recv] empty transcript: {path.name}")
                await _send_telegram_chunk(path, name, uid, text)
            except Exception as exc:
                print(f"[voice-recv] transcribe error {path.name}: {exc}")
            finally:
                with contextlib.suppress(OSError):
                    path.unlink()

    async def _flush_loop(self) -> None:
        while not self.done.is_set():
            try:
                stale_items = self.chunker.flush_stale(IDLE_FLUSH_SECONDS)
                if stale_items:
                    print(f"[voice-recv] idle flush chunks={len(stale_items)}")
                for item in stale_items:
                    await self.queue.put(item)
            except Exception as exc:
                print(f"[voice-recv] flush loop error: {exc}")
            await asyncio.sleep(0.5)

    async def _run(self) -> None:
        try:
            if not TOKEN or not GUILD_ID or not CHANNEL_ID:
                raise RuntimeError("DISCORD_VOICE_RECV_TOKEN/GUILD_ID/CHANNEL_ID are required")
            _ensure_voice_logs_table()
            tg_targets = _resolve_telegram_chat_ids(CHANNEL_ID) if SEND_TELEGRAM_CHUNKS and TELEGRAM_TOKEN else []
            print(
                f"[voice-recv] start whisper={WHISPER_URL} "
                f"chunk={CHUNK_SECONDS:.1f}s include_bots={INCLUDE_BOTS} "
                f"idle_flush={IDLE_FLUSH_SECONDS:.1f}s "
                f"tg_chunks={SEND_TELEGRAM_CHUNKS and bool(TELEGRAM_TOKEN)} tg_targets={len(tg_targets)}"
            )
            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
            channel = guild.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError(f"Channel {CHANNEL_ID} is not voice/stage")
            print(f"[voice-recv] connect guild={GUILD_ID} channel={CHANNEL_ID}")
            self.voice = await channel.connect(cls=voice_recv.VoiceRecvClient)
            with contextlib.suppress(Exception):
                await guild.change_voice_state(channel=channel, self_mute=False, self_deaf=False)
            sink = voice_recv.BasicSink(self._on_voice, decode=True)
            self.voice.listen(sink)
            while True:
                await asyncio.sleep(5)
        except Exception as exc:
            print(f"[voice-recv] ERROR: {exc}")
        finally:
            for item in self.chunker.flush():
                await self.queue.put(item)
            self.done.set()
            if self.voice and self.voice.is_connected():
                with contextlib.suppress(Exception):
                    await self.voice.disconnect(force=True)
            if self._consumer_task:
                self._consumer_task.cancel()
            if self._flush_task:
                self._flush_task.cancel()


async def _main() -> int:
    worker = VoiceRecvWorker()
    await worker.login(TOKEN or "")
    connect_task = asyncio.create_task(worker.connect(reconnect=True))
    await worker.done.wait()
    await worker.close()
    with contextlib.suppress(Exception):
        await connect_task
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
