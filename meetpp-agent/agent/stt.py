"""Speech-to-text: faster-whisper (CTranslate2), small int8 multilingual.

One worker serves a FIFO of utterances ordered by end time. The model is
loaded lazily so the container starts fast. A remote endpoint
(STT_REMOTE_URL) can be configured; it must expose POST /transcribe taking
16 kHz mono float32 PCM and returning JSON {text, language, avg_logprob}.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("meetpp.agent")


@dataclass
class Transcription:
    text: str
    language: str | None = None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


class STTEngine:
    def __init__(self, model: str, degrade_model: str, compute_type: str, threads: int) -> None:
        self.model_name = model
        self.degrade_model = degrade_model
        self.compute_type = compute_type
        self.threads = threads
        self._model = None
        self._degraded = False
        self.remote_url = os.environ.get("STT_REMOTE_URL", "")
        self.backlog_s = 0.0
        self.rtf = 0.0

    def load(self) -> None:
        if self.remote_url:
            return
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.threads,
                download_root="/models/whisper",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("STT model load failed: %s", exc)
            self._model = None

    def _ensure(self, degraded: bool) -> None:
        if self._model is None and not self.remote_url:
            self.load()
        if degraded and not self._degraded and self.degrade_model:
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.degrade_model,
                    device="cpu",
                    compute_type=self.compute_type,
                    cpu_threads=self.threads,
                    download_root="/models/whisper",
                )
                self._degraded = True
            except Exception:  # noqa: BLE001
                pass

    async def transcribe(self, audio: np.ndarray, language: str, prompt: str = "") -> Transcription:
        deg = self.backlog_s > 30
        self._ensure(deg)
        started = time.monotonic()
        duration = len(audio) / 16000.0 if len(audio) else 0.0
        if self.remote_url:
            result = await self._transcribe_remote(audio, language, prompt)
        else:
            result = await asyncio.to_thread(self._transcribe_local, audio, language, prompt)
        elapsed = time.monotonic() - started
        if duration > 0:
            self.rtf = elapsed / duration
        return result

    async def _transcribe_remote(self, audio: np.ndarray, language: str, prompt: str) -> Transcription:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    self.remote_url,
                    content=audio.astype(np.float32).tobytes(),
                    headers={"Content-Type": "application/octet-stream", "X-Language": language},
                )
                r.raise_for_status()
                data = r.json()
                return Transcription(
                    text=(data.get("text") or "").strip(),
                    language=data.get("language") or language,
                    avg_logprob=data.get("avg_logprob"),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("remote STT failed: %s", exc)
            return Transcription(text="")

    def _transcribe_local(self, audio: np.ndarray, language: str, prompt: str) -> Transcription:
        if self._model is None:
            return Transcription(text="")
        try:
            segments, info = self._model.transcribe(
                audio.astype(np.float32),
                language=language or None,
                beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=False,
                initial_prompt=(prompt or None),
            )
            text_parts = []
            no_speech = None
            logprob = None
            for seg in segments:
                text_parts.append(seg.text)
                no_speech = seg.no_speech_prob
                logprob = seg.avg_logprob
            text = " ".join(t.strip() for t in text_parts).strip()
            # Drop obvious artefacts.
            if no_speech is not None and no_speech > 0.6:
                return Transcription(text="")
            if logprob is not None and logprob < -1.0:
                return Transcription(text="")
            if _is_blocklisted(text):
                return Transcription(text="")
            return Transcription(
                text=text,
                language=getattr(info, "language", language),
                avg_logprob=logprob,
                no_speech_prob=no_speech,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("local STT failed: %s", exc)
            return Transcription(text="")


_BLOCKLIST = (
    "thank you for watching",
    "thanks for watching",
    "ondertitels ingediend door",
    "amara.org",
    "subtitles by",
    "please subscribe",
)


def _is_blocklisted(text: str) -> bool:
    low = text.lower()
    return any(b in low for b in _BLOCKLIST)
