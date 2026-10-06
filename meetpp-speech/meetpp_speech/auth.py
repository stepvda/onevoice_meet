"""HMAC request signing, version 2 (contract section 6.2).

    X-Meetpp-Timestamp: <unix seconds>
    X-Meetpp-Signature: hex(HMAC_SHA256(secret, msg))
    msg = "v2\\n" + ts + "\\n" + METHOD + "\\n" + target + "\\n" + hex(sha256(raw_body))

`target` is the raw request path, plus "?" + the raw query string when there is
one, exactly as sent on the wire, so the prompt and language are signed too.
Requests outside +/-60 s of the server clock are rejected, and so is a
signature that was already accepted within that window (replay).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any

TS_HEADER = "X-Meetpp-Timestamp"
SIG_HEADER = "X-Meetpp-Signature"


def sign(secret: bytes | str, ts: int | str, method: str, target: str, body: bytes) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = f"v2\n{ts}\n{method}\n{target}\n".encode("utf-8") + hashlib.sha256(body).hexdigest().encode("ascii")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def signed_headers(secret: bytes | str, method: str, target: str, body: bytes,
                   ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else int(ts)
    return {TS_HEADER: str(ts), SIG_HEADER: sign(secret, ts, method, target, body)}


def request_target(scope: Mapping[str, Any]) -> str:
    """Raw path (+ "?" + raw query) of an ASGI request, as the client sent it."""
    raw = scope.get("raw_path") or scope["path"].encode("utf-8")
    target = raw.decode("latin-1")
    query = scope.get("query_string") or b""
    return f"{target}?{query.decode('latin-1')}" if query else target


def check_timestamp(ts_header: str | None, skew_s: int, now: float | None = None) -> str | None:
    """Return an error reason, or None when the timestamp is acceptable."""
    if not ts_header:
        return "missing timestamp"
    ts_header = ts_header.strip()
    if not ts_header.isdigit() or len(ts_header) > 12:
        return "malformed timestamp"
    now = time.time() if now is None else now
    if abs(now - int(ts_header)) > skew_s:
        return "stale timestamp"
    return None


def verify(
    secret: bytes,
    ts_header: str | None,
    sig_header: str | None,
    method: str,
    target: str,
    body: bytes,
    skew_s: int = 60,
    now: float | None = None,
) -> str | None:
    """Return an error reason, or None when the request is authentic."""
    reason = check_timestamp(ts_header, skew_s, now)
    if reason:
        return reason
    if not sig_header:
        return "missing signature"
    expected = sign(secret, ts_header.strip(), method, target, body)
    if not hmac.compare_digest(expected, sig_header.strip().lower()):
        return "bad signature"
    return None


class ReplayGuard:
    """Signatures accepted recently; a repeat is a replay.

    A timestamp accepted at time t stays inside the window until t + 2 * skew
    at the latest, so each signature is kept that long. Entries expire in
    insertion order, which keeps pruning cheap."""

    def __init__(self, skew_s: float):
        self.keep_s = 2 * skew_s
        self._seen: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._seen)

    def replayed(self, signature: str, now: float | None = None) -> bool:
        """True if `signature` was already accepted; otherwise remember it."""
        now = time.time() if now is None else now
        while self._seen:
            oldest = next(iter(self._seen))
            if self._seen[oldest] > now:
                break
            del self._seen[oldest]
        signature = signature.strip().lower()
        if signature in self._seen:
            return True
        self._seen[signature] = now + self.keep_s
        return False
