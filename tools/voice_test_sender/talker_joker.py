import argparse
import asyncio
import contextlib
import json
import os
import random
import tempfile
from pathlib import Path
import urllib.request

import discord

JUST_ANOTHER_GUILD_ID = 810599126114762762
JUST_ANOTHER_CHANNEL_ID = 810599126114762767


TOPICS = [
    "код", "сервер", "бот", "релиз", "бэкап", "чат", "дедлайн", "лог", "API", "Wi-Fi",
    "pull request", "тест", "прод", "мониторинг", "скрипт", "конфиг",
]

SETUPS = [
    "Почему {topic} всегда приходит ночью?",
    "Как-то раз {topic} зашёл в созвон.",
    "Спросили у меня про {topic}.",
    "В нашей команде {topic} — это отдельный персонаж.",
    "Сегодня {topic} снова удивил всех.",
]

PUNCHES = [
    "Потому что днём он делает вид, что всё стабильно.",
    "И сразу попросил: только без срочных фиксов.",
    "Я ответил: зависит от того, что говорит мониторинг.",
    "Он сказал: это не ошибка, это дорожная карта.",
    "Сначала упал, потом сказал, что это плановое окно.",
    "Он работает идеально, пока на него не смотрят.",
    "После этого мы добавили ещё один алерт и чай.",
]


