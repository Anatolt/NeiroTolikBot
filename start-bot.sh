#!/usr/bin/env bash
set -euo pipefail

trap "kill 0" SIGINT SIGTERM

python tbot.py &
python discord_bot.py &

wait -n
