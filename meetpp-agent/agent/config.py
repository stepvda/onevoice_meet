"""Environment configuration (contract §8).

Classes take their parameters explicitly (tests pass their own); these are
only the process-wide defaults read once at import.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


SAMPLE_RATE = 16000
LANGUAGE = "en"  # English only in Release 1.1

MEETING_API_URL = os.environ.get("MEETING_API_URL", "http://meeting-api:8080").rstrip("/")
INTERNAL_SECRET = os.environ.get("MEETPP_INTERNAL_SECRET", "")
# Overrides the ws_url sent by meeting-api / returned by agent-token: inside
# Docker the agent must reach LiveKit on the host network.
LIVEKIT_WS_URL_INTERNAL = os.environ.get("LIVEKIT_WS_URL_INTERNAL", "").strip()

SPEECH_URL = os.environ.get("MEETPP_SPEECH_URL", "").strip().rstrip("/")
SPEECH_SECRET = os.environ.get("MEETPP_SPEECH_SECRET", "")

DATA_DIR = Path(os.environ.get("MEETPP_DATA_DIR", "/var/lib/meet/meetpp"))
MIN_FREE_BYTES = int(_float("MEETPP_MIN_FREE_GB", 3.0) * 1024**3)

STT_MODEL = os.environ.get("STT_MODEL", "small")
STT_DEGRADE_MODEL = os.environ.get("STT_DEGRADE_MODEL", "base")
STT_THREADS = _int("STT_THREADS", 2)
STT_COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8")
# Baked into the image at /models/whisper; locally (unset dir) faster-whisper
# uses the Hugging Face cache and downloads on first use.
_default_whisper_dir = "/models/whisper" if Path("/models/whisper").is_dir() else ""
WHISPER_MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", _default_whisper_dir) or None

SILERO_VAD_MODEL = os.environ.get("SILERO_VAD_MODEL", str(PACKAGE_ROOT / "models" / "silero_vad.onnx"))
