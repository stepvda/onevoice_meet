import io

import numpy as np
import pytest
import soundfile as sf

from meetpp_speech.audio import AudioDecodeError, decode_to_16k_mono, encode, encode_ogg_opus


def _sine(sr: int, seconds: float, channels: int = 1) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    x = (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    return np.stack([x] * channels, axis=1) if channels > 1 else x


@pytest.mark.parametrize("sr,channels,fmt,subtype", [
    (16000, 1, "WAV", "PCM_16"),
    (44100, 2, "WAV", "PCM_16"),
    (48000, 1, "WAV", "FLOAT"),
    (48000, 1, "OGG", "OPUS"),
    (16000, 1, "OGG", "OPUS"),
    (44100, 1, "OGG", "VORBIS"),
])
def test_decode_to_16k_mono(sr, channels, fmt, subtype):
    buf = io.BytesIO()
    sf.write(buf, _sine(sr, 2.0, channels), sr, format=fmt, subtype=subtype)
    x = decode_to_16k_mono(buf.getvalue())
    assert x.dtype == np.float32 and x.ndim == 1
    assert abs(len(x) / 16000 - 2.0) < 0.05
    assert 0.1 < np.abs(x).max() < 1.0


def test_garbage_and_empty_are_rejected():
    with pytest.raises(AudioDecodeError):
        decode_to_16k_mono(b"")
    with pytest.raises(AudioDecodeError):
        decode_to_16k_mono(b"this is not audio" * 100)


def test_encode_ogg_opus_header_and_roundtrip():
    data = encode_ogg_opus(_sine(24000, 1.0), 24000)
    assert data[:4] == b"OggS" and b"OpusHead" in data[:64]
    x, sr = sf.read(io.BytesIO(data))
    assert abs(len(x) / sr - 1.0) < 0.05


def test_encode_resamples_unsupported_opus_rate():
    data = encode_ogg_opus(_sine(22050, 0.5), 22050)
    assert data[:4] == b"OggS"


def test_encode_falls_back_to_wav(monkeypatch):
    import meetpp_speech.audio as audio

    def boom(*a, **k):
        raise RuntimeError("no opus")

    monkeypatch.setattr(audio, "encode_ogg_opus", boom)
    data, ctype = audio.encode(_sine(24000, 0.5), 24000, "ogg")
    assert ctype == "audio/wav" and data[:4] == b"RIFF"
    data, ctype = encode(_sine(24000, 0.5), 24000, "wav")
    assert ctype == "audio/wav" and data[:4] == b"RIFF"
