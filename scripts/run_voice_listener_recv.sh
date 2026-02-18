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

# By default, use main Neiro token as listener.
LISTENER_TOKEN="${DISCORD_LISTENER_TOKEN:-${DISCORD_BOT_TOKEN:-}}"

if [[ -z "${LISTENER_TOKEN}" ]]; then
  echo "ERROR: set DISCORD_LISTENER_TOKEN or DISCORD_BOT_TOKEN"
  exit 2
fi

if [[ -z "${DISCORD_TEST_GUILD_ID:-}" || -z "${DISCORD_TEST_CHANNEL_ID:-}" ]]; then
  echo "ERROR: DISCORD_TEST_GUILD_ID and DISCORD_TEST_CHANNEL_ID must be set"
  exit 2
fi

if [[ ! -x "tools/voice_test_sender/.venv_recv/bin/python" ]]; then
  echo "ERROR: missing tools/voice_test_sender/.venv_recv"
  echo "Install with:"
  echo "  cd tools/voice_test_sender && python3 -m venv .venv_recv"
  echo "  .venv_recv/bin/pip install 'discord.py[voice]>=2.5,<2.6' discord-ext-voice-recv"
  exit 2
fi

exec tools/voice_test_sender/.venv_recv/bin/python tools/voice_test_sender/listen_voice_recv.py \
  --token "${LISTENER_TOKEN}" \
  --guild-id "${DISCORD_TEST_GUILD_ID}" \
  --channel-id "${DISCORD_TEST_CHANNEL_ID}" \
  "$@"
