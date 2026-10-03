"""One live LiveKit session: subscribe to each speaker's microphone, VAD-gate
utterances, run STT and post segments + presence to meeting-api.

Speaker attribution comes free from the LiveKit identity. Opted-out
participants are unsubscribed, so their audio never reaches STT.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .stt import STTEngine
from .vad import FRAME_SAMPLES, SileroVAD, UtteranceSegmenter

log = logging.getLogger("meetpp.agent")

MEETING_API_URL = os.environ.get("MEETING_API_URL", "http://meeting-api:8080")
INTERNAL_SECRET = os.environ.get("MEETPP_INTERNAL_SECRET", "")
MAX_RETRY_BUFFER = 300  # seconds


@dataclass
class SessionConfig:
    session_id: str
    room: str
    ws_url: str
    token: str
    language: str = "en"
    stt_model: str = "small"
    # Default-deny: only identities that explicitly accepted transcription are
    # subscribed. `opted_out` is kept for transitions to opt-out.
    accepted: list[str] = field(default_factory=list)
    opted_out: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)


def _sign(body: bytes) -> dict:
    ts = str(int(time.time()))
    msg = ts.encode("utf-8") + b"." + body
    sig = hmac.new(INTERNAL_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"X-Meetpp-Timestamp": ts, "X-Meetpp-Signature": sig, "Content-Type": "application/json"}


class AgentSession:
    def __init__(self, cfg: SessionConfig, stt: STTEngine, tts) -> None:
        self.cfg = cfg
        self.stt = stt
        self.tts = tts
        self.room = None
        self.opted_out = set(cfg.opted_out)
        self.accepted = set(cfg.accepted)
        self.paused = False
        self.seq_local = 0
        # Bounded so a sustained STT backlog drops audio instead of growing
        # memory without limit (≈ 128 utterances ≈ several minutes).
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._tasks: list[asyncio.Task] = []
        self._segmenters: dict[str, UtteranceSegmenter] = {}
        self._buffers: dict[str, np.ndarray] = {}
        self._names: dict[str, str] = {}
        self._retry: deque = deque()
        self.backlog_s = 0.0
        self._closing = False
        self._started_at = time.monotonic()

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        from livekit import rtc

        self._rtc = rtc
        self.room = rtc.Room()
        self.room.on("track_published", self._on_track_published)
        self.room.on("track_subscribed", self._on_track_subscribed)
        self.room.on("participant_connected", self._on_participant_connected)
        self.room.on("participant_disconnected", self._on_participant_disconnected)
        self.room.on("disconnected", self._on_disconnected)
        opts = rtc.RoomOptions(auto_subscribe=False)
        await self.room.connect(self.cfg.ws_url, self.cfg.token, options=opts)
        for participant in self.room.remote_participants.values():
            for publication in participant.track_publications.values():
                self._maybe_subscribe(publication, participant)
        self._tasks.append(asyncio.create_task(self._stt_worker()))
        self._tasks.append(asyncio.create_task(self._retry_worker()))
        self._tasks.append(asyncio.create_task(self._resubscribe_worker()))
        log.info("agent joined room=%s session=%s", self.cfg.room, self.cfg.session_id)

    async def stop(self) -> None:
        self._closing = True
        # Flush any pending utterances first.
        for identity, seg in list(self._segmenters.items()):
            audio = seg.flush()
            if audio is not None and len(audio):
                self._enqueue(identity, audio)
        await self._drain_queue(timeout=20)
        for task in self._tasks:
            task.cancel()
        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def drain(self) -> None:
        await self._drain_queue(timeout=20)

    async def _drain_queue(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self.queue.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    # ── events ─────────────────────────────────────────────────────────
    def _on_track_published(self, publication, participant) -> None:
        self._maybe_subscribe(publication, participant)

    def _maybe_subscribe(self, publication, participant, announce: bool = True) -> None:
        try:
            source = publication.source
            is_mic = source == self._rtc.TrackSource.SOURCE_MICROPHONE
        except Exception:  # noqa: BLE001
            is_mic = True
        identity = participant.identity
        # Default-deny: subscribe only when the participant accepted AND has
        # not since opted out.
        if (
            not is_mic
            or identity not in self.accepted
            or identity in self.opted_out
            or self._is_bot(identity)
        ):
            # Presence is still reported (participants who opt out remain
            # "present but not transcribed").
            if announce and participant.kind is not None:
                self._post_presence(identity, participant.name or identity, participant.kind, "connected")
            return
        self._names[identity] = participant.name or identity
        try:
            publication.set_subscribed(True)
        except Exception:  # noqa: BLE001
            log.debug("subscribe failed for %s", identity)
        if announce:
            self._post_presence(identity, participant.name or identity, participant.kind, "connected")

    async def _resubscribe_worker(self) -> None:
        """Every 20 s re-assert subscriptions for accepted speakers. Guards
        against a silent subscription loss that would stop transcription."""
        while True:
            await asyncio.sleep(20)
            if self.room is None or self.paused or self._closing:
                continue
            try:
                for participant in self.room.remote_participants.values():
                    if participant.identity in self.accepted and participant.identity not in self.opted_out:
                        for pub in participant.track_publications.values():
                            self._maybe_subscribe(pub, participant, announce=False)
            except Exception:  # noqa: BLE001
                log.debug("resubscribe pass failed", exc_info=True)

    def _on_track_subscribed(self, track, publication, participant) -> None:
        if track.kind != self._rtc.TrackKind.KIND_AUDIO:
            return
        identity = participant.identity
        if identity not in self.accepted or identity in self.opted_out:
            return
        self._tasks.append(asyncio.create_task(self._consume_track(track, identity)))

    def _on_participant_connected(self, participant) -> None:
        self._post_presence(participant.identity, participant.name or participant.identity, participant.kind, "connected")

    def _on_participant_disconnected(self, participant) -> None:
        self._post_presence(participant.identity, participant.name or participant.identity, participant.kind, "disconnected")

    def _on_disconnected(self, *args) -> None:
        log.warning("agent disconnected room=%s", self.cfg.room)

    def _is_bot(self, identity: str) -> bool:
        return identity.startswith(("meetpp-", "playback", "composite-", "viewer-", "egress-", "ingress-"))

    # ── audio ──────────────────────────────────────────────────────────
    async def _consume_track(self, track, identity: str) -> None:
        try:
            stream = self._rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            async for event in stream:
                if self.paused or self._closing:
                    continue
                frame = event.frame
                data = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
                buf = np.concatenate([self._buffers.get(identity, np.zeros(0, dtype=np.float32)), data])
                seg = self._segmenters.get(identity)
                if seg is None:
                    seg = UtteranceSegmenter(SileroVAD())
                    self._segmenters[identity] = seg
                while len(buf) >= FRAME_SAMPLES:
                    chunk, buf = buf[:FRAME_SAMPLES], buf[FRAME_SAMPLES:]
                    # Silero VAD runs ONNX inference; keep it off the event loop.
                    utterance = await asyncio.to_thread(seg.push, chunk)
                    if utterance is not None and len(utterance):
                        self._enqueue(identity, utterance)
                self._buffers[identity] = buf
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("audio consume failed for %s: %s", identity, exc)

    def _enqueue(self, identity: str, audio: np.ndarray) -> None:
        duration = len(audio) / 16000.0
        now = datetime.now(timezone.utc)
        self.seq_local += 1
        item = {
            "identity": identity,
            "name": self._names.get(identity, identity),
            "audio": audio,
            "t_end": now.isoformat(),
            "t_start": (now.timestamp() - duration),
            "seq_local": self.seq_local,
        }
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            # Backlog: drop the oldest buffered utterance and keep the newest.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

    async def _stt_worker(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                self.backlog_s = self.queue.qsize() * 3.0
                prompt = ", ".join(self.cfg.vocabulary[:40])
                result = await self.stt.transcribe(item["audio"], self.cfg.language, prompt)
                self.backlog_s = self.queue.qsize() * 3.0
                if not result.text:
                    continue
                from datetime import datetime as _dt

                t_end = _dt.fromisoformat(item["t_end"])
                payload = {
                    "seq_local": item["seq_local"],
                    "identity": item["identity"],
                    "name": item["name"],
                    "t_start": _dt.fromtimestamp(item["t_start"], tz=timezone.utc).isoformat(),
                    "t_end": t_end.isoformat(),
                    "text": result.text,
                    "lang": result.language or self.cfg.language,
                    "avg_logprob": result.avg_logprob,
                    "no_speech_prob": result.no_speech_prob,
                }
                await self._post(f"/v1/internal/meetpp/sessions/{self.cfg.session_id}/segments", [payload])
                await self._post_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("stt worker error: %s", exc)

    async def _retry_worker(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._flush_retry()

    async def _flush_retry(self) -> None:
        if not self._retry:
            return
        while self._retry:
            path, body = self._retry[0]
            ok = await self._post(path, body, retry=False)
            if not ok:
                break
            self._retry.popleft()

    # ── HTTP ───────────────────────────────────────────────────────────
    async def _post(self, path: str, body, retry: bool = True) -> bool:
        import httpx

        raw = json.dumps(body).encode("utf-8")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{MEETING_API_URL}/api{path}", content=raw, headers=_sign(raw))
                if r.status_code < 400:
                    return True
                log.warning("agent post %s -> %s", path, r.status_code)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent post %s failed: %s", path, exc)
        if retry:
            self._retry.append((path, body))
            while len(self._retry) > 500:
                self._retry.popleft()
        return False

    def _post_presence(self, identity: str, name: str, kind, event: str) -> None:
        try:
            kind_name = getattr(kind, "name", str(kind))
        except Exception:  # noqa: BLE001
            kind_name = "standard"
        body = {
            "identity": identity,
            "name": name,
            "kind": "standard" if "STANDARD" in str(kind_name).upper() or kind_name == "0" else str(kind_name).lower(),
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        asyncio.create_task(self._post(f"/v1/internal/meetpp/sessions/{self.cfg.session_id}/presence", body))

    async def _post_status(self) -> None:
        await self._post(
            f"/v1/internal/meetpp/sessions/{self.cfg.session_id}/agent-status",
            {"status": "behind" if self.backlog_s > 30 else "listening", "backlog_s": self.backlog_s},
            retry=False,
        )

    # ── controls ───────────────────────────────────────────────────────
    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def set_accepted(self, identities: list[str]) -> None:
        """Update the consent allow-list and subscribe/unsubscribe accordingly."""
        new = set(identities)
        added = new - self.accepted
        removed = self.accepted - new
        self.accepted = new
        if self.room is None:
            return
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                try:
                    if participant.identity in added and participant.identity not in self.opted_out:
                        self._maybe_subscribe(pub, participant)
                    elif participant.identity in removed:
                        pub.set_subscribed(False)
                except Exception:  # noqa: BLE001
                    pass

    def set_opted_out(self, identities: list[str]) -> None:
        new = set(identities)
        for identity in new - self.opted_out:
            # Unsubscribe from the newly opted-out participant's mic.
            if self.room is not None:
                for p in self.room.remote_participants.values():
                    if p.identity != identity:
                        continue
                    for pub in p.track_publications.values():
                        try:
                            pub.set_subscribed(False)
                        except Exception:  # noqa: BLE001
                            pass
        self.opted_out = new

    def health(self) -> dict:
        return {
            "session_id": self.cfg.session_id,
            "room": self.cfg.room,
            "backlog_s": self.backlog_s,
            "paused": self.paused,
            "queue": self.queue.qsize(),
            "rtf": self.stt.rtf,
        }
