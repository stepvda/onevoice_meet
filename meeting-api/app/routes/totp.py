"""
TOTP 2FA — native accounts.

Three-step setup:
  1. POST /v1/me/2fa/setup       → returns a fresh secret + otpauth URI
                                    (does NOT enable yet)
  2. POST /v1/me/2fa/enable      → user submits a code from their app;
                                    on first-success we flip enabled=True
                                    and return the recovery codes ONCE
  3. POST /v1/me/2fa/disable     → requires password + a current code
                                    or recovery code

Login becomes two-step when totp_enabled:
  POST /v1/auth/login            → 200 {requires_2fa: true,
                                         challenge_token: "..."}
  POST /v1/auth/login/2fa        → {challenge_token, code}  →  access_token

Recovery codes are 8-char base32 strings, 10 issued on enable. Stored as
argon2 hashes so a DB read doesn't disclose them. Each is single-use.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
import redis as _redis_pkg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from jose import JWTError, jwt
from passlib.hash import argon2
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import RequireUser, issue_meet_token
from app.config import settings
from app.db import get_db
from app.models import User
from app.services.email import send_email
from app.services.email_templates import login_otp as login_otp_template
from app.services.rate_limit import check as rate_limit_check

# Redis client for email-OTP storage. Codes live with a 5-minute TTL keyed
# by `eotp:<user_id>`; one code in flight at a time per user.
_redis = _redis_pkg.Redis.from_url(settings.redis_url, decode_responses=True)
_EMAIL_OTP_TTL_SECONDS = 300

router = APIRouter(prefix="/v1")

# Short-lived (5 min) intermediate JWT issued between password-OK and
# code-OK. Bound to the user; one-step-only via `type=2fa-challenge`.
_CHALLENGE_TTL_SECONDS = 300

# Number of recovery codes generated when 2FA is enabled.
_RECOVERY_CODE_COUNT = 10
# Crockford-style base32, no I/O/0/1 — easy to read and write down.
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_recovery_code() -> str:
    return "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))


def _hash_codes(codes: list[str]) -> str:
    return json.dumps([argon2.hash(c.upper()) for c in codes])


def _consume_recovery(u: User, supplied: str) -> bool:
    """If `supplied` matches one of the user's stored recovery hashes,
    remove that hash (single-use) and return True. Otherwise False."""
    if not u.totp_recovery_hashes:
        return False
    try:
        hashes: list[str] = json.loads(u.totp_recovery_hashes)
    except ValueError:
        return False
    s = supplied.strip().upper()
    for i, h in enumerate(hashes):
        try:
            if argon2.verify(s, h):
                hashes.pop(i)
                u.totp_recovery_hashes = json.dumps(hashes)
                return True
        except (ValueError, TypeError):
            continue
    return False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


# ─── Email OTP storage helpers ─────────────────────────────────────────


def _gen_email_otp() -> str:
    # 6 random digits; secrets so it's not predictable. zfill keeps leading 0s.
    return f"{secrets.randbelow(1_000_000):06d}"


def _email_otp_store(user_id: int, code: str) -> None:
    try:
        _redis.setex(f"eotp:{user_id}", _EMAIL_OTP_TTL_SECONDS, code)
    except _redis_pkg.RedisError:
        # If Redis is down, the user simply can't use email OTP this attempt.
        # We don't want to silently let any code through, so we re-raise as
        # a 503 at the call site if needed; the helper itself stays quiet.
        pass


def _email_otp_consume(user_id: int, supplied: str) -> bool:
    """Atomic compare-and-delete: a stored code matches the supplied one
    exactly once. Constant-time compare so a side-channel attacker can't
    learn the code's prefix."""
    try:
        stored = _redis.get(f"eotp:{user_id}")
    except _redis_pkg.RedisError:
        return False
    if not stored:
        return False
    if not secrets.compare_digest(stored, supplied.strip()):
        return False
    try:
        _redis.delete(f"eotp:{user_id}")
    except _redis_pkg.RedisError:
        pass
    return True


def _send_email_otp_async(name: str | None, username: str, email: str, code: str) -> None:
    subject, html, text = login_otp_template(
        name=name,
        username=username,
        code=code,
        expires_in_minutes=_EMAIL_OTP_TTL_SECONDS // 60,
    )

    async def _go() -> None:
        await send_email(to=email, subject=subject, html=html, text=text)

    asyncio.run(_go())


# ─── Challenge JWT (bridges password step → code step) ────────────────


def issue_challenge_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": "meet",
            "sub": f"m:{user_id}",
            "type": "2fa-challenge",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_CHALLENGE_TTL_SECONDS)).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _decode_challenge(token: str) -> int:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise HTTPException(status_code=401, detail="invalid or expired challenge") from e
    if claims.get("type") != "2fa-challenge":
        raise HTTPException(status_code=401, detail="not a 2FA challenge token")
    sub = str(claims.get("sub") or "")
    if not sub.startswith("m:"):
        raise HTTPException(status_code=401, detail="malformed challenge")
    try:
        return int(sub[2:])
    except ValueError as e:
        raise HTTPException(status_code=401, detail="malformed challenge") from e


