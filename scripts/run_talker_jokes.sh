#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PY_BIN="python3"
if [ -x ".venv/bin/python" ]; then
  PY_BIN=".venv/bin/python"
fi

if [ -z "${DISCORD_TEST_GUILD_ID:-}" ] || [ -z "${DISCORD_TEST_CHANNEL_ID:-}" ]; then
  echo "Set DISCORD_TEST_GUILD_ID and DISCORD_TEST_CHANNEL_ID in .env"
  exit 1
fi

exec "$PY_BIN" tools/voice_test_sender/talker_joker.py \
  --guild-id "$DISCORD_TEST_GUILD_ID" \
  --channel-id "$DISCORD_TEST_CHANNEL_ID" \
  "$@"
