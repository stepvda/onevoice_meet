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

  - **One Voice Mail** (when `MAIL_ISSUER` is set) — ES256 JWT minted by
    Mail for audience `meet`, verified against Mail's own JWKS. A user who
    linked One Voice carries `onevoice_sub` and maps to their SSO account
    (`AuthUser.sub` = that one.witysk.org user_id, same Meet account as at
    meet.witysk.org); anyone else is a `kind="mail"` user with
    `external_id = "mail:<Mail user id>"` and `AuthUser.sub` = the same.
    See docs/MAIL_INTEGRATION.md.

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
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

log = logging.getLogger(__name__)

ACCESS_TOKEN_TTL_HOURS = 24 * 7  # mirrors one.witysk.org's typical session length


@dataclass(frozen=True)
class AuthUser:
    """Resolved request principal. The `user_id` is meet's internal users.id;
    `external_id` is the one.witysk.org user_id when relevant. Admin status
    is computed from the `User` row at resolution time so trial / voucher
    expiry takes effect without needing a sweep job."""
    user_id: int
    kind: str  # "sso" | "native" | "mail"
    external_id: str | None
    email: str | None
    is_admin: bool
    is_platform_admin: bool
    # The original JWT `sub` claim — needed by older routes that store
    # owner_user_id as a free-form string keyed off the JWT sub.
    sub: str


# one.witysk.org JWKS cache (its ES256 public keys). Short TTL; on a fetch
# failure we fall back to the last-known keys so a transient one.witysk.org blip
# does not 401 every SSO login.
_JWKS_CACHE: dict = {"keys": None, "at": 0.0}
_JWKS_TTL_SECONDS = 600


def _onevoice_jwks() -> list[dict]:
    now = time.time()
    cached = _JWKS_CACHE["keys"]
    if cached is not None and (now - _JWKS_CACHE["at"]) < _JWKS_TTL_SECONDS:
        return cached
    try:
        resp = httpx.get(settings.onevoice_jwks_url, timeout=5.0)
        resp.raise_for_status()
        keys = resp.json().get("keys") or []
        _JWKS_CACHE["keys"] = keys
        _JWKS_CACHE["at"] = now
        return keys
    except Exception:
        return cached or []


# One Voice Mail's JWKS cache — kept apart from one.witysk.org's so neither
# issuer's keys can ever verify the other's tokens. Same TTL and fallback; a
# `kid` the cache does not know (Mail rotated its key) triggers one refetch;
# such forced refetches happen at most every _MAIL_JWKS_MIN_REFETCH_SECONDS so
# random kids cannot make us hammer Mail.
_MAIL_JWKS_CACHE: dict = {"keys": None, "at": 0.0, "forced_at": 0.0}
_MAIL_JWKS_MIN_REFETCH_SECONDS = 10.0


def is_mail_issuer(iss: object) -> bool:
    """True when `iss` is the configured One Voice Mail issuer (integration on)."""
    issuer = settings.mail_issuer_normalised
    return bool(issuer) and isinstance(iss, str) and iss.strip().rstrip("/") == issuer


def _fetch_mail_jwks() -> list[dict]:
    resp = httpx.get(settings.mail_jwks_endpoint, timeout=5.0)
    resp.raise_for_status()
    return resp.json().get("keys") or []


def _mail_jwks(*, refresh: bool = False) -> list[dict]:
    now = time.time()
    cached = _MAIL_JWKS_CACHE["keys"]
    if cached is not None and now - _MAIL_JWKS_CACHE["at"] < _JWKS_TTL_SECONDS and not refresh:
        return cached
    if refresh:
        if now - _MAIL_JWKS_CACHE.get("forced_at", 0.0) < _MAIL_JWKS_MIN_REFETCH_SECONDS:
            return cached or []
        _MAIL_JWKS_CACHE["forced_at"] = now
    try:
        keys = _fetch_mail_jwks()
    except Exception as exc:  # noqa: BLE001 — keep the last-known keys
        log.warning("mail jwks fetch from %s failed: %s", settings.mail_jwks_endpoint, exc)
        return cached or []
    _MAIL_JWKS_CACHE["keys"] = keys
    _MAIL_JWKS_CACHE["at"] = now
    return keys


def _pick_jwk(keys: list[dict], kid: str | None) -> dict | None:
    jwk = next((k for k in keys if kid and k.get("kid") == kid), None)
    if jwk is None and not kid and len(keys) == 1:
        jwk = keys[0]  # single published key → kid is optional
    return jwk


def _decode_mail_token(token: str, header: dict) -> dict:
    """Verify a Mail-minted token: Mail's JWKS, PINNED to ES256, audience
    MAIL_AUDIENCE, `exp` required. The caller routed here on the unverified
    `iss`; the verified signature covers that claim, so it cannot be swapped."""
    kid = header.get("kid")
    jwk = _pick_jwk(_mail_jwks(), kid)
    if jwk is None and kid:
        jwk = _pick_jwk(_mail_jwks(refresh=True), kid)
    if jwk is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no matching verification key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return jwt.decode(
        token,
        jwk,
        algorithms=["ES256"],
        audience=settings.mail_audience,
        options={"leeway": 30, "require_exp": True, "require_aud": True},
    )