# ─── Setup / enable / disable ─────────────────────────────────────────


class SetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class EnableBody(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class EnableOut(BaseModel):
    ok: bool
    recovery_codes: list[str]


class DisableBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    # Either a fresh TOTP code OR an unused recovery code. Both kinds work
    # so a user who lost their authenticator can still turn 2FA off using a
    # recovery code.
    code: str = Field(min_length=6, max_length=20)


@router.post("/me/2fa/setup")
def totp_setup(user: RequireUser, db: Session = Depends(get_db)) -> SetupOut:
    """Generate (or rotate, if not yet enabled) a TOTP secret. Does NOT
    enable 2FA — the user must call /enable with a valid code first."""
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    if u.kind != "native":
        raise HTTPException(status_code=403, detail="2FA is only configurable for native accounts")
    if u.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA already enabled — disable it first to rotate")
    secret = pyotp.random_base32()
    u.totp_secret = secret
    db.commit()
    label = u.email or u.username or f"user-{u.id}"
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name="meet.witysk.org")
    return SetupOut(secret=secret, otpauth_uri=uri)


@router.post("/me/2fa/enable")
def totp_enable(body: EnableBody, user: RequireUser, db: Session = Depends(get_db)) -> EnableOut:
    """Confirm the secret with a live code, then flip enabled=True and
    return recovery codes (only chance the user gets to see the plaintext)."""
    u = db.get(User, user.user_id)
    if not u or u.kind != "native":
        raise HTTPException(status_code=403, detail="not applicable")
    if u.totp_enabled:
        raise HTTPException(status_code=409, detail="already enabled")
    if not u.totp_secret:
        raise HTTPException(status_code=400, detail="run /2fa/setup first")
    totp = pyotp.TOTP(u.totp_secret)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="invalid code")
    codes = [_gen_recovery_code() for _ in range(_RECOVERY_CODE_COUNT)]
    u.totp_enabled = True
    u.totp_recovery_hashes = _hash_codes(codes)
    db.commit()
    return EnableOut(ok=True, recovery_codes=codes)


