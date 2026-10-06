"""mlx-whisper transcriber, loaded once and kept resident.

Never openai-whisper with --device mps (NaN logits on Apple Silicon): this
uses mlx-whisper, which runs natively on the Apple GPU through MLX.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

log = logging.getLogger("meetpp_speech.stt")

TEMPERATURES = (0.0, 0.2, 0.4)
SAMPLE_RATE = 16000
# Decode budget (same rule as the agent): a looping decode otherwise runs to
# Whisper's token limit at every fallback temperature.
TOKENS_BASE = 16
TOKENS_PER_SECOND = 6
TOKENS_MAX = 224


def max_new_tokens(audio_s: float) -> int:
    return max(TOKENS_BASE, min(TOKENS_MAX, int(TOKENS_BASE + TOKENS_PER_SECOND * audio_s)))


class Transcriber:
    def __init__(self, repo: str, cache_limit_mb: int = 512):
        self.repo = repo
        self.cache_limit_mb = cache_limit_mb
        self.loaded = False
        self._languages: set[str] = set()

    def load(self) -> float:
        """Import MLX, download (if needed) and load the weights. Returns seconds."""
        t0 = time.perf_counter()
        import mlx.core as mx
        from mlx_whisper.tokenizer import LANGUAGES
        from mlx_whisper.transcribe import ModelHolder

        if self.cache_limit_mb:
            # MLX keeps freed buffers in a cache that otherwise grows to the peak
            # working set; cap it so the mail app on the same Mac keeps its memory.
            mx.set_cache_limit(self.cache_limit_mb * 1024 * 1024)
        ModelHolder.get_model(self.repo, mx.float16)
        self._languages = set(LANGUAGES)
        self.loaded = True
        return time.perf_counter() - t0

    def supports_language(self, lang: str) -> bool:
        return lang in self._languages

    def memory_mb(self) -> dict[str, float]:
        import mlx.core as mx

        return {
            "active_mb": round(mx.get_active_memory() / 1e6, 1),
            "cache_mb": round(mx.get_cache_memory() / 1e6, 1),
            "peak_mb": round(mx.get_peak_memory() / 1e6, 1),
        }

    def transcribe(self, audio: np.ndarray, language: str = "en", prompt: str | None = None) -> dict[str, Any]:
        """Blocking; run it in a worker thread. `audio` is 16 kHz mono float32."""
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.repo,
            language=language,
            initial_prompt=prompt or None,
            temperature=TEMPERATURES,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
            # one utterance per request: the agent supplies context via prompt
            condition_on_previous_text=False,
            word_timestamps=False,
            verbose=None,  # no tqdm, no printing of text
            sample_len=max_new_tokens(len(audio) / SAMPLE_RATE),
        )
        segments = result.get("segments") or []
        weights = [max(1, len(s.get("tokens") or [])) for s in segments]
        if segments:
            total = float(sum(weights))
            avg_logprob = sum(s["avg_logprob"] * w for s, w in zip(segments, weights)) / total
            no_speech = max(float(s["no_speech_prob"]) for s in segments)
            compression = max(float(s["compression_ratio"]) for s in segments)
            temperature = max(float(s["temperature"]) for s in segments)
        else:
            avg_logprob = None
            no_speech = compression = temperature = None
        return {
            "text": (result.get("text") or "").strip(),
            "avg_logprob": None if avg_logprob is None else round(float(avg_logprob), 4),
            "no_speech_prob": None if no_speech is None else round(no_speech, 4),
            "compression_ratio": None if compression is None else round(compression, 3),
            "temperature": temperature,
            "segments": len(segments),
        }
