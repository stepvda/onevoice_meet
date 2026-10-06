import os

import pytest

from meetpp_speech.tts import ESPEAK_PATH_MAX, cap_text, espeak_data_path


def test_cap_text_keeps_short_text():
    assert cap_text("  Item   three.\n", 400) == ("Item three.", False)


def test_cap_text_cuts_at_sentence_then_word():
    s = "First sentence here. " * 30
    out, cut = cap_text(s, 400)
    assert cut and len(out) <= 400 and out.endswith(".")
    out, cut = cap_text("word " * 200, 400)
    assert cut and len(out) <= 400 and out.endswith("word.")


def _fake_espeak(tmp_path, depth: int):
    src = tmp_path / ("d" * 40 if depth else "e")
    for i in range(depth):
        src = src / ("x" * 40)
    src = src / "espeak-ng-data"
    (src / "voices").mkdir(parents=True)
    (src / "phontab").write_bytes(b"p")
    (src / "en_dict").write_bytes(b"e")
    return src


def test_espeak_short_path_used_as_is(tmp_path, monkeypatch):
    src = _fake_espeak(tmp_path, 0)
    if len(str(src)) >= ESPEAK_PATH_MAX:
        pytest.skip("tmp path itself too long")
    monkeypatch.setattr("espeakng_loader.get_data_path", lambda: str(src))
    assert espeak_data_path(tmp_path / "cache") == str(src)


def test_espeak_long_path_is_copied_to_short_dir(tmp_path, monkeypatch):
    src = _fake_espeak(tmp_path, 4)
    assert len(str(src)) >= ESPEAK_PATH_MAX
    monkeypatch.setattr("espeakng_loader.get_data_path", lambda: str(src))
    cache = tmp_path / "c"
    out = espeak_data_path(cache)
    assert out == str(cache / "espeak-ng-data")
    assert os.path.isfile(os.path.join(out, "phontab")) and os.path.isfile(os.path.join(out, "en_dict"))
    # second call reuses the copy
    mtime = os.path.getmtime(os.path.join(out, ".meetpp-source"))
    assert espeak_data_path(cache) == out
    assert os.path.getmtime(os.path.join(out, ".meetpp-source")) == mtime
