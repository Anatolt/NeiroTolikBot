import argparse
import asyncio
import contextlib
import os
import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path

import audioop
import discord
from discord.ext import voice_recv


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join Discord voice channel and capture incoming audio via discord-ext-voice-recv"
    )
    parser.add_argument("--token", default=os.getenv("DISCORD_TEST_BOT_TOKEN", ""))
    parser.add_argument("--guild-id", required=True, type=int)
    parser.add_argument("--channel-id", required=True, type=int)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--chunk-seconds", type=float, default=4.0)
    parser.add_argument("--out-dir", default="data/voice_listener_recv")
    parser.add_argument("--label", default="listener-recv")
    parser.add_argument("--connect-timeout", type=float, default=25.0)
    return parser.parse_args()


def _safe_name(value: str) -> str:
    out = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:64] or "unknown"


class RollingPcmChunker:
    def __init__(self, chunk_seconds: float, out_dir: Path, sample_rate: int = 48000, channels: int = 2) -> None:
        self.chunk_seconds = float(chunk_seconds)
        self.out_dir = out_dir
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_width = 2
        self.bytes_per_second = self.sample_rate * self.channels * self.sample_width
        self.target_bytes = int(self.bytes_per_second * self.chunk_seconds)
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}

    def _open_state(self, user_id: str, user_name: str) -> dict:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        w = wave.open(f, "wb")
        w.setnchannels(self.channels)
        w.setsampwidth(self.sample_width)
        w.setframerate(self.sample_rate)
        state = {
            "id": user_id,
            "name": user_name,
            "file": f,
            "wave": w,
            "path": f.name,
            "bytes": 0,
            "peak": 0,
            "packets": 0,
        }
        self._states[user_id] = state
        return state

    def _finalize_state(self, user_id: str) -> None:
        state = self._states.pop(user_id, None)
        if not state:
            return
        wav = state["wave"]
        fh = state["file"]
        wav.close()
        fh.close()
        total = int(state["bytes"])
        if total <= 0:
            with contextlib.suppress(OSError):
                os.unlink(state["path"])
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_name = f"{ts}_{_safe_name(state['name'])}_{state['id']}.wav"
        out_path = self.out_dir / out_name
        with self._lock:
            os.replace(state["path"], out_path)
        print(
            f"[listener-recv] chunk user={state['name']}({state['id']}) "
            f"duration={total / self.bytes_per_second:.2f}s packets={state['packets']} "
            f"peak={state['peak']} file={out_path}"
        )

    def write(self, user_id: str, user_name: str, pcm: bytes) -> None:
        if not pcm:
            return
        state = self._states.get(user_id) or self._open_state(user_id, user_name)
        state["wave"].writeframesraw(pcm)
        state["bytes"] += len(pcm)
        state["packets"] += 1
        with contextlib.suppress(audioop.error):
            p = audioop.max(pcm, self.sample_width)
            if p > state["peak"]:
                state["peak"] = p
        if state["bytes"] >= self.target_bytes:
            self._finalize_state(user_id)

    def flush(self) -> None:
        for uid in list(self._states.keys()):
            self._finalize_state(uid)


class VoiceRecvListener(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.args = args
        self.done = asyncio.Event()
        self.exit_code = 0
        self.voice: voice_recv.VoiceRecvClient | None = None
        self.chunker: RollingPcmChunker | None = None
        self._seen_packets = 0

    async def on_ready(self) -> None:
        asyncio.create_task(self._run())

    def _resolve_user(self, user, voice_data: voice_recv.VoiceData) -> tuple[str, str]:
        source = user or getattr(voice_data, "source", None)
        uid = getattr(source, "id", None)
        name = getattr(source, "display_name", None) or getattr(source, "name", None)
        if uid is None:
            uid = "unknown"
        if not name:
            name = f"user_{uid}"
        return str(uid), str(name)

    def _on_voice(self, user, voice_data: voice_recv.VoiceData) -> None:
        if not self.chunker:
            return
        pcm = getattr(voice_data, "pcm", None) or b""
        if not pcm:
            return
        uid, name = self._resolve_user(user, voice_data)
        self._seen_packets += 1
        if self._seen_packets == 1:
            opus_size = len(getattr(voice_data, "opus", b"") or b"")
            print(
                f"[listener-recv] first packet user={name}({uid}) "
                f"pcm={len(pcm)} opus={opus_size} source={type(getattr(voice_data, 'source', None)).__name__}"
            )
        self.chunker.write(uid, name, pcm)

    async def _run(self) -> None:
        try:
            guild = self.get_guild(self.args.guild_id) or await self.fetch_guild(self.args.guild_id)
            channel = guild.get_channel(self.args.channel_id) or await self.fetch_channel(self.args.channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError(f"Channel {self.args.channel_id} is not voice/stage")

            out_dir = Path(self.args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            self.chunker = RollingPcmChunker(self.args.chunk_seconds, out_dir=out_dir)
            print(
                f"[{self.args.label}] connect guild={guild.id} channel={channel.id} "
                f"capture={self.args.duration:.1f}s chunk={self.args.chunk_seconds:.1f}s"
            )

            self.voice = await asyncio.wait_for(
                channel.connect(cls=voice_recv.VoiceRecvClient),
                timeout=self.args.connect_timeout,
            )
            with contextlib.suppress(Exception):
                await guild.change_voice_state(channel=channel, self_mute=False, self_deaf=False)

            sink = voice_recv.BasicSink(self._on_voice, decode=True)
            self.voice.listen(sink)
            await asyncio.sleep(max(0.0, self.args.duration))
            self.voice.stop_listening()
            self.chunker.flush()
            print(f"[{self.args.label}] capture finished packets={self._seen_packets}")
        except Exception as exc:
            self.exit_code = 1
            print(f"[{self.args.label}] ERROR: {type(exc).__name__} {exc}")
        finally:
            if self.voice and self.voice.is_connected():
                with contextlib.suppress(Exception):
                    await self.voice.disconnect(force=True)
            self.done.set()


async def _main() -> int:
    args = _parse_args()
    if not args.token:
        print("ERROR: DISCORD_TEST_BOT_TOKEN is not set")
        return 2
    client = VoiceRecvListener(args)
    await client.login(args.token)
    connect_task = asyncio.create_task(client.connect(reconnect=False))
    await client.done.wait()
    await client.close()
    with contextlib.suppress(Exception):
        await connect_task
    return client.exit_code


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
