"""Voice activity detection and utterance segmentation (FDD §7.3).

Silero VAD ONNX, vendored at ``meetpp-agent/models/silero_vad.onnx`` and
verified against ``SILERO_SHA256`` at startup (and at image build time via
``python -m agent.vad --verify <path>``). The model is fed exactly as Silero
expects at 16 kHz: 512-sample frames, each prefixed with the last 64 samples
of the previous frame (context carry-over), with the recurrent state and the
context reset for every new audio stream.

If the model cannot be loaded the agent does *not* silently degrade: it logs
CRITICAL and reports ``vad="energy"`` in /health, using a crude energy gate.

Segmentation (per speaker stream):
  start   speech prob >= 0.5 for 250 ms (a frame < 0.35 resets the run)
  end     800 ms after the first frame < 0.35, unless a frame >= 0.5 comes back
  min     400 ms of speech (onset → silence start), shorter blips discarded
  pre     300 ms of audio before the detected onset
  post    150 ms of audio after the silence start
  max     15 s; cut at the lowest-energy 30 ms window in the last 3 s and
          continue the utterance from there
"""
from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("meetpp.agent")

SAMPLE_RATE = 16000
FRAME = 512  # samples per Silero frame at 16 kHz (32 ms)
CONTEXT = 64  # samples of the previous frame prepended to every frame

# silero-vad master as of 2026-10-06 (identical to release tag v6.2/v6.2.1).
# Same I/O contract as v5 (input [1, 64+512], state [2, 1, 128], sr int64).
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SILERO_SOURCE = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"

START_PROB = 0.5
END_PROB = 0.35
START_MS = 250
END_SILENCE_MS = 800
MIN_SPEECH_MS = 400
PRE_ROLL_MS = 300
POST_ROLL_MS = 150
MAX_UTTERANCE_S = 15.0
CUT_SEARCH_S = 3.0
CUT_WINDOW_MS = 30
CUT_HOP_MS = 10

# Peak below this is digital silence (muted / no packets): skip the model.
_DIGITAL_SILENCE = 1e-4


def _ms(ms: float) -> int:
    return int(round(ms * SAMPLE_RATE / 1000.0))


class _Buffer:
    """Growable float32 buffer (amortised O(1) append, cheap front drops)."""

    def __init__(self, data: np.ndarray | None = None) -> None:
        data = np.zeros(0, dtype=np.float32) if data is None else data.astype(np.float32, copy=False)
        self._data = np.zeros(max(8192, 2 * len(data)), dtype=np.float32)
        self._data[: len(data)] = data
        self.n = len(data)

    def __len__(self) -> int:
        return self.n

    def append(self, x: np.ndarray) -> None:
        need = self.n + len(x)
        if need > len(self._data):
            grown = np.zeros(max(need, 2 * len(self._data)), dtype=np.float32)
            grown[: self.n] = self._data[: self.n]
            self._data = grown
        self._data[self.n : need] = x
        self.n = need

    def view(self) -> np.ndarray:
        return self._data[: self.n]

    def drop_front(self, k: int) -> None:
        k = min(max(0, k), self.n)
        if k:
            self._data[: self.n - k] = self._data[k : self.n]
            self.n -= k


# ─── model ─────────────────────────────────────────────────────────────────


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_model(path: str | Path, expected: str = SILERO_SHA256) -> bool:
    try:
        return sha256_file(path) == expected
    except OSError:
        return False


class VADUnavailable(RuntimeError):
    pass


class SileroModel:
    """Shared ONNX Runtime session. ``InferenceSession.run`` is thread-safe,
    so one session serves every stream; per-stream state lives in
    ``SileroStream``."""

    def __init__(self, path: str | Path | None = None, *, session=None, expected_sha: str = SILERO_SHA256) -> None:
        if session is not None:
            self._session = session
            return
        if path is None:
            raise VADUnavailable("no model path")
        p = Path(path)
        if not p.is_file():
            raise VADUnavailable(f"model file missing: {p}")
        digest = sha256_file(p)
        if digest != expected_sha:
            raise VADUnavailable(f"checksum mismatch for {p}: {digest} != {expected_sha}")
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 3
            self._session = ort.InferenceSession(str(p), sess_options=opts, providers=["CPUExecutionProvider"])
        except Exception as exc:  # noqa: BLE001
            raise VADUnavailable(f"onnxruntime could not load {p}: {exc}") from exc

    def run(self, x: np.ndarray, state: np.ndarray) -> tuple[float, np.ndarray]:
        out, new_state = self._session.run(
            None,
            {"input": x, "state": state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
        )
        return float(np.asarray(out).reshape(-1)[0]), new_state


class SileroStream:
    """Per-stream Silero state: 64-sample context and the RNN state."""

    kind = "silero"

    def __init__(self, model: SileroModel) -> None:
        self.model = model
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        if len(frame) != FRAME:
            raise ValueError(f"Silero expects {FRAME}-sample frames at 16 kHz, got {len(frame)}")
        frame = frame.astype(np.float32, copy=False)
        if float(np.max(np.abs(frame))) < _DIGITAL_SILENCE:
            # Muted/empty input: nothing to detect; keep framing continuous.
            self._context = frame[-CONTEXT:].copy()
            return 0.0
        x = np.concatenate([self._context, frame]).reshape(1, CONTEXT + FRAME)
        p, self._state = self.model.run(x, self._state)
        self._context = frame[-CONTEXT:].copy()
        return p


class EnergyStream:
    """Emergency fallback only (never selected silently: CRITICAL at load).

    Maps frame RMS to a pseudo-probability: -50 dBFS → 0, -30 dBFS → 1, so the
    0.5 start threshold sits at -40 dBFS (RMS 0.01, the Release 1 gate)."""

    kind = "energy"

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))) + 1e-12)
        db = 20.0 * np.log10(rms)
        return float(min(1.0, max(0.0, (db + 50.0) / 20.0)))


