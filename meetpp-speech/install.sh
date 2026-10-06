#!/bin/bash
# meetpp-speech installer (per-user, no sudo, no Homebrew, no ffmpeg needed).
#
#   ./install.sh              venv + pinned deps + Kokoro files + whisper weights
#   ./install.sh --launchd    ... and install/restart the LaunchAgent
#   ./install.sh --dev        ... and the test dependencies (pytest, httpx)
#   ./install.sh --fresh      recreate .venv from scratch
#   ./install.sh --uv         use uv even if a suitable python3 exists (bootstraps uv)
#   ./install.sh --no-uv-bootstrap   never download uv; fail if no Python 3.11/3.12
#
# Python: uses `uv` if present (~/.local/bin/uv or PATH), else a local
# python3.12/3.11 with `python -m venv`. If neither exists (the Mac Studio only
# has /usr/bin/python3 = 3.9, too old for MLX), it installs uv with Astral's
# official standalone installer into ~/.local/bin (no shell profile changes)
# and lets uv provide Python 3.12 (~/.local/share/uv/python).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
MODELS="$HERE/models"
PY_WANT="3.12"
LABEL="org.witysk.meetpp-speech"
ENV_DIR="$HOME/.config/meetpp-speech"
ENV_FILE="$ENV_DIR/env"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_ONNX_SHA256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
KOKORO_VOICES_SHA256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"

DO_LAUNCHD=0; DO_DEV=0; FRESH=0; UV_BOOTSTRAP=1; FORCE_UV=0
for arg in "$@"; do
  case "$arg" in
    --launchd) DO_LAUNCHD=1 ;;
    --dev) DO_DEV=1 ;;
    --fresh) FRESH=1 ;;
    --no-uv-bootstrap) UV_BOOTSTRAP=0 ;;
    --uv) FORCE_UV=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "macOS only (MLX needs Apple Silicon)"
[ "$(uname -m)" = "arm64" ] || die "Apple Silicon (arm64) required, got $(uname -m)"

# Model override from the env file, if any (same variable the service reads).
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
WHISPER_REPO="${MEETPP_SPEECH_MODEL:-mlx-community/whisper-large-v3-turbo}"

py_ok() {  # $1 = interpreter; true if it is CPython 3.11 or 3.12
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)' 2>/dev/null
}

# ---------------------------------------------------------------- 1. Python
UV_DIR="${UV_INSTALL_DIR:-$HOME/.local/bin}"
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$UV_DIR/uv" ]; then UV="$UV_DIR/uv"; fi
PY=""
if [ -z "$UV" ] && [ "$FORCE_UV" = 0 ]; then
  for c in "${PYTHON:-}" python3.12 python3.11 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3; do
    [ -n "$c" ] || continue
    p="$(command -v "$c" 2>/dev/null || true)"
    if [ -n "$p" ] && py_ok "$p"; then PY="$p"; break; fi
  done
fi
if [ -z "$UV" ] && [ -z "$PY" ]; then
  [ "$UV_BOOTSTRAP" = 1 ] || die "no Python 3.11/3.12 and no uv; install uv (https://docs.astral.sh/uv/) or rerun without --no-uv-bootstrap"
  if [ "$FORCE_UV" = 1 ]; then say "--uv: installing uv into $UV_DIR"; else
  say "No Python 3.11/3.12 found ($(/usr/bin/python3 --version 2>&1 || echo none)); installing uv into $UV_DIR"; fi
  command -v curl >/dev/null || die "curl missing"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh
  UV="$UV_DIR/uv"
  [ -x "$UV" ] || die "uv installation failed"
fi

if [ "$FRESH" = 1 ] && [ -d "$VENV" ]; then say "Removing old .venv"; rm -rf "$VENV"; fi
if [ -x "$VENV/bin/python" ] && ! py_ok "$VENV/bin/python"; then
  say "Existing .venv has an unsupported Python; recreating"; rm -rf "$VENV"
fi

if [ -n "$UV" ]; then
  say "Using uv ($("$UV" --version))"
  if [ ! -x "$VENV/bin/python" ]; then
    "$UV" python install "$PY_WANT"
    "$UV" venv --python "$PY_WANT" "$VENV"
  fi
  pip_install() { "$UV" pip install --python "$VENV/bin/python" --no-deps --only-binary :all: "$@"; }
else
  say "Using $PY ($("$PY" --version 2>&1))"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  pip_install() { "$VENV/bin/python" -m pip install --quiet --no-deps --only-binary=:all: "$@"; }
fi

