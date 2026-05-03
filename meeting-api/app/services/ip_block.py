"""
Persistent IP blocklist + middleware.

The `BlockedIP` table is the source of truth. We mirror it into an in-memory
cache (`_cache`) so the middleware can decide in O(1) without touching the DB
on the hot path. Admin mutations (add/remove/toggle) call `reload()` to refresh
the cache.

Supported entry formats:
  - exact IP:        "203.0.113.5", "2001:db8::1"
  - CIDR:            "203.0.113.0/24", "2001:db8::/32"
  - dash range (v4): "203.0.113.5-50" (last octet only)
"""
from __future__ import annotations

import ipaddress
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.db import SessionLocal
from app.models import BlockedIP
from app.services.intrusion_detector import detector


@dataclass(frozen=True)
class _Entry:
    raw: str
    network: ipaddress._BaseNetwork | None  # None for dash ranges
    dash: tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None  # IPv4 range

    def matches(self, ip: ipaddress._BaseAddress) -> bool:
        if self.network is not None:
            try:
                return ip in self.network
            except TypeError:
                # IPv4 entry vs IPv6 client (or vice versa)
                return False
        if self.dash is not None and isinstance(ip, ipaddress.IPv4Address):
            return self.dash[0] <= ip <= self.dash[1]
        return False


_lock = threading.RLock()
_cache: list[_Entry] = []
# Hit counter for live blocks — flushed back to BlockedIP.block_count
# periodically and on shutdown so the admin panel sees fresh numbers.
_hits: dict[str, int] = {}


def parse_entry(raw: str) -> _Entry:
    """Parse a stored ip_address string into a structured _Entry. Raises
    ValueError on invalid input."""
    s = raw.strip()
    if not s:
        raise ValueError("empty IP entry")
    # Dash range — IPv4 only, last-octet form like "1.2.3.5-99"
    if "-" in s and "/" not in s and ":" not in s:
        head, _, tail = s.rpartition("-")
        try:
            start = ipaddress.IPv4Address(head)
        except ValueError as e:
            raise ValueError(f"invalid IPv4 in range: {head}") from e
        try:
            tail_int = int(tail)
        except ValueError as e:
            raise ValueError(f"range must end in an integer last-octet: {tail}") from e
        if not 0 <= tail_int <= 255:
            raise ValueError("range last-octet must be 0-255")
        # Build the end address by replacing the last octet.
        parts = str(start).split(".")
        parts[-1] = str(tail_int)
        end = ipaddress.IPv4Address(".".join(parts))
        if end < start:
            raise ValueError("range end is before start")
        return _Entry(raw=s, network=None, dash=(start, end))
    # CIDR or exact IP
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError as e:
        raise ValueError(f"not a valid IP / CIDR: {s}") from e
    return _Entry(raw=s, network=net, dash=None)


def reload() -> None:
    """Re-read the BlockedIP table into the in-memory cache. Called on startup
    and after every admin mutation (add / delete / toggle)."""
    global _cache
    fresh: list[_Entry] = []
    try:
        with SessionLocal() as db:
            rows = db.query(BlockedIP).filter(BlockedIP.is_enabled.is_(True)).all()
            for r in rows:
                try:
                    fresh.append(parse_entry(r.ip_address))
                except ValueError:
                    # Bad rows just don't match — surface in the admin UI as-is.
                    continue
    except Exception:
        # Fail open if the DB is briefly unreachable — better to serve than block.
        return
    with _lock:
        _cache = fresh


def is_blocked(ip_str: str) -> str | None:
    """Returns the matching entry's raw form if blocked, else None."""
    if not ip_str:
        return None
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    with _lock:
        for entry in _cache:
            if entry.matches(ip):
                _hits[entry.raw] = _hits.get(entry.raw, 0) + 1
                return entry.raw
    return None


def flush_hits() -> None:
    """Persist accumulated hit counts to BlockedIP.block_count. Best-effort."""
    with _lock:
        if not _hits:
            return
        snapshot = dict(_hits)
        _hits.clear()
    try:
        with SessionLocal() as db:
            for raw, n in snapshot.items():
                row = db.query(BlockedIP).filter_by(ip_address=raw).first()
                if row:
                    row.block_count = (row.block_count or 0) + n
            db.commit()
    except Exception:
        pass


def hit_counts() -> dict[str, int]:
    """Snapshot of per-entry hits since last flush — surfaced to the admin UI."""
    with _lock:
        return dict(_hits)


# Paths the middleware must always serve, even from blocked IPs. Keeping the
# health probe and the OpenAPI doc reachable lets ops verify connectivity
# without first un-blocking themselves.
_BYPASS_PREFIXES = ("/api/v1/health", "/api/openapi.json", "/api/docs")


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Caddy sets X-Forwarded-For; fall back to the
    socket address."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return ""


class IPBlockMiddleware(BaseHTTPMiddleware):
    """Reject requests from IPs matched by the persistent blocklist or by an
    active IDS temp block. Mounted before any application logic so blocked
    callers never touch the database or auth code."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        for p in _BYPASS_PREFIXES:
            if path.startswith(p):
                return await call_next(request)

        ip = _client_ip(request)
        if ip:
            matched = is_blocked(ip)
            if matched:
                detector.record(
                    "blocked_ip_attempt",
                    ip,
                    severity="block",
                    path=path,
                    user_agent=request.headers.get("user-agent", ""),
                    details=f"matched={matched}",
                    persist=False,
                )
                return JSONResponse(
                    {"detail": "access denied"},
                    status_code=403,
                )
            blocked, remaining = detector.is_temp_blocked(ip)
            if blocked:
                return JSONResponse(
                    {"detail": "temporarily blocked", "retry_after_seconds": remaining},
                    status_code=429,
                    headers={"Retry-After": str(remaining)},
                )
        return await call_next(request)


# Convenience: list the current cache for the admin panel.
def cache_snapshot() -> list[str]:
    with _lock:
        return [e.raw for e in _cache]
