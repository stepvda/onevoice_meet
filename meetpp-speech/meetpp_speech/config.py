"""Settings from the environment (contract section 8)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: meetpp-speech/ (the directory holding this package, run.sh, models/ ...)
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


class ConfigError(RuntimeError):
    pass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def short_model_name(repo: str) -> str:
    """'mlx-community/whisper-large-v3-turbo' -> 'large-v3-turbo' (as in /health)."""
    name = repo.rstrip("/").rsplit("/", 1)[-1]
    return name[len("whisper-"):] if name.startswith("whisper-") else name


@dataclass(frozen=True)
class Settings:
    secret: bytes
    bind: str = "127.0.0.1"
    port: int = 9310
    model: str = DEFAULT_MODEL
    kokoro_model: Path = ROOT / "models" / "kokoro-v1.0.onnx"
    kokoro_voices: Path = ROOT / "models" / "voices-v1.0.bin"
    # --- tuning (optional env, sensible defaults) ---
    max_parallel: int = 2          # transcriptions running at once
    queue_max_s: float = 30.0      # seconds of audio allowed to wait for a slot
    max_audio_s: float = 300.0     # longest single request accepted
    max_body_bytes: int = 4 * 1024 * 1024  # 300 s of Opus at 24 kbit/s is ~0.9 MB
    decode_parallel: int = 2       # audio decodes running at once
    tts_parallel: int = 1
    tts_queue_max: int = 4         # TTS requests allowed to wait
    tts_max_chars: int = 400
    kokoro_threads: int = 4        # onnxruntime intra-op threads
    mlx_cache_mb: int = 512        # MLX buffer cache cap (keeps memory modest)
    clock_skew_s: int = 60
    prompt_max_chars: int = 600

    @property
    def model_name(self) -> str:
        return short_model_name(self.model)

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("MEETPP_SPEECH_SECRET", "")
        if not secret.strip():
            raise ConfigError(
                "MEETPP_SPEECH_SECRET is required (shared with meetpp-agent); "
                "set it in ~/.config/meetpp-speech/env"
            )
        return cls(
            secret=secret.strip().encode("utf-8"),
            bind=os.environ.get("MEETPP_SPEECH_BIND", "").strip() or "127.0.0.1",
            port=_int("MEETPP_SPEECH_PORT", 9310),
            model=os.environ.get("MEETPP_SPEECH_MODEL", "").strip() or DEFAULT_MODEL,
            kokoro_model=Path(
                os.environ.get("KOKORO_MODEL", "").strip()
                or ROOT / "models" / "kokoro-v1.0.onnx"
            ).expanduser(),
            kokoro_voices=Path(
                os.environ.get("KOKORO_VOICES", "").strip()
                or ROOT / "models" / "voices-v1.0.bin"
            ).expanduser(),
            max_parallel=max(1, _int("MEETPP_SPEECH_MAX_PARALLEL", 2)),
            queue_max_s=_float("MEETPP_SPEECH_QUEUE_S", 30.0),
            max_audio_s=_float("MEETPP_SPEECH_MAX_AUDIO_S", 300.0),
            kokoro_threads=max(1, _int("KOKORO_THREADS", 4)),
            mlx_cache_mb=max(0, _int("MEETPP_SPEECH_MLX_CACHE_MB", 512)),
        )