class VADProvider:
    """Creates one VAD stream per audio stream."""

    def __init__(self, model: SileroModel | None, error: str | None = None) -> None:
        self.model = model
        self.error = error

    @property
    def kind(self) -> str:
        return "silero" if self.model is not None else "energy"

    def new_stream(self):
        return SileroStream(self.model) if self.model is not None else EnergyStream()


def load_vad(path: str | Path) -> VADProvider:
    try:
        model = SileroModel(path)
    except VADUnavailable as exc:
        log.critical(
            "MEETPP_VAD Silero VAD unavailable (%s); FALLING BACK TO ENERGY GATE. "
            "Transcription quality will be degraded; fix the image.",
            exc,
        )
        return VADProvider(None, str(exc))
    log.info("MEETPP_VAD active=silero path=%s sha256=%s (verified)", path, SILERO_SHA256[:12])
    return VADProvider(model)


# ─── segmentation ──────────────────────────────────────────────────────────


@dataclass
class Utterance:
    audio: np.ndarray  # float32 mono 16 kHz
    start_sample: int  # absolute stream index of audio[0]
    end_sample: int  # absolute stream index one past the last sample
    speech_ms: int
    cut: bool = False  # ended by the max-length cut

    @property
    def duration_s(self) -> float:
        return len(self.audio) / SAMPLE_RATE


