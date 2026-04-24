"""
Authentication layer.

Current reality (April 2026):
  one.witysk.org issues HS256 JWTs signed with a shared SECRET_KEY.
  There is no JWKS endpoint, no DPoP, no asymmetric signing.

This module validates those HS256 tokens. Set JWT_SECRET_KEY in .env to the
same value as one.witysk.org's SECRET_KEY so tokens are valid across both
services.

Upgrade path (future): when one.witysk.org publishes a JWKS endpoint and issues
DPoP-bound access tokens, swap the body of `decode_access_token` to fetch the
JWKS, verify RS256/ES256 signatures, and validate the DPoP proof header. Keep
the public surface of this module the same so callers do not change.
"""
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt

from app.config import settings


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str | None
    session: str | None


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            # audience check is optional — one.witysk.org currently does not set `aud`,
            # so we don't require it. If it starts setting one, add options={"require_aud": True}.
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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


def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    token = _extract_bearer(authorization)
    claims = decode_access_token(token)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token missing 'sub' claim",
        )
    # onevoice tokens distinguish access vs refresh; reject refresh tokens here.
    if claims.get("type") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token not accepted here",
        )
    return AuthUser(
        user_id=str(sub),
        email=claims.get("email"),
        session=claims.get("session"),
    )


RequireUser = Annotated[AuthUser, Depends(require_user)]
