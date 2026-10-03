"""Voice activity detection.

Silero VAD (ONNX) on 30 ms frames when the model is available, with an
energy-based fallback so the agent still runs without it. Utterances start
after 250 ms of speech with 300 ms pre-roll and end after 600 ms of silence;
they are hard-capped at 20 s.
"""
from __future__ import annotations

import os
from collections import deque

import numpy as np

FRAME_SAMPLES = 480  # 30 ms at 16 kHz
SPEECH_THRESHOLD = 0.5
START_MS = 250
END_MS = 600
PRE_ROLL_MS = 300
MAX_UTTERANCE_S = 20.0


class SileroVAD:
    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path or os.environ.get("SILERO_VAD_MODEL", "/models/silero_vad.onnx")
        self._session = None
        self._state = None
        try:
            import onnxruntime as ort

            if os.path.exists(self.model_path):
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 1
                self._session = ort.InferenceSession(self.model_path, sess_options=opts, providers=["CPUExecutionProvider"])
                self._state = np.zeros((2, 1, 128), dtype=np.float32)
        except Exception:  # noqa: BLE001
            self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    def probability(self, frame: np.ndarray) -> float:
        if self._session is None:
            # Energy gate fallback.
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)) + 1e-9)
            return 1.0 if rms > 0.01 else 0.0
        try:
            inp = frame.astype(np.float32).reshape(1, -1)
            sr = np.array(16000, dtype=np.int64)
            out, self._state = self._session.run(None, {"input": inp, "state": self._state, "sr": sr})
            return float(out[0][0])
        except Exception:  # noqa: BLE001
            return 0.0


class UtteranceSegmenter:
    """Turns a stream of 30 ms frames into complete utterances."""

    def __init__(self, vad: SileroVAD) -> None:
        self.vad = vad
        self._pre = deque(maxlen=int(PRE_ROLL_MS / 30))
        self._frames: list[np.ndarray] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_speech = False

    def push(self, frame: np.ndarray) -> np.ndarray | None:
        """Feed one 30 ms frame; return a completed utterance (float32) or None."""
        prob = self.vad.probability(frame)
        if not self._in_speech:
            self._pre.append(frame)
            if prob >= SPEECH_THRESHOLD:
                self._speech_ms += 30
                if self._speech_ms >= START_MS:
                    self._in_speech = True
                    self._frames = list(self._pre)
                    self._silence_ms = 0
            else:
                self._speech_ms = 0
            return None
        self._frames.append(frame)
        if prob >= SPEECH_THRESHOLD:
            self._silence_ms = 0
        else:
            self._silence_ms += 30
        total_s = len(self._frames) * 0.03
        if self._silence_ms >= END_MS or total_s >= MAX_UTTERANCE_S:
            audio = np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.float32)
            self._reset()
            return audio
        return None

    def flush(self) -> np.ndarray | None:
        if self._frames:
            audio = np.concatenate(self._frames)
            self._reset()
            return audio
        return None

    def _reset(self) -> None:
        self._frames = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_speech = False
