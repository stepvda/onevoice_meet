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
    if user is None:
        user = User(
            kind="sso",
            external_id=sub,
            email=claims.get("email"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif claims.get("email") and user.email != claims["email"]:
        # Keep email in sync if one.witysk.org starts including it later.
        user.email = claims["email"]
        db.commit()
    return user


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
    now = datetime.now(timezone.utc)
    return AuthUser(
        user_id=user.id,
        kind=user.kind,
        external_id=user.external_id,
        email=user.email,
        is_admin=user.is_admin_now(now),
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


RequireUser = Annotated[AuthUser, Depends(require_user)]
RequireAdmin = Annotated[AuthUser, Depends(require_admin)]
RequireVoucherAdmin = Annotated[AuthUser, Depends(require_voucher_admin)]
