#!/usr/bin/env python3
import asyncio
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.memory import get_all_admins


@dataclass(frozen=True)
class TunnelProbe:
    name: str
    port: int
    ssh_user: str
    ssh_key: Path
    ssh_host: str = "localhost"
    ssh_extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortStatus:
    ok: bool
    detail: str


@dataclass(frozen=True)
class SshStatus:
    ok: bool
    detail: str
    hostname: str | None = None
    username: str | None = None


PROBES: tuple[TunnelProbe, ...] = (
    TunnelProbe(
        name="Nitro5",
        port=2222,
        ssh_user="t",
        ssh_key=Path("/home/nitro5/.ssh/windows_t_reverse_tunnel_ed25519"),
        ssh_extra_args=(
            "-o",
            "UserKnownHostsFile=/home/nitro5/.ssh/known_hosts_win_tunnel",
        ),
    ),
    TunnelProbe(
        name="Gaming3080",
        port=2223,
        ssh_user="anato",
        ssh_key=Path("/home/Gaming3080/.ssh/gaming3080_reverse_tunnel_ed25519"),
        ssh_extra_args=(
            "-o",
            "StrictHostKeyChecking=accept-new",
        ),
    ),
)


def _resolve_report_chat_ids() -> list[int]:
    raw = os.getenv("DAILY_REPORT_CHAT_IDS", "").strip()
    if raw:
        chat_ids: list[int] = []
        for part in raw.split(","):
            item = part.strip()
            if not item:
                continue
            try:
                chat_ids.append(int(item))
            except ValueError:
                raise SystemExit(f"Invalid DAILY_REPORT_CHAT_IDS value: {item}")
        if chat_ids:
            return chat_ids


def _probe_port(port: int) -> PortStatus:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return PortStatus(ok=True, detail="open")
    except OSError as exc:
        return PortStatus(ok=False, detail=str(exc))


def _probe_ssh(probe: TunnelProbe) -> SshStatus:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-i",
        str(probe.ssh_key),
        *probe.ssh_extra_args,
        "-p",
        str(probe.port),
        f"{probe.ssh_user}@{probe.ssh_host}",
        "hostname && whoami",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return SshStatus(ok=False, detail="timeout after 15s")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return SshStatus(ok=False, detail=detail)

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    hostname = lines[0] if len(lines) >= 1 else None
    username = lines[1] if len(lines) >= 2 else None
    return SshStatus(ok=True, detail="ok", hostname=hostname, username=username)


def _format_probe_section(probe: TunnelProbe) -> str:
    port_status = _probe_port(probe.port)
    ssh_status = _probe_ssh(probe)
    icon = "✅" if ssh_status.ok else "❌"
    lines = [f"{icon} {probe.name}"]
    lines.append(f"Порт {probe.port}: {'open' if port_status.ok else 'down'} ({port_status.detail})")
    if ssh_status.ok:
        lines.append("SSH: ok")
        if ssh_status.hostname:
            lines.append(f"Host: {ssh_status.hostname}")
        if ssh_status.username:
            lines.append(f"User: {ssh_status.username}")
    else:
        lines.append(f"SSH: down ({ssh_status.detail})")
    return "\n".join(lines)


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    chat_ids = _resolve_report_chat_ids()
    if not chat_ids:
        admins = get_all_admins()
        if not admins:
            raise SystemExit("No admins configured")
        chat_ids = [int(a["chat_id"]) for a in admins if a.get("chat_id")]
    if not chat_ids:
        raise SystemExit("No report chat ids configured")

    now = datetime.now()
    header = f"🔌 Отчет по туннелям на {socket.gethostname()} ({now:%Y-%m-%d %H:%M})"
    sections = [_format_probe_section(probe) for probe in PROBES]
    text = f"{header}\n\n" + "\n\n".join(sections)

    bot = Bot(token=token)
    for chat_id in chat_ids:
        await bot.send_message(chat_id=chat_id, text=text)


if __name__ == "__main__":
    asyncio.run(main())
