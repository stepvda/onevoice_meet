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


def _wav(sr: int, seconds: float, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, _sine(sr, seconds, channels), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_too_long_is_refused_from_the_header_before_decoding(monkeypatch):
    from meetpp_speech.audio import AudioTooLong

    buf = io.BytesIO()
    sf.write(buf, _sine(48000, 6.0), 48000, format="OGG", subtype="OPUS")  # small body, longer audio
    reads = []
    real_read = sf.SoundFile.read
    monkeypatch.setattr(sf.SoundFile, "read", lambda self, *a, **k: reads.append(k) or real_read(self, *a, **k))
    with pytest.raises(AudioTooLong):
        decode_to_16k_mono(buf.getvalue(), max_s=5.0)
    assert reads == []  # nothing decoded
    x = decode_to_16k_mono(buf.getvalue(), max_s=6.5)
    assert abs(len(x) / 16000 - 6.0) < 0.05
    assert reads[-1]["frames"] == int(6.5 * 48000)  # the read itself is capped too


def test_more_than_two_channels_or_odd_rates_are_refused():
    with pytest.raises(AudioDecodeError, match="channels"):
        decode_to_16k_mono(_wav(16000, 0.5, channels=3))
    with pytest.raises(AudioDecodeError, match="sample rate"):
        decode_to_16k_mono(_wav(192000, 0.1))
    assert len(decode_to_16k_mono(_wav(44100, 0.5, channels=2), max_s=1.0)) == 8000
