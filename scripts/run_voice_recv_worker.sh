#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ ! -x tools/voice_test_sender/.venv_recv/bin/python ]]; then
  echo "Missing tools/voice_test_sender/.venv_recv"
  echo "Install with:"
  echo "  cd tools/voice_test_sender && python3 -m venv .venv_recv"
  echo "  .venv_recv/bin/pip install 'discord.py[voice]>=2.5,<2.6' discord-ext-voice-recv aiohttp python-dotenv"
  exit 1
fi

exec tools/voice_test_sender/.venv_recv/bin/python tools/voice_recv_worker/worker.py
