"""
Voucher endpoints.

- Issue / list / revoke: gated to specific one.witysk.org user_ids
  (currently user_ids 1 and 404 — Stephane and David). Configured via
  `voucher_admin_user_ids` in settings.
- Redeem: any signed-in user; grants a 30/60/90-day entitlement to
  meeting creation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import RequireUser, RequireVoucherAdmin
from app.db import get_db
from app.models import User, Voucher
from app.services.rate_limit import check as rate_limit_check
from app.services.vouchers import (
    VoucherKeyMissing,
    generate_code,
    hmac_for,
    verify,
)

router = APIRouter(prefix="/v1")

_ALLOWED_DURATION_DAYS = (30, 60, 90)  # 1 / 2 / 3 months


class IssueVoucherBody(BaseModel):
    duration_days: int = Field(default=30)
    note: str | None = Field(default=None, max_length=200)


class VoucherOut(BaseModel):
    id: int
    code: str
    duration_days: int
    note: str | None
    issued_by: str
    redeemed_by_user_id: int | None
    redeemed_at: datetime | None
    created_at: datetime
    expires_at: datetime


def _to_out(v: Voucher) -> VoucherOut:
    return VoucherOut(
        id=v.id,
        code=v.code,
        duration_days=v.duration_days,
        note=v.note,
        issued_by=v.issued_by,
        redeemed_by_user_id=v.redeemed_by_user_id,
        redeemed_at=v.redeemed_at,
        created_at=v.created_at,
        expires_at=v.expires_at,
    )


# ─── Admin: issue / list / revoke ─────────────────────────────────────


@router.post("/vouchers", status_code=201)
def issue_voucher(
    body: IssueVoucherBody,
    user: RequireVoucherAdmin,
    db: Session = Depends(get_db),
) -> VoucherOut:
    if body.duration_days not in _ALLOWED_DURATION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"duration_days must be one of {_ALLOWED_DURATION_DAYS}",
        )
    try:
        # Retry a couple of times on the unlikely chance of code collision.
        for _ in range(5):
            code = generate_code()
            v = Voucher(
                code=code,
                code_hmac=hmac_for(code),
                duration_days=body.duration_days,
                note=(body.note or "").strip() or None,
                issued_by=user.sub,
            )
            db.add(v)
            try:
                db.commit()
                db.refresh(v)
                return _to_out(v)
            except IntegrityError:
                db.rollback()
                continue
        raise HTTPException(status_code=500, detail="failed to allocate a unique code")
    except VoucherKeyMissing as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/vouchers")
def list_vouchers(
    user: RequireVoucherAdmin,
    db: Session = Depends(get_db),
) -> list[VoucherOut]:
    _ = user
    rows = db.query(Voucher).order_by(Voucher.created_at.desc()).limit(500).all()
    return [_to_out(v) for v in rows]


@router.delete("/vouchers/{code}")
def revoke_voucher(
    code: str,
    user: RequireVoucherAdmin,
    db: Session = Depends(get_db),
) -> dict:
    """Delete a voucher. Two cases:
      - **Unredeemed**: simple hard-delete; the code becomes unusable.
      - **Redeemed**: hard-delete AND revoke the redeemer's entitlement
        when it currently came from a voucher (we don't touch entitlements
        tied to a paid PayPal subscription — those have their own
        lifecycle). The user immediately loses meeting-creation rights;
        joining meetings, audio Café and chat keep working as for any
        signed-in user.
    Returns {"ok": true, "revoked_user_id": …} on 200.
    """
    _ = user
    v = db.query(Voucher).filter_by(code=code.upper()).first()
    if not v:
        raise HTTPException(status_code=404, detail="voucher not found")

    revoked_user_id: int | None = None
    if v.redeemed_by_user_id is not None:
        u = db.get(User, v.redeemed_by_user_id)
        if u and u.entitlement_kind == "voucher":
            u.entitlement_kind = None
            u.entitlement_expires_at = None
            revoked_user_id = u.id

    db.delete(v)
    db.commit()
    return {"ok": True, "revoked_user_id": revoked_user_id}


# ─── User-facing: redeem ──────────────────────────────────────────────


class RedeemBody(BaseModel):
    code: str = Field(min_length=1, max_length=16)


@router.post("/vouchers/redeem")
def redeem_voucher(
    body: RedeemBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Redeem a voucher. Granting policy:

    - Native users only (SSO already has admin rights — they don't need
      vouchers).
    - Single-use: the row's `redeemed_by_user_id` flips on success and
      future attempts return 409.
    - Code matches the stored HMAC, so a row that was inserted without
      knowledge of the signing key (e.g. via a DB import) can't be
      redeemed.
    - The entitlement EXTENDS any existing one rather than replacing it,
      so stacking two vouchers gives you the sum of their durations.
    """
    # Cap per-user attempts so a single account can't be used to brute-force
    # the 8-char voucher alphabet (~10^11 codes; rate limit makes online
    # guessing infeasible regardless).
    rate_limit_check(
        "voucher_redeem",
        f"u:{user.user_id}",
        limit=10,
        window_seconds=3600,
        detail="too many voucher redemption attempts; try again in an hour",
    )
    if user.kind != "sso":
        u = db.get(User, user.user_id)
        if not u:
            raise HTTPException(status_code=404, detail="account not found")
    else:
        # SSO already always-admin; redemption is a no-op for them and we
        # refuse rather than silently waste the code.
        raise HTTPException(status_code=403, detail="SSO accounts already have admin rights")

    code = body.code.strip().upper()
    v = db.query(Voucher).filter_by(code=code).first()
    if not v or not verify(code, v.code_hmac):
        raise HTTPException(status_code=404, detail="invalid or unknown voucher")
    if v.redeemed_by_user_id is not None:
        raise HTTPException(status_code=409, detail="voucher already redeemed")
    # Vouchers expire 3 months after issue. Once past that we refuse rather
    # than silently grant. Naive datetimes from SQLite are treated as UTC.
    expires = v.expires_at if v.expires_at.tzinfo else v.expires_at.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="voucher has expired")

    now = datetime.now(timezone.utc)
    # SQLite strips tzinfo on read; normalise the stored expiry to UTC-aware
    # before comparing against `now`, otherwise the > raises TypeError.
    cur = u.entitlement_expires_at
    if cur is not None and cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    base = cur if cur and cur > now else now
    new_expiry = base + timedelta(days=v.duration_days)

    u.entitlement_kind = "voucher"
    u.entitlement_expires_at = new_expiry
    v.redeemed_by_user_id = u.id
    v.redeemed_at = now
    db.commit()
    db.refresh(u)

    return {
        "ok": True,
        "duration_days": v.duration_days,
        "entitlement_expires_at": new_expiry.isoformat(),
    }
