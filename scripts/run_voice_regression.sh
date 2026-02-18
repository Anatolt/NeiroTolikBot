#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
python3 tests/voice/run_voice_regression.py --generate-fixtures "$@"
