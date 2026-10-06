#!/bin/bash
# Start meetpp-speech in the foreground (used by the LaunchAgent, or by hand).
# Reads ~/.config/meetpp-speech/env (override with MEETPP_SPEECH_ENV_FILE).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${MEETPP_SPEECH_ENV_FILE:-$HOME/.config/meetpp-speech/env}"

if [ -f "$ENV_FILE" ]; then
  perms="$(stat -f '%Lp' "$ENV_FILE")"
  case "$perms" in
    600|400) ;;
    *) echo "WARNING: $ENV_FILE has mode $perms; run: chmod 600 $ENV_FILE" >&2 ;;
  esac
  set -a; . "$ENV_FILE"; set +a
fi

[ -x "$HERE/.venv/bin/python" ] || { echo "ERROR: $HERE/.venv missing; run ./install.sh" >&2; exit 1; }

# Weights are pre-downloaded by install.sh: start without contacting the Hub.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export HF_HUB_DISABLE_TELEMETRY=1
export HF_HUB_DISABLE_PROGRESS_BARS=1
export TOKENIZERS_PARALLELISM=false
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

cd "$HERE"
exec "$HERE/.venv/bin/python" -m meetpp_speech "$@"
