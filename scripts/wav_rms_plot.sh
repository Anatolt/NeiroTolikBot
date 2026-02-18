#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <audio_path> [width] [height]"
  exit 1
fi

INPUT_PATH="$1"
WIDTH="${2:-160}"
HEIGHT="${3:-30}"

if [[ ! -f "$INPUT_PATH" ]]; then
  echo "ERROR: file not found: $INPUT_PATH" >&2
  exit 1
fi

POINTS_FILE="$(mktemp)"
FFMPEG_ERR_FILE="$(mktemp)"
cleanup() {
  rm -f "$POINTS_FILE" "$FFMPEG_ERR_FILE"
}
trap cleanup EXIT

ffmpeg -v error -i "$INPUT_PATH" \
  -af "aformat=channel_layouts=mono,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-" \
  -f null - 2>"$FFMPEG_ERR_FILE" \
| awk '
  /pts_time:/ {
    if (match($0, /pts_time:[0-9.]+/)) {
      t = substr($0, RSTART + 9, RLENGTH - 9)
    }
  }
  /lavfi\.astats\.Overall\.RMS_level=/ {
    split($0, a, "=")
    if (t != "" && a[2] != "" && a[2] != "-inf") {
      print t, a[2]
    }
  }
' >"$POINTS_FILE"

if [[ ! -s "$POINTS_FILE" ]]; then
  echo "ERROR: no RMS points extracted from: $INPUT_PATH" >&2
  if [[ -s "$FFMPEG_ERR_FILE" ]]; then
    sed -n '1,40p' "$FFMPEG_ERR_FILE" >&2
  fi
  exit 1
fi

gnuplot -e "set terminal dumb ${WIDTH} ${HEIGHT}; set title 'RMS Envelope (dB)'; set xlabel 'time, s'; set ylabel 'RMS dB'; set key off; plot '$POINTS_FILE' using 1:2 with lines"
