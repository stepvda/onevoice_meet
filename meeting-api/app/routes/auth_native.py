"""
Native account auth + profile + facepic.

These endpoints exist alongside one.witysk.org SSO. Tokens minted here
share the JWT secret/algorithm so the unified `require_user` resolver
in app.auth handles both kinds in one path.

Trial: a native account gets a one-time 10-day trial on signup. After
expiry, meeting creation is blocked unless they hold a voucher
entitlement (Phase 2) or paid PayPal subscription (Phase 3).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from passlib.hash import argon2
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.auth import RequireUser, is_bootstrap_admin_email, issue_meet_token
from app.config import settings
from app.db import get_db
from app.models import PasswordResetToken, User
from app.services.email import send_email
from app.services.email_templates import account_welcome, password_reset
from app.services.intrusion_detector import EventType, SEVERITY_WARN, detector
from app.services.rate_limit import check as rate_limit_check


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit bucketing. Behind Caddy the
    real address is in `X-Forwarded-For` — take its first hop. Falls back
    to the socket address (useful in dev)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"

PASSWORD_RESET_TTL_MINUTES = 30

router = APIRouter(prefix="/v1")

# Login handle is either email or username; allow letters/digits/._- in
# usernames to keep them URL-safe and human-friendly.
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
_PASSWORD_MIN = 8

_ALLOWED_FACEPIC_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _trial_remaining_days(u: User) -> int | None:
    """Whole days left of the 10-day trial. Returns None if trial never started
    or has already expired."""
    if u.trial_started_at is None:
        return None
    from datetime import timedelta as _td
    end = u.trial_started_at if u.trial_started_at.tzinfo else u.trial_started_at.replace(tzinfo=timezone.utc)
    end = end + _td(days=10)
    now = datetime.now(timezone.utc)
    if end <= now:
        return None
    return max(0, (end - now).days)


class SignupBody(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=_PASSWORD_MIN, max_length=200)
    name: str | None = Field(default=None, max_length=120)


class LoginBody(BaseModel):
    # Accept either email or username in the same field, like most apps.
    handle: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class TokenAndUserOut(BaseModel):
    access_token: str
    user: "MeOut"


class MeOut(BaseModel):
    id: int
    kind: str
    # one.witysk.org user_id for SSO accounts; null for native. Surfaced so the
    # SPA can decide who sees the voucher-admin entry without re-fetching from
    # one.witysk.org.
    external_id: str | None
    email: str | None
    username: str | None
    name: str | None
    facepic_path: str | None
    is_admin: bool
    is_voucher_admin: bool
    is_platform_admin: bool
    # Trial bookkeeping for native accounts. `trial_used=False` AND no
    # entitlement means the SPA can offer the "Start trial" button on /upgrade.
    trial_used: bool
    trial_days_remaining: int | None
    entitlement_kind: str | None
    entitlement_expires_at: datetime | None
    # 2FA state for native users. SSO accounts surface as *_enabled=False
    # since their second factor (if any) is owned by one.witysk.org.
    totp_enabled: bool
    totp_recovery_remaining: int
    email_otp_enabled: bool


def _to_me(u: User) -> MeOut:
    now = datetime.now(timezone.utc)
    recovery_remaining = 0
    if u.totp_recovery_hashes:
        try:
            import json as _json
            recovery_remaining = len(_json.loads(u.totp_recovery_hashes))
        except ValueError:
            recovery_remaining = 0
    return MeOut(
        id=u.id,
        kind=u.kind,
        external_id=u.external_id,
        email=u.email,
        username=u.username,
        name=u.name,
        facepic_path=u.facepic_path,
        is_admin=u.is_admin_now(now),
        is_voucher_admin=(u.kind == "sso" and u.external_id in settings.voucher_admin_user_ids),
        is_platform_admin=bool(u.is_platform_admin),
        trial_used=bool(u.trial_used),
        trial_days_remaining=_trial_remaining_days(u),
        entitlement_kind=u.entitlement_kind,
        entitlement_expires_at=u.entitlement_expires_at,
        totp_enabled=bool(u.totp_enabled),
        totp_recovery_remaining=recovery_remaining,
        email_otp_enabled=bool(u.email_otp_enabled),
    )


# ─── Signup / Login / Logout ───────────────────────────────────────────


def _send_welcome_async(name: str | None, username: str, email: str) -> None:
    """Resend's HTTP API is async; FastAPI's BackgroundTasks runs sync and async
    callables both, so we wrap the coroutine to keep the call site ergonomic."""
    subject, html, text = account_welcome(
        name=name,
        username=username,
        signup_url=f"{settings.public_url}/login",
    )

    async def _go() -> None:
        await send_email(to=email, subject=subject, html=html, text=text)

    asyncio.run(_go())


@router.post("/auth/signup", status_code=201)
def signup(body: SignupBody, background: BackgroundTasks, request: Request, db: Session = Depends(get_db)) -> TokenAndUserOut:
    """Create a native meet account. Starts the one-time 10-day trial.

    Username must be lowercase letters/digits/._-, 3–32 chars. We compare
    case-insensitively to avoid look-alike collisions.
    """
    rate_limit_check(
        "signup",
        _client_ip(request),
        limit=5,
        window_seconds=3600,
        detail="too many signups from this address; try again in an hour",
    )
    uname = body.username.strip().lower()
    if not _USERNAME_RE.match(uname):
        raise HTTPException(status_code=400, detail="username must be 3-32 chars: letters, digits, . _ -")
    email = str(body.email).strip().lower()
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(status_code=409, detail="email already registered")
    if db.query(User).filter_by(username=uname).first():
        raise HTTPException(status_code=409, detail="username taken")
    now = datetime.now(timezone.utc)
    u = User(
        kind="native",
        email=email,
        username=uname,
        name=(body.name or "").strip() or None,
        password_hash=argon2.hash(body.password),
        # Promote on signup if their email matches PLATFORM_ADMIN_EMAILS so a
        # configured operator who signs up natively (rather than via SSO)
        # gets the panel without an extra restart.
        is_platform_admin=is_bootstrap_admin_email(email),
        # Trial is opt-in (per spec: "a free trial that they can sign-up for").
        # Native users start with no entitlement; clicking "Start free trial"
        # on /upgrade is what flips trial_used + trial_started_at and grants
        # the 10-day admin window. Until then they can sign in, edit profile,
        # join meetings — but cannot create meetings.
    )
    _ = now  # kept above for symmetry; trial fields stay default (None / False)
    db.add(u)
    db.commit()
    db.refresh(u)
    # Welcome email — fire-and-forget so signup doesn't fail if Resend is
    # down or rate-limited.
    background.add_task(_send_welcome_async, u.name, uname, email)
    return TokenAndUserOut(access_token=issue_meet_token(u.id), user=_to_me(u))


@router.post("/auth/login")
def login(body: LoginBody, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)) -> TokenAndUserOut | dict:
    # Two limits in parallel: per-IP (catches credential stuffing from one
    # source) and per-handle (catches a botnet hammering one account).
    handle = body.handle.strip().lower()
    rate_limit_check(
        "login_ip",
        _client_ip(request),
        limit=20,
        window_seconds=3600,
        detail="too many login attempts; try again in an hour",
    )
    rate_limit_check(
        "login_handle",
        handle,
        limit=10,
        window_seconds=900,
        detail="too many login attempts for this account; try again shortly",
    )
    u = (
        db.query(User)
        .filter(
            (User.email == handle) | (User.username == handle),
            User.kind == "native",
        )
        .first()
    )
    if not u or not u.password_hash or not argon2.verify(body.password, u.password_hash):
        # Feed the IDS so brute-force attempts trip the temp-block threshold.
        detector.record(
            EventType.AUTH_FAILURE,
            _client_ip(request),
            severity=SEVERITY_WARN,
            user_id=u.id if u else None,
            handle=handle,
            path="/v1/auth/login",
            user_agent=request.headers.get("user-agent", ""),
        )
        # Same generic error for "no such user" and "wrong password" so we
        # don't disclose which accounts exist.
        raise HTTPException(status_code=401, detail="invalid credentials")
    if u.is_disabled:
        raise HTTPException(status_code=403, detail="account suspended")
    if u.totp_enabled or u.email_otp_enabled:
        # Defer access-token issue until /auth/login/2fa succeeds.
        # Late import: app.routes.totp imports _to_me from this module.
        from app.routes.totp import (  # noqa: PLC0415
            _email_otp_store,
            _gen_email_otp,
            _mask_email,
            _send_email_otp_async,
            issue_challenge_token,
        )

        ch = issue_challenge_token(u.id)
        # If the user only has email OTP, send the code immediately so the
        # SPA can show "code sent — enter it below" without an extra round
        # trip. When TOTP is also enabled, we wait for an explicit request
        # so we don't email people who'll just use their authenticator.
        sent_to: str | None = None
        if u.email_otp_enabled and not u.totp_enabled and u.email:
            code = _gen_email_otp()
            _email_otp_store(u.id, code)
            background.add_task(_send_email_otp_async, u.name, u.username or "user", u.email, code)
            sent_to = _mask_email(u.email)
        return {
            "requires_2fa": True,
            "challenge_token": ch,
            "totp_enabled": bool(u.totp_enabled),
            "email_otp_enabled": bool(u.email_otp_enabled),
            "email_otp_sent_to": sent_to,
        }
    return TokenAndUserOut(access_token=issue_meet_token(u.id), user=_to_me(u))


@router.post("/auth/logout")
def logout(user: RequireUser) -> dict:
    """Stateless JWT — nothing to invalidate server-side. The SPA drops the
    token from localStorage. Kept as an explicit endpoint so the client has a
    single canonical "I'm logging off" hook for future audit logging."""
    _ = user
    return {"ok": True}


@router.post("/me/start-trial")
def start_trial(user: RequireUser, db: Session = Depends(get_db)) -> MeOut:
    """Claim the one-time 10-day free trial. Native-only; rejected if the
    user has already used their trial. SSO accounts already have admin and
    don't need this."""
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    if u.kind != "native":
        raise HTTPException(status_code=403, detail="trial is only for native accounts")
    if u.trial_used:
        raise HTTPException(status_code=409, detail="trial has already been used")
    u.trial_started_at = datetime.now(timezone.utc)
    u.trial_used = True
    db.commit()
    db.refresh(u)
    return _to_me(u)


