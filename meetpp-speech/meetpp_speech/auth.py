"""HMAC request signing (contract section 6.2).

    X-Meetpp-Timestamp: <unix seconds>
    X-Meetpp-Signature: hex(HMAC_SHA256(secret, ts + "." + raw_body))

Only the raw body is signed (not the query string), exactly as the contract
says. Requests outside +/-60 s of the server clock are rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import time

TS_HEADER = "X-Meetpp-Timestamp"
SIG_HEADER = "X-Meetpp-Signature"


def sign(secret: bytes | str, ts: int | str, body: bytes) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = str(ts).encode("ascii") + b"." + body
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def signed_headers(secret: bytes | str, body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else int(ts)
    return {TS_HEADER: str(ts), SIG_HEADER: sign(secret, ts, body)}


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
    expected = sign(secret, ts_header.strip(), body)
    if not hmac.compare_digest(expected, sig_header.strip().lower()):
        return "bad signature"
    return None