def decode_access_token(token: str) -> dict:
    """Verify a bearer JWT. Dual-scheme during the one.witysk.org HS256→ES256
    signing migration:

      - **ES256** (header alg) → a one.witysk.org SSO token: verify against the
        published JWKS public key, PINNED to ES256.
      - **HS256** → a pre-migration SSO token OR a meet-native token: verify
        against the shared secret, PINNED to HS256.

    The per-branch algorithm pin is the security crux (algorithm-confusion
    defense): an attacker who signs an HS256 token using the ES256 *public* key
    as the HMAC secret routes to the HS256 branch and is checked against the real
    shared secret — which they do not have — so the forgery fails. `leeway`
    tolerates ~30s clock drift between meet and one.witysk.org.

    A token whose unverified `iss` is MAIL_ISSUER takes neither branch: it is
    verified against Mail's JWKS only (`_decode_mail_token`), whatever its
    header says."""
    opts = {"options": {"verify_aud": False, "leeway": 30}}
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.get_unverified_claims(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    alg = header.get("alg")
    try:
        if isinstance(unverified, dict) and is_mail_issuer(unverified.get("iss")):
            return _decode_mail_token(token, header)
        if alg == "ES256":
            kid = header.get("kid")
            keys = _onevoice_jwks()
            jwk = next((k for k in keys if k.get("kid") == kid), None)
            if jwk is None and len(keys) == 1:
                jwk = keys[0]  # single published key → kid is optional
            if jwk is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="no matching verification key",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return jwt.decode(token, jwk, algorithms=["ES256"], **opts)
        if alg == "HS256":
            return jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"], **opts)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"unsupported token algorithm: {alg}",
            headers={"WWW-Authenticate": "Bearer"},
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

    # Only `decode_access_token` produces claims, and it verifies every
    # Mail-issuer token against Mail's keys, so this branch is Mail's word.
    if is_mail_issuer(iss):
        return _mail_user(claims, db)

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


def principal_sub(claims: dict) -> str:
    """`AuthUser.sub` for verified claims: the key meetings, co-host lists
    and LiveKit identities (`user-<sub>`) use. A Mail token maps to the
    linked one.witysk.org user_id, else to `mail:<Mail user id>`."""
    if is_mail_issuer(claims.get("iss")):
        linked = str(claims.get("onevoice_sub") or "").strip()
        return linked or f"mail:{claims.get('sub')}"
    return str(claims.get("sub"))


def _mail_email(value: object) -> str | None:
    email = str(value or "").strip().lower()
    return email[:320] if "@" in email else None


def _mail_name(value: object) -> str | None:
    return str(value or "").strip()[:120] or None


def _email_free(db: Session, email: str | None, user_id: int | None) -> bool:
    """`users.email` is unique: a Mail identity never takes the address of
    another account (a native signup, an SSO user) — the sync is skipped."""
    if not email:
        return False
    q = db.query(User.id).filter(func.lower(User.email) == email)
    if user_id is not None:
        q = q.filter(User.id != user_id)
    return q.first() is None


def _sync_mail_profile(db: Session, user: User, email: str | None, name: str | None) -> bool:
    dirty = False
    if email and (user.email or "").lower() != email and _email_free(db, email, user.id):
        user.email = email
        dirty = True
    if name and user.name != name:
        user.name = name
        dirty = True
    return dirty


def _mail_user(claims: dict, db: Session) -> User:
    """Principal of a Mail-minted token (docs/MAIL_INTEGRATION.md §Auth).

    With `onevoice_sub` the user linked One Voice in Mail: the SSO row of that
    one.witysk.org user_id (auto-provisioned as for SSO), so Mail and
    meet.witysk.org share one Meet account. Otherwise a `kind="mail"` row keyed
    `mail:<Mail user id>`. Email and name follow the claims. A `kind="mail"`
    user is never made a platform admin by email (see bootstrap_platform_admins)."""
    email = _mail_email(claims.get("email"))
    name = _mail_name(claims.get("name"))
    linked = str(claims.get("onevoice_sub") or "").strip()
    if linked:
        kind, external_id = "sso", linked
    else:
        kind, external_id = "mail", f"mail:{claims.get('sub')}"
    user = db.query(User).filter_by(external_id=external_id, kind=kind).first()
    if user is None:
        user = User(
            kind=kind,
            external_id=external_id,
            email=email if _email_free(db, email, None) else None,
            name=name,
            # Same rule as the SSO path for the voucher admins (by user_id).
            is_platform_admin=kind == "sso" and linked in settings.voucher_admin_user_ids,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent first request created the row (or took the email).
            db.rollback()
            user = db.query(User).filter_by(external_id=external_id, kind=kind).first()
            if user is None:
                raise HTTPException(status_code=409, detail="account provisioning conflict") from None
            return user
        db.refresh(user)
    elif _sync_mail_profile(db, user, email, name):
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
        sub=principal_sub(claims),
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
            # Emails asserted by One Voice Mail never make a platform admin.
            email_match = bool(u.kind != "mail" and u.email and u.email.strip().lower() in email_needles)
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
        sub=principal_sub(claims),
    )


RequireUser = Annotated[AuthUser, Depends(require_user)]
OptionalUser = Annotated["AuthUser | None", Depends(optional_user)]
RequireAdmin = Annotated[AuthUser, Depends(require_admin)]
RequireVoucherAdmin = Annotated[AuthUser, Depends(require_voucher_admin)]
RequirePlatformAdmin = Annotated[AuthUser, Depends(require_platform_admin)]