# ─── Profile read / update ─────────────────────────────────────────────


class UpdateMeBody(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    username: str | None = Field(default=None, min_length=3, max_length=32)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=_PASSWORD_MIN, max_length=200)


@router.get("/me")
def get_me(user: RequireUser, db: Session = Depends(get_db)) -> MeOut:
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    return _to_me(u)


@router.patch("/me")
def update_me(body: UpdateMeBody, user: RequireUser, db: Session = Depends(get_db)) -> MeOut:
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    # SSO users keep their identity from one.witysk.org — we don't let them
    # rewrite name/email/username locally, since one.witysk.org is the source
    # of truth and any change here would just diverge.
    if u.kind == "sso":
        raise HTTPException(status_code=403, detail="SSO accounts manage profile on one.witysk.org")
    if body.name is not None:
        u.name = body.name.strip() or None
    if body.email is not None:
        new_email = str(body.email).strip().lower()
        if new_email != u.email:
            if db.query(User).filter_by(email=new_email).first():
                raise HTTPException(status_code=409, detail="email already registered")
            u.email = new_email
    if body.username is not None:
        new_un = body.username.strip().lower()
        if new_un != u.username:
            if not _USERNAME_RE.match(new_un):
                raise HTTPException(status_code=400, detail="invalid username")
            if db.query(User).filter_by(username=new_un).first():
                raise HTTPException(status_code=409, detail="username taken")
            u.username = new_un
    db.commit()
    db.refresh(u)
    return _to_me(u)


