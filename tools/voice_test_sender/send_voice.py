import argparse
import asyncio
import contextlib
import os
from pathlib import Path

import discord


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join Discord voice and play a single audio file")
    parser.add_argument("--token", default=os.getenv("DISCORD_TEST_BOT_TOKEN", ""))
    parser.add_argument("--guild-id", required=True, type=int)
    parser.add_argument("--channel-id", required=True, type=int)
    parser.add_argument("--file", required=True)
    parser.add_argument("--label", default="case")
    parser.add_argument("--connect-timeout", type=float, default=25.0)
    parser.add_argument("--play-timeout", type=float, default=120.0)
    parser.add_argument("--post-wait", type=float, default=1.5)
    return parser.parse_args()


class VoiceSender(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.args = args
        self._done = asyncio.Event()
        self.exit_code = 0

    async def on_ready(self) -> None:
        asyncio.create_task(self._run_case())

    async def _run_case(self) -> None:
        try:
            guild = self.get_guild(self.args.guild_id)
            if guild is None:
                guild = await self.fetch_guild(self.args.guild_id)

            channel = guild.get_channel(self.args.channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.args.channel_id)

            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError(f"Channel {self.args.channel_id} is not a voice/stage channel")

            path = Path(self.args.file)
            if not path.exists() or not path.is_file():
                raise RuntimeError(f"Audio file does not exist: {path}")

            print(f"[{self.args.label}] Connecting to voice channel {channel.id} ({channel.name})")
            voice = await asyncio.wait_for(channel.connect(), timeout=self.args.connect_timeout)

            loop = asyncio.get_running_loop()
            finished = loop.create_future()

            def _after_play(err: Exception | None) -> None:
                if err:
                    if not finished.done():
                        finished.set_exception(err)
                else:
                    if not finished.done():
                        finished.set_result(True)

            source = await self._build_source(path)
            voice.play(source, after=_after_play)
            print(f"[{self.args.label}] Playback started: {path.name}")
            await asyncio.wait_for(finished, timeout=self.args.play_timeout)
            print(f"[{self.args.label}] Playback finished")

            await asyncio.sleep(max(0.0, self.args.post_wait))
            await voice.disconnect(force=True)
        except Exception as exc:
            self.exit_code = 1
            print(f"[{self.args.label}] ERROR: {exc}")
        finally:
            self._done.set()

    async def _build_source(self, path: Path):
        """Prefer opus stream to avoid extra re-encode artifacts in bot-to-bot tests."""
        try:
            return await discord.FFmpegOpusAudio.from_probe(str(path))
        except Exception:
            return discord.FFmpegPCMAudio(str(path))


async def _main() -> int:
    args = _parse_args()
    if not args.token:
        print("ERROR: DISCORD_TEST_BOT_TOKEN is not set")
        return 2

    client = VoiceSender(args)
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
