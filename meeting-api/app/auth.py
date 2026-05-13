"""
Authentication layer.

Two issuers feed into one resolver:

  - **one.witysk.org SSO** — HS256 JWT signed with the shared `JWT_SECRET_KEY`.
    Identified by absence of (or any non-"meet" value in) the `iss` claim.
    `sub` is the one.witysk.org `user_id`. We auto-provision a `User` row
    on first encounter so the rest of the app has a stable `users.id`
    foreign-key target.

  - **meet native accounts** — HS256 JWT minted locally on /v1/auth/login
    with `iss="meet"` and `sub="m:<users.id>"`. Same secret, same algorithm,
    so the same `decode_access_token` validates both.

`require_user` returns an `AuthUser` that wraps the matched `User` row's id
plus a flat snapshot of admin-state and contact info. Routes that mutate
meeting-creation state (POST /v1/meetings) should use `RequireAdmin`
instead, which 403s when the user has neither SSO, an active trial, a
valid voucher, nor a paid subscription.

Upgrade path (future): when one.witysk.org publishes a JWKS endpoint and
issues asymmetric tokens, swap the body of `decode_access_token` to verify
RS256/ES256 signatures via the JWKS and check the DPoP proof. The public
shape of this module stays the same.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

ACCESS_TOKEN_TTL_HOURS = 24 * 7  # mirrors one.witysk.org's typical session length


@dataclass(frozen=True)
class AuthUser:
    """Resolved request principal. The `user_id` is meet's internal users.id;
    `external_id` is the one.witysk.org user_id when relevant. Admin status
    is computed from the `User` row at resolution time so trial / voucher
    expiry takes effect without needing a sweep job."""
    user_id: int
    kind: str  # "sso" | "native"
    external_id: str | None
    email: str | None
    is_admin: bool
    is_platform_admin: bool
    # The original JWT `sub` claim — needed by older routes that store
    # owner_user_id as a free-form string keyed off the JWT sub.
    sub: str


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            # leeway tolerates small clock drift between meet and one.witysk.org
            # (both validate the same signed JWT); without it freshly-minted
            # access tokens can 401 here for ~30s after issuance.
            options={"verify_aud": False, "leeway": 30},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def issue_meet_token(user_id: int) -> str:
    """Mint a JWT for a native meet account. Same secret as SSO so a single
    decoder validates both; `iss="meet"` distinguishes the issuer."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": "meet",
            "sub": f"m:{user_id}",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=ACCESS_TOKEN_TTL_HOURS)).timestamp()),
            "type": "access",
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="expected 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _user_from_claims(claims: dict, db: Session) -> User:
    """Look up (or auto-provision) the meet User matching the JWT claims.

    Native: sub starts with "m:" — strict lookup by users.id.
    SSO: sub is the one.witysk.org user_id (string of digits) — we
    auto-create a User row with kind="sso" the first time we see them
    so subsequent requests don't need this branch."""
    sub = str(claims.get("sub") or "")
    if not sub:
        raise HTTPException(status_code=401, detail="token missing 'sub'")
    iss = claims.get("iss")

    if iss == "meet" and sub.startswith("m:"):
        try:
            uid = int(sub[2:])
        except ValueError as e:
            raise HTTPException(status_code=401, detail="malformed sub claim") from e
        u = db.query(User).filter_by(id=uid, kind="native").first()
        if not u:
            raise HTTPException(status_code=401, detail="account no longer exists")
        return u

    # SSO path. The `email` claim isn't always populated by one.witysk.org's
    # current token (we've seen sub+session+admin only), but we accept it
    # opportunistically.
    user = db.query(User).filter_by(external_id=sub, kind="sso").first()
    claim_email = claims.get("email")
    # Voucher admins (one.witysk.org user_ids 1 and 404) are platform admins
    # by spec. one.witysk.org's JWT doesn't always carry an email claim, so
    # email-based bootstrap can't catch them — match on external_id too.
    bootstrap_admin = _is_bootstrap_admin_email(claim_email) or sub in settings.voucher_admin_user_ids
    if user is None:
        user = User(
            kind="sso",
            external_id=sub,
            email=claim_email,
            is_platform_admin=bootstrap_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        dirty = False
        if claim_email and user.email != claim_email:
            # Keep email in sync if one.witysk.org starts including it later.
            user.email = claim_email
            dirty = True
        # Late-bootstrap: if the bootstrap list was edited after this user was
        # first auto-provisioned, promote on the next request.
        if not user.is_platform_admin and bootstrap_admin:
            user.is_platform_admin = True
            dirty = True
        if dirty:
            db.commit()
    return user


def is_bootstrap_admin_email(email: str | None) -> bool:
    """Public so signup can promote a native user immediately if their email
    is in the bootstrap list (avoids waiting for the next restart)."""
    if not email:
        return False
    needle = email.strip().lower()
    return any(needle == e.strip().lower() for e in settings.platform_admin_emails)


# Internal alias — kept so existing call sites elsewhere stay short.
_is_bootstrap_admin_email = is_bootstrap_admin_email


def require_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> AuthUser:
    token = _extract_bearer(authorization)
    claims = decode_access_token(token)
    # ENFORCE type=="access" — onevoice signs several JWT types with the same
    # secret (refresh, email_verification, etc.).  Previously only "refresh"
    # was blocked; any other type slipped through.  An email-verification
    # token leaked via mailbox or referer would otherwise authenticate fully.
    token_type = claims.get("type")
    if token_type != "access":
        raise HTTPException(
            status_code=401,
            detail=f"only access tokens accepted (got type={token_type!r})",
        )
    user = _user_from_claims(claims, db)
    if user.is_disabled:
        raise HTTPException(status_code=403, detail="account suspended")
    now = datetime.now(timezone.utc)
    return AuthUser(
        user_id=user.id,
        kind=user.kind,
        external_id=user.external_id,
        email=user.email,
        is_admin=user.is_admin_now(now),
        is_platform_admin=bool(user.is_platform_admin),
        sub=str(claims.get("sub")),
    )


def require_admin(user: Annotated[AuthUser, Depends(require_user)]) -> AuthUser:
    """Guard for endpoints that mutate meeting-creation state. SSO is always
    admin; native users are admin while their trial / voucher / subscription
    is active."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="admin rights required — your trial has expired and you have no active subscription",
        )
    return user


def require_voucher_admin(user: Annotated[AuthUser, Depends(require_user)]) -> AuthUser:
    """Two specific one.witysk.org users are allowed to mint vouchers. Hard-
    coded by spec; controlled via `voucher_admin_user_ids` in settings."""
    if user.kind != "sso" or user.external_id not in settings.voucher_admin_user_ids:
        raise HTTPException(status_code=403, detail="not authorised to issue vouchers")
    return user


def require_platform_admin(user: Annotated[AuthUser, Depends(require_user)]) -> AuthUser:
    """Guard for the admin panel — user management, IDS, IP blocking. Distinct
    from `require_admin` (which gates meeting creation)."""
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="platform admin rights required")
    return user


def bootstrap_platform_admins() -> None:
    """Idempotent startup hook — promotes any existing user that should be a
    platform admin. Two paths:
      1. SSO users whose `external_id` is in voucher_admin_user_ids (1, 404)
         — these are admin by spec regardless of email presence.
      2. Any user whose `email` is in PLATFORM_ADMIN_EMAILS.
    Catches users who signed up before the bootstrap list was configured."""
    from app.db import SessionLocal  # local import to avoid circular at module load
    email_needles = [e.strip().lower() for e in settings.platform_admin_emails if e.strip()]
    voucher_ids = set(settings.voucher_admin_user_ids or [])
    if not email_needles and not voucher_ids:
        return
    with SessionLocal() as db:
        for u in db.query(User).filter(User.is_platform_admin.is_(False)).all():
            email_match = bool(u.email and u.email.strip().lower() in email_needles)
            ext_match = bool(u.kind == "sso" and u.external_id in voucher_ids)
            if email_match or ext_match:
                u.is_platform_admin = True
        db.commit()


def optional_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> "AuthUser | None":
    """Best-effort auth: returns the AuthUser if a valid access JWT is
    supplied, None otherwise (no header, malformed, expired, wrong type,
    disabled account). Used by endpoints that are anonymous-friendly but
    behave slightly differently for signed-in users — for example
    `anon-token` mints a `user-<sub>` LiveKit identity instead of an
    `anon-<ULID>` one so the owner can promote them to co-host."""
    if not authorization:
        return None
    try:
        token = _extract_bearer(authorization)
        claims = decode_access_token(token)
    except HTTPException:
        return None
    if claims.get("type") != "access":
        return None
    try:
        user = _user_from_claims(claims, db)
    except HTTPException:
        return None
    if user.is_disabled:
        return None
    now = datetime.now(timezone.utc)
    return AuthUser(
        user_id=user.id,
        kind=user.kind,
        external_id=user.external_id,
        email=user.email,
        is_admin=user.is_admin_now(now),
        is_platform_admin=bool(user.is_platform_admin),
        sub=str(claims.get("sub")),
    )


RequireUser = Annotated[AuthUser, Depends(require_user)]
OptionalUser = Annotated["AuthUser | None", Depends(optional_user)]
RequireAdmin = Annotated[AuthUser, Depends(require_admin)]
RequireVoucherAdmin = Annotated[AuthUser, Depends(require_voucher_admin)]
RequirePlatformAdmin = Annotated[AuthUser, Depends(require_platform_admin)]
