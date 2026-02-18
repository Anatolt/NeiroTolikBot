#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -z "${DISCORD_TEST_BOT_TOKEN:-}" ] || [ -z "${DISCORD_TEST_GUILD_ID:-}" ] || [ -z "${DISCORD_TEST_CHANNEL_ID:-}" ]; then
  echo "Set DISCORD_TEST_BOT_TOKEN, DISCORD_TEST_GUILD_ID, DISCORD_TEST_CHANNEL_ID in .env"
  exit 1
fi

if [ ! -x tools/voice_test_sender/.venv_disnake/bin/python ]; then
  echo "Missing tools/voice_test_sender/.venv_disnake. Install with:"
  echo "  cd tools/voice_test_sender && python3 -m venv .venv_disnake && .venv_disnake/bin/pip install 'disnake[voice]>=2.10,<2.11'"
  exit 1
fi

FILE=${1:-tests/voice/fixtures/wake_check_short.wav}

exec tools/voice_test_sender/.venv_disnake/bin/python tools/voice_test_sender/send_voice_disnake.py \
  --guild-id "$DISCORD_TEST_GUILD_ID" \
  --channel-id "$DISCORD_TEST_CHANNEL_ID" \
  --file "$FILE" \
  --label disnake_sender
