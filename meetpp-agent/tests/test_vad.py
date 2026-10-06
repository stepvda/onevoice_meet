from __future__ import annotations

import logging
import shutil

import numpy as np
import pytest

from agent import vad as vadmod
from agent.vad import (
    CONTEXT,
    FRAME,
    SILERO_SHA256,
    SileroModel,
    SileroStream,
    UtteranceSegmenter,
    VADUnavailable,
    load_vad,
    lowest_energy_cut,
    verify_model,
)
from tests.fakes import EnergyGateVAD, ScriptedVAD, silence, tone

F = FRAME  # 512 samples = 32 ms


def feed(seg: UtteranceSegmenter, audio: np.ndarray, chunk: int = 160) -> list:
    """Feed in odd-sized chunks (LiveKit gives 10 ms frames)."""
    out = []
    for i in range(0, len(audio), chunk):
        out.extend(seg.process(audio[i : i + chunk]))
    return out


# ── thresholds, pre-roll, post-roll ──


def test_onset_preroll_and_postroll_exact():
    audio = np.concatenate([silence(40 * F), tone(60 * F), silence(40 * F)])
    seg = UtteranceSegmenter(EnergyGateVAD())
    utts = feed(seg, audio)
    assert len(utts) == 1
    u = utts[0]
    onset, silence_start = 40 * F, 100 * F
    assert u.start_sample == onset - 4800  # exactly 300 ms pre-roll
    assert u.end_sample == silence_start + 2400  # exactly 150 ms post-roll
    assert len(u.audio) == u.end_sample - u.start_sample
    assert u.speech_ms == round(60 * F / 16)
    # the pre-roll really is the audio before the onset (silence), then the tone
    assert np.allclose(u.audio[:4800], 0.0)
    assert np.allclose(u.audio[4800 : 4800 + 100], audio[onset : onset + 100])


def test_preroll_is_clipped_at_stream_start():
    audio = np.concatenate([silence(3 * F), tone(30 * F), silence(30 * F)])
    u = feed(UtteranceSegmenter(EnergyGateVAD()), audio)[0]
    assert u.start_sample == 0


def test_start_needs_250ms_of_speech():
    # 7 frames = 224 ms: never starts
    seg = UtteranceSegmenter(EnergyGateVAD())
    assert feed(seg, np.concatenate([silence(20 * F), tone(7 * F), silence(40 * F)])) == []
    assert seg.short_discarded == 0 and not seg.in_speech


def test_min_utterance_400ms():
    # 8 frames (256 ms) starts an utterance but is below the 400 ms minimum
    seg = UtteranceSegmenter(EnergyGateVAD())
    assert feed(seg, np.concatenate([silence(20 * F), tone(8 * F), silence(40 * F)])) == []
    assert seg.short_discarded == 1
    # 13 frames (416 ms) is kept
    seg = UtteranceSegmenter(EnergyGateVAD())
    utts = feed(seg, np.concatenate([silence(20 * F), tone(13 * F), silence(40 * F)]))
    assert len(utts) == 1 and utts[0].speech_ms >= 400


def test_end_needs_800ms_of_silence():
    # a 640 ms pause does not split the utterance
    a = np.concatenate([silence(20 * F), tone(30 * F), silence(20 * F), tone(30 * F), silence(40 * F)])
    assert len(feed(UtteranceSegmenter(EnergyGateVAD()), a)) == 1
    # an 832 ms pause does
    b = np.concatenate([silence(20 * F), tone(30 * F), silence(26 * F), tone(30 * F), silence(40 * F)])
    utts = feed(UtteranceSegmenter(EnergyGateVAD()), b)
    assert len(utts) == 2
    assert utts[0].end_sample <= utts[1].start_sample  # never overlapping


def test_end_threshold_hysteresis():
    # probabilities between 0.35 and 0.5 neither start the silence timer...
    probs = [0.0] * 5 + [0.9] * 10 + [0.4] * 60 + [0.9] * 5 + [0.1] * 30
    seg = UtteranceSegmenter(ScriptedVAD(probs))
    utts = feed(seg, tone(len(probs) * F))
    assert len(utts) == 1
    assert utts[0].end_sample == (5 + 10 + 60 + 5) * F + 2400
    # ...nor reset it once a < 0.35 frame started it
    probs = [0.0] * 5 + [0.9] * 15 + [0.2] + [0.4] * 40
    seg = UtteranceSegmenter(ScriptedVAD(probs))
    utts = feed(seg, tone(len(probs) * F))
    assert len(utts) == 1
    assert utts[0].end_sample == 20 * F + 2400


def test_max_length_cut_at_lowest_energy_window():
    rng = np.random.default_rng(1)
    audio = (0.3 * rng.standard_normal(20 * 16000)).astype(np.float32)
    dip = int(13.5 * 16000)
    audio[dip : dip + 640] = 0.001  # 40 ms quiet spot inside the last 3 s
    seg = UtteranceSegmenter(ScriptedVAD([0.9]))
    utts = feed(seg, audio, chunk=1024)
    assert len(utts) == 1
    first = utts[0]
    assert first.cut is True
    assert len(first.audio) <= 15 * 16000
    assert 12 * 16000 <= first.end_sample
    assert dip <= first.end_sample <= dip + 640  # inside the quiet window
    rest = seg.flush()
    assert rest is not None and rest.start_sample == first.end_sample  # continuous, nothing lost
    assert rest.end_sample == len(audio)