class UtteranceSegmenter:
    """Turns a PCM stream into utterances. Feed any number of samples with
    ``process``; full 512-sample frames are framed internally."""

    def __init__(
        self,
        vad,
        *,
        start_prob: float = START_PROB,
        end_prob: float = END_PROB,
        start_ms: int = START_MS,
        end_silence_ms: int = END_SILENCE_MS,
        min_speech_ms: int = MIN_SPEECH_MS,
        pre_roll_ms: int = PRE_ROLL_MS,
        post_roll_ms: int = POST_ROLL_MS,
        max_utterance_s: float = MAX_UTTERANCE_S,
        cut_search_s: float = CUT_SEARCH_S,
    ) -> None:
        self.vad = vad
        self.start_prob = start_prob
        self.end_prob = end_prob
        self.start_samples = _ms(start_ms)
        self.end_samples = _ms(end_silence_ms)
        self.min_speech = _ms(min_speech_ms)
        self.pre = _ms(pre_roll_ms)
        self.post = _ms(post_roll_ms)
        self.max_samples = int(max_utterance_s * SAMPLE_RATE)
        self.cut_search = int(cut_search_s * SAMPLE_RATE)
        self.short_discarded = 0
        self.last_prob = 0.0
        self._pending = np.zeros(0, dtype=np.float32)
        self._total = 0
        self._reset_state(None, 0)

    # ── public ──
    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_active(self) -> bool:
        """True while an utterance is open or a speech run is building."""
        return self._in_speech or self._run_start is not None

    @property
    def samples_seen(self) -> int:
        return self._total

    def process(self, pcm: np.ndarray) -> list[Utterance]:
        out: list[Utterance] = []
        if len(pcm):
            self._pending = np.concatenate([self._pending, pcm.astype(np.float32, copy=False)])
        n_frames = len(self._pending) // FRAME
        for i in range(n_frames):
            u = self._push_frame(self._pending[i * FRAME : (i + 1) * FRAME])
            if u is not None:
                out.append(u)
        if n_frames:
            self._pending = self._pending[n_frames * FRAME :].copy()
        return out

    def flush(self) -> Utterance | None:
        """Close an open utterance (stream end, pause, stop). Applies the
        minimum-length rule. Leaves the segmenter idle."""
        u = None
        if self._in_speech:
            end = self._silence_start + self.post if self._silence_start is not None else self._total
            u = self._finish(min(end, self._total))
        self._reset_state(None, self._total)
        return u

    def reset(self) -> None:
        """Drop everything (pause/opt-out) and reset the model state."""
        self._pending = np.zeros(0, dtype=np.float32)
        self._reset_state(None, self._total)
        self.vad.reset()

    # ── internals ──
    def _reset_state(self, buf: np.ndarray | None, buf_start: int) -> None:
        self._in_speech = False
        self._buf = _Buffer(buf)  # idle: recent audio for pre-roll; speech: utterance audio
        self._buf_start = buf_start
        self._run_start: int | None = None
        self._run_speech = 0
        self._onset = 0
        self._silence_start: int | None = None

    def _push_frame(self, frame: np.ndarray) -> Utterance | None:
        frame_start = self._total
        self._total += FRAME
        p = self.vad.prob(frame)
        self.last_prob = p
        self._buf.append(frame)
        if not self._in_speech:
            if p >= self.start_prob:
                if self._run_start is None:
                    self._run_start = frame_start
                self._run_speech += FRAME
                if self._run_speech >= self.start_samples:
                    self._open(self._run_start)
                    return None
            elif p < self.end_prob:
                self._run_start = None
                self._run_speech = 0
            keep_from = (self._run_start if self._run_start is not None else self._total) - self.pre
            if keep_from > self._buf_start:
                self._buf.drop_front(keep_from - self._buf_start)
                self._buf_start = keep_from
            return None
        # in speech
        if p >= self.start_prob:
            self._silence_start = None
        elif p < self.end_prob and self._silence_start is None:
            self._silence_start = frame_start
        if self._silence_start is not None and self._total - self._silence_start >= self.end_samples:
            return self._finish(self._silence_start + self.post)
        if len(self._buf) >= self.max_samples:
            return self._cut()
        return None

    def _open(self, onset: int) -> None:
        start = max(self._buf_start, onset - self.pre)
        self._buf.drop_front(start - self._buf_start)
        self._buf_start = start
        self._in_speech = True
        self._onset = onset
        self._silence_start = None
        self._run_start = None
        self._run_speech = 0

    def _finish(self, end: int) -> Utterance | None:
        end = max(self._buf_start, min(end, self._buf_start + len(self._buf)))
        n = end - self._buf_start
        data = self._buf.view()
        audio = data[:n].copy()
        speech_end = self._silence_start if self._silence_start is not None else end
        speech = speech_end - self._onset
        start = self._buf_start
        # The rest (trailing silence) seeds the idle buffer for the next pre-roll.
        self._reset_state(data[n:].copy(), end)
        if speech < self.min_speech:
            self.short_discarded += 1
            return None
        return Utterance(audio=audio, start_sample=start, end_sample=end, speech_ms=int(speech * 1000 / SAMPLE_RATE))

    def _cut(self) -> Utterance:
        data = self._buf.view()
        cut = lowest_energy_cut(data, self.cut_search)
        audio = data[:cut].copy()
        start = self._buf_start
        speech = max(0, start + cut - self._onset)
        self._buf.drop_front(cut)
        self._buf_start = start + cut
        self._onset = self._buf_start
        return Utterance(
            audio=audio,
            start_sample=start,
            end_sample=start + cut,
            speech_ms=int(speech * 1000 / SAMPLE_RATE),
            cut=True,
        )


def lowest_energy_cut(buf: np.ndarray, search: int, window: int = _ms(CUT_WINDOW_MS), hop: int = _ms(CUT_HOP_MS)) -> int:
    """Index (centre of the quietest ``window``-sample window) within the last
    ``search`` samples of ``buf``."""
    n = len(buf)
    lo = max(0, n - search)
    seg = buf[lo:].astype(np.float64)
    if len(seg) < window:
        return n
    csum = np.concatenate([[0.0], np.cumsum(seg * seg)])
    starts = np.arange(0, len(seg) - window + 1, hop)
    energy = csum[starts + window] - csum[starts]
    best = int(starts[int(np.argmin(energy))])
    return lo + best + window // 2


def _main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--verify":
        path = argv[2]
        try:
            digest = sha256_file(path)
        except OSError as exc:
            print(f"silero_vad: cannot read {path}: {exc}", file=sys.stderr)
            return 1
        if digest != SILERO_SHA256:
            print(f"silero_vad: checksum mismatch for {path}\n  got      {digest}\n  expected {SILERO_SHA256}", file=sys.stderr)
            return 1
        print(f"silero_vad: {path} OK ({digest})")
        return 0
    print("usage: python -m agent.vad --verify <path>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
