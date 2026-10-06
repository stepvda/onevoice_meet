"""Audio decode/encode with soundfile (bundled libsndfile, incl. Ogg/Opus).

No ffmpeg anywhere in the service path: the Mac Studio has no ffmpeg, and
mlx_whisper's own file loader shells out to it, so the service always hands
mlx_whisper a 16 kHz mono float32 numpy array.
"""

from __future__ import annotations

import io

import numpy as np
import soundfile as sf
import soxr

TARGET_SR = 16000
OPUS_RATES = (8000, 12000, 16000, 24000, 48000)


class AudioDecodeError(ValueError):
    pass


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return np.ascontiguousarray(x, dtype=np.float32)
    return np.ascontiguousarray(soxr.resample(x, sr_in, sr_out, quality="HQ"), dtype=np.float32)


def decode_to_16k_mono(data: bytes) -> np.ndarray:
    """Decode WAV / Ogg-Opus / Ogg-Vorbis / FLAC / AIFF bytes to 16 kHz mono float32."""
    if not data:
        raise AudioDecodeError("empty body")
    try:
        x, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:  # soundfile raises LibsndfileError / RuntimeError / TypeError
        raise AudioDecodeError(f"cannot decode audio: {exc}") from None
    if x.size == 0:
        return np.zeros(0, dtype=np.float32)
    mono = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    if not np.isfinite(mono).all():
        raise AudioDecodeError("audio contains NaN/Inf samples")
    return resample(mono, int(sr), TARGET_SR)


def encode_ogg_opus(samples: np.ndarray, sr: int) -> bytes:
    if sr not in OPUS_RATES:
        samples, sr = resample(samples, sr, 48000), 48000
    buf = io.BytesIO()
    sf.write(buf, np.asarray(samples, dtype=np.float32), sr, format="OGG", subtype="OPUS")
    return buf.getvalue()


def encode_wav(samples: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.asarray(samples, dtype=np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def encode(samples: np.ndarray, sr: int, fmt: str) -> tuple[bytes, str]:
    """Encode to the requested format; Ogg/Opus falls back to WAV if unavailable.

    Returns (bytes, content_type).
    """
    if fmt == "ogg":
        try:
            return encode_ogg_opus(samples, sr), "audio/ogg"
        except Exception:
            pass  # libsndfile without Opus: fall through to WAV
    return encode_wav(samples, sr), "audio/wav"