def test_lowest_energy_cut_helper():
    buf = np.ones(48000, dtype=np.float32)
    buf[30000:30480] = 0.0
    cut = lowest_energy_cut(buf, 48000)
    assert abs(cut - 30240) <= 160


def test_flush_and_reset():
    seg = UtteranceSegmenter(EnergyGateVAD())
    feed(seg, np.concatenate([silence(10 * F), tone(20 * F)]))
    assert seg.in_speech
    u = seg.flush()
    assert u is not None and u.end_sample == 30 * F
    assert not seg.in_speech
    feed(seg, tone(20 * F))
    seg.reset()
    assert not seg.speech_active and seg.flush() is None


# ── Silero framing ──


class RecordingSession:
    """Stands in for onnxruntime.InferenceSession."""

    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def run(self, _outputs, feeds):
        self.inputs.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        state = feeds["state"] + 1.0
        return [np.array([[0.7]], dtype=np.float32), state]


def test_silero_framing_context_and_state():
    sess = RecordingSession()
    stream = SileroStream(SileroModel(session=sess))
    rng = np.random.default_rng(0)
    frames = [rng.uniform(-0.5, 0.5, F).astype(np.float32) for _ in range(3)]
    probs = [stream.prob(f) for f in frames]
    assert probs == [pytest.approx(0.7)] * 3
    x0, x1, x2 = (i["input"] for i in sess.inputs)
    assert x0.shape == (1, CONTEXT + F) == (1, 576)
    assert np.all(x0[0, :CONTEXT] == 0)  # fresh stream: zero context
    assert np.array_equal(x1[0, :CONTEXT], frames[0][-CONTEXT:])  # 64-sample carry-over
    assert np.array_equal(x2[0, :CONTEXT], frames[1][-CONTEXT:])
    assert np.array_equal(x1[0, CONTEXT:], frames[1])
    # recurrent state is fed back; sr is int64 16000
    assert sess.inputs[0]["state"].shape == (2, 1, 128) and np.all(sess.inputs[0]["state"] == 0)
    assert np.all(sess.inputs[2]["state"] == 2.0)
    assert sess.inputs[0]["sr"].dtype == np.int64 and int(sess.inputs[0]["sr"]) == 16000
    stream.reset()
    stream.prob(frames[2])
    assert np.all(sess.inputs[-1]["state"] == 0) and np.all(sess.inputs[-1]["input"][0, :CONTEXT] == 0)
    with pytest.raises(ValueError):
        stream.prob(np.zeros(480, dtype=np.float32))  # Release 1's 480-sample frames are rejected


def test_state_is_per_stream(silero_path):
    provider = load_vad(silero_path)
    a, b = provider.new_stream(), provider.new_stream()
    rng = np.random.default_rng(3)
    for _ in range(5):
        a.prob(rng.uniform(-0.3, 0.3, F).astype(np.float32))
    assert np.any(a._state != 0)
    assert np.all(b._state == 0) and np.all(b._context == 0)


# ── checksum / loading ──


def test_vendored_model_checksum(silero_path):
    assert verify_model(silero_path)
    assert vadmod.sha256_file(silero_path) == SILERO_SHA256
    assert vadmod._main(["vad", "--verify", str(silero_path)]) == 0


def test_tampered_model_is_refused_and_reported(tmp_path, silero_path, caplog):
    bad = tmp_path / "silero_vad.onnx"
    shutil.copy(silero_path, bad)
    with open(bad, "r+b") as fh:
        fh.seek(1000)
        fh.write(b"\x00\x01\x02")
    assert not verify_model(bad)
    assert vadmod._main(["vad", "--verify", str(bad)]) == 1
    with pytest.raises(VADUnavailable):
        SileroModel(bad)
    with caplog.at_level(logging.CRITICAL, logger="meetpp.agent"):
        provider = load_vad(bad)
    assert provider.kind == "energy"
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert load_vad(tmp_path / "missing.onnx").kind == "energy"


def test_real_silero_loads(silero_path):
    assert load_vad(silero_path).kind == "silero"


# ── real Silero on speech ──


def test_real_silero_segments_speech_with_preroll(speech, silero_path):
    provider = load_vad(silero_path)
    lead = 16000
    audio = np.concatenate([silence(lead), 0.002 * np.random.default_rng(0).standard_normal(8000).astype(np.float32), speech, silence(24000)])
    seg = UtteranceSegmenter(provider.new_stream())
    utts = feed(seg, audio)
    tail = seg.flush()
    if tail:
        utts.append(tail)
    assert utts, "Silero found no speech in the say() sentence"
    # first utterance starts 300 ms before the detected onset, which cannot be
    # before the speech itself starts
    onset_guess = utts[0].start_sample + 4800
    assert onset_guess >= lead + 8000 - 2 * F
    speech_s = sum(u.speech_ms for u in utts) / 1000
    assert speech_s >= 0.6 * len(speech) / 16000


def test_real_silero_ignores_silence_and_low_noise(silero_path):
    provider = load_vad(silero_path)
    rng = np.random.default_rng(1)
    audio = np.concatenate([silence(32000), 0.003 * rng.standard_normal(64000).astype(np.float32)])
    seg = UtteranceSegmenter(provider.new_stream())
    assert feed(seg, audio) == [] and seg.flush() is None
