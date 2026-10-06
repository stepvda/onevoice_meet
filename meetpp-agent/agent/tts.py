"""Announcement clips (contract §6.1 ``POST /tts``).

Proxied to meetpp-speech (Kokoro, voice am_michael) and cached on the shared
volume at ``$MEETPP_DATA_DIR/tts/<sha256(voice|text)>.ogg``; meeting-api
serves them at ``/api/v1/meetpp/tts/<hash>.ogg``. Without tier 2 the call
returns 503 and clients fall back to browser speechSynthesis.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from .tier2 import Tier2Client, Tier2Error

log = logging.getLogger("meetpp.agent")

DEFAULT_VOICE = "am_michael"
TTS_SAMPLE_RATE = 24000  # Kokoro's native rate; Opus supports it
TTS_BITRATE = 32000


class TTSUnavailable(RuntimeError):
    pass


def clip_hash(voice: str, text: str) -> str:
    return hashlib.sha256(f"{voice}|{text}".encode("utf-8")).hexdigest()


class TTSCache:
    def __init__(self, data_dir: Path | str, tier2: Tier2Client | None) -> None:
        self.dir = Path(data_dir) / "tts"
        self.tier2 = tier2
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, text: str, voice: str = DEFAULT_VOICE) -> dict:
        voice = voice or DEFAULT_VOICE
        digest = clip_hash(voice, text)
        path = self.dir / f"{digest}.ogg"
        if path.is_file() and path.stat().st_size > 0:
            return {"hash": digest, "path": str(path)}
        if self.tier2 is None or not self.tier2.up:
            raise TTSUnavailable("tier 2 unavailable")
        lock = self._locks.setdefault(digest, asyncio.Lock())
        async with lock:
            if path.is_file() and path.stat().st_size > 0:
                return {"hash": digest, "path": str(path)}
            try:
                data, ctype = await self.tier2.tts(text, voice)
            except Tier2Error as exc:
                log.warning("MEETPP_TTS synthesis failed: %s", exc)
                raise TTSUnavailable(str(exc)) from exc
            if not data.startswith(b"OggS"):
                # WAV fallback from the service: the cache must hold real Ogg.
                try:
                    data = await asyncio.to_thread(_to_ogg, data)
                except Exception as exc:  # noqa: BLE001
                    log.warning("MEETPP_TTS could not transcode %s clip: %s", ctype, exc)
                    raise TTSUnavailable("unsupported clip format") from exc
            await asyncio.to_thread(_atomic_write, path, data)
        self._locks.pop(digest, None)
        return {"hash": digest, "path": str(path)}


def _to_ogg(data: bytes) -> bytes:
    from .audio_store import decode_audio, encode_opus

    return encode_opus(decode_audio(data, TTS_SAMPLE_RATE), TTS_SAMPLE_RATE, TTS_BITRATE)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".ogg.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