@router.post("/me/password")
def change_password(body: ChangePasswordBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    u = db.get(User, user.user_id)
    if not u or u.kind != "native" or not u.password_hash:
        raise HTTPException(status_code=403, detail="not applicable for SSO accounts")
    if not argon2.verify(body.current_password, u.password_hash):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    u.password_hash = argon2.hash(body.new_password)
    db.commit()
    return {"ok": True}


# ─── Facepic upload / serve / delete ───────────────────────────────────


@router.post("/me/facepic")
async def upload_facepic(
    user: RequireUser,
    file: Annotated[UploadFile, File()],
    db: Session = Depends(get_db),
) -> MeOut:
    """Native-only. SSO users always render one.witysk.org's facepic via the
    SPA's Facepic component, so meet has no upload UI for them."""
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    if u.kind == "sso":
        raise HTTPException(status_code=403, detail="SSO accounts upload their facepic on one.witysk.org")

    ct = (file.content_type or "").lower()
    ext = _ALLOWED_FACEPIC_TYPES.get(ct)
    if not ext:
        raise HTTPException(status_code=415, detail=f"unsupported content type {ct!r}; use jpeg/png/webp/gif")

    Path(settings.facepics_dir).mkdir(parents=True, exist_ok=True)
    target = Path(settings.facepics_dir) / f"{uuid.uuid4().hex}{ext}"
    cap = settings.facepic_max_bytes
    total = 0
    with target.open("wb") as out:
        while chunk := await file.read(64 * 1024):
            total += len(chunk)
            if total > cap:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"image exceeds {cap} bytes")
            out.write(chunk)

    # Best-effort: remove the previous file so we don't accumulate orphans.
    if u.facepic_path and u.facepic_path != str(target):
        try:
            Path(u.facepic_path).unlink(missing_ok=True)
        except OSError:
            pass
    u.facepic_path = str(target)
    db.commit()
    db.refresh(u)
    return _to_me(u)


