"""Tier-1 speech-to-text: faster-whisper ``small`` (multilingual, forced to
English), int8 on CPU, with ``base`` as the overload model (FDD §7.3).

Decoding mirrors the configuration measured at 10.4 % WER: beam 1,
temperature fallback 0.0 → 0.2 → 0.4 (compression ratio > 2.4 or avg
log-prob < −1.0), no conditioning on previous text, no internal VAD, and an
``initial_prompt`` made of the prose glossary plus the last 200 characters of
the session transcript.

Filters: Whisper's own no-speech rule (no_speech_prob > 0.6 AND
avg_logprob < −1.0; faster-whisper applies it internally and returns no
segment, we re-check per segment) and a full-utterance hallucination
blocklist. Every drop carries a reason; the caller logs and counts it.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import config

log = logging.getLogger("meetpp.agent")

TEMPERATURES = (0.0, 0.2, 0.4)
COMPRESSION_RATIO_THRESHOLD = 2.4
LOG_PROB_THRESHOLD = -1.0
NO_SPEECH_THRESHOLD = 0.6
PROMPT_TAIL_CHARS = 200
GLOSSARY_MAX_CHARS = 1000
# Decode budget: a looping decode ("I'm going to say" x 40, ". . . .") runs to
# the token limit at every fallback temperature, which stalls a 2-vCPU box for
# tens of seconds. Speech is ~3-4 tokens/s, so this leaves ample headroom.
TOKENS_BASE = 16
TOKENS_PER_SECOND = 6
TOKENS_MAX = 200  # prompt (<= 224) + new tokens must stay within Whisper's 448


def max_new_tokens(audio_s: float) -> int:
    return max(TOKENS_BASE, min(TOKENS_MAX, int(TOKENS_BASE + TOKENS_PER_SECOND * audio_s)))

# ─── filters ───────────────────────────────────────────────────────────────

# Full-utterance matches only (after normalisation: lower case, punctuation
# and brackets removed). Classic Whisper hallucinations on silence/noise.
BLOCKLIST_PHRASES: frozenset[str] = frozenset(
    {
        "you",
        "thank you",
        "thanks for watching",
        "thank you for watching",
        "thank you very much for watching",
        "thank you so much for watching",
        "thank you for watching and see you next time",
        "thanks for watching and see you next time",
        "thank you for watching please subscribe",
        "thank you for watching dont forget to subscribe",
        "please subscribe",
        "please subscribe to my channel",
        "please like and subscribe",
        "like and subscribe",
        "dont forget to like and subscribe",
        "dont forget to subscribe",
        "subscribe to my channel",
        "subscribe to the channel",
        "hit the bell icon",
        "see you in the next video",
        "ill see you in the next video",
        "subtitles by the amaraorg community",
        "subtitles made by the community of amaraorg",
        "amaraorg",
        "subtitles",
        "english subtitles",
        "transcription by castingwords",
        "the end",
        "music",
        "music playing",
        "upbeat music",
        "soft music",
        "applause",
        "laughter",
        "silence",
        "blank audio",
        "blankaudio",
        "no speech",
        "inaudible",
        "foreign",
        "all rights reserved",
        "copyright",
        "beep",
        "sigh",
    }
)

# Anchored full-utterance patterns (credits lines with a name).
BLOCKLIST_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^(subtitles?|subtitled|captions?|captioned|captioning|closed captioning|transcribed|transcription|translated|translation)( made)? by( the)? [\w .'\-]{1,60}$"),
    re.compile(r"^(c )?copyright \d{4}[\w .'\-]{0,60}$"),
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("’", "'").replace("‘", "'")
    t = _PUNCT_RE.sub("", t)
    t = t.replace("_", " ")
    return _SPACE_RE.sub(" ", t).strip()


def blocklist_match(text: str) -> str | None:
    """Return the matched entry when the *whole* utterance is a known
    hallucination, else None."""
    n = normalize(text)
    if not n:
        return None
    if n in BLOCKLIST_PHRASES:
        return n
    for pat in BLOCKLIST_PATTERNS:
        if pat.match(n):
            return pat.pattern
    return None


def whisper_should_drop(
    no_speech_prob: float | None,
    avg_logprob: float | None,
    no_speech_threshold: float = NO_SPEECH_THRESHOLD,
    log_prob_threshold: float = LOG_PROB_THRESHOLD,
) -> bool:
    """Whisper's rule: silence only when BOTH conditions hold."""
    if no_speech_prob is None or avg_logprob is None:
        return False
    return no_speech_prob > no_speech_threshold and avg_logprob < log_prob_threshold


def tail_text(text: str, n: int) -> str:
    """Last ``n`` characters of ``text``, starting at a word boundary."""
    text = _SPACE_RE.sub(" ", text or "").strip()
    if len(text) <= n:
        return text
    cut = text[-n:]
    sp = cut.find(" ")
    return cut[sp + 1 :] if 0 <= sp < len(cut) - 1 else cut


def build_prompt(glossary: str, transcript: str, tail_chars: int = PROMPT_TAIL_CHARS) -> str | None:
    g = _SPACE_RE.sub(" ", glossary or "").strip()
    if len(g) > GLOSSARY_MAX_CHARS:
        g = g[:GLOSSARY_MAX_CHARS].rsplit(" ", 1)[0]
    t = tail_text(transcript, tail_chars)
    prompt = f"{g} {t}".strip()
    return prompt or None


