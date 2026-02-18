import argparse
import asyncio
import contextlib
import os
from pathlib import Path

import discord


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join Discord voice and play audio (discord.py stack)")
    parser.add_argument("--token", default=os.getenv("DISCORD_TEST_BOT_TOKEN", ""))
    parser.add_argument("--guild-id", required=True, type=int)
    parser.add_argument("--channel-id", required=True, type=int)
    parser.add_argument("--file", required=True)
    parser.add_argument("--label", default="case-dpy")
    parser.add_argument("--connect-timeout", type=float, default=25.0)
    parser.add_argument("--play-timeout", type=float, default=120.0)
    parser.add_argument("--post-wait", type=float, default=1.0)
    return parser.parse_args()


class VoiceSender(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.args = args
        self.exit_code = 0
        self.done = asyncio.Event()

    async def on_ready(self) -> None:
        asyncio.create_task(self._run_case())

    async def _run_case(self) -> None:
        voice = None
        try:
            guild = self.get_guild(self.args.guild_id) or await self.fetch_guild(self.args.guild_id)
            channel = guild.get_channel(self.args.channel_id) or await self.fetch_channel(self.args.channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError(f"Channel {self.args.channel_id} is not voice/stage")

            path = Path(self.args.file)
            if not path.exists():
                raise RuntimeError(f"Audio file not found: {path}")

            print(f"[{self.args.label}] (discord.py) connect -> {channel.id} ({channel.name})")
            voice = await asyncio.wait_for(channel.connect(), timeout=self.args.connect_timeout)

            loop = asyncio.get_running_loop()
            finished = loop.create_future()

            def _after(err: Exception | None) -> None:
                if err:
                    if not finished.done():
                        finished.set_exception(err)
                else:
                    if not finished.done():
                        finished.set_result(True)

            source = discord.FFmpegPCMAudio(str(path))
            voice.play(source, after=_after)
            print(f"[{self.args.label}] (discord.py) playback started: {path.name}")
            await asyncio.wait_for(finished, timeout=self.args.play_timeout)
            print(f"[{self.args.label}] (discord.py) playback finished")
            await asyncio.sleep(max(0.0, self.args.post_wait))
        except Exception as exc:
            self.exit_code = 1
            print(
                f"[{self.args.label}] (discord.py) ERROR: "
                f"{type(exc).__name__} {repr(exc)}"
            )
        finally:
            if voice and voice.is_connected():
                try:
                    await voice.disconnect(force=True)
                except Exception:
                    pass
            self.done.set()


async def _main() -> int:
    args = _parse_args()
    if not args.token:
        print("ERROR: DISCORD_TEST_BOT_TOKEN is not set")
        return 2
    client = VoiceSender(args)
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