@router.delete("/me/facepic")
def delete_facepic(user: RequireUser, db: Session = Depends(get_db)) -> MeOut:
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    if u.kind == "sso":
        raise HTTPException(status_code=403, detail="SSO accounts manage their facepic on one.witysk.org")
    if u.facepic_path:
        try:
            Path(u.facepic_path).unlink(missing_ok=True)
        except OSError:
            pass
    u.facepic_path = None
    db.commit()
    db.refresh(u)
    return _to_me(u)


@router.get("/users/{user_id}/facepic")
def get_user_facepic(user_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Serve a native user's uploaded facepic. Public — facepics are not
    sensitive (they're shown to other meeting participants regardless of
    auth state). Returns 404 if the user is SSO (no local facepic) or has
    no upload."""
    u = db.get(User, user_id)
    if not u or u.kind != "native" or not u.facepic_path:
        raise HTTPException(status_code=404, detail="no facepic")
    p = Path(u.facepic_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="facepic file missing")
    ext = p.suffix.lower()
    media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
    return FileResponse(path=str(p), media_type=media, headers={"Cache-Control": "public, max-age=300"})


# Forward-ref resolution so MeOut is bound before TokenAndUserOut uses it.
TokenAndUserOut.model_rebuild()


# ─── Password reset (request + confirm) ───────────────────────────────


class PasswordResetRequestBody(BaseModel):
    email: EmailStr


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=_PASSWORD_MIN, max_length=200)


def _send_reset_email(name: str | None, username: str, email: str, token: str) -> None:
    """Synchronous helper invoked from BackgroundTasks. Wraps the async send
    so we don't need an async route for what is fundamentally a fire-and-forget
    side effect."""
    reset_url = f"{settings.public_url}/reset-password#token={token}"
    subject, html, text = password_reset(
        name=name,
        username=username,
        reset_url=reset_url,
        expires_in_minutes=PASSWORD_RESET_TTL_MINUTES,
    )

    async def _go() -> None:
        await send_email(to=email, subject=subject, html=html, text=text)

    asyncio.run(_go())


@router.post("/auth/password-reset/request")
def request_password_reset(
    body: PasswordResetRequestBody,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Always returns OK — we never confirm whether an email is registered,
    since that would let an attacker enumerate accounts. The email only
    gets sent if the address actually has a native account."""
    rate_limit_check(
        "pwreset_ip",
        _client_ip(request),
        limit=5,
        window_seconds=3600,
        detail="too many reset requests from this address; try again in an hour",
    )
    rate_limit_check(
        "pwreset_email",
        str(body.email).strip().lower(),
        limit=3,
        window_seconds=3600,
        detail="too many reset requests for this address; try again later",
    )
    email = str(body.email).strip().lower()
    u = db.query(User).filter_by(email=email, kind="native").first()
    if u and u.password_hash:
        # 32 bytes of randomness encoded base32-ish (urlsafe). Mailed as the
        # plaintext token; only a SHA-256 hash is stored so a DB leak doesn't
        # surface live tokens.
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # Invalidate previously-issued tokens for this user — only the most
        # recent one should be redeemable.
        db.query(PasswordResetToken).filter_by(user_id=u.id, used_at=None).update(
            {PasswordResetToken.used_at: datetime.now(timezone.utc)}
        )
        row = PasswordResetToken(
            user_id=u.id,
            token_hash=digest,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
        )
        db.add(row)
        db.commit()
        background.add_task(_send_reset_email, u.name, u.username or "user", email, raw)
    return {"ok": True}


@router.post("/auth/password-reset/confirm")
def confirm_password_reset(
    body: PasswordResetConfirmBody,
    db: Session = Depends(get_db),
) -> dict:
    digest = hashlib.sha256(body.token.encode("utf-8")).hexdigest()
    row = db.query(PasswordResetToken).filter_by(token_hash=digest).first()
    now = datetime.now(timezone.utc)
    if not row or row.used_at is not None:
        raise HTTPException(status_code=400, detail="invalid or already-used token")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise HTTPException(status_code=400, detail="token expired")
    u = db.get(User, row.user_id)
    if not u or u.kind != "native":
        raise HTTPException(status_code=400, detail="account no longer eligible")
    u.password_hash = argon2.hash(body.new_password)
    row.used_at = now
    db.commit()
    return {"ok": True}


# ─── Account deletion ─────────────────────────────────────────────────


class DeleteAccountBody(BaseModel):
    # Confirm with current password to make accidental clicks harder.
    password: str = Field(min_length=1, max_length=200)


@router.delete("/me")
def delete_my_account(
    body: DeleteAccountBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Native-only. SSO accounts are managed on one.witysk.org and
    can't be deleted from meet — closing them would orphan their
    one.witysk.org identity here on next login anyway. We hard-delete
    the User row plus its facepic file; meetings the user owned stay
    around (they're keyed off the JWT-sub string and the historical
    rows still serve as audit history). Vouchers they redeemed keep
    their `redeemed_by_user_id` foreign-key cleared via SET NULL is
    *not* configured today — instead we null it explicitly here so
    the voucher row stays intact for ledger purposes."""
    u = db.get(User, user.user_id)
    if not u:
        raise HTTPException(status_code=404, detail="account not found")
    if u.kind != "native":
        raise HTTPException(status_code=403, detail="SSO accounts can only be closed on one.witysk.org")
    if not u.password_hash or not argon2.verify(body.password, u.password_hash):
        raise HTTPException(status_code=401, detail="incorrect password")

    # Best-effort facepic cleanup.
    if u.facepic_path:
        try:
            Path(u.facepic_path).unlink(missing_ok=True)
        except OSError:
            pass

    # Detach this user from any voucher rows so the ledger stays readable
    # (the redemption fact remains; the user reference disappears).
    from sqlalchemy import update as _update
    from app.models import Voucher as _Voucher

    db.execute(
        _update(_Voucher)
        .where(_Voucher.redeemed_by_user_id == u.id)
        .values(redeemed_by_user_id=None)
    )
    # And invalidate any pending password-reset tokens.
    db.query(PasswordResetToken).filter_by(user_id=u.id).delete()
    db.delete(u)
    db.commit()
    return {"ok": True}
