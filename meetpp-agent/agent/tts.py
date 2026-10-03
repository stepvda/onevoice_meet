"""Text-to-speech via Piper.

Clips are cached by content hash on a shared volume so meeting-api can serve
them same-origin. Output is OGG/Vorbis when libsndfile supports it, else WAV.
If Piper or the voice is unavailable, the clip is not produced and clients
fall back to browser speechSynthesis.
"""
from __future__ import annotations

import hashlib
import logging
import os
import wave
from io import BytesIO
from pathlib import Path

log = logging.getLogger("meetpp.agent")


def parse_voices(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (spec or "").split(","):
        if ":" in part:
            lang, voice = part.split(":", 1)
            out[lang.strip()] = voice.strip()
    return out


class PiperTTS:
    def __init__(self, voices: dict[str, str], cache_dir: str, model_dir: str = "/models/piper") -> None:
        self.voices = voices
        self.cache_dir = Path(cache_dir)
        self.model_dir = Path(model_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._voices: dict[str, object] = {}
        self._soundfile = None
        try:
            import soundfile  # type: ignore

            self._soundfile = soundfile
        except Exception:  # noqa: BLE001
            self._soundfile = None

    def _load(self, lang: str):
        voice_name = self.voices.get(lang) or self.voices.get("en")
        if not voice_name:
            return None
        if voice_name in self._voices:
            return self._voices[voice_name]
        model = self.model_dir / f"{voice_name}.onnx"
        config = self.model_dir / f"{voice_name}.onnx.json"
        if not model.exists() or not config.exists():
            return None
        try:
            from piper.voice import PiperVoice

            voice = PiperVoice.load(str(model), config_path=str(config))
            self._voices[voice_name] = voice
            return voice
        except Exception as exc:  # noqa: BLE001
            log.warning("piper voice load failed for %s: %s", voice_name, exc)
            return None

    def synthesize(self, text: str, lang: str) -> dict | None:
        text = (text or "").strip()
        if not text:
            return None
        digest = hashlib.sha256(f"{lang}:{text}".encode("utf-8")).hexdigest()
        for ext in ("ogg", "wav"):
            cached = self.cache_dir / f"{digest}.{ext}"
            if cached.exists():
                return {"hash": digest, "ext": ext, "duration_ms": _duration_ms(cached)}
        voice = self._load(lang)
        if voice is None:
            return None
        try:
            buf = BytesIO()
            with wave.open(buf, "wb") as wav:
                voice.synthesize(text, wav)
            wav_bytes = buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            log.warning("piper synth failed: %s", exc)
            return None
        ext = "wav"
        out = wav_bytes
        if self._soundfile is not None:
            try:
                data, sr = self._soundfile.read(BytesIO(wav_bytes), dtype="int16")
                ogg_buf = BytesIO()
                self._soundfile.write(ogg_buf, data, sr, format="OGG", subtype="VORBIS")
                out = ogg_buf.getvalue()
                ext = "ogg"
            except Exception:  # noqa: BLE001
                out, ext = wav_bytes, "wav"
        path = self.cache_dir / f"{digest}.{ext}"
        path.write_bytes(out)
        return {"hash": digest, "ext": ext, "duration_ms": _duration_ms(path)}


def _duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as w:
            return int(w.getnframes() / float(w.getframerate()) * 1000)
    except Exception:  # noqa: BLE001
        try:
            import soundfile  # type: ignore

            data, sr = soundfile.read(str(path))
            return int(len(data) / sr * 1000)
        except Exception:  # noqa: BLE001
            return 2500
