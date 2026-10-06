"""Kokoro-82M TTS via kokoro-onnx (CPU onnxruntime, bounded threads)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("meetpp_speech.tts")
# phonemizer warns "words count mismatch" on most inputs (harmless for Kokoro).
# Its get_logger() resets the level on every call, but logger filters survive.
logging.getLogger("phonemizer").addFilter(lambda record: record.levelno >= logging.ERROR)

DEFAULT_VOICE = "am_michael"

#: libespeak-ng copies the data path into a 160-byte buffer; a longer path is
#: silently replaced by the wheel's build-time path and espeak then calls exit(),
#: killing the whole service. Symlinks are not accepted either.
ESPEAK_PATH_MAX = 150


def espeak_data_path(cache_dir: Path | None = None) -> str:
    """espeakng-loader's data dir, or a copy at a short path when it is too long
    (deep install directory). The copy (19 MB) is refreshed when the source changes."""
    import espeakng_loader

    src = espeakng_loader.get_data_path()
    if len(src.encode()) < ESPEAK_PATH_MAX:
        return src
    dest = (cache_dir or Path.home() / "Library" / "Caches" / "meetpp-speech") / "espeak-ng-data"
    if len(str(dest).encode()) >= ESPEAK_PATH_MAX:
        raise RuntimeError(f"espeak-ng data path too long ({src}); install meetpp-speech in a shorter directory")
    stamp = f"{src}\n{os.path.getmtime(os.path.join(src, 'phontab'))}\n"
    marker = dest / ".meetpp-source"
    if not (marker.is_file() and marker.read_text() == stamp):
        log.info("espeak-ng data path is %d bytes; using a copy at %s", len(src), dest)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        (tmp / ".meetpp-source").write_text(stamp)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    return str(dest)


def cap_text(text: str, limit: int) -> tuple[str, bool]:
    """Collapse whitespace and cut to `limit` chars at a sentence/word boundary."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= limit // 2:
        return head[:cut + 1], True
    head = head[:limit - 1]  # room for the closing period
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return head.rstrip(" ,;:-") + ".", True


class Synthesizer:
    def __init__(self, model_path: Path, voices_path: Path, threads: int = 4):
        self.model_path = Path(model_path)
        self.voices_path = Path(voices_path)
        self.threads = threads
        self.loaded = False
        self.voices: set[str] = set()
        self._kokoro = None

    def available(self) -> bool:
        return self.model_path.is_file() and self.voices_path.is_file()

    def load(self) -> float:
        t0 = time.perf_counter()
        import onnxruntime as rt
        from kokoro_onnx import EspeakConfig, Kokoro

        data_path = espeak_data_path()
        if not os.path.isfile(os.path.join(data_path, "phontab")):
            # espeak-ng would exit() the process; fail here so only /tts is disabled
            raise RuntimeError(f"espeak-ng data missing at {data_path}")
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        session = rt.InferenceSession(
            str(self.model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._kokoro = Kokoro.from_session(
            session, str(self.voices_path), espeak_config=EspeakConfig(data_path=data_path)
        )
        self.voices = set(self._kokoro.get_voices())
        self.loaded = True
        return time.perf_counter() - t0

    def synthesize(self, text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> tuple[np.ndarray, int]:
        """Blocking; run it in a worker thread. Returns (float32 samples, sample rate)."""
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, sr = self._kokoro.create(text, voice=voice, speed=speed, lang=lang)
        return np.asarray(samples, dtype=np.float32), int(sr)