@router.post("/me/2fa/disable")
def totp_disable(body: DisableBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    u = db.get(User, user.user_id)
    if not u or u.kind != "native" or not u.password_hash:
        raise HTTPException(status_code=403, detail="not applicable")
    if not u.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is not enabled")
    if not argon2.verify(body.password, u.password_hash):
        raise HTTPException(status_code=401, detail="incorrect password")
    code = body.code.strip()
    ok = False
    if u.totp_secret and pyotp.TOTP(u.totp_secret).verify(code, valid_window=1):
        ok = True
    elif _consume_recovery(u, code):
        ok = True
    if not ok:
        raise HTTPException(status_code=400, detail="invalid 2FA code")
    u.totp_enabled = False
    u.totp_secret = None
    u.totp_recovery_hashes = None
    db.commit()
    return {"ok": True}


@router.post("/me/2fa/recovery-regenerate")
def totp_regenerate_recovery(body: EnableBody, user: RequireUser, db: Session = Depends(get_db)) -> EnableOut:
    """Re-issue the 10 recovery codes. Requires a fresh TOTP code so a
    drive-by request with a stolen session can't quietly mint new ones.
    Old codes are invalidated."""
    u = db.get(User, user.user_id)
    if not u or u.kind != "native":
        raise HTTPException(status_code=403, detail="not applicable")
    if not u.totp_enabled or not u.totp_secret:
        raise HTTPException(status_code=409, detail="2FA is not enabled")
    if not pyotp.TOTP(u.totp_secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="invalid code")
    codes = [_gen_recovery_code() for _ in range(_RECOVERY_CODE_COUNT)]
    u.totp_recovery_hashes = _hash_codes(codes)
    db.commit()
    return EnableOut(ok=True, recovery_codes=codes)


# ─── Email OTP enable / disable ───────────────────────────────────────


class EmailOtpStartOut(BaseModel):
    ok: bool
    sent_to: str  # masked email so the SPA can show "we sent a code to a***@b.com"


class EmailOtpConfirmBody(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class EmailOtpDisableBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


def _mask_email(addr: str) -> str:
    name, _, domain = addr.partition("@")
    if not domain or len(name) < 2:
        return addr
    return f"{name[0]}***@{domain}"


@router.post("/me/2fa/email/start")
def email_otp_start(
    user: RequireUser,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> EmailOtpStartOut:
    """Mail a one-time code to the user's verified address. The user submits
    that code to /me/2fa/email/confirm to flip email_otp_enabled. Re-runnable
    so the user can request a fresh code if the first didn't arrive."""
    rate_limit_check(
        "eotp_send",
        f"u:{user.user_id}",
        limit=3,
        window_seconds=600,
        detail="too many code requests; try again in a few minutes",
    )
    u = db.get(User, user.user_id)
    if not u or u.kind != "native":
        raise HTTPException(status_code=403, detail="email OTP is only configurable for native accounts")
    if not u.email:
        raise HTTPException(status_code=400, detail="set an email on your account first")
    code = _gen_email_otp()
    _email_otp_store(u.id, code)
    background.add_task(_send_email_otp_async, u.name, u.username or "user", u.email, code)
    return EmailOtpStartOut(ok=True, sent_to=_mask_email(u.email))


@router.post("/me/2fa/email/confirm")
def email_otp_confirm(body: EmailOtpConfirmBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    """Confirm the code mailed by /me/2fa/email/start; flips the flag on."""
    u = db.get(User, user.user_id)
    if not u or u.kind != "native":
        raise HTTPException(status_code=403, detail="not applicable")
    if u.email_otp_enabled:
        raise HTTPException(status_code=409, detail="email OTP already enabled")
    if not _email_otp_consume(u.id, body.code):
        raise HTTPException(status_code=400, detail="invalid or expired code")
    u.email_otp_enabled = True
    db.commit()
    return {"ok": True}


@router.post("/me/2fa/email/disable")
def email_otp_disable(body: EmailOtpDisableBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    u = db.get(User, user.user_id)
    if not u or u.kind != "native" or not u.password_hash:
        raise HTTPException(status_code=403, detail="not applicable")
    if not u.email_otp_enabled:
        raise HTTPException(status_code=409, detail="email OTP is not enabled")
    if not argon2.verify(body.password, u.password_hash):
        raise HTTPException(status_code=401, detail="incorrect password")
    u.email_otp_enabled = False
    db.commit()
    return {"ok": True}


# ─── Login second step ────────────────────────────────────────────────


class LoginEmailOtpSendBody(BaseModel):
    challenge_token: str = Field(min_length=20, max_length=4096)


class LoginEmailOtpSendOut(BaseModel):
    ok: bool
    sent_to: str


@router.post("/auth/login/email-otp/send")
def login_email_otp_send(
    body: LoginEmailOtpSendBody,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> LoginEmailOtpSendOut:
    """During two-step login, the SPA can ask us to mail a one-time code.
    Bound to the challenge token (so a random caller can't spam users) and
    to the user's email-OTP flag (we don't email codes to accounts that
    haven't opted in)."""
    rate_limit_check(
        "login_eotp",
        _client_ip(request),
        limit=10,
        window_seconds=600,
        detail="too many code requests; try again shortly",
    )
    user_id = _decode_challenge(body.challenge_token)
    u = db.get(User, user_id)
    if not u or not u.email_otp_enabled or not u.email:
        raise HTTPException(status_code=400, detail="email OTP not enabled for this account")
    code = _gen_email_otp()
    _email_otp_store(u.id, code)
    background.add_task(_send_email_otp_async, u.name, u.username or "user", u.email, code)
    return LoginEmailOtpSendOut(ok=True, sent_to=_mask_email(u.email))


class LoginVerifyBody(BaseModel):
    challenge_token: str = Field(min_length=20, max_length=4096)
    code: str = Field(min_length=6, max_length=20)


@router.post("/auth/login/2fa")
def login_verify(body: LoginVerifyBody, request: Request, db: Session = Depends(get_db)) -> dict:
    """Second step of two-step login. The supplied `code` is checked, in
    order, against:
      1. The user's TOTP secret (if totp_enabled)
      2. A pending email OTP (if email_otp_enabled and one is in Redis)
      3. The user's recovery codes (single-use; only set up alongside TOTP)
    Returns the real access token on first match.
    """
    rate_limit_check(
        "login_2fa",
        _client_ip(request),
        limit=20,
        window_seconds=900,
        detail="too many 2FA attempts; try again shortly",
    )
    user_id = _decode_challenge(body.challenge_token)
    u = db.get(User, user_id)
    if not u or (not u.totp_enabled and not u.email_otp_enabled):
        raise HTTPException(status_code=400, detail="2FA not enabled for this account")
    code = body.code.strip()
    ok = False
    if u.totp_enabled and u.totp_secret and pyotp.TOTP(u.totp_secret).verify(code, valid_window=1):
        ok = True
    elif u.email_otp_enabled and _email_otp_consume(u.id, code):
        ok = True
    elif _consume_recovery(u, code):
        ok = True
    if not ok:
        # Feed the IDS — 2FA failures are tighter than auth failures since
        # the password was already correct, so a small threshold trips a block.
        from app.services.intrusion_detector import EventType, SEVERITY_WARN, detector  # noqa: PLC0415

        detector.record(
            EventType.TWOFA_FAILURE,
            _client_ip(request),
            severity=SEVERITY_WARN,
            user_id=u.id,
            handle=u.username or u.email,
            path="/v1/auth/login/2fa",
            user_agent=request.headers.get("user-agent", ""),
        )
        raise HTTPException(status_code=401, detail="invalid 2FA code")
    db.commit()
    # Late import to avoid a circular dep with auth_native at module load.
    from app.routes.auth_native import _to_me  # noqa: PLC0415

    return {
        "access_token": issue_meet_token(u.id),
        "user": _to_me(u).model_dump(mode="json"),
    }
