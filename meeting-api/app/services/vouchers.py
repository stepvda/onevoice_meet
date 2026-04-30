"""
Voucher code generation + verification.

Vouchers are 8-character human-friendly codes. Each carries an HMAC
fingerprint stored in the DB row, so a redemption attempt verifies both
that the row exists AND that the code matches the HMAC of the secret —
prevents an attacker who learned a row id from forging redeemable codes
without knowing the signing key.

The signing key is a long random string in `.env` as `VOUCHER_SIGNING_KEY`.
Rotating it invalidates every previously-issued unredeemed voucher,
which is the intended behaviour.

Code alphabet skips visually-confusing characters (0/O, 1/I/L) so vouchers
read cleanly off paper / chat. 32^8 ≈ 1.1×10¹² code space; for realistic
voucher counts (low thousands) collisions are improbable but we still
retry on a unique-constraint violation just in case.
"""
from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from app.config import settings

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L


class VoucherKeyMissing(RuntimeError):
    """Raised when VOUCHER_SIGNING_KEY hasn't been configured."""


def _require_key() -> bytes:
    if not settings.voucher_signing_key:
        raise VoucherKeyMissing(
            "VOUCHER_SIGNING_KEY is not set in .env — vouchers cannot be issued or redeemed"
        )
    return settings.voucher_signing_key.encode("utf-8")


def generate_code() -> str:
    """Eight characters drawn uniformly from the unambiguous alphabet."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


def hmac_for(code: str) -> str:
    """Hex HMAC-SHA256 of the upper-cased code under the signing key."""
    return hmac.new(_require_key(), code.upper().encode("utf-8"), sha256).hexdigest()


def verify(code: str, expected_hmac: str) -> bool:
    """Constant-time comparison of the code's HMAC against the stored value."""
    try:
        return hmac.compare_digest(hmac_for(code), expected_hmac)
    except VoucherKeyMissing:
        return False
