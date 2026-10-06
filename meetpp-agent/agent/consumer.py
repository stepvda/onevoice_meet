"""One supervised audio consumer per subscribed microphone track (FDD §7.5).

The supervisor restarts the consumer after any exception or unexpected end
of stream with backoff 1, 2, 5, 10 s (reset after a minute of healthy
running). ``restart()`` (liveness) restarts it immediately. Each run gets a
fresh audio stream and a fresh segmenter, so the VAD state is reset per
stream.
"""
from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from typing import AsyncIterator, Callable

import numpy as np

from .vad import SAMPLE_RATE, Utterance, UtteranceSegmenter

log = logging.getLogger("meetpp.agent")

BACKOFF_S = (1.0, 2.0, 5.0, 10.0)
STABLE_AFTER_S = 60.0
CHUNK_SAMPLES = 1024  # hand ~64 ms at a time to the VAD thread


async def livekit_pcm(track) -> AsyncIterator[np.ndarray]:
    """LiveKit remote audio track → float32 mono 16 kHz chunks."""
    from livekit import rtc

    stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
    try:
        async for event in stream:
            frame = event.frame
            yield np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        await stream.aclose()


class SampleClock:
    """Maps stream sample indices to wall-clock time. Re-anchors when the
    arrival time drifts more than ``tolerance_s`` from the sample count
    (muted tracks, network stalls, paused capture)."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, tolerance_s: float = 1.0) -> None:
        self.sr = sample_rate
        self.tol = tolerance_s
        self.total = 0
        self._samples: list[int] = []
        self._walls: list[float] = []

    def on_samples(self, n: int, now: float) -> None:
        if not self._samples:
            self._anchor(now - n / self.sr)
        else:
            expected_end = self._walls[-1] + (self.total + n - self._samples[-1]) / self.sr
            if abs(now - expected_end) > self.tol:
                self._anchor(now - n / self.sr)
        self.total += n

    def _anchor(self, wall: float) -> None:
        self._samples.append(self.total)
        self._walls.append(wall)
        if len(self._samples) > 256:
            del self._samples[:128]
            del self._walls[:128]

    def wall(self, sample: int) -> float:
        if not self._samples:
            return time.time()
        i = max(0, bisect_right(self._samples, sample) - 1)
        return self._walls[i] + (sample - self._samples[i]) / self.sr


class TrackConsumer:
    def __init__(
        self,
        *,
        track_sid: str,
        identity: str,
        name: str,
        track,
        stream_factory: Callable[[object], AsyncIterator[np.ndarray]],
        vad_factory: Callable[[], object],
        on_utterance: Callable[["TrackConsumer", Utterance, float, float], None],
        on_restart: Callable[["TrackConsumer", str, float, float], None] | None = None,
        paused: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        backoff: tuple[float, ...] = BACKOFF_S,
        offload: Callable | None = None,
    ) -> None:
        self.track_sid = track_sid
        self.identity = identity
        self.name = name
        self.track = track
        self._stream_factory = stream_factory
        self._vad_factory = vad_factory
        self._on_utterance = on_utterance
        self._on_restart = on_restart
        self._paused = paused
        self._clock = clock
        self._wall = wall
        self.backoff = backoff
        # VAD inference runs off the event loop.
        self._offload = offload or asyncio.to_thread
        self.alive = False
        self.restarts = 0
        self.started_at = clock()
        self.last_activity = 0.0  # monotonic time of last VAD speech activity
        self.last_failure_wall: float | None = None
        self.segmenter: UtteranceSegmenter | None = None
        self._sclock: SampleClock | None = None
        self._stopping = False
        self._stop_event: asyncio.Event | None = None
        self._restart_reason: str | None = None
        self._task: asyncio.Task | None = None
        self._inner: asyncio.Task | None = None
        self._was_paused = False

    # ── control ──
    def start(self) -> None:
        if self._task is None:
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._supervise(), name=f"consumer-{self.track_sid}")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def restart(self, reason: str) -> None:
        """Restart the stream now (liveness). No backoff."""
        if self._inner is not None and not self._inner.done():
            self._restart_reason = reason
            self._inner.cancel()

    async def stop(self, flush: bool = True) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()  # interrupts a backoff wait
        seg = self.segmenter
        if self._inner is not None and not self._inner.done():
            self._inner.cancel()
        if self._task is not None:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if seg is not None:
            if flush:
                self._emit(seg.flush())
            else:
                seg.reset()
        self.alive = False

    # ── supervision ──
    async def _supervise(self) -> None:
        attempt = 0
        while not self._stopping:
            run_started = self._clock()
            self.started_at = run_started
            self._inner = asyncio.create_task(self._consume_once())
            immediate = False
            try:
                await self._inner
                reason = "stream ended"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    self.alive = False
                    raise  # the supervisor itself is being cancelled
                if self._stopping:
                    break
                reason = self._restart_reason or "cancelled"
                immediate = self._restart_reason is not None
                self._restart_reason = None
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self.alive = False
            if self._stopping:
                break
            if self._clock() - run_started >= STABLE_AFTER_S:
                attempt = 0
            delay = 0.0 if immediate else self.backoff[min(attempt, len(self.backoff) - 1)]
            if not immediate:
                attempt += 1
            self.restarts += 1
            self.last_failure_wall = self._wall()
            log.warning(
                "MEETPP_CONSUMER restart track=%s identity=%s attempt=%d in %.0fs: %s",
                self.track_sid,
                self.identity,
                self.restarts,
                delay,
                reason,
            )
            if self._on_restart is not None:
                try:
                    self._on_restart(self, reason, delay, self.last_failure_wall)
                except Exception:  # noqa: BLE001
                    log.exception("on_restart callback failed")
            if delay:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    async def _consume_once(self) -> None:
        seg = UtteranceSegmenter(self._vad_factory())
        sclock = SampleClock()
        prev, self.segmenter = self.segmenter, seg
        if prev is not None:
            # Leftover from a crashed run: keep the speech we already have.
            self._emit(prev.flush())
        self._sclock = sclock
        stream = self._stream_factory(self.track)
        chunks: list[np.ndarray] = []
        n = 0
        try:
            self.alive = True
            async for pcm in stream:
                if self._paused():
                    if not self._was_paused:
                        self._was_paused = True
                        self._emit(seg.flush())
                        seg.reset()
                        chunks, n = [], 0
                    continue
                self._was_paused = False
                sclock.on_samples(len(pcm), self._wall())
                chunks.append(pcm)
                n += len(pcm)
                if n < CHUNK_SAMPLES:
                    continue
                data = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
                chunks, n = [], 0
                busy = asyncio.ensure_future(self._offload(seg.process, data))
                try:
                    utterances = await asyncio.shield(busy)
                except asyncio.CancelledError:
                    # Never leave a VAD thread running on this segmenter: let
                    # the call finish (a few ms) and keep its utterances.
                    try:
                        for u in await busy:
                            self._emit(u)
                    except BaseException:  # noqa: BLE001
                        pass
                    raise
                if seg.speech_active or utterances:
                    self.last_activity = self._clock()
                for u in utterances:
                    self._emit(u)
        finally:
            self.alive = False
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

    def _emit(self, utt: Utterance | None) -> None:
        if utt is None or not len(utt.audio):
            return
        self.last_activity = self._clock()
        sclock = self._sclock
        if sclock is not None:
            t_end = sclock.wall(utt.end_sample)
        else:
            t_end = self._wall()
        t_start = t_end - utt.duration_s
        try:
            self._on_utterance(self, utt, t_start, t_end)
        except Exception:  # noqa: BLE001
            log.exception("on_utterance callback failed")
