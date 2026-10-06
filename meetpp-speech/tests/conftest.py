from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meetpp_speech.audio import TARGET_SR, resample  # noqa: E402
from meetpp_speech.config import Settings  # noqa: E402

TEST_SECRET = "test-secret-0123456789abcdef0123456789abcdef"

LONG_TEXT = (
    "Good evening everyone. Welcome to the board meeting of the association. "
    "Tonight we will review the minutes of the previous meeting, approve the budget "
    "for next year, and discuss the renovation of the community hall. The treasurer "
    "will present the financial report, and then we will vote on the proposal to "
    "increase the membership fee by ten euros."
)
SHORT_TEXT = "The board approved the budget for the community hall renovation."


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs the real models (starts or uses a running service)")


# --------------------------------------------------------------------------- audio fixtures
def _say(text: str, path: Path) -> None:
    if not shutil.which("say"):
        pytest.skip("macOS `say` not available")
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    args = ["say", "-o", str(path)]
    if "Samantha" in voices:
        args[1:1] = ["-v", "Samantha"]
    subprocess.run(args + [text], check=True)


def _to_formats(aiff: Path, out_dir: Path, stem: str) -> dict[str, bytes]:
    """AIFF from `say` -> 16 kHz WAV and Ogg/Opus, with soundfile only (no ffmpeg)."""
    x, sr = sf.read(aiff, dtype="float32", always_2d=True)
    mono16 = resample(x.mean(axis=1), sr, TARGET_SR)
    out: dict[str, bytes] = {}
    buf = io.BytesIO()
    sf.write(buf, mono16, TARGET_SR, format="WAV", subtype="PCM_16")
    out["wav"] = buf.getvalue()
    buf = io.BytesIO()
    sf.write(buf, resample(x.mean(axis=1), sr, 48000), 48000, format="OGG", subtype="OPUS")
    out["ogg"] = buf.getvalue()
    out["duration_s"] = len(mono16) / TARGET_SR  # type: ignore[assignment]
    # Agent-style Opus (libopus 24 kbit/s via ffmpeg) when ffmpeg exists on the dev box;
    # the service itself never uses ffmpeg.
    if shutil.which("ffmpeg"):
        dst = out_dir / f"{stem}.ffmpeg.ogg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ac", "1", "-ar", "48000",
                        "-c:a", "libopus", "-b:a", "24k", str(dst)], check=True)
        out["ogg_ffmpeg"] = dst.read_bytes()
    return out


@pytest.fixture(scope="session")
def speech(tmp_path_factory) -> dict[str, dict]:
    d = tmp_path_factory.mktemp("speech")
    res = {}
    for stem, text in (("long", LONG_TEXT), ("short", SHORT_TEXT)):
        aiff = d / f"{stem}.aiff"
        _say(text, aiff)
        res[stem] = _to_formats(aiff, d, stem)
    return res


# --------------------------------------------------------------------------- fake engines
class FakeTranscriber:
    loaded = True

    def __init__(self, text: str = "hello world", delay: float = 0.0):
        self.text = text
        self.delay = delay
        self.calls: list[tuple[float, str, str | None]] = []

    def supports_language(self, lang: str) -> bool:
        return lang in {"en", "fr", "nl", "de"}

    def transcribe(self, audio: np.ndarray, language: str = "en", prompt: str | None = None) -> dict:
        self.calls.append((len(audio) / TARGET_SR, language, prompt))
        if self.delay:
            time.sleep(self.delay)
        return {"text": self.text, "avg_logprob": -0.2, "no_speech_prob": 0.01,
                "compression_ratio": 1.4, "temperature": 0.0, "segments": 1}


class FakeSynthesizer:
    loaded = True
    voices = {"am_michael", "af_heart", "bm_george"}

    def available(self) -> bool:
        return True

    def synthesize(self, text: str, voice: str = "am_michael", speed: float = 1.0):
        sr = 24000
        t = np.arange(int(sr * 0.8)) / sr
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr


def make_settings(**kw) -> Settings:
    base = dict(secret=TEST_SECRET.encode())
    base.update(kw)
    return Settings(**base)


def tone_wav(seconds: float, sr: int = 16000) -> bytes:
    t = np.arange(int(sr * seconds)) / sr
    buf = io.BytesIO()
    sf.write(buf, (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# --------------------------------------------------------------------------- live service
def _health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def live(tmp_path_factory):
    """A running service with the real models.

    Uses MEETPP_SPEECH_TEST_URL (+ MEETPP_SPEECH_TEST_SECRET) when set, otherwise
    starts ./run.sh on a free port with a throw-away env file and stops it after.
    """
    url = os.environ.get("MEETPP_SPEECH_TEST_URL")
    if url:
        secret = os.environ.get("MEETPP_SPEECH_TEST_SECRET", TEST_SECRET)
        if not _health(url):
            pytest.fail(f"no meetpp-speech at {url}")
        yield {"url": url.rstrip("/"), "secret": secret, "proc": None}
        return
    if not (ROOT / ".venv" / "bin" / "python").exists():
        pytest.skip("no .venv; run ./install.sh --dev")
    port = _free_port()
    env_file = tmp_path_factory.mktemp("env") / "env"
    env_file.write_text(f"MEETPP_SPEECH_SECRET={TEST_SECRET}\nMEETPP_SPEECH_BIND=127.0.0.1\n"
                        f"MEETPP_SPEECH_PORT={port}\n")
    env_file.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEETPP_", "KOKORO_"))}
    env["MEETPP_SPEECH_ENV_FILE"] = str(env_file)
    log_path = tmp_path_factory.mktemp("log") / "service.log"
    log = open(log_path, "wb")
    proc = subprocess.Popen(["/bin/bash", str(ROOT / "run.sh")], env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    t0 = time.time()
    try:
        while time.time() - t0 < 600:
            if proc.poll() is not None:
                pytest.fail(f"service exited {proc.returncode}:\n{log_path.read_text()[-3000:]}")
            h = _health(url)
            if h and h.get("ok"):
                break
            time.sleep(0.5)
        else:
            pytest.fail("service did not become healthy")
        yield {"url": url, "secret": TEST_SECRET, "proc": proc, "startup_s": time.time() - t0, "log": log_path}
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()
