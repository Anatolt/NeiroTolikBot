#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <wav_path> [width] [height] [sample_rate]"
  exit 1
fi

WAV_PATH="$1"
WIDTH="${2:-160}"
HEIGHT="${3:-30}"
SAMPLE_RATE="${4:-2000}"

sox "$WAV_PATH" -c 1 -r "$SAMPLE_RATE" -t dat - \
| gnuplot -e "set terminal dumb ${WIDTH} ${HEIGHT}; set key off; set title 'Waveform'; plot '-' using 1:2 with lines"