# ---------------------------------------------------------------- 2. Dependencies
say "Installing pinned dependencies (requirements.txt, --no-deps: no torch)"
pip_install -r "$HERE/requirements.txt"
if [ "$DO_DEV" = 1 ]; then
  say "Installing test dependencies (requirements-dev.txt)"
  pip_install -r "$HERE/requirements-dev.txt"
fi
"$VENV/bin/python" - <<'PY'
import soundfile as sf
import mlx.core, mlx_whisper, kokoro_onnx, onnxruntime, soxr, fastapi, uvicorn  # noqa: F401
assert "OPUS" in sf.available_subtypes("OGG"), "libsndfile without Ogg/Opus"
print(f"    imports ok; libsndfile {sf.__libsndfile_version__} with Ogg/Opus; mlx {mlx.core.__version__}")
PY

# ---------------------------------------------------------------- 3. Kokoro files
fetch() {  # url dest sha256
  local url="$1" dest="$2" sha="$3" got
  if [ -f "$dest" ] && [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sha" ]; then
    echo "    $(basename "$dest"): present, checksum ok"; return
  fi
  echo "    downloading $(basename "$dest")"
  curl -fL --retry 3 --progress-bar -o "$dest.part" "$url"
  got="$(shasum -a 256 "$dest.part" | cut -d' ' -f1)"
  if [ "$got" != "$sha" ]; then
    rm -f "$dest.part"; die "checksum mismatch for $(basename "$dest"): got $got, want $sha"
  fi
  mv "$dest.part" "$dest"
  echo "    $(basename "$dest"): checksum ok"
}
say "Kokoro model files -> $MODELS"
mkdir -p "$MODELS"
fetch "$KOKORO_BASE/kokoro-v1.0.onnx" "$MODELS/kokoro-v1.0.onnx" "$KOKORO_ONNX_SHA256"
fetch "$KOKORO_BASE/voices-v1.0.bin" "$MODELS/voices-v1.0.bin" "$KOKORO_VOICES_SHA256"

# ---------------------------------------------------------------- 4. Whisper weights
say "Whisper weights ($WHISPER_REPO) -> Hugging Face cache"
HF_HUB_DISABLE_TELEMETRY=1 "$VENV/bin/python" - "$WHISPER_REPO" <<'PY'
import sys
from huggingface_hub import snapshot_download
path = snapshot_download(sys.argv[1])
print(f"    cached at {path}")
PY

# ---------------------------------------------------------------- 5. LaunchAgent
if [ "$DO_LAUNCHD" = 1 ]; then
  say "LaunchAgent $LABEL"
  mkdir -p "$ENV_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  chmod 700 "$ENV_DIR"
  if [ ! -f "$ENV_FILE" ]; then
    ( umask 077; cat > "$ENV_FILE" <<EOF
# meetpp-speech environment (sourced by run.sh). Keep mode 600.
# The secret must equal MEETPP_SPEECH_SECRET in meetpp-agent's environment on turn.witysk.org.
MEETPP_SPEECH_SECRET=$(openssl rand -hex 32)
MEETPP_SPEECH_BIND=10.88.0.2
MEETPP_SPEECH_PORT=9310
MEETPP_SPEECH_MODEL=mlx-community/whisper-large-v3-turbo
KOKORO_MODEL=$MODELS/kokoro-v1.0.onnx
KOKORO_VOICES=$MODELS/voices-v1.0.bin
EOF
    )
    echo "    created $ENV_FILE with a new random secret (copy it to the agent's MEETPP_SPEECH_SECRET)"
  else
    echo "    keeping existing $ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
  sed -e "s#__INSTALL_DIR__#$HERE#g" -e "s#__HOME__#$HOME#g" \
    "$HERE/$LABEL.plist" > "$AGENT_PLIST"
  plutil -lint "$AGENT_PLIST" >/dev/null
  for d in "gui/$(id -u)" "user/$(id -u)"; do launchctl bootout "$d/$LABEL" 2>/dev/null || true; done
  # bootout returns before the service has stopped; bootstrapping before then
  # fails with "5: Input/output error" and leaves the service unloaded.
  for _ in $(seq 1 30); do
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl print "user/$(id -u)/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST" 2>/dev/null; then
    echo "    loaded in gui/$(id -u) (starts at every login of $(id -un))"
  else
    # e.g. installing over SSH while nobody is logged in to the desktop
    launchctl bootstrap "user/$(id -u)" "$AGENT_PLIST"
    echo "    no GUI session: loaded in user/$(id -u) for now; it moves to gui/ at the next desktop login"
  fi
  echo "    logs: ~/Library/Logs/meetpp-speech.log"
  echo "    check:  curl -s http://10.88.0.2:9310/health   (ready ~10-20 s after start)"
fi

say "Done. Start manually with ./run.sh (reads $ENV_FILE)."
