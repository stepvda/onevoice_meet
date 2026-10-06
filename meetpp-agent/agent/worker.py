"""The single STT worker thread (FDD §7.3 "Queue", §7.7).

Backlog is *measured*: the age of the oldest utterance that has been queued
but not finished (in flight or waiting). It drives:
  > 20 s  → decode with the degrade model (``base``) until it is < 5 s
  > 60 s  → drop the oldest waiting utterances (each one reported as a gap
            with reason "overloaded" and logged at WARNING by the session)

Nothing is dropped silently: every drop goes through ``on_drop``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .stt import STTEngine, STTResult

log = logging.getLogger("meetpp.agent")

DEGRADE_ABOVE_S = 20.0
RECOVER_BELOW_S = 5.0
DROP_ABOVE_S = 60.0


@dataclass
class WorkItem:
    session_id: str
    utterance_id: str
    identity: str
    name: str
    audio: np.ndarray
    t_start: float  # epoch seconds
    t_end: float
    on_result: Callable[["WorkItem", STTResult], None]
    on_drop: Callable[["WorkItem", str], None]
    prompt_fn: Callable[[], str | None] = lambda: None
    enqueued_at: float = 0.0  # monotonic, set by submit()
    meta: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return len(self.audio) / 16000.0


class STTWorker:
    def __init__(
        self,
        engine: STTEngine,
        *,
        dispatch: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        degrade_above_s: float = DEGRADE_ABOVE_S,
        recover_below_s: float = RECOVER_BELOW_S,
        drop_above_s: float = DROP_ABOVE_S,
    ) -> None:
        self.engine = engine
        # How callbacks reach the event loop (loop.call_soon_threadsafe).
        self._dispatch_fn = dispatch or (lambda fn, *a: fn(*a))
        self._clock = clock
        self.degrade_above_s = degrade_above_s
        self.recover_below_s = recover_below_s
        self.drop_above_s = drop_above_s
        self._queue: deque[WorkItem] = deque()
        self._inflight: WorkItem | None = None
        self._cond = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None
        self.degraded = False
        self.processed = 0
        self.dropped = 0

    def _dispatch(self, fn, *args) -> None:
        try:
            self._dispatch_fn(fn, *args)
        except Exception:  # noqa: BLE001  (e.g. loop closed at shutdown)
            log.exception("MEETPP_STT could not deliver a result to the event loop")

    # ── lifecycle ──
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="stt-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    # ── queue ──
    def submit(self, item: WorkItem) -> None:
        item.enqueued_at = self._clock()
        with self._cond:
            self._queue.append(item)
            dropped = self._enforce_locked()
            self._cond.notify_all()
        self._report_drops(dropped)

    def backlog_s(self, session_id: str | None = None) -> float:
        with self._cond:
            return self._backlog_locked(session_id)

    def pending(self, session_id: str | None = None) -> int:
        with self._cond:
            n = sum(1 for it in self._queue if session_id is None or it.session_id == session_id)
            if self._inflight is not None and (session_id is None or self._inflight.session_id == session_id):
                n += 1
            return n

    def queued_audio_s(self, session_id: str | None = None) -> float:
        with self._cond:
            return float(sum(it.duration_s for it in self._queue if session_id is None or it.session_id == session_id))

    def cancel_session(self, session_id: str) -> list[WorkItem]:
        """Remove a session's waiting items (in-flight one completes)."""
        with self._cond:
            keep = deque(it for it in self._queue if it.session_id != session_id)
            removed = [it for it in self._queue if it.session_id == session_id]
            self._queue = keep
        return removed

    # ── internals ──
    def _backlog_locked(self, session_id: str | None) -> float:
        now = self._clock()
        oldest = None
        if self._inflight is not None and (session_id is None or self._inflight.session_id == session_id):
            oldest = self._inflight.enqueued_at
        for it in self._queue:
            if session_id is None or it.session_id == session_id:
                oldest = it.enqueued_at if oldest is None else min(oldest, it.enqueued_at)
                break  # FIFO: first match is the oldest of that session
        return max(0.0, now - oldest) if oldest is not None else 0.0

    def _enforce_locked(self) -> list[WorkItem]:
        dropped = []
        now = self._clock()
        while self._queue and now - self._queue[0].enqueued_at > self.drop_above_s:
            dropped.append(self._queue.popleft())
        return dropped

    def _report_drops(self, dropped: list[WorkItem]) -> None:
        for it in dropped:
            self.dropped += 1
            self._dispatch(it.on_drop, it, "overloaded")

    def _update_degrade_locked(self) -> None:
        b = self._backlog_locked(None)
        if not self.degraded and b > self.degrade_above_s:
            self.degraded = True
            log.warning(
                "MEETPP_STT backlog %.1fs > %.0fs: degrading to %s",
                b,
                self.degrade_above_s,
                self.engine.model_for(True),
            )
        elif self.degraded and b < self.recover_below_s:
            self.degraded = False
            log.info("MEETPP_STT backlog %.1fs < %.0fs: back to %s", b, self.recover_below_s, self.engine.primary)

    def next_item(self, timeout: float | None = 0.5) -> WorkItem | None:
        """Pop the next item (applying drop + degrade policy); mark in flight."""
        with self._cond:
            if not self._queue and not self._stop:
                self._cond.wait(timeout)
            dropped = self._enforce_locked()
            item = self._queue.popleft() if self._queue else None
            self._inflight = item
            if item is not None:
                self._update_degrade_locked()
        self._report_drops(dropped)
        return item

    def process_one(self, item: WorkItem) -> STTResult:
        try:
            try:
                prompt = item.prompt_fn()
            except Exception:  # noqa: BLE001
                prompt = None
            try:
                result = self.engine.transcribe(item.audio, prompt, degraded=self.degraded)
            except Exception as exc:  # noqa: BLE001
                log.exception("MEETPP_STT decode failed utterance=%s", item.utterance_id)
                result = STTResult(dropped="stt_error", detail=str(exc), audio_s=item.duration_s)
            self.processed += 1
            self._dispatch(item.on_result, item, result)
            return result
        finally:
            with self._cond:
                self._inflight = None
                self._cond.notify_all()

    def _run(self) -> None:
        while not self._stop:
            if not self.engine.ready.wait(timeout=0.5):
                # Model still loading: the queue grows; the drop policy still applies.
                with self._cond:
                    dropped = self._enforce_locked()
                self._report_drops(dropped)
                continue
            item = self.next_item()
            if item is None:
                continue
            self.process_one(item)