# ─── engine ────────────────────────────────────────────────────────────────


@dataclass
class STTResult:
    text: str = ""
    dropped: str | None = None  # reason when nothing is emitted
    detail: str = ""
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    model: str = ""
    audio_s: float = 0.0
    elapsed_s: float = 0.0

    @property
    def rtf(self) -> float:
        return self.elapsed_s / self.audio_s if self.audio_s > 0 else 0.0


def _default_factory(name: str, compute_type: str, threads: int, download_root: str | None):
    from faster_whisper import WhisperModel

    return WhisperModel(
        name,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=threads,
        num_workers=1,
        download_root=download_root,
    )


class STTEngine:
    """Holds the primary and the degrade model. ``load`` and ``transcribe``
    block: call them from a worker thread, never on the event loop."""

    def __init__(
        self,
        primary: str = config.STT_MODEL,
        degrade: str = config.STT_DEGRADE_MODEL,
        *,
        compute_type: str = config.STT_COMPUTE_TYPE,
        threads: int = config.STT_THREADS,
        download_root: str | None = config.WHISPER_MODEL_DIR,
        language: str = config.LANGUAGE,
        model_factory: Callable | None = None,
    ) -> None:
        self.primary = primary
        self.degrade = degrade
        self.compute_type = compute_type
        self.threads = threads
        self.download_root = download_root
        self.language = language
        self._factory = model_factory or (lambda name: _default_factory(name, compute_type, threads, download_root))
        self._models: dict[str, object] = {}
        self.ready = threading.Event()
        self.load_error: str | None = None

    def load(self) -> None:
        for name in dict.fromkeys([self.primary, self.degrade]):
            if not name:
                continue
            t0 = time.monotonic()
            try:
                self._models[name] = self._factory(name)
                log.info("MEETPP_STT model loaded name=%s threads=%s in %.1fs", name, self.threads, time.monotonic() - t0)
            except Exception as exc:  # noqa: BLE001
                log.error("MEETPP_STT model load failed name=%s: %s", name, exc)
                if name == self.primary:
                    self.load_error = f"{name}: {exc}"
        if self.primary in self._models:
            self.ready.set()
        elif self._models:
            # Primary missing but degrade loaded: run on it rather than not at all.
            log.error("MEETPP_STT primary model unavailable; running on %s", self.degrade)
            self.primary = next(iter(self._models))
            self.ready.set()

    @property
    def loaded(self) -> list[str]:
        return list(self._models)

    def model_for(self, degraded: bool) -> str:
        if degraded and self.degrade in self._models:
            return self.degrade
        return self.primary

    def transcribe(self, audio: np.ndarray, prompt: str | None, degraded: bool = False) -> STTResult:
        name = self.model_for(degraded)
        model = self._models.get(name)
        audio_s = len(audio) / config.SAMPLE_RATE
        if model is None:
            return STTResult(dropped="model_unavailable", model=name, audio_s=audio_s)
        t0 = time.perf_counter()
        segments, _info = model.transcribe(
            audio.astype(np.float32, copy=False),
            language=self.language,
            task="transcribe",
            beam_size=1,
            temperature=list(TEMPERATURES),
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            log_prob_threshold=LOG_PROB_THRESHOLD,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=prompt or None,
            max_new_tokens=max_new_tokens(audio_s),
        )
        segs = list(segments)  # the generator does the decoding
        elapsed = time.perf_counter() - t0
        return self.postprocess(segs, name=name, audio_s=audio_s, elapsed_s=elapsed)

    @staticmethod
    def postprocess(segs: list, *, name: str = "", audio_s: float = 0.0, elapsed_s: float = 0.0) -> STTResult:
        base = dict(model=name, audio_s=audio_s, elapsed_s=elapsed_s)
        if not segs:
            # faster-whisper skipped the window under Whisper's no-speech rule.
            return STTResult(dropped="no_speech", detail="whisper skipped window", **base)
        kept = [s for s in segs if not whisper_should_drop(s.no_speech_prob, s.avg_logprob)]
        first = segs[0]
        if not kept:
            return STTResult(
                dropped="no_speech",
                detail=f"no_speech_prob={first.no_speech_prob:.2f} avg_logprob={first.avg_logprob:.2f}",
                avg_logprob=first.avg_logprob,
                no_speech_prob=first.no_speech_prob,
                **base,
            )
        text = " ".join(s.text.strip() for s in kept).strip()
        avg_lp = float(np.mean([s.avg_logprob for s in kept]))
        ns = float(max(s.no_speech_prob for s in kept))
        cr = float(max(getattr(s, "compression_ratio", 0.0) or 0.0 for s in kept))
        if not normalize(text):
            return STTResult(dropped="empty", detail=repr(text), avg_logprob=avg_lp, no_speech_prob=ns, **base)
        hit = blocklist_match(text)
        if hit:
            return STTResult(dropped="hallucination", detail=text, avg_logprob=avg_lp, no_speech_prob=ns, **base)
        return STTResult(text=text, avg_logprob=avg_lp, no_speech_prob=ns, compression_ratio=cr, **base)
