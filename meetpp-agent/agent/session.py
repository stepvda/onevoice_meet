"""One live Meet++ session in a LiveKit room (FDD §7.3–7.6).

* Joins hidden/subscribe-only with ``auto_subscribe=False`` and subscribes
  only to the microphone of STANDARD participants listed in
  ``accepted_identities`` (default-deny). The list is re-sent by meeting-api
  every 20 s; every PATCH reconciles subscriptions and consumers.
* One supervised ``TrackConsumer`` per track SID (never two).
* Room ``disconnected`` → fresh token from meeting-api, reconnect with
  backoff, gap marker for the outage.
* Liveness: an accepted speaker that LiveKit reports as active for ≥ 10 s of
  a 20 s window without any VAD activity gets its consumer restarted.
* Delivery through the ordered ``Poster``; agent-status every 5 s and on
  change; ``MEETPP_STT`` summary every 60 s.
* Tier 2: each stored utterance is refined near-live; ``finalize`` runs the
  final pass over the whole audio store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config
from .audio_store import AudioStore
from .consumer import TrackConsumer, livekit_pcm
from .metrics import Rolling, iso, r
from .poster import InternalApi, Poster
from .stt import STTResult, build_prompt, tail_text
from .tier2 import (
    CONCURRENCY as TIER2_CONCURRENCY,
    FINAL_TIMEOUT_S,
    NEAR_LIVE_BUDGET_S,
    Tier2Busy,
    Tier2Client,
    Tier2Error,
    build_tier2_prompt,
    degenerate_reason,
)
from .vad import VADProvider
from .worker import STTWorker, WorkItem

log = logging.getLogger("meetpp.agent")

KIND_STANDARD = 0  # rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
SOURCE_MICROPHONE = 2  # rtc.TrackSource.SOURCE_MICROPHONE

TICK_S = 1.0
STATUS_INTERVAL_S = 5.0
SUMMARY_INTERVAL_S = 60.0
BEHIND_S = 10.0
LIVENESS_WINDOW_S = 20.0
LIVENESS_SPEAKING_S = 10.0
RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)
TIER2_CONTEXT_S = 30.0
SDK_RECONNECT_GAP_S = 3.0
FINAL_PASS_MAX_S = 15 * 60.0
FINAL_BUSY_RETRIES = 60
DRAIN_STT_FINAL_S = 180.0
DRAIN_STT_CLOSE_S = 20.0


def _is_standard(p) -> bool:
    k = getattr(p, "kind", KIND_STANDARD)
    return k == KIND_STANDARD or str(k).upper().endswith("STANDARD")


def _is_mic(pub) -> bool:
    s = getattr(pub, "source", None)
    return s == SOURCE_MICROPHONE or str(s).upper().endswith("MICROPHONE")


def _default_room_factory():
    from livekit import rtc

    return rtc.Room()


def _room_options():
    try:
        from livekit import rtc

        return rtc.RoomOptions(auto_subscribe=False)
    except Exception:  # noqa: BLE001
        return None


@dataclass
class Services:
    """Process-wide collaborators shared by all sessions."""

    api: InternalApi
    worker: STTWorker
    vad: VADProvider
    tier2: Tier2Client | None
    data_dir: Path
    min_free_bytes: int = config.MIN_FREE_BYTES
    room_factory: Callable[[], Any] = _default_room_factory
    stream_factory: Callable = livekit_pcm
    offload: Callable | None = None  # VAD offload (default asyncio.to_thread)
    ws_url_override: str = config.LIVEKIT_WS_URL_INTERNAL
    rtf: Rolling = field(default_factory=Rolling)
    clock: Callable[[], float] = time.monotonic
    reconnect_backoff: tuple[float, ...] = RECONNECT_BACKOFF_S


class AgentSession:
    def __init__(
        self,
        sid: str,
        room_name: str,
        ws_url: str,
        token: str,
        *,
        services: Services,
        glossary: str = "",
        accepted: list[str] | tuple[str, ...] = (),
        paused: bool = False,
    ) -> None:
        self.sid = sid
        self.room_name = room_name
        self.ws_url = ws_url
        self.token = token
        self.services = services
        self.glossary = glossary or ""
        self.accepted: set[str] = set(accepted)
        self.paused = paused
        self.identity = f"meetpp-scribe-{sid}"
        self._clock = services.clock

        self.poster = Poster(services.api, sid)
        self.store = AudioStore(services.data_dir, sid, min_free_bytes=services.min_free_bytes)

        self._room = None
        self.connected = False
        self.reconnecting = False
        self.gave_up = False
        self.capture_stopped = False
        self.closed = False
        self.final_pass: str | None = None

        self.consumers: dict[str, TrackConsumer] = {}
        self.present: dict[str, str] = {}  # STANDARD participants: identity → name
        self.speaker_ok: dict[str, bool] = {}
        self._speaking: dict[str, deque] = {}
        self._speaking_now: set[str] = set()
        self._resumed_at = self._clock()

        self.transcript = ""  # rolling tier-1 text for the STT prompt
        self._texts: dict[str, dict] = {}  # utterance_id → {t1, t2, t_end}
        self._timeline: deque[tuple[float, str]] = deque(maxlen=400)

        # metrics
        self.utterances = 0
        self.decoded = 0
        self.dropped: Counter = Counter()
        self.tier2_ok = 0
        self.tier2_fail = 0
        self.tier2_kept_t1 = 0
        self.reconnects = 0
        self.liveness_restarts = 0
        self.rtf = Rolling()
        self.last_segment_wall: float | None = None
        self._stt_outstanding = 0

        self._tasks: set[asyncio.Task] = set()
        self._store_tasks: set[asyncio.Task] = set()
        self._refine_tasks: set[asyncio.Task] = set()
        self._reconnect_task: asyncio.Task | None = None
        self._final_task: asyncio.Task | None = None
        self._status_task: asyncio.Task | None = None
        self._status_lock = asyncio.Lock()
        self._status_frozen = False
        self._last_status_sig: str | None = None
        self._last_status_at = 0.0
        self._last_summary = self._clock()
        self._sdk_outage_from: float | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self._connect(self.ws_url, self.token)
        self.poster.start()
        self._spawn(self._tick_loop())
        log.info(
            "MEETPP_SESSION start sid=%s room=%s accepted=%d paused=%s vad=%s tier2=%s",
            self.sid,
            self.room_name,
            len(self.accepted),
            self.paused,
            self.services.vad.kind,
            self.tier2_state(),
        )

    async def _connect(self, ws_url: str, token: str) -> None:
        room = self.services.room_factory()
        self._bind(room)
        url = self.services.ws_url_override or ws_url
        opts = _room_options()
        if opts is not None:
            await room.connect(url, token, options=opts)
        else:
            await room.connect(url, token)
        self._room = room
        self.connected = True
        self._sync_room()

    def _bind(self, room) -> None:
        def guard(fn):
            def handler(*args):
                if room is not self._room and self._room is not None:
                    return  # event from a previous (dead) room
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    log.exception("MEETPP_SESSION sid=%s event handler %s failed", self.sid, fn.__name__)

            return handler

        room.on("participant_connected", guard(self._on_participant_connected))
        room.on("participant_disconnected", guard(self._on_participant_disconnected))
        room.on("track_published", guard(self._on_track_published))
        room.on("track_unpublished", guard(self._on_track_unpublished))
        room.on("track_subscribed", guard(self._on_track_subscribed))
        room.on("track_unsubscribed", guard(self._on_track_unsubscribed))
        room.on("active_speakers_changed", guard(self._on_active_speakers))
        room.on("reconnecting", guard(self._on_reconnecting))
        room.on("reconnected", guard(self._on_reconnected))
        room.on("disconnected", lambda *a: self._on_disconnected(room, *a))

    def _spawn(self, coro, bucket: set | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro)
        bucket = self._tasks if bucket is None else bucket
        bucket.add(task)
        task.add_done_callback(bucket.discard)
        return task

    # ── room state ────────────────────────────────────────────────────────
    def _sync_room(self) -> None:
        room = self._room
        if room is None:
            return
        seen = set()
        for p in list(room.remote_participants.values()):
            if _is_standard(p) and p.identity != self.identity:
                seen.add(p.identity)
                self._presence_connected(p)
            for pub in list(p.track_publications.values()):
                self._reconcile_pub(pub, p)
        for identity in [i for i in self.present if i not in seen]:
            self._presence_disconnected(identity)

    def _eligible(self, pub, p) -> bool:
        return (
            not self.capture_stopped
            and not self.closed
            and p.identity != self.identity
            and _is_standard(p)
            and _is_mic(pub)
            and p.identity in self.accepted
        )

    def _reconcile_pub(self, pub, p) -> None:
        if not (_is_standard(p) and _is_mic(pub)):
            return
        if self._eligible(pub, p):
            if not getattr(pub, "subscribed", False):
                pub.set_subscribed(True)
            elif getattr(pub, "track", None) is not None and pub.sid not in self.consumers:
                self._start_consumer(pub.track, pub, p)
        else:
            if pub.sid in self.consumers:
                self._stop_consumer(pub.sid, flush=False)
            if getattr(pub, "subscribed", False):
                pub.set_subscribed(False)

    def reconcile(self) -> None:
        if self._room is not None and self.connected:
            for p in list(self._room.remote_participants.values()):
                for pub in list(p.track_publications.values()):
                    self._reconcile_pub(pub, p)

    # ── events ────────────────────────────────────────────────────────────
    def _on_participant_connected(self, p) -> None:
        if _is_standard(p) and p.identity != self.identity:
            self._presence_connected(p)

    def _on_participant_disconnected(self, p) -> None:
        for sid, c in list(self.consumers.items()):
            if c.identity == p.identity:
                self._stop_consumer(sid, flush=True)
        self._speaking_now.discard(p.identity)
        if p.identity in self.present:
            self._presence_disconnected(p.identity)

    def _on_track_published(self, pub, p) -> None:
        self._reconcile_pub(pub, p)

    def _on_track_unpublished(self, pub, p) -> None:
        self._stop_consumer(pub.sid, flush=True)

    def _on_track_subscribed(self, track, pub, p) -> None:
        if self._eligible(pub, p):
            self._start_consumer(track, pub, p)
        elif _is_mic(pub) or getattr(track, "kind", None) == 1:
            # Never consume audio without consent.
            pub.set_subscribed(False)

    def _on_track_unsubscribed(self, track, pub, p) -> None:
        self._stop_consumer(pub.sid, flush=True)

    def _on_active_speakers(self, speakers) -> None:
        now = self._clock()
        current = {getattr(p, "identity", None) for p in speakers or []} - {None}
        for ident in current - self._speaking_now:
            self._speaking.setdefault(ident, deque(maxlen=200)).append([now, None])
        for ident in self._speaking_now - current:
            iv = self._speaking.get(ident)
            if iv and iv[-1][1] is None:
                iv[-1][1] = now
        self._speaking_now = current

    def _on_reconnecting(self, *args) -> None:
        self.reconnecting = True
        self._sdk_outage_from = time.time()
        log.warning("MEETPP_SESSION sid=%s LiveKit connection interrupted; SDK reconnecting", self.sid)

    def _on_reconnected(self, *args) -> None:
        self.reconnecting = False
        started = self._sdk_outage_from
        self._sdk_outage_from = None
        now = time.time()
        if started is not None:
            log.warning("MEETPP_SESSION sid=%s LiveKit connection resumed after %.1fs", self.sid, now - started)
            if now - started >= SDK_RECONNECT_GAP_S:
                self.poster.gap({"t_from": iso(started), "t_to": iso(now), "reason": "agent_reconnect"})
        self._sync_room()

    def _on_disconnected(self, room, *args) -> None:
        if room is not self._room:
            return
        self.connected = False
        if self.closed or self.capture_stopped:
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            reason = args[0] if args else None
            self._reconnect_task = self._spawn(self._reconnect(reason))

    # ── consumers ─────────────────────────────────────────────────────────
    def _start_consumer(self, track, pub, p) -> None:
        if pub.sid in self.consumers:
            return  # never two consumers per track
        c = TrackConsumer(
            track_sid=pub.sid,
            identity=p.identity,
            name=p.name or p.identity,
            track=track,
            stream_factory=self.services.stream_factory,
            vad_factory=self.services.vad.new_stream,
            on_utterance=self._on_utterance,
            on_restart=self._on_consumer_restart,
            paused=lambda: self.paused,
            clock=self._clock,
            offload=self.services.offload,
        )
        self.consumers[pub.sid] = c
        self.speaker_ok.setdefault(p.identity, True)
        c.start()
        log.info("MEETPP_CONSUMER start sid=%s track=%s identity=%s", self.sid, pub.sid, p.identity)

    def _stop_consumer(self, track_sid: str, flush: bool) -> None:
        c = self.consumers.pop(track_sid, None)
        if c is not None:
            log.info("MEETPP_CONSUMER stop sid=%s track=%s identity=%s flush=%s", self.sid, track_sid, c.identity, flush)
            self._spawn(c.stop(flush=flush))

    async def _stop_all_consumers(self, flush: bool) -> None:
        consumers = list(self.consumers.values())
        self.consumers.clear()
        await asyncio.gather(*(c.stop(flush=flush) for c in consumers), return_exceptions=True)

    def _on_consumer_restart(self, c: TrackConsumer, reason: str, delay: float, at_wall: float) -> None:
        if reason == "liveness":
            return  # the liveness check posts its own gap
        self.poster.gap(
            {
                "identity": c.identity,
                "name": c.name,
                "t_from": iso(at_wall),
                "t_to": iso(at_wall + max(delay, 1.0)),
                "reason": "consumer_restart",
            }
        )

    # ── utterances → STT ──────────────────────────────────────────────────
    def _on_utterance(self, c: TrackConsumer, utt, t_start: float, t_end: float) -> None:
        self.utterances += 1
        self.speaker_ok[c.identity] = True
        self._stt_outstanding += 1
        self.services.worker.submit(
            WorkItem(
                session_id=self.sid,
                utterance_id=uuid.uuid4().hex,
                identity=c.identity,
                name=c.name,
                audio=utt.audio,
                t_start=t_start,
                t_end=t_end,
                on_result=self._on_stt_result,
                on_drop=self._on_stt_drop,
                prompt_fn=self._prompt,
            )
        )

    def _prompt(self) -> str | None:
        # Runs on the STT thread at decode time (latest glossary + context).
        return build_prompt(self.glossary, self.transcript)

    def _on_stt_result(self, item: WorkItem, res: STTResult) -> None:
        self._stt_outstanding = max(0, self._stt_outstanding - 1)
        if res.audio_s > 0 and res.elapsed_s > 0:
            self.rtf.add(res.rtf)
            self.services.rtf.add(res.rtf)
        if res.dropped:
            self.dropped[res.dropped] += 1
            log.info(
                "MEETPP_STT drop sid=%s utterance=%s identity=%s reason=%s audio_s=%.1f model=%s %s",
                self.sid,
                item.utterance_id,
                item.identity,
                res.dropped,
                item.duration_s,
                res.model,
                res.detail[:120],
            )
            return
        self.decoded += 1
        self.last_segment_wall = time.time()
        self.poster.segment(
            {
                "utterance_id": item.utterance_id,
                "identity": item.identity,
                "name": item.name,
                "t_start": iso(item.t_start),
                "t_end": iso(item.t_end),
                "text": res.text,
                "lang": config.LANGUAGE,
                "avg_logprob": r(res.avg_logprob, 3),
                "no_speech_prob": r(res.no_speech_prob, 3),
            }
        )
        self.transcript = tail_text(f"{self.transcript} {res.text}", 1000)
        self._texts[item.utterance_id] = {"t1": res.text, "t2": None, "t_end": item.t_end}
        self._timeline.append((item.t_end, item.utterance_id))
        if len(self._texts) > 2000:
            for uid in list(self._texts)[:500]:
                self._texts.pop(uid, None)
        self._spawn(self._store_and_refine(item, res.text), self._store_tasks)

    def _on_stt_drop(self, item: WorkItem, reason: str) -> None:
        self._stt_outstanding = max(0, self._stt_outstanding - 1)
        self.dropped[reason] += 1
        log.warning(
            "MEETPP_STT queue drop sid=%s utterance=%s identity=%s audio_s=%.1f reason=%s (backlog > %.0fs): gap reported",
            self.sid,
            item.utterance_id,
            item.identity,
            item.duration_s,
            reason,
            self.services.worker.drop_above_s,
        )
        self.poster.gap(
            {
                "identity": item.identity,
                "name": item.name,
                "t_from": iso(item.t_start),
                "t_to": iso(item.t_end),
                "reason": reason,
            }
        )

    # ── audio store + near-live tier 2 ────────────────────────────────────
    async def _store_and_refine(self, item: WorkItem, text: str) -> None:
        data = await asyncio.to_thread(
            self.store.write,
            item.utterance_id,
            item.audio,
            identity=item.identity,
            name=item.name,
            t_start=iso(item.t_start),
            t_end=iso(item.t_end),
            text=text,
        )
        duration = item.duration_s
        item.audio = item.audio[:0]
        if data is None or self.capture_stopped or not self._tier2_live():
            return
        task = asyncio.current_task()
        if task is not None:
            self._refine_tasks.add(task)
            task.add_done_callback(self._refine_tasks.discard)
        prompt = build_tier2_prompt(self.glossary, self._context_before(item.t_start))
        try:
            res = await self.services.tier2.transcribe(data, prompt, timeout=NEAR_LIVE_BUDGET_S)
        except Tier2Busy:
            # Shed load: never queue near-live work on a busy Mac; tier 1 stands
            # and the final pass covers this utterance.
            self.tier2_fail += 1
            log.debug("MEETPP_TIER2 busy sid=%s utterance=%s: keeping tier 1", self.sid, item.utterance_id)
            return
        except Tier2Error as exc:
            self.tier2_fail += 1
            log.info("MEETPP_TIER2 near-live failed sid=%s utterance=%s: %s", self.sid, item.utterance_id, exc)
            return
        text2 = (res.get("text") or "").strip()
        why = "empty" if not text2 else degenerate_reason(text2, duration, bool(res.get("repetition")), text)
        self.tier2_ok += 1
        if why:
            self.tier2_kept_t1 += 1
            log.info("MEETPP_TIER2 kept tier-1 text sid=%s utterance=%s reason=%s", self.sid, item.utterance_id, why)
            return
        if item.utterance_id in self._texts:
            self._texts[item.utterance_id]["t2"] = text2
        self.poster.refinement({"utterance_id": item.utterance_id, "text": text2, "final": False})

    def _context_before(self, t: float, window: float = TIER2_CONTEXT_S) -> str:
        parts = []
        for t_end, uid in sorted(self._timeline):
            if t - window <= t_end <= t + 0.05:
                e = self._texts.get(uid)
                if e:
                    parts.append(e["t2"] or e["t1"])
        return " ".join(parts)

    def _tier2_live(self) -> bool:
        return self.services.tier2 is not None and self.services.tier2.up and self.store.ok

    def tier2_state(self) -> str:
        if self.services.tier2 is None:
            return "off"
        return "up" if self._tier2_live() else "down"

    # ── controls ──────────────────────────────────────────────────────────
    def set_accepted(self, identities: list[str]) -> None:
        new = set(identities)
        added, removed = new - self.accepted, self.accepted - new
        self.accepted = new
        if added or removed:
            log.info(
                "MEETPP_CONSENT sid=%s accepted=%d added=%s removed=%s",
                self.sid,
                len(new),
                sorted(added),
                sorted(removed),
            )
        for track_sid, c in list(self.consumers.items()):
            if c.identity in removed:
                self._stop_consumer(track_sid, flush=False)  # opt-out: stop now
        self.reconcile()

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        if not paused:
            self._resumed_at = self._clock()
        log.info("MEETPP_SESSION sid=%s %s", self.sid, "paused" if paused else "resumed")
        self._last_status_sig = None  # post now

    def set_glossary(self, glossary: str) -> None:
        self.glossary = glossary or ""

    # ── reconnect ─────────────────────────────────────────────────────────
    async def _reconnect(self, reason) -> None:
        outage_from = time.time()
        self.reconnecting = True
        self._last_status_sig = None
        log.warning(
            "MEETPP_SESSION sid=%s disconnected from room %s (reason=%s): reconnecting with a fresh token",
            self.sid,
            self.room_name,
            reason,
        )
        await self._stop_all_consumers(flush=True)
        backoff = self.services.reconnect_backoff
        attempt = 0
        while not (self.closed or self.capture_stopped):
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
            attempt += 1
            try:
                resp = await self.services.api.post(self.sid, "agent-token", {}, timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("MEETPP_SESSION sid=%s agent-token request failed (attempt %d): %s", self.sid, attempt, exc)
                continue
            if resp.status_code in (404, 410):
                log.error("MEETPP_SESSION sid=%s agent-token -> %s: session gone, giving up", self.sid, resp.status_code)
                self.gave_up = True
                self.reconnecting = False
                return
            if resp.status_code >= 400:
                log.warning("MEETPP_SESSION sid=%s agent-token -> HTTP %s (attempt %d)", self.sid, resp.status_code, attempt)
                continue
            try:
                data = resp.json()
                token = data["token"]
                ws_url = data.get("ws_url") or self.ws_url
            except Exception as exc:  # noqa: BLE001
                log.warning("MEETPP_SESSION sid=%s bad agent-token response: %s", self.sid, exc)
                continue
            try:
                await self._connect(ws_url, token)
            except Exception as exc:  # noqa: BLE001
                log.warning("MEETPP_SESSION sid=%s reconnect attempt %d failed: %s", self.sid, attempt, exc)
                continue
            self.token, self.ws_url = token, ws_url
            break
        else:
            self.reconnecting = False
            return
        self.reconnecting = False
        self.reconnects += 1
        now = time.time()
        log.warning("MEETPP_SESSION sid=%s reconnected after %.1fs (%d attempts)", self.sid, now - outage_from, attempt)
        self.poster.gap({"t_from": iso(outage_from), "t_to": iso(now), "reason": "agent_reconnect"})
        self._last_status_sig = None

    # ── presence ──────────────────────────────────────────────────────────
    def _presence_connected(self, p) -> None:
        name = p.name or p.identity
        if p.identity in self.present:
            self.present[p.identity] = name
            return
        self.present[p.identity] = name
        self.poster.presence(
            {"identity": p.identity, "name": name, "kind": "standard", "event": "connected", "at": iso(time.time())}
        )

    def _presence_disconnected(self, identity: str) -> None:
        name = self.present.pop(identity, identity)
        self.poster.presence(
            {"identity": identity, "name": name, "kind": "standard", "event": "disconnected", "at": iso(time.time())}
        )

    # ── liveness ──────────────────────────────────────────────────────────
    def speaking_seconds(self, identity: str, a: float, b: float) -> float:
        total = 0.0
        for start, end in self._speaking.get(identity, ()):
            e = b if end is None else min(end, b)
            total += max(0.0, e - max(start, a))
        return total

    def check_liveness(self, now: float | None = None) -> list[str]:
        now = self._clock() if now is None else now
        restarted: list[str] = []
        if self.paused or not self.connected or self.capture_stopped:
            return restarted
        if now - self._resumed_at < LIVENESS_WINDOW_S:
            return restarted
        for c in list(self.consumers.values()):
            if not c.alive or now - c.started_at < LIVENESS_WINDOW_S:
                continue
            if c.last_activity >= now - LIVENESS_WINDOW_S:
                continue
            spoke = self.speaking_seconds(c.identity, now - LIVENESS_WINDOW_S, now)
            if spoke < LIVENESS_SPEAKING_S:
                continue
            self.liveness_restarts += 1
            self.speaker_ok[c.identity] = False
            wall = time.time()
            log.warning(
                "MEETPP_LIVENESS sid=%s identity=%s active speaker %.0fs of the last %.0fs with no VAD activity: restarting consumer track=%s",
                self.sid,
                c.identity,
                spoke,
                LIVENESS_WINDOW_S,
                c.track_sid,
            )
            self.poster.gap(
                {
                    "identity": c.identity,
                    "name": c.name,
                    "t_from": iso(wall - LIVENESS_WINDOW_S),
                    "t_to": iso(wall),
                    "reason": "liveness",
                }
            )
            c.restart("liveness")
            restarted.append(c.track_sid)
        return restarted

    # ── status / observability ────────────────────────────────────────────
    def backlog_s(self) -> float:
        return self.services.worker.backlog_s(self.sid)

    def status_value(self) -> str:
        if self.closed or self.capture_stopped or self.gave_up:
            return "offline"
        if self.reconnecting or not self.connected:
            return "reconnecting"
        if self.paused:
            return "paused"
        if self.backlog_s() > BEHIND_S:
            return "behind"
        return "listening"

    def speakers(self) -> list[dict]:
        out = []
        by_identity: dict[str, list[TrackConsumer]] = {}
        for c in self.consumers.values():
            by_identity.setdefault(c.identity, []).append(c)
        for identity, cs in by_identity.items():
            ok = self.speaker_ok.get(identity, True) and any(c.alive for c in cs)
            out.append({"identity": identity, "name": cs[0].name, "ok": bool(ok)})
        return sorted(out, key=lambda s: s["name"].lower())

    def status_body(self) -> dict:
        body = {
            "status": self.status_value(),
            "backlog_s": round(self.backlog_s(), 1),
            "rtf_p50": r(self.rtf.percentile(50)),
            "speakers": self.speakers(),
            "tier2": self.tier2_state(),
        }
        if self.final_pass:
            body["final_pass"] = self.final_pass
        return body

    async def _post_status(self, body: dict, sig: str) -> None:
        async with self._status_lock:
            if self._status_frozen:
                return
            ok = await self.poster.post_status(body)
        if ok:
            self._last_status_sig = sig

    def _maybe_post_status(self, now: float) -> None:
        if self._status_frozen:
            return
        body = self.status_body()
        sig = json.dumps([body["status"], body["tier2"], body["speakers"], body.get("final_pass")])
        if sig == self._last_status_sig and now - self._last_status_at < STATUS_INTERVAL_S:
            return
        if self._status_task is not None and not self._status_task.done():
            return
        self._last_status_at = now
        self._status_task = self._spawn(self._post_status(body, sig))

    def summary_line(self) -> str:
        total_dropped = sum(self.dropped.values())
        reasons = ",".join(f"{k}:{v}" for k, v in sorted(self.dropped.items()))
        p50 = self.rtf.percentile(50)
        p95 = self.rtf.percentile(95)
        age = "-" if self.last_segment_wall is None else f"{time.time() - self.last_segment_wall:.0f}"
        return (
            f"MEETPP_STT sid={self.sid} speakers={len(self.speakers())} "
            f"consumers_alive={sum(1 for c in self.consumers.values() if c.alive)} "
            f"utterances={self.utterances} decoded={self.decoded} "
            f"dropped={total_dropped}{f'({reasons})' if total_dropped else ''} "
            f"backlog_s={self.backlog_s():.1f} "
            f"rtf_p50={'-' if p50 is None else f'{p50:.2f}'} rtf_p95={'-' if p95 is None else f'{p95:.2f}'} "
            f"tier2_ok={self.tier2_ok} tier2_fail={self.tier2_fail} last_segment_age_s={age}"
        )

    async def _tick_loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(TICK_S)
            try:
                now = self._clock()
                self.check_liveness(now)
                self._maybe_post_status(now)
                if now - self._last_summary >= SUMMARY_INTERVAL_S:
                    self._last_summary = now
                    log.info(self.summary_line())
            except Exception:  # noqa: BLE001
                log.exception("MEETPP_SESSION sid=%s tick failed", self.sid)

    def health(self) -> dict:
        return {
            "sid": self.sid,
            "connected": self.connected,
            "consumers": sum(1 for c in self.consumers.values() if c.alive),
            "backlog_s": round(self.backlog_s(), 1),
            "paused": self.paused,
            "tier2": self.tier2_state(),
            "status": self.status_value(),
            "final_pass": self.final_pass,
            "speakers": self.speakers(),
        }

    # ── finalize / close ──────────────────────────────────────────────────
    def finalize(self) -> str:
        if self._final_task is None:
            self.final_pass = "running" if self.services.tier2 is not None else None
            self._final_task = self._spawn(self._run_final())
        return self.final_pass or "skipped"

    async def _stop_capture(self) -> None:
        self.capture_stopped = True
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        await self._stop_all_consumers(flush=True)
        room, self._room = self._room, None
        self.connected = False
        if room is not None:
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _wait_stt(self, timeout: float) -> int:
        deadline = self._clock() + timeout
        while self._stt_outstanding > 0 and self._clock() < deadline:
            await asyncio.sleep(0.1)
        return self._stt_outstanding

    @staticmethod
    async def _wait_tasks(tasks: set, timeout: float) -> None:
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _run_final(self) -> None:
        t0 = time.monotonic()
        log.info("MEETPP_FINAL sid=%s finalize: stopping capture and draining", self.sid)
        await self._stop_capture()
        left = await self._wait_stt(DRAIN_STT_FINAL_S)
        if left:
            log.warning("MEETPP_FINAL sid=%s %d utterances still queued after %.0fs", self.sid, left, DRAIN_STT_FINAL_S)
        await self._wait_tasks(self._store_tasks, 60.0)
        await self._wait_tasks(self._refine_tasks, NEAR_LIVE_BUDGET_S + 2)
        tier2 = self.services.tier2
        if tier2 is None:
            log.info("MEETPP_FINAL sid=%s tier 2 not configured: final pass skipped", self.sid)
            await self._set_final("skipped")
            return
        if not tier2.up:
            await tier2.check_health()
        if not tier2.up:
            log.warning("MEETPP_FINAL sid=%s tier 2 unavailable: final pass failed (live text stands)", self.sid)
            await self._set_final("failed")
            return
        self.final_pass = "running"
        try:
            ok = await asyncio.wait_for(self._final_pass(tier2), FINAL_PASS_MAX_S)
        except asyncio.TimeoutError:
            log.warning("MEETPP_FINAL sid=%s final pass exceeded %.0fs", self.sid, FINAL_PASS_MAX_S)
            ok = False
        except Exception:  # noqa: BLE001
            log.exception("MEETPP_FINAL sid=%s final pass crashed", self.sid)
            ok = False
        if self.store.refused or self.store.errors:
            log.warning(
                "MEETPP_FINAL sid=%s audio store incomplete (refused=%d errors=%d)",
                self.sid,
                self.store.refused,
                self.store.errors,
            )
            ok = False
        await self._set_final("done" if ok else "failed")
        log.info("MEETPP_FINAL sid=%s %s in %.0fs", self.sid, self.final_pass, time.monotonic() - t0)

    async def _final_pass(self, tier2: Tier2Client) -> bool:
        entries = await asyncio.to_thread(self.store.entries)
        if not entries:
            log.info("MEETPP_FINAL sid=%s no stored utterances", self.sid)
            return True
        log.info(
            "MEETPP_FINAL sid=%s submitting %d utterances from %d speakers",
            self.sid,
            len(entries),
            len({e.get("identity") for e in entries}),
        )
        context: dict[str, str] = {}

        async def one(e: dict) -> dict:
            data = await asyncio.to_thread(self.store.read, e["utterance_id"])
            if data is None:
                raise Tier2Error("audio file missing")
            prompt = build_tier2_prompt(self.glossary, context.get(e.get("identity", ""), ""))
            errors = busy = 0
            while True:
                try:
                    return await tier2.transcribe(data, prompt, timeout=FINAL_TIMEOUT_S)
                except Tier2Busy as exc:  # 503: wait as told, keep ≤ 2 in flight
                    busy += 1
                    if busy > FINAL_BUSY_RETRIES:
                        raise
                    await asyncio.sleep(exc.retry_after)
                except Tier2Error:
                    errors += 1
                    if errors >= 2:
                        raise

        pending = list(entries)  # time order
        running: dict[asyncio.Task, dict] = {}
        counts = {"ok": 0, "fail": 0, "kept": 0}
        try:
            await self._final_loop(one, pending, running, context, counts)
        finally:
            for t in running:  # timeout/cancel: no refinement after the verdict
                t.cancel()
        log.info(
            "MEETPP_FINAL sid=%s refined=%d kept=%d failed=%d",
            self.sid,
            counts["ok"],
            counts["kept"],
            counts["fail"],
        )
        return counts["fail"] == 0

    async def _final_loop(self, one, pending: list, running: dict, context: dict, counts: dict) -> None:
        """Submit in time order, at most one request per speaker in flight (so
        each speaker's previous text is known) and CONCURRENCY overall."""
        busy: set[str] = set()
        while pending or running:
            i = 0
            while len(running) < TIER2_CONCURRENCY and i < len(pending):
                e = pending[i]
                ident = e.get("identity", "")
                if ident in busy:
                    i += 1
                    continue
                pending.pop(i)
                busy.add(ident)
                running[asyncio.create_task(one(e))] = e
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                e = running.pop(t)
                ident = e.get("identity", "")
                busy.discard(ident)
                uid = e["utterance_id"]
                tier1 = e.get("text") or (self._texts.get(uid) or {}).get("t1") or ""
                earlier = (self._texts.get(uid) or {}).get("t2") or tier1
                try:
                    res = t.result()
                except Exception as exc:  # noqa: BLE001
                    counts["fail"] += 1
                    if counts["fail"] <= 5:
                        log.warning("MEETPP_FINAL sid=%s utterance=%s failed: %s", self.sid, uid, exc)
                    context[ident] = tail_text(f"{context.get(ident, '')} {earlier}", 600)
                    continue
                text = (res.get("text") or "").strip()
                why = "empty" if not text else degenerate_reason(text, float(e.get("duration_s") or 0), bool(res.get("repetition")), tier1)
                if why:
                    counts["kept"] += 1
                    log.info("MEETPP_FINAL sid=%s utterance=%s kept earlier text (%s)", self.sid, uid, why)
                    context[ident] = tail_text(f"{context.get(ident, '')} {earlier}", 600)
                    continue
                self.poster.refinement({"utterance_id": uid, "text": text, "final": True})
                counts["ok"] += 1
                context[ident] = tail_text(f"{context.get(ident, '')} {text}", 600)

    async def _set_final(self, value: str) -> None:
        self.final_pass = value
        # Refinements first, then the final status (ordered queue).
        await self.poster.drain(60.0)
        async with self._status_lock:
            self._status_frozen = True
        self.poster.status(self.status_body())

    async def close(self) -> None:
        """DELETE: leave the room, no final pass (one already running ends normally)."""
        if self.closed:
            return
        log.info("MEETPP_SESSION sid=%s closing", self.sid)
        await self._stop_capture()
        left = await self._wait_stt(DRAIN_STT_CLOSE_S)
        if left:
            removed = self.services.worker.cancel_session(self.sid)
            self._stt_outstanding = 0
            log.warning(
                "MEETPP_STT sid=%s session closed with %d utterances (%.1fs audio) not transcribed",
                self.sid,
                len(removed),
                sum(it.duration_s for it in removed),
            )
            self.dropped["session_closed"] += len(removed)
        await self._wait_tasks(self._store_tasks, 10.0)
        for t in list(self._refine_tasks):
            t.cancel()
        if self._final_task is not None and not self._final_task.done():
            await asyncio.wait([self._final_task], timeout=FINAL_PASS_MAX_S)
        self.closed = True
        log.info(self.summary_line())
        async with self._status_lock:
            frozen, self._status_frozen = self._status_frozen, True
        if not frozen:
            self.poster.status(self.status_body())
        await self.poster.close(drain_timeout=10.0)
        for t in list(self._tasks):
            if t is not asyncio.current_task():
                t.cancel()
        log.info("MEETPP_SESSION sid=%s closed", self.sid)
