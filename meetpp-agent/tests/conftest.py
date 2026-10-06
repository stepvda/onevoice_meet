from __future__ import annotations

import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEECH_TEXT = (
    "Good morning everyone. The board approves the budget for next year. "
    "Maria will send the minutes to all members on Friday."
)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_SLOW") == "1":
        return
    skip = pytest.mark.skip(reason="slow test: set RUN_SLOW=1 (needs Whisper models)")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


@pytest.fixture(scope="session")
def speech_wav(tmp_path_factory) -> Path:
    """An English sentence spoken by macOS `say`, as 16 kHz mono WAV
    (generated at test time; nothing binary is committed)."""
    if not shutil.which("say"):
        pytest.skip("macOS `say` not available")
    d = tmp_path_factory.mktemp("speech")
    aiff, wav = d / "speech.aiff", d / "speech.wav"
    subprocess.run(["say", "-o", str(aiff), SPEECH_TEXT], check=True)
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(wav)]
    else:
        cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)]
    subprocess.run(cmd, check=True)
    return wav


@pytest.fixture(scope="session")
def speech(speech_wav) -> np.ndarray:
    return read_wav(speech_wav)


@pytest.fixture
def silero_path() -> Path:
    return ROOT / "models" / "silero_vad.onnx"
