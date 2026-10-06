"""Test doubles: scripted VAD, fake LiveKit room, fake STT engine, a
recording meeting-api, and helpers."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from agent.poster import InternalApi
from agent.session import Services
from agent.stt import STTResult
from agent.vad import FRAME, VADProvider
from agent.worker import STTWorker

SECRET = "test-secret"


# ── audio helpers ──
def tone(n: int, amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / 16000)).astype(np.float32)


def silence(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


class EnergyGateVAD:
    """Deterministic VAD for segmenter tests: loud frame → 0.9, else 0.02."""

    kind = "test"

    def __init__(self, threshold: float = 0.05) -> None:
        self.threshold = threshold
        self.frames = 0

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        assert len(frame) == FRAME
        self.frames += 1
        return 0.9 if float(np.sqrt(np.mean(frame**2))) > self.threshold else 0.02


class ScriptedVAD:
    """Returns the given probabilities frame by frame (then the last one)."""

    kind = "test"

    def __init__(self, probs: list[float]) -> None:
        self.probs = list(probs)
        self.i = 0

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        p = self.probs[min(self.i, len(self.probs) - 1)]
        self.i += 1
        return p


async def sync_offload(fn, *args):
    return fn(*args)


# ── fake LiveKit ──
class FakeTrack:
    kind = 1  # audio

    def __init__(self, sid: str) -> None:
        self.sid = sid


class FakePub:
    def __init__(self, sid: str, source: int = 2) -> None:
        self.sid = sid
        self.source = source
        self.subscribed = False
        self.track: FakeTrack | None = None
        self.calls: list[bool] = []

    def set_subscribed(self, value: bool) -> None:
        self.calls.append(value)
        self.subscribed = value
        if not value:
            self.track = None


class FakeParticipant:
    def __init__(self, identity: str, name: str | None = None, kind: int = 0) -> None:
        self.identity = identity
        self.name = name or identity.title()
        self.kind = kind
        self.track_publications: dict[str, FakePub] = {}

    def add(self, pub: FakePub) -> FakePub:
        self.track_publications[pub.sid] = pub
        return pub


class FakeRoom:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.remote_participants: dict[str, FakeParticipant] = {}
        self.connected_with: tuple[str, str] | None = None
        self.disconnected = False
        self.fail_connect = False

    def on(self, event: str, cb) -> None:
        self.handlers.setdefault(event, []).append(cb)

    def emit(self, event: str, *args) -> None:
        for cb in list(self.handlers.get(event, [])):
            cb(*args)

    async def connect(self, url: str, token: str, options=None) -> None:
        if self.fail_connect:
            raise ConnectionError("boom")
        self.connected_with = (url, token)

    async def disconnect(self) -> None:
        self.disconnected = True

    def add(self, p: FakeParticipant) -> FakeParticipant:
        self.remote_participants[p.identity] = p
        return p

    # simulate the SFU completing a subscription
    def subscribe_complete(self, p: FakeParticipant, pub: FakePub) -> None:
        pub.subscribed = True
        pub.track = FakeTrack(pub.sid)
        self.emit("track_subscribed", pub.track, pub, p)


async def silent_stream(track):
    """Endless 10 ms silent frames (yields control between frames)."""
    while True:
        await asyncio.sleep(0.01)
        yield np.zeros(160, dtype=np.float32)


# ── fake STT ──
class FakeEngine:
    primary = "small"
    degrade = "base"

    def __init__(self, text: str = "hello world") -> None:
        self.ready = threading.Event()
        self.ready.set()
        self.text = text
        self.calls: list[tuple[str, str | None]] = []

    def model_for(self, degraded: bool) -> str:
        return self.degrade if degraded else self.primary

    def transcribe(self, audio, prompt, degraded=False) -> STTResult:
        name = self.model_for(degraded)
        self.calls.append((name, prompt))
        return STTResult(text=self.text, model=name, audio_s=len(audio) / 16000, elapsed_s=0.01, avg_logprob=-0.2, no_speech_prob=0.01)


# ── recording meeting-api ──
@dataclass
class Recorder:
    requests: list[tuple[str, dict]] = field(default_factory=list)
    responses: dict[str, list] = field(default_factory=dict)  # endpoint → list of (status, json)

    def handler(self, request: httpx.Request) -> httpx.Response:
        ts = request.headers["X-Meetpp-Timestamp"]
        expected = hmac.new(SECRET.encode(), ts.encode() + b"." + request.content, hashlib.sha256).hexdigest()
        assert request.headers["X-Meetpp-Signature"] == expected, "bad HMAC"
        endpoint = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.requests.append((endpoint, body))
        queue = self.responses.get(endpoint)
        if queue:
            status, payload = queue.pop(0)
            return httpx.Response(status, json=payload)
        if endpoint == "agent-token":
            return httpx.Response(200, json={"token": "fresh-token", "ws_url": "ws://lk"})
        return httpx.Response(200, json={"ok": True})

    def of(self, endpoint: str) -> list[dict]:
        return [b for e, b in self.requests if e == endpoint]

    def items(self, key: str) -> list[dict]:
        out = []
        for e, b in self.requests:
            if e == "segments":
                out.extend(b.get(key, []))
        return out


def make_services(tmp_path: Path, recorder: Recorder, *, tier2=None, engine=None, rooms: list | None = None, stream_factory=silent_stream, min_free_bytes: int = 0, clock=None) -> Services:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    rooms = rooms if rooms is not None else []

    def room_factory():
        room = FakeRoom()
        rooms.append(room)
        return room

    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return Services(
        api=InternalApi(client, "http://meeting-api", SECRET),
        worker=STTWorker(engine or FakeEngine()),
        vad=VADProvider(None),
        tier2=tier2,
        data_dir=tmp_path,
        min_free_bytes=min_free_bytes,
        room_factory=room_factory,
        stream_factory=stream_factory,
        offload=sync_offload,
        ws_url_override="",
        reconnect_backoff=(0.01,),
        **kwargs,
    )


async def settle(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)
