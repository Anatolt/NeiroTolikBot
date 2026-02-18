#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -f /root/discord-voice-test.env ]]; then
  # shellcheck disable=SC1091
  source /root/discord-voice-test.env
fi

if [[ -z "${DISCORD_TEST_BOT_TOKEN:-}" ]]; then
  echo "ERROR: DISCORD_TEST_BOT_TOKEN is not set (env or /root/discord-voice-test.env)"
  exit 2
fi

if [[ -z "${DISCORD_TEST_GUILD_ID:-}" || -z "${DISCORD_TEST_CHANNEL_ID:-}" ]]; then
  echo "ERROR: DISCORD_TEST_GUILD_ID and DISCORD_TEST_CHANNEL_ID must be set"
  exit 2
fi

PY_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PY_BIN=".venv/bin/python"
fi

"$PY_BIN" tools/voice_test_sender/listen_voice.py \
  --token "$DISCORD_TEST_BOT_TOKEN" \
  --guild-id "$DISCORD_TEST_GUILD_ID" \
  --channel-id "$DISCORD_TEST_CHANNEL_ID" \
  "$@"
