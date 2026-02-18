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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join Discord voice channel and capture incoming audio chunks"
    )
    parser.add_argument("--token", default=os.getenv("DISCORD_TEST_BOT_TOKEN", ""))
    parser.add_argument("--guild-id", required=True, type=int)
    parser.add_argument("--channel-id", required=True, type=int)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--chunk-seconds", type=float, default=5.0)
    parser.add_argument("--out-dir", default="data/voice_listener")
    parser.add_argument("--label", default="listener")
    parser.add_argument("--connect-timeout", type=float, default=25.0)
    return parser.parse_args()


def _safe_name(value: str) -> str:
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_"):
            allowed.append(ch)
        else:
            allowed.append("_")
    return "".join(allowed)[:48] or "unknown"


class RollingCaptureSink(discord.sinks.Sink):
    def __init__(self, chunk_seconds: float, out_dir: Path, guild: discord.Guild):
        super().__init__()
        self.chunk_seconds = float(chunk_seconds)
        self.out_dir = out_dir
        self.guild = guild
        self._states: dict[object, dict[str, object]] = {}
        self._lock = threading.Lock()
        self._bps: int | None = None
        self._target_bytes: int | None = None

    def init(self, vc):
        self.vc = vc
        super().init(vc)
        self._bps = int(vc.decoder.SAMPLING_RATE * vc.decoder.SAMPLE_SIZE)
        self._target_bytes = int(self.chunk_seconds * self._bps)

    def _open_state(self, user) -> dict[str, object]:
        file_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        wav = wave.open(file_handle, "wb")
        wav.setnchannels(self.vc.decoder.CHANNELS)
        wav.setsampwidth(self.vc.decoder.SAMPLE_SIZE // self.vc.decoder.CHANNELS)
        wav.setframerate(self.vc.decoder.SAMPLING_RATE)
        state = {
            "user": user,
            "file": file_handle,
            "wav": wav,
            "path": file_handle.name,
            "bytes": 0,
            "peak": 0,
        }
        self._states[user] = state
        return state

    def _user_display(self, user_key) -> tuple[str, str]:
        if isinstance(user_key, int):
            member = self.guild.get_member(user_key)
            if member:
                return str(member.id), _safe_name(member.display_name)
            return str(user_key), f"user_{user_key}"
        raw = str(user_key)
        return raw, _safe_name(raw)

    def _finalize_state(self, user) -> None:
        state = self._states.pop(user, None)
        if not state or not self._bps:
            return
        wav = state.get("wav")
        file_handle = state.get("file")
        if wav:
            wav.close()
        if file_handle:
            file_handle.close()
        total_bytes = int(state.get("bytes") or 0)
        if total_bytes <= 0:
            with contextlib.suppress(OSError):
                os.unlink(str(state.get("path")))
            return
        user_id, user_name = self._user_display(state.get("user"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_name = f"{ts}_{user_name}_{user_id}.wav"
        out_path = self.out_dir / out_name
        with self._lock:
            os.replace(str(state.get("path")), out_path)
        duration = total_bytes / self._bps
        peak = int(state.get("peak") or 0)
        print(
            f"[listener] chunk user={user_name}({user_id}) "
            f"duration={duration:.2f}s peak={peak} file={out_path}"
        )

    @discord.sinks.Filters.container
    def write(self, data, user):
        if not self._target_bytes:
            return
        state = self._states.get(user) or self._open_state(user)
        state["wav"].writeframesraw(data)
        state["bytes"] = int(state.get("bytes") or 0) + len(data)
        with contextlib.suppress(audioop.error):
            pcm_peak = audioop.max(data, 2)
            if pcm_peak > int(state.get("peak") or 0):
                state["peak"] = pcm_peak
        if int(state.get("bytes") or 0) >= self._target_bytes:
            self._finalize_state(user)

    def flush(self):
        for user in list(self._states.keys()):
            self._finalize_state(user)

    def cleanup(self):
        self.finished = True
        self.flush()


class VoiceListener(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.args = args
        self.done = asyncio.Event()
        self.exit_code = 0
        self.voice: discord.VoiceClient | None = None

    async def on_ready(self) -> None:
        asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            guild = self.get_guild(self.args.guild_id) or await self.fetch_guild(self.args.guild_id)
            channel = guild.get_channel(self.args.channel_id) or await self.fetch_channel(self.args.channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError(f"Channel {self.args.channel_id} is not voice/stage")

            out_dir = Path(self.args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            print(
                f"[{self.args.label}] connect guild={guild.id} channel={channel.id} "
                f"capture={self.args.duration:.1f}s chunk={self.args.chunk_seconds:.1f}s"
            )
            self.voice = await asyncio.wait_for(
                channel.connect(), timeout=self.args.connect_timeout
            )
            with contextlib.suppress(Exception):
                await guild.change_voice_state(
                    channel=channel,
                    self_mute=False,
                    self_deaf=False,
                )
            sink = RollingCaptureSink(self.args.chunk_seconds, out_dir=out_dir, guild=guild)
            done_event = asyncio.Event()
            self.voice.start_recording(sink, self._on_recording_done, self.voice, done_event)
            await asyncio.sleep(max(0.0, self.args.duration))
            if self.voice.recording:
                await asyncio.to_thread(self.voice.stop_recording)
            sink.flush()
            print(f"[{self.args.label}] capture finished")
        except Exception as exc:
            self.exit_code = 1
            print(f"[{self.args.label}] ERROR: {type(exc).__name__} {exc}")
        finally:
            if self.voice and self.voice.is_connected():
                with contextlib.suppress(Exception):
                    await self.voice.disconnect(force=True)
            self.done.set()

    async def _on_recording_done(self, sink, voice, done_event):  # noqa: ANN001
        done_event.set()


async def _main() -> int:
    args = _parse_args()
    if not args.token:
        print("ERROR: DISCORD_TEST_BOT_TOKEN is not set")
        return 2
    client = VoiceListener(args)
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
