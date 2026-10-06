"""Delivery to meeting-api's internal API (contract §2.4).

* ``sign`` implements the shared HMAC scheme
  ``X-Meetpp-Signature = hex(HMAC_SHA256(secret, ts + "." + body))``.
* ``Poster`` is one ordered delivery queue per session on a persistent
  ``httpx.AsyncClient``. Items stay in order; on 5xx or network errors the
  head is retried with backoff for up to 5 minutes (then dropped with a
  WARNING); a 4xx is never retried (logged and skipped). Consecutive items of
  the same kind are batched.
* ``post_status`` is the best-effort, latest-wins path for periodic
  agent-status (not buffered).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger("meetpp.agent")

RETRY_WINDOW_S = 300.0
MAX_BATCH = 50
BACKOFF_S = (1.0, 2.0, 5.0, 10.0)


def sign(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    stamp = str(int(time.time()) if ts is None else int(ts))
    sig = hmac.new(secret.encode("utf-8"), stamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Meetpp-Timestamp": stamp, "X-Meetpp-Signature": sig}


def dumps(body: Any) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class InternalApi:
    """Signed JSON calls to meeting-api ``/api/v1/internal/meetpp/...``."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, secret: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.secret = secret

    def url(self, sid: str, endpoint: str) -> str:
        return f"{self.base_url}/api/v1/internal/meetpp/sessions/{sid}/{endpoint}"

    async def post(self, sid: str, endpoint: str, body: Any, timeout: float = 10.0) -> httpx.Response:
        raw = dumps(body)
        headers = {"Content-Type": "application/json", **sign(self.secret, raw)}
        return await self.client.post(self.url(sid, endpoint), content=raw, headers=headers, timeout=timeout)


# kind → (endpoint, body key)
_KINDS = {
    "segments": ("segments", "segments"),
    "refinements": ("segments", "refinements"),
    "gaps": ("segments", "gaps"),
    "presence": ("presence", "events"),
    "status": ("agent-status", None),
}


@dataclass
class _Item:
    kind: str
    payload: dict
    created: float


class Poster:
    def __init__(
        self,
        api: InternalApi,
        sid: str,
        *,
        retry_window_s: float = RETRY_WINDOW_S,
        max_batch: int = MAX_BATCH,
        backoff: tuple[float, ...] = BACKOFF_S,
        clock: Callable[[], float] = time.monotonic,
        on_seqs: Callable[[dict], None] | None = None,
    ) -> None:
        self.api = api
        self.sid = sid
        self.retry_window_s = retry_window_s
        self.max_batch = max_batch
        self.backoff = backoff
        self._clock = clock
        self.on_seqs = on_seqs
        self._pending: deque[_Item] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._failing_since: float | None = None
        self.sent = 0
        self.rejected = 0
        self.expired = 0
        self.retries = 0

    # ── enqueue ──
    def segment(self, seg: dict) -> None:
        self._put("segments", seg)

    def refinement(self, ref: dict) -> None:
        self._put("refinements", ref)

    def gap(self, gap: dict) -> None:
        self._put("gaps", gap)

    def presence(self, event: dict) -> None:
        self._put("presence", event)

    def status(self, body: dict) -> None:
        """Ordered (reliable) agent-status, e.g. the final_pass result."""
        self._put("status", body)

    def _put(self, kind: str, payload: dict) -> None:
        self._pending.append(_Item(kind, payload, self._clock()))
        self._idle.clear()
        self._wake.set()

    @property
    def backlog(self) -> int:
        return len(self._pending)

    # ── lifecycle ──
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"poster-{self.sid}")

    async def drain(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self, drain_timeout: float = 10.0) -> None:
        if self._task is None:  # never started (session failed to join)
            self._closing = True
            if self._pending:
                log.warning("MEETPP_POST sid=%s never started; %d items not delivered", self.sid, len(self._pending))
            return
        if self._pending:
            if not await self.drain(drain_timeout):
                log.warning(
                    "MEETPP_POST sid=%s closing with %d undelivered items (dropped)",
                    self.sid,
                    len(self._pending),
                )
        self._closing = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def post_status(self, body: dict) -> bool:
        try:
            r = await self.api.post(self.sid, "agent-status", body, timeout=5.0)
            if r.status_code >= 400:
                log.debug("MEETPP_POST sid=%s agent-status -> %s", self.sid, r.status_code)
            return r.status_code < 400
        except Exception as exc:  # noqa: BLE001
            log.debug("MEETPP_POST sid=%s agent-status failed: %s", self.sid, exc)
            return False

    # ── loop ──
    async def run(self) -> None:
        attempt = 0
        while not self._closing:
            if not self._pending:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            self._expire()
            if not self._pending:
                continue
            batch = self._take_batch()
            outcome = await self._send(batch)
            if outcome == "retry":
                self.retries += 1
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
                continue
            attempt = 0
            for _ in batch:
                self._pending.popleft()
        self._idle.set()

    def _expire(self) -> None:
        now = self._clock()
        dropped: dict[str, int] = {}
        while self._pending and now - self._pending[0].created > self.retry_window_s:
            it = self._pending.popleft()
            dropped[it.kind] = dropped.get(it.kind, 0) + 1
        if dropped:
            n = sum(dropped.values())
            self.expired += n
            log.warning(
                "MEETPP_POST sid=%s retry window (%.0fs) exceeded: dropped %s",
                self.sid,
                self.retry_window_s,
                ", ".join(f"{k}={v}" for k, v in dropped.items()),
            )

    def _take_batch(self) -> list[_Item]:
        head = self._pending[0]
        if head.kind == "status":
            return [head]
        batch = [head]
        for it in list(self._pending)[1 : self.max_batch]:
            if it.kind != head.kind:
                break
            batch.append(it)
        return batch

    async def _send(self, batch: list[_Item]) -> str:
        kind = batch[0].kind
        endpoint, key = _KINDS[kind]
        body = batch[0].payload if key is None else {key: [it.payload for it in batch]}
        try:
            r = await self.api.post(self.sid, endpoint, body)
        except Exception as exc:  # noqa: BLE001  (network, timeout)
            self._note_failure(f"{type(exc).__name__}: {exc}", endpoint)
            return "retry"
        if r.status_code >= 500:
            self._note_failure(f"HTTP {r.status_code}", endpoint)
            return "retry"
        if r.status_code >= 400:
            self.rejected += len(batch)
            log.warning(
                "MEETPP_POST sid=%s %s rejected HTTP %s (not retried, %d %s dropped): %s",
                self.sid,
                endpoint,
                r.status_code,
                len(batch),
                kind,
                (r.text or "")[:300],
            )
            return "rejected"
        if self._failing_since is not None:
            log.info(
                "MEETPP_POST sid=%s delivery recovered after %.0fs",
                self.sid,
                self._clock() - self._failing_since,
            )
            self._failing_since = None
        self.sent += len(batch)
        if self.on_seqs is not None and kind == "segments":
            try:
                seqs = (r.json() or {}).get("seqs") or {}
                if seqs:
                    self.on_seqs(seqs)
            except Exception:  # noqa: BLE001
                pass
        return "ok"

    def _note_failure(self, why: str, endpoint: str) -> None:
        if self._failing_since is None:
            self._failing_since = self._clock()
            log.warning(
                "MEETPP_POST sid=%s %s failed (%s); retrying in order for up to %.0fs (%d buffered)",
                self.sid,
                endpoint,
                why,
                self.retry_window_s,
                len(self._pending),
            )
        else:
            log.debug("MEETPP_POST sid=%s %s still failing: %s", self.sid, endpoint, why)
