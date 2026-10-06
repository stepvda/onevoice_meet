from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import numpy as np

from agent.audio_store import AudioStore, decode_audio, encode_opus
from tests.fakes import tone

GB = 1024**3


def test_write_ogg_opus_and_index(tmp_path):
    store = AudioStore(tmp_path, "sess1", min_free_bytes=0)
    audio = tone(40000)  # 2.5 s
    data = store.write("u1", audio, identity="user-a", name="Ann", t_start="2026-10-06T10:00:00.000Z", t_end="2026-10-06T10:00:02.500Z", text="hello")
    assert data is not None and data[:4] == b"OggS" and b"OpusHead" in data[:100]
    path = tmp_path / "sess1" / "audio" / "u1.ogg"
    assert path.read_bytes() == data
    # ≈ 24 kbit/s (+ Ogg overhead): well under 40 kbit/s for 2.5 s
    assert len(data) < 2.5 * 40000 / 8
    decoded = decode_audio(data)
    assert abs(len(decoded) / 16000 - 2.5) < 0.05
    import av, io

    with av.open(io.BytesIO(data)) as c:
        assert c.streams.audio[0].codec_context.name == "opus"
        assert c.streams.audio[0].channels == 1
    line = json.loads((tmp_path / "sess1" / "audio" / "index.jsonl").read_text().strip())
    assert line == {
        "utterance_id": "u1",
        "identity": "user-a",
        "name": "Ann",
        "t_start": "2026-10-06T10:00:00.000Z",
        "t_end": "2026-10-06T10:00:02.500Z",
        "duration_s": 2.5,
        "text": "hello",
    }
    assert store.read("u1") == data and store.read("nope") is None


def test_entries_sorted_and_deduplicated(tmp_path):
    store = AudioStore(tmp_path, "s", min_free_bytes=0)
    store.write("b", tone(8000), identity="x", name="X", t_start="2026-10-06T10:00:05.000Z", t_end="2026-10-06T10:00:05.500Z")
    store.write("a", tone(8000), identity="y", name="Y", t_start="2026-10-06T10:00:01.000Z", t_end="2026-10-06T10:00:01.500Z")
    store.write("a", tone(8000), identity="y", name="Y", t_start="2026-10-06T10:00:01.000Z", t_end="2026-10-06T10:00:01.500Z")
    assert [e["utterance_id"] for e in store.entries()] == ["a", "b"]


def test_disk_guard_refuses_below_3gb(tmp_path, caplog):
    free = [1 * GB]
    store = AudioStore(tmp_path, "s", min_free_bytes=3 * GB, disk_usage=lambda p: SimpleNamespace(free=free[0]))
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        assert store.write("u1", tone(8000), identity="x", name="X", t_start="a", t_end="b") is None
    assert store.ok is False and store.refused == 1
    assert not (tmp_path / "s" / "audio" / "u1.ogg").exists()
    assert any("refusing to store audio" in r.message for r in caplog.records)
    free[0] = 10 * GB
    assert store.write("u2", tone(8000), identity="x", name="X", t_start="a", t_end="b") is not None
    assert store.ok is True


def test_encode_clips_and_handles_short_audio():
    data = encode_opus(np.full(800, 2.0, dtype=np.float32))  # 50 ms, out of range
    assert data[:4] == b"OggS"
    assert len(decode_audio(data)) > 0


def test_close_refuses_writes_and_never_recreates_the_directory(tmp_path):
    store = AudioStore(tmp_path, "s", min_free_bytes=0)
    assert store.write("u1", tone(8000), identity="x", name="X", t_start="a", t_end="b") is not None
    store.close()
    import shutil

    shutil.rmtree(tmp_path / "s")  # meeting-api deleting the session
    assert store.write("u2", tone(8000), identity="x", name="X", t_start="a", t_end="b") is None
    assert not (tmp_path / "s").exists()
    assert store.refused == 0 and store.errors == 0  # not a disk or encode problem
