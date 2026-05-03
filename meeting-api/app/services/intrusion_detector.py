"""
Lightweight Intrusion Detection System.

In-process, single-instance. Tracks security events in per-IP sliding windows
and decides when to temp-block an IP. Persists notable events to the
`security_events` table so the admin panel can show a history beyond what's
still in memory.

Detected scenarios (thresholds in `app.config.Settings.ids_*`):
  - **brute_force_login** — N auth failures from one IP in a short window
  - **twofa_brute_force** — fewer 2FA failures count as brute force, since
    the password was already correct
  - **path_scan** — many 404s in a short window (probing for endpoints)
  - **manual** — admin-issued temp block (TODO if needed)

Temp blocks are kept in memory only (a process restart clears them); permanent
blocks live in the `blocked_ips` table and are managed by the admin panel.

This is *not* a substitute for a perimeter firewall — it's a defense-in-depth
signal that runs as close as possible to the application logic so it can see
things like "wrong 2FA code after a correct password" that a network-level
filter cannot.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Iterable

from app.config import settings
from app.db import SessionLocal
from app.models import SecurityEvent


# Event types the rest of the app records. Centralised here so callers
# don't sprinkle string literals across the codebase.
class EventType:
    AUTH_FAILURE = "auth_failure"
    TWOFA_FAILURE = "twofa_failure"
    SIGNUP_FAILURE = "signup_failure"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMIT = "rate_limit"
    MANUAL_BLOCK = "manual_block"
    TEMP_BLOCK = "temp_block"


SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_BLOCK = "block"
SEVERITY_ALERT = "alert"


@dataclass
class _Event:
    ts: datetime
    event_type: str
    user_id: int | None
    handle: str | None
    path: str | None
    user_agent: str | None
    details: str | None


@dataclass
class _IpState:
    events: Deque[_Event] = field(default_factory=deque)


class IntrusionDetector:
    """Single global detector. Thread-safe via a coarse-grained lock — the
    record path is fast, contention is bounded, and we'd rather hold a lock
    for ~10µs than reach for per-IP locks and worry about reentrancy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_ip: dict[str, _IpState] = {}
        # IP -> unix epoch seconds when the temp block expires
        self._temp_blocks: dict[str, float] = {}
        # Counter — number of times the IP has tripped a temp-block. Surfaced
        # in the admin panel so repeat offenders stand out.
        self._temp_block_hits: dict[str, int] = {}
        # Recent events surfaced to the admin panel (newest first), bounded.
        self._recent: Deque[dict] = deque(maxlen=500)

    # ─── public API ────────────────────────────────────────────────────

    def record(
        self,
        event_type: str,
        ip: str | None,
        *,
        severity: str = SEVERITY_INFO,
        user_id: int | None = None,
        handle: str | None = None,
        path: str | None = None,
        user_agent: str | None = None,
        details: str | None = None,
        persist: bool = True,
    ) -> None:
        """Record one signal. Cheap; safe to call from any handler."""
        if not settings.ids_enabled:
            return
        if not ip:
            ip = "unknown"
        now = datetime.now(timezone.utc)
        ev = _Event(
            ts=now,
            event_type=event_type,
            user_id=user_id,
            handle=handle,
            path=path,
            user_agent=(user_agent or "")[:255] or None,
            details=(details or "")[:512] or None,
        )

        with self._lock:
            state = self._by_ip.setdefault(ip, _IpState())
            state.events.append(ev)
            self._trim(state)
            self._recent.appendleft(
                {
                    "ts": now.isoformat(),
                    "event_type": event_type,
                    "severity": severity,
                    "ip": ip,
                    "user_id": user_id,
                    "handle": handle,
                    "path": path,
                    "user_agent": ev.user_agent,
                    "details": ev.details,
                }
            )
            self._evaluate(ip, state, now)

        if persist:
            self._persist(now, event_type, severity, ip, user_id, handle, path, ev.user_agent, ev.details)

    def is_temp_blocked(self, ip: str) -> tuple[bool, int]:
        """Returns (blocked, seconds_remaining). seconds_remaining=0 if not blocked."""
        if not ip:
            return False, 0
        with self._lock:
            until = self._temp_blocks.get(ip)
            if until is None:
                return False, 0
            now_ts = datetime.now(timezone.utc).timestamp()
            if until <= now_ts:
                # Lazy cleanup — the block has expired naturally.
                self._temp_blocks.pop(ip, None)
                return False, 0
            return True, int(until - now_ts)

    def unblock(self, ip: str) -> bool:
        """Manual unblock from the admin panel. Returns True if there was an
        active block to remove."""
        with self._lock:
            return self._temp_blocks.pop(ip, None) is not None

    def temp_blocks(self) -> list[dict]:
        """Snapshot of currently-active temp blocks for the admin panel."""
        now_ts = datetime.now(timezone.utc).timestamp()
        with self._lock:
            out = []
            for ip, until in list(self._temp_blocks.items()):
                if until <= now_ts:
                    self._temp_blocks.pop(ip, None)
                    continue
                out.append(
                    {
                        "ip": ip,
                        "expires_at": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
                        "seconds_remaining": int(until - now_ts),
                        "hits": self._temp_block_hits.get(ip, 0),
                    }
                )
            return sorted(out, key=lambda r: r["seconds_remaining"], reverse=True)

    def recent_events(self, limit: int = 100) -> list[dict]:
        """Recent in-memory events, newest first."""
        with self._lock:
            return list(self._recent)[:limit]

    def stats(self) -> dict:
        with self._lock:
            return {
                "tracked_ips": len(self._by_ip),
                "temp_blocked": len(self._temp_blocks),
                "events_in_memory": sum(len(s.events) for s in self._by_ip.values()),
                "enabled": bool(settings.ids_enabled),
            }

    # ─── internals ─────────────────────────────────────────────────────

    def _trim(self, state: _IpState) -> None:
        # Bound the per-IP ring — drop oldest until <= cap. The widest window
        # we evaluate is ids_brute_force_window_seconds, so we also drop
        # anything older than ~2x that to keep memory tidy under low traffic.
        cap = settings.ids_max_events_per_ip
        while len(state.events) > cap:
            state.events.popleft()
        if not state.events:
            return
        oldest_keep = datetime.now(timezone.utc) - timedelta(
            seconds=max(
                settings.ids_brute_force_window_seconds,
                settings.ids_twofa_brute_force_window_seconds,
                settings.ids_path_scan_window_seconds,
            ) * 2
        )
        while state.events and state.events[0].ts < oldest_keep:
            state.events.popleft()

    def _evaluate(self, ip: str, state: _IpState, now: datetime) -> None:
        """Inspect this IP's recent events and decide whether to temp-block.
        Holds `self._lock` from the caller."""
        # Already blocked — don't re-evaluate.
        if ip in self._temp_blocks and self._temp_blocks[ip] > now.timestamp():
            return

        # 1. brute_force_login
        n_auth = self._count_within(
            state.events,
            (EventType.AUTH_FAILURE,),
            settings.ids_brute_force_window_seconds,
            now,
        )
        if n_auth >= settings.ids_brute_force_threshold:
            self._apply_temp_block(ip, "brute_force_login", n_auth)
            return

        # 2. 2FA brute force
        n_2fa = self._count_within(
            state.events,
            (EventType.TWOFA_FAILURE,),
            settings.ids_twofa_brute_force_window_seconds,
            now,
        )
        if n_2fa >= settings.ids_twofa_brute_force_threshold:
            self._apply_temp_block(ip, "twofa_brute_force", n_2fa)
            return

        # 3. path scanning
        n_404 = self._count_within(
            state.events,
            (EventType.NOT_FOUND,),
            settings.ids_path_scan_window_seconds,
            now,
        )
        if n_404 >= settings.ids_path_scan_threshold:
            self._apply_temp_block(ip, "path_scan", n_404)

    def _count_within(
        self,
        events: Iterable[_Event],
        types: tuple[str, ...],
        window_seconds: int,
        now: datetime,
    ) -> int:
        cutoff = now - timedelta(seconds=window_seconds)
        return sum(1 for e in events if e.ts >= cutoff and e.event_type in types)

    def _apply_temp_block(self, ip: str, scenario: str, count: int) -> None:
        """Caller holds `self._lock`."""
        until_dt = datetime.now(timezone.utc) + timedelta(minutes=settings.ids_temp_block_minutes)
        self._temp_blocks[ip] = until_dt.timestamp()
        self._temp_block_hits[ip] = self._temp_block_hits.get(ip, 0) + 1
        details = f"scenario={scenario} count={count} duration_min={settings.ids_temp_block_minutes}"
        self._recent.appendleft(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event_type": EventType.TEMP_BLOCK,
                "severity": SEVERITY_BLOCK,
                "ip": ip,
                "user_id": None,
                "handle": None,
                "path": None,
                "user_agent": None,
                "details": details,
            }
        )
        self._persist(
            datetime.now(timezone.utc),
            EventType.TEMP_BLOCK,
            SEVERITY_BLOCK,
            ip,
            None,
            None,
            None,
            None,
            details,
        )

    def _persist(
        self,
        ts: datetime,
        event_type: str,
        severity: str,
        ip: str | None,
        user_id: int | None,
        handle: str | None,
        path: str | None,
        user_agent: str | None,
        details: str | None,
    ) -> None:
        """Best-effort write to security_events. Swallow errors so a slow or
        broken DB never blocks the request path."""
        try:
            with SessionLocal() as db:
                db.add(
                    SecurityEvent(
                        event_type=event_type,
                        severity=severity,
                        ip_address=ip,
                        user_id=user_id,
                        handle=(handle or "")[:255] or None,
                        path=(path or "")[:255] or None,
                        user_agent=(user_agent or "")[:255] or None,
                        details=(details or "")[:512] or None,
                        created_at=ts,
                    )
                )
                db.commit()
        except Exception:
            pass


detector = IntrusionDetector()