class JokeStore:
    def __init__(
        self,
        seed_file: Path,
        db_file: Path,
        state_file: Path,
        replenish_threshold: int,
        replenish_batch: int,
    ) -> None:
        self.seed_file = seed_file
        self.db_file = db_file
        self.state_file = state_file
        self.replenish_threshold = replenish_threshold
        self.replenish_batch = replenish_batch
        self.jokes: list[str] = []
        self.index = 0
        self._rng = random.Random()

    def load(self) -> None:
        seed = self._read_json_list(self.seed_file)
        db = self._read_json_list(self.db_file)
        self.jokes = db if db else list(seed)
        if not self.jokes:
            raise RuntimeError("Joke database is empty")

        state = {"index": 0}
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                state = {"index": 0}
        self.index = max(0, int(state.get("index", 0)))
        self._save_db()
        self._save_state()

    def next(self) -> tuple[str, int]:
        if self.index >= len(self.jokes):
            self._extend(self.replenish_batch)

        remaining = len(self.jokes) - self.index
        if remaining <= self.replenish_threshold:
            self._extend(self.replenish_batch)

        joke = self.jokes[self.index]
        idx = self.index
        self.index += 1
        self._save_state()
        return joke, idx

    def _extend(self, count: int) -> None:
        generated = self._generate_jokes(count)
        if generated:
            self.jokes.extend(generated)
            self._save_db()

    def _generate_jokes(self, count: int) -> list[str]:
        existing = set(self.jokes)
        generated: list[str] = []
        attempts = 0
        while len(generated) < count and attempts < count * 30:
            attempts += 1
            topic = self._rng.choice(TOPICS)
            setup = self._rng.choice(SETUPS).format(topic=topic)
            punch = self._rng.choice(PUNCHES)
            text = f"{setup} {punch}"
            if text in existing:
                continue
            existing.add(text)
            generated.append(text)
        return generated

    def _read_json_list(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [str(x).strip() for x in data if str(x).strip()]

    def _save_db(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.db_file.write_text(
            json.dumps(self.jokes, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({"index": self.index}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _request_tts(piper_url: str, text: str) -> bytes:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        piper_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        return response.read()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talker mode: endless joke speaker for Discord voice")
    parser.add_argument("--token", default=os.getenv("DISCORD_TEST_BOT_TOKEN", ""))
    parser.add_argument("--guild-id", type=int, default=JUST_ANOTHER_GUILD_ID)
    parser.add_argument("--channel-id", type=int, default=JUST_ANOTHER_CHANNEL_ID)
    parser.add_argument("--piper-url", default=os.getenv("TALKER_PIPER_URL", "http://127.0.0.1:8001/tts"))
    parser.add_argument("--seed-file", default="tools/voice_test_sender/data/talker_jokes_seed.json")
    parser.add_argument("--db-file", default="data/talker_jokes_db.json")
    parser.add_argument("--state-file", default="data/talker_jokes_state.json")
    parser.add_argument("--replenish-threshold", type=int, default=5)
    parser.add_argument("--replenish-batch", type=int, default=20)
    parser.add_argument("--pause-seconds", type=float, default=2.8)
    parser.add_argument("--max-jokes", type=int, default=0, help="0 = endless")
    return parser.parse_args()


class TalkerJoker(discord.Client):
    def __init__(self, args: argparse.Namespace, store: JokeStore) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.args = args
        self.store = store
        self._done = asyncio.Event()
        self.exit_code = 0

    async def on_ready(self) -> None:
        asyncio.create_task(self._run())

    async def _run(self) -> None:
        voice = None
        try:
            # For debugging consistency keep Talker pinned to Just another server.
            guild_id = JUST_ANOTHER_GUILD_ID
            channel_id = JUST_ANOTHER_CHANNEL_ID
            guild = self.get_guild(guild_id) or await self.fetch_guild(guild_id)
            channel = guild.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError(f"Channel {channel_id} is not a voice channel")

            listeners = [
                member
                for member in (getattr(channel, "members", None) or [])
                if not self.user or member.id != self.user.id
            ]
            if not listeners:
                print(
                    f"[talker] skip connect: no listeners in channel {channel.id} "
                    f"({channel.name})"
                )
                return

            print(f"[talker] connect -> {channel.id} ({channel.name}) in fixed guild {guild_id}")
            voice = await channel.connect()

            spoken = 0
            while True:
                if self.args.max_jokes > 0 and spoken >= self.args.max_jokes:
                    break

                joke, idx = self.store.next()
                text = joke
                print(f"[talker] #{idx+1} text={joke[:90]}")

                with tempfile.TemporaryDirectory(prefix="talker_") as tmp_dir:
                    raw_path = Path(tmp_dir) / "raw.wav"
                    raw_path.write_bytes(_request_tts(self.args.piper_url, text))
                    source = await self._build_source(raw_path)
                    await self._play_source(voice, source)

                spoken += 1
                await asyncio.sleep(max(0.0, self.args.pause_seconds))

        except Exception as exc:
            self.exit_code = 1
            print(f"[talker] ERROR: {exc}")
        finally:
            if voice and voice.is_connected():
                with contextlib.suppress(Exception):
                    await voice.disconnect(force=True)
            self._done.set()

    async def _build_source(self, path: Path):
        # Use PCM so py-cord handles opus encoding itself.
        return discord.FFmpegPCMAudio(str(path))

    async def _play_source(self, voice: discord.VoiceClient, source) -> None:
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def _after_play(err: Exception | None) -> None:
            if err:
                if not finished.done():
                    finished.set_exception(err)
            else:
                if not finished.done():
                    finished.set_result(True)

        voice.play(source, after=_after_play)
        await finished


async def _main() -> int:
    args = _parse_args()
    if not args.token:
        print("[talker] ERROR: DISCORD_TEST_BOT_TOKEN is not set")
        return 2

    store = JokeStore(
        seed_file=Path(args.seed_file),
        db_file=Path(args.db_file),
        state_file=Path(args.state_file),
        replenish_threshold=max(1, args.replenish_threshold),
        replenish_batch=max(1, args.replenish_batch),
    )
    store.load()

    client = TalkerJoker(args, store)
    await client.login(args.token)
    connect_task = asyncio.create_task(client.connect(reconnect=False))
    await client._done.wait()
    await client.close()
    with contextlib.suppress(Exception):
        await connect_task
    return client.exit_code


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
